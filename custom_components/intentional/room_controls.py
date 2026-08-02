"""Area-derived room control helpers for Intentional entities."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er


@dataclass(frozen=True)
class AreaInfo:
    """Resolved Home Assistant area metadata for one target entity."""

    id: str
    name: str


@dataclass
class RoomControl:
    """Aggregated Intentional state for one Home Assistant area."""

    area_id: str
    name: str
    rule_ids: set[str] = field(default_factory=set)
    targets: set[str] = field(default_factory=set)
    active_rule_ids: set[str] = field(default_factory=set)
    paused_rule_ids: set[str] = field(default_factory=set)
    manual_override_targets: set[str] = field(default_factory=set)
    active_intent_count: int = 0

    @property
    def paused(self) -> bool:
        """Return whether every rule for this room is paused."""
        return bool(self.rule_ids) and self.rule_ids <= self.paused_rule_ids


def room_controls_for_engine(
    engine: Any,
    area_for_target: Callable[[str], AreaInfo | None],
    *,
    statuses: dict[str, dict[str, Any]] | None = None,
) -> dict[str, RoomControl]:
    """Build room controls from authored rule targets and active overrides."""
    controls: dict[str, RoomControl] = {}
    statuses = statuses if statuses is not None else engine.list_authored_rule_statuses()
    for status in statuses.values():
        rule_id = str(status.get("rule_id") or "")
        if not rule_id:
            continue
        areas = _areas_for_targets(status.get("targets", []), area_for_target)
        for area in areas.values():
            control = _control_for_area(controls, area)
            control.rule_ids.add(rule_id)
            control.targets.update(_entity_targets_for_area(status.get("targets", []), area.id, area_for_target))
            if bool(status.get("active")) or int(status.get("active_intent_count") or 0):
                control.active_rule_ids.add(rule_id)
            if bool(status.get("paused")):
                control.paused_rule_ids.add(rule_id)
            control.active_intent_count += int(status.get("active_intent_count") or 0)

    for intent in engine.list_active_user_intents():
        area = area_for_target(intent.target)
        if area is None:
            continue
        control = _control_for_area(controls, area)
        control.targets.add(intent.target)
        control.manual_override_targets.add(intent.target)

    return dict(sorted(controls.items(), key=lambda item: item[1].name.lower()))


def area_for_target(hass: HomeAssistant, target: str) -> AreaInfo | None:
    """Infer a target entity's Home Assistant area."""
    entity_entry = er.async_get(hass).async_get(target)
    if entity_entry is None:
        return None
    area_id = entity_entry.area_id
    if area_id is None and entity_entry.device_id:
        device_entry = dr.async_get(hass).async_get(entity_entry.device_id)
        if device_entry is not None:
            area_id = device_entry.area_id
    if area_id is None:
        return None
    area_entry = ar.async_get(hass).async_get_area(area_id)
    if area_entry is None:
        return AreaInfo(id=area_id, name=area_id)
    return AreaInfo(id=area_id, name=area_entry.name)


def slugify_area_id(area_id: str) -> str:
    """Return a stable entity unique-id segment for an area id."""
    return "".join(ch if ch.isalnum() else "_" for ch in area_id).strip("_") or "area"


def _control_for_area(controls: dict[str, RoomControl], area: AreaInfo) -> RoomControl:
    control = controls.get(area.id)
    if control is None:
        control = RoomControl(area_id=area.id, name=area.name)
        controls[area.id] = control
    return control


def _areas_for_targets(
    targets: Any,
    area_for_target: Callable[[str], AreaInfo | None],
) -> dict[str, AreaInfo]:
    areas: dict[str, AreaInfo] = {}
    for target in targets if isinstance(targets, list) else []:
        if not isinstance(target, str) or "." not in target:
            continue
        area = area_for_target(target)
        if area is not None:
            areas[area.id] = area
    return areas


def _entity_targets_for_area(
    targets: Any,
    area_id: str,
    area_for_target: Callable[[str], AreaInfo | None],
) -> set[str]:
    result: set[str] = set()
    for target in targets if isinstance(targets, list) else []:
        if not isinstance(target, str) or "." not in target:
            continue
        area = area_for_target(target)
        if area is not None and area.id == area_id:
            result.add(target)
    return result
