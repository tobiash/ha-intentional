"""Plan the Home Assistant entities whose state Intentional must ingest."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SelectorKey:
    """The registry metadata that affects selector membership."""

    domain: str | None
    area: str | None
    label: str | None
    device: str | None
    entity: str | None
    purpose: str | None
    exclude: tuple[str, ...]


@dataclass(frozen=True)
class MembershipChange:
    """Relevant entities added to or removed from the ingest set."""

    added: frozenset[str]
    removed: frozenset[str]


class SelectorMembershipPlanner:
    """Cache selector expansion by selector identity and registry generation."""

    def __init__(
        self,
        entries: Callable[[], Iterable[Any]],
        *,
        area_for_entry: Callable[[Any], str | None] | None = None,
        state_entity_ids: Callable[[], Iterable[str]] | None = None,
        state_metadata: Callable[[str], dict[str, Any] | None] | None = None,
    ) -> None:
        self._entries = entries
        self._area_for_entry = area_for_entry or (lambda entry: getattr(entry, "area_id", None))
        self._state_entities = set(state_entity_ids() if state_entity_ids is not None else ())
        self._state_metadata = state_metadata or (lambda _entity_id: None)
        self._registry_entity_ids: set[str] = set()
        self._live_device_class_candidates: set[str] = set()
        self._generation = 0
        self._cache: dict[tuple[SelectorKey, int], frozenset[str]] = {}
        self._selectors: tuple[SelectorKey, ...] = ()
        self._static: frozenset[str] = frozenset()
        self._owned: frozenset[str] = frozenset()
        self._selector_members: frozenset[str] = frozenset()
        self._relevant: frozenset[str] = frozenset()

    @property
    def relevant(self) -> frozenset[str]:
        return self._relevant

    @property
    def generation(self) -> int:
        return self._generation

    def resolve(self, selector: Any) -> list[str]:
        """Resolve one selector using the current registry generation."""
        key = _selector_key(selector)
        cache_key = (key, self._generation)
        members = self._cache.get(cache_key)
        if members is None:
            registry_entries = tuple(self._entries())
            registry_ids = {entry.entity_id for entry in registry_entries}
            self._registry_entity_ids = registry_ids
            registry_members = {
                entry.entity_id
                for entry in registry_entries
                if _entry_matches(entry, key, self._area_for_entry, self._state_metadata(entry.entity_id))
            }
            state_only_members = {
                entity_id
                for entity_id in self._state_entities - registry_ids
                if _state_only_entity_matches(entity_id, key, self._state_metadata(entity_id))
            }
            if key.purpose:
                self._remember_live_device_class_candidates(key, registry_entries, registry_ids)
            members = frozenset(registry_members | state_only_members)
            self._cache[cache_key] = members
        return sorted(members)

    def configure(self, rules: Iterable[Any], static: Iterable[str]) -> MembershipChange:
        """Replace authored inputs, as on initial load or rule reload."""
        self._selectors = tuple(dict.fromkeys(
            _selector_key(selector)
            for rule in rules
            for selector in (*rule.intent_selectors, *rule.observe_selectors, *(group.selector for group in getattr(rule, "observation_groups", ())), *(group.selector for group in getattr(rule, "hold_observation_groups", ())), *(group.selector for group in getattr(rule, "hold_until_observation_groups", ())))
        ))
        self._static = frozenset(static)
        self._live_device_class_candidates.clear()
        return self._recompute()

    def registry_changed(self) -> MembershipChange:
        """Invalidate selector expansions after registry metadata changes."""
        self._generation += 1
        self._cache.clear()
        self._live_device_class_candidates.clear()
        return self._recompute()

    def state_changed(
        self, entity_id: str, *, exists: bool, device_class_changed: bool
    ) -> MembershipChange:
        """Update state-machine membership for entities without registry metadata."""
        if exists == (entity_id in self._state_entities):
            if device_class_changed and any(selector.purpose for selector in self._selectors):
                self._generation += 1
                self._cache.clear()
                self._live_device_class_candidates.clear()
                return self._recompute()
            return MembershipChange(frozenset(), frozenset())
        live_device_class_candidate = entity_id in self._live_device_class_candidates
        if exists:
            self._state_entities.add(entity_id)
        else:
            self._state_entities.discard(entity_id)
        if not live_device_class_candidate and not self._is_state_only_selector_candidate(entity_id):
            return MembershipChange(frozenset(), frozenset())
        self._generation += 1
        self._cache.clear()
        self._live_device_class_candidates.clear()
        return self._recompute()

    def update_owned(self, entity_ids: Iterable[str]) -> MembershipChange:
        """Replace active/retained Target ownership."""
        self._owned = frozenset(entity_ids)
        return self._recompute()

    def _recompute(self) -> MembershipChange:
        previous = self._relevant
        self._selector_members = frozenset(
            entity_id for selector in self._selectors for entity_id in self.resolve(selector)
        )
        self._relevant = self._static | self._selector_members | self._owned
        return MembershipChange(self._relevant - previous, previous - self._relevant)

    def _is_state_only_selector_candidate(
        self, entity_id: str, *, semantic_only: bool = False
    ) -> bool:
        if entity_id in self._registry_entity_ids:
            return False
        domain = entity_id.partition(".")[0]
        return any(
            (selector.purpose or not semantic_only)
            and not selector.area
            and not selector.device
            and not selector.label
            and (not selector.domain or selector.domain == domain)
            and (not selector.entity or selector.entity == entity_id)
            for selector in self._selectors
        )

    def _remember_live_device_class_candidates(
        self,
        selector: SelectorKey,
        registry_entries: tuple[Any, ...],
        registry_ids: set[str],
    ) -> None:
        for entry in registry_entries:
            if (
                getattr(entry, "device_class", None) is None
                and getattr(entry, "original_device_class", None) is None
                and _entry_matches_without_purpose(entry, selector, self._area_for_entry)
            ):
                self._live_device_class_candidates.add(entry.entity_id)
        for entity_id in self._state_entities - registry_ids:
            if _state_only_entity_matches_without_purpose(entity_id, selector):
                self._live_device_class_candidates.add(entity_id)


def _selector_key(selector: Any) -> SelectorKey:
    return SelectorKey(
        domain=selector.domain,
        area=selector.area,
        label=selector.label,
        device=getattr(selector, "device", None),
        entity=getattr(selector, "entity", None),
        purpose=getattr(selector, "purpose", None),
        exclude=tuple(sorted(selector.exclude)),
    )


def _entry_matches(
    entry: Any,
    selector: SelectorKey,
    area_for_entry: Callable[[Any], str | None],
    metadata: dict[str, Any] | None,
) -> bool:
    entity_id = entry.entity_id
    if entity_id in selector.exclude:
        return False
    if selector.domain and entity_id.partition(".")[0] != selector.domain:
        return False
    if selector.area and area_for_entry(entry) != selector.area:
        return False
    if selector.device and getattr(entry, "device_id", None) != selector.device:
        return False
    if selector.entity and entity_id != selector.entity:
        return False
    if selector.purpose and not _purpose_matches(entity_id, selector.purpose, entry, metadata):
        return False
    return not (
        selector.label and selector.label not in (getattr(entry, "labels", None) or ())
    )


def _entry_matches_without_purpose(
    entry: Any,
    selector: SelectorKey,
    area_for_entry: Callable[[Any], str | None],
) -> bool:
    return _entry_matches(entry, selector, area_for_entry, None) or (
        selector.purpose is not None
        and _entry_matches(
            entry,
            SelectorKey(
                selector.domain,
                selector.area,
                selector.label,
                selector.device,
                selector.entity,
                None,
                selector.exclude,
            ),
            area_for_entry,
            None,
        )
    )


def _state_only_entity_matches(entity_id: str, selector: SelectorKey, metadata: dict[str, Any] | None) -> bool:
    """Match selectors that do not require unavailable registry metadata."""
    if entity_id in selector.exclude or selector.area or selector.label or selector.device:
        return False
    if selector.entity and entity_id != selector.entity:
        return False
    if selector.domain and entity_id.partition(".")[0] != selector.domain:
        return False
    return not selector.purpose or _purpose_matches(entity_id, selector.purpose, None, metadata)


def _state_only_entity_matches_without_purpose(
    entity_id: str, selector: SelectorKey
) -> bool:
    return _state_only_entity_matches(
        entity_id,
        SelectorKey(
            selector.domain,
            selector.area,
            selector.label,
            selector.device,
            selector.entity,
            None,
            selector.exclude,
        ),
        None,
    )


_PURPOSES = {
    "motion": ("binary_sensor", "motion"), "occupancy": ("binary_sensor", "occupancy"),
    "door": ("binary_sensor", "door"), "window": ("binary_sensor", "window"),
    "moisture": ("binary_sensor", "moisture"), "temperature": ("sensor", "temperature"),
    "illuminance": ("sensor", "illuminance"),
}


def _purpose_matches(entity_id: str, purpose: str, entry: Any, metadata: dict[str, Any] | None) -> bool:
    domain, device_class = _PURPOSES[purpose]
    if entity_id.partition(".")[0] != domain:
        return False
    effective = getattr(entry, "device_class", None) if entry is not None else None
    original = getattr(entry, "original_device_class", None) if entry is not None else None
    fallback = (metadata or {}).get("device_class")
    return (effective or original or fallback) == device_class
