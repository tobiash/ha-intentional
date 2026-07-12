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
    ) -> None:
        self._entries = entries
        self._area_for_entry = area_for_entry or (lambda entry: getattr(entry, "area_id", None))
        self._state_entities = set(state_entity_ids() if state_entity_ids is not None else ())
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
            registry_members = {
                entry.entity_id
                for entry in registry_entries
                if _entry_matches(entry, key, self._area_for_entry)
            }
            state_only_members = {
                entity_id
                for entity_id in self._state_entities - registry_ids
                if _state_only_entity_matches(entity_id, key)
            }
            members = frozenset(registry_members | state_only_members)
            self._cache[cache_key] = members
        return sorted(members)

    def configure(self, rules: Iterable[Any], static: Iterable[str]) -> MembershipChange:
        """Replace authored inputs, as on initial load or rule reload."""
        self._selectors = tuple(dict.fromkeys(
            _selector_key(selector)
            for rule in rules
            for selector in (*rule.intent_selectors, *rule.observe_selectors)
        ))
        self._static = frozenset(static)
        return self._recompute()

    def registry_changed(self) -> MembershipChange:
        """Invalidate selector expansions after registry metadata changes."""
        self._generation += 1
        self._cache.clear()
        return self._recompute()

    def state_changed(self, entity_id: str, *, exists: bool) -> MembershipChange:
        """Update state-machine membership for entities without registry metadata."""
        if exists == (entity_id in self._state_entities):
            return MembershipChange(frozenset(), frozenset())
        if exists:
            self._state_entities.add(entity_id)
        else:
            self._state_entities.discard(entity_id)
        self._generation += 1
        self._cache.clear()
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


def _selector_key(selector: Any) -> SelectorKey:
    return SelectorKey(
        domain=selector.domain,
        area=selector.area,
        label=selector.label,
        exclude=tuple(sorted(selector.exclude)),
    )


def _entry_matches(
    entry: Any,
    selector: SelectorKey,
    area_for_entry: Callable[[Any], str | None],
) -> bool:
    entity_id = entry.entity_id
    if entity_id in selector.exclude:
        return False
    if selector.domain and entity_id.partition(".")[0] != selector.domain:
        return False
    if selector.area and area_for_entry(entry) != selector.area:
        return False
    return not (
        selector.label and selector.label not in (getattr(entry, "labels", None) or ())
    )


def _state_only_entity_matches(entity_id: str, selector: SelectorKey) -> bool:
    """Match selectors that do not require unavailable registry metadata."""
    if entity_id in selector.exclude or selector.area or selector.label:
        return False
    return not selector.domain or entity_id.partition(".")[0] == selector.domain
