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
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

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
    async_add_entities([summary])

    # Per-target sensors — one per known target
    targets: set[str] = set()
    for parsed in engine._rules.values():  # noqa: SLF001 (intentional access)
        targets.add(parsed.rule.target)

    target_entities = [IntentionalTargetSensor(hass, engine, entry, t) for t in sorted(targets)]
    async_add_entities(target_entities)

    # Listen for refresh events from __init__.py and call async_write_ha_state
    async def _on_refresh(event) -> None:
        if event.data.get("entry_id") != entry.entry_id:
            return
        for entity in target_entities:
            entity.async_write_ha_state()
        summary.async_write_ha_state()

    entry.async_on_unload(
        hass.bus.async_listen(f"{DOMAIN}_refresh", _on_refresh)
    )


class IntentionalTargetSensor(SensorEntity):
    """A sensor that reports the resolved state for a single target entity.

    The state is a string summary of the resolved value (e.g.
    "brightness_pct=40, color_temp_k=2700"). Detailed information is
    available in the attributes.
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:target"

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
        """Return a human-readable summary of the resolved value."""
        resolved = self._engine.resolve(self._target)
        if resolved is None:
            return "idle"
        parts = [f"{k}={v}" for k, v in resolved.value.items()]
        return ", ".join(parts) if parts else "no_value"

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
        return len(self._engine._active_intents)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        targets = {i.target for i in self._engine._active_intents}
        return {
            "rule_count": len(self._engine._rules),
            "active_intent_count": len(self._engine._active_intents),
            "target_count": len(targets),
            "active_targets": sorted(targets),
        }
