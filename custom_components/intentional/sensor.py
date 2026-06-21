"""Sensor entities exposed by the Intentional integration.

Two kinds of entities are registered:

1. Per-target sensors (one per entity_id referenced by any rule):
   - state: the resolved value summary (e.g. "brightness_pct=40, color_temp_k=2700")
   - attributes: active_intents (list of dicts), reason, ttl_remaining_ms

2. A single summary sensor for the whole engine:
   - state: number of currently active intents across all targets
   - attributes: rule_count, target_count, recent_log_lines
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from ._engine.presentation import intent_sensor_state, value_summary
from .const import (
    ATTR_ACTIVE_INTENTS,
    ATTR_AUTHORITY,
    ATTR_REASON,
    ATTR_RULE_ID,
    ATTR_TARGET,
    ATTR_TTL_REMAINING,
    DEFAULT_NAME,
    DOMAIN,
)
from .room_controls import area_for_target, room_controls_for_engine, slugify_area_id

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Intentional sensor entities."""
    engine = hass.data[DOMAIN][entry.entry_id]

    # Summary sensor — always present
    summary = IntentionalSummarySensor(hass, engine, entry)
    room_entities = {
        area_id: IntentionalRoomStatusSensor(hass, engine, entry, area_id)
        for area_id in room_controls_for_engine(
            engine,
            lambda target: area_for_target(hass, target),
        )
    }
    async_add_entities([summary, *room_entities.values()])

    _cleanup_legacy_target_sensors(hass, entry)

    # Listen for refresh events from __init__.py and call async_write_ha_state
    async def _on_refresh(event) -> None:
        if event.data.get("entry_id") != entry.entry_id:
            return
        summary.async_write_ha_state()
        current_room_ids = set(room_controls_for_engine(
            engine,
            lambda target: area_for_target(hass, target),
        ))
        for removed_id in set(room_entities) - current_room_ids:
            entity = room_entities.pop(removed_id)
            await entity.async_remove()
        new_entities = []
        for area_id in current_room_ids - set(room_entities):
            entity = IntentionalRoomStatusSensor(hass, engine, entry, area_id)
            room_entities[area_id] = entity
            new_entities.append(entity)
        for area_id in current_room_ids & set(room_entities):
            room_entities[area_id].async_write_ha_state()
        if new_entities:
            async_add_entities(new_entities)

    entry.async_on_unload(
        hass.bus.async_listen(f"{DOMAIN}_refresh", _on_refresh)
    )


def _cleanup_legacy_target_sensors(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove old per-target intent sensors to avoid registry bloat."""
    registry = er.async_get(hass)
    prefix = f"{entry.entry_id}_"
    for entity_id, registry_entry in list(registry.entities.items()):
        if registry_entry.platform != DOMAIN:
            continue
        if registry_entry.domain != Platform.SENSOR:
            continue
        unique_id = registry_entry.unique_id
        if unique_id.startswith(prefix) and not unique_id.startswith(f"{prefix}area_"):
            registry.async_remove(entity_id)


class IntentionalTargetSensor(SensorEntity):
    """A sensor that reports the resolved state for a single target entity.

    The state is a string summary of the resolved value (e.g.
    "brightness_pct=40, color_temp_k=2700"). Detailed information is
    available in the attributes.
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:target"
    _attr_translation_key = "target_intent"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, hass: HomeAssistant, engine, entry: ConfigEntry, target: str) -> None:
        self.hass = hass
        self._engine = engine
        self._entry = entry
        self._target = target
        self._attr_name = f"Intent {target.split('.', 1)[1] if '.' in target else target}"
        self._attr_unique_id = f"{entry.entry_id}_{target}"
        self._resolved = None

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": DEFAULT_NAME,
            "manufacturer": "Intentional",
            "model": "Intent Engine",
        }

    @property
    def native_value(self) -> str:
        """Return a compact, translatable state for the resolved value."""
        return intent_sensor_state(self._engine.resolve(self._target))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        resolved = self._engine.resolve(self._target)
        if resolved is None:
            return {
                ATTR_TARGET: self._target,
                ATTR_ACTIVE_INTENTS: [],
                ATTR_TTL_REMAINING: None,
            }
        return {
            ATTR_TARGET: self._target,
            "summary": value_summary(resolved.value),
            "desired_state": resolved.value,
            "active_intent_count": len(resolved.all_active_intents),
            ATTR_ACTIVE_INTENTS: [
                {
                    ATTR_RULE_ID: i.rule_id or "<manual>",
                    ATTR_AUTHORITY: i.authority.value,
                    ATTR_REASON: i.reason,
                    ATTR_TTL_REMAINING: (
                        max(0, (i.created_at_ms + (i.ttl_ms or 0)) - self._engine.now_ms())
                        if i.ttl_ms is not None else None
                    ),
                }
                for i in resolved.all_active_intents
            ],
            ATTR_TTL_REMAINING: resolved.ttl_remaining_ms,
            "value": resolved.value,
            "winning_rule": resolved.winning_intent.rule_id if resolved.winning_intent else None,
        }

    async def async_update(self) -> None:
        """Called on every state poll — the engine does the work."""
        # Resolution happens in native_value/extra_state_attributes,
        # which HA calls on every refresh. Nothing to do here.
        pass


class IntentionalSummarySensor(SensorEntity):
    """A summary sensor: total active intents across all targets."""

    _attr_has_entity_name = True
    _attr_name = "Intent Engine Summary"
    _attr_icon = "mdi:palette"
    _attr_unique_id = "intentional_summary"
    _attr_translation_key = "summary"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hass: HomeAssistant, engine, entry: ConfigEntry) -> None:
        self.hass = hass
        self._engine = engine
        self._entry = entry

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": DEFAULT_NAME,
            "manufacturer": "Intentional",
            "model": "Intent Engine",
        }

    @property
    def native_value(self) -> int:
        """Total number of active intents across all targets."""
        return self._engine.active_intent_count()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        active_targets = self._engine.list_active_targets()
        return {
            "rule_count": self._engine.rule_count(),
            "active_intent_count": self._engine.active_intent_count(),
            "target_count": len(active_targets),
            "active_targets": list(active_targets),
        }


class IntentionalRoomStatusSensor(SensorEntity):
    """A sensor summarizing rules and overrides for one Home Assistant area."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:floor-plan"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        hass: HomeAssistant,
        engine,
        entry: ConfigEntry,
        area_id: str,
    ) -> None:
        self.hass = hass
        self._engine = engine
        self._entry = entry
        self._area_id = area_id
        self._attr_unique_id = f"{entry.entry_id}_area_{slugify_area_id(area_id)}_status"

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": DEFAULT_NAME,
            "manufacturer": "Intentional",
            "model": "Intent Engine",
        }

    @property
    def name(self) -> str | None:
        control = self._room_control()
        room_name = control.name if control is not None else self._area_id
        return f"{room_name} status"

    @property
    def native_value(self) -> str:
        control = self._room_control()
        if control is None:
            return "unknown"
        if control.paused:
            return "paused"
        if control.manual_override_targets:
            return "manual_override"
        if control.active_rule_ids:
            return "active"
        return "idle"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        control = self._room_control()
        if control is None:
            return {"area_id": self._area_id}
        return {
            "area_id": control.area_id,
            "area_name": control.name,
            "paused": control.paused,
            "rule_count": len(control.rule_ids),
            "active_rule_count": len(control.active_rule_ids),
            "manual_override_count": len(control.manual_override_targets),
            "active_intent_count": control.active_intent_count,
            "rule_ids": sorted(control.rule_ids),
            "active_rule_ids": sorted(control.active_rule_ids),
            "paused_rule_ids": sorted(control.paused_rule_ids),
            "targets": sorted(control.targets),
            "manual_override_targets": sorted(control.manual_override_targets),
        }

    def _room_control(self):
        return room_controls_for_engine(
            self._engine,
            lambda target: area_for_target(self.hass, target),
        ).get(self._area_id)
