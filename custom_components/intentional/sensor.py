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
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from ._engine.alerting import AlertCoordinator
from ._engine.presentation import intent_sensor_state, value_summary
from ._engine.runtime import TickRuntime, runtime_key
from .alerting import alert_sensor_unique_id, alerting_coordinator_for, alerting_signal
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
from .entity_lifecycle import RegistrationAwareEntity
from .publication import publication_signal
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
    alerting = alerting_coordinator_for(hass, entry.entry_id)
    alert_entities = {
        (str(alert["rule_id"]), str(alert["name"])): IntentionalAlertSensor(
            alerting, entry, str(alert["rule_id"]), str(alert["name"])
        )
        for alert in (alerting.list_alerts() if alerting is not None else [])
    }
    for key, entity in alert_entities.items():
        entity.set_removal_callback(
            lambda key=key, entity=entity: alert_entities.pop(key, None)
            if alert_entities.get(key) is entity else None
        )
    _cleanup_stale_alert_sensors(hass, entry, set(alert_entities))
    _cleanup_stale_room_sensors(hass, entry, set(room_entities))
    for area_id, entity in room_entities.items():
        entity.set_removal_callback(
            lambda area_id=area_id, entity=entity: room_entities.pop(area_id, None)
            if room_entities.get(area_id) is entity else None
        )
    async_add_entities([summary, *room_entities.values(), *alert_entities.values()])

    _cleanup_legacy_target_sensors(hass, entry)

    async def _on_publication(changed: frozenset[str]) -> None:
        runtime = hass.data[DOMAIN].get(runtime_key(entry.entry_id))
        if isinstance(runtime, TickRuntime) and runtime.unloading:
            return
        if "summary" in changed:
            summary.async_write_if_registered()
        current_room_ids = set(room_controls_for_engine(
            engine,
            lambda target: area_for_target(hass, target),
        ))
        for removed_id in set(room_entities) - current_room_ids:
            await room_entities[removed_id].async_mark_removed()
        _cleanup_stale_room_sensors(hass, entry, current_room_ids)
        new_entities = []
        for area_id in current_room_ids - set(room_entities):
            entity = IntentionalRoomStatusSensor(hass, engine, entry, area_id)
            room_entities[area_id] = entity
            entity.set_removal_callback(
                lambda area_id=area_id, entity=entity: room_entities.pop(area_id, None)
                if room_entities.get(area_id) is entity else None
            )
            new_entities.append(entity)
        for area_id in current_room_ids & set(room_entities):
            room_entities[area_id].mark_desired()
            if f"room:{area_id}" in changed:
                room_entities[area_id].async_write_if_registered()
        if new_entities:
            async_add_entities(new_entities)

    entry.async_on_unload(
        async_dispatcher_connect(hass, publication_signal(entry.entry_id), _on_publication)
    )

    if alerting is not None:
        async def _on_alerting() -> None:
            current = {
                (str(alert["rule_id"]), str(alert["name"]))
                for alert in alerting.list_alerts()
            }
            for removed in set(alert_entities) - current:
                await alert_entities[removed].async_mark_removed()
            _cleanup_stale_alert_sensors(hass, entry, current)
            new_entities = []
            for key in current - set(alert_entities):
                entity = IntentionalAlertSensor(alerting, entry, *key)
                alert_entities[key] = entity
                entity.set_removal_callback(
                    lambda key=key, entity=entity: alert_entities.pop(key, None)
                    if alert_entities.get(key) is entity else None
                )
                new_entities.append(entity)
            for key in current & set(alert_entities):
                alert_entities[key].mark_desired()
                alert_entities[key].async_write_if_registered()
            if new_entities:
                async_add_entities(new_entities)

        entry.async_on_unload(
            async_dispatcher_connect(hass, alerting_signal(entry.entry_id), _on_alerting)
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
        if (
            unique_id.startswith(prefix)
            and not unique_id.startswith(f"{prefix}area_")
            and not unique_id.startswith(f"{prefix}alert_")
        ):
            registry.async_remove(entity_id)


def _cleanup_stale_room_sensors(
    hass: HomeAssistant, entry: ConfigEntry, current_area_ids: set[str]
) -> None:
    """Remove registry entries for room controls that no longer exist."""
    registry = er.async_get(hass)
    prefix = f"{entry.entry_id}_area_"
    current_unique_ids = {
        f"{prefix}{slugify_area_id(area_id)}_status" for area_id in current_area_ids
    }
    for entity_id, registry_entry in list(registry.entities.items()):
        if registry_entry.platform != DOMAIN or registry_entry.domain != Platform.SENSOR:
            continue
        unique_id = registry_entry.unique_id
        if (
            unique_id.startswith(prefix)
            and unique_id.endswith("_status")
            and unique_id not in current_unique_ids
        ):
            registry.async_remove(entity_id)


def _cleanup_stale_alert_sensors(
    hass: HomeAssistant,
    entry: ConfigEntry,
    current: set[tuple[str, str]],
) -> None:
    """Remove registry entries for Alert definitions that no longer exist."""
    registry = er.async_get(hass)
    prefix = f"{entry.entry_id}_alert_"
    current_unique_ids = {
        alert_sensor_unique_id(entry.entry_id, rule_id, name) for rule_id, name in current
    }
    for entity_id, registry_entry in list(registry.entities.items()):
        if registry_entry.platform != DOMAIN or registry_entry.domain != Platform.SENSOR:
            continue
        unique_id = registry_entry.unique_id
        if unique_id.startswith(prefix) and unique_id not in current_unique_ids:
            registry.async_remove(entity_id)


class IntentionalAlertSensor(RegistrationAwareEntity, SensorEntity):
    """Lifecycle sensor for one authored Alert definition."""

    _attr_icon = "mdi:alert"

    def __init__(
        self,
        coordinator: AlertCoordinator,
        entry: ConfigEntry,
        rule_id: str,
        alert_name: str,
    ) -> None:
        super().__init__()
        self._coordinator = coordinator
        self._rule_id = rule_id
        self._alert_name = alert_name
        self._attr_unique_id = alert_sensor_unique_id(
            entry.entry_id, rule_id, alert_name
        )
        self._attr_name = f"Intentional {alert_name}"

    def _projection(self) -> dict[str, object] | None:
        return next(
            (
                alert
                for alert in self._coordinator.list_alerts()
                if alert.get("rule_id") == self._rule_id
                and alert.get("name") == self._alert_name
            ),
            None,
        )

    @property
    def available(self) -> bool:
        """Return whether this Alert definition has a current projection."""
        return self._coordinator.available and self._projection() is not None

    @property
    def native_value(self) -> str | None:
        """Return inactive, pending, or firing."""
        projection = self._projection()
        return None if projection is None else str(projection["state"])

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Expose bounded Alert context."""
        projection = self._projection()
        if projection is None:
            return {}
        return {
            key: projection.get(key)
            for key in (
                "rule_id",
                "name",
                "severity",
                "summary",
                "instance_id",
                "evaluation_status",
            )
        }


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


class IntentionalSummarySensor(RegistrationAwareEntity, SensorEntity):
    """A summary sensor: total active intents across all targets."""

    _attr_has_entity_name = True
    _attr_name = "Intent Engine Summary"
    _attr_icon = "mdi:palette"
    _attr_unique_id = "intentional_summary"
    _attr_translation_key = "summary"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hass: HomeAssistant, engine, entry: ConfigEntry) -> None:
        super().__init__()
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


class IntentionalRoomStatusSensor(RegistrationAwareEntity, SensorEntity):
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
        super().__init__()
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
