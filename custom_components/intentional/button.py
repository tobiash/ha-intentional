"""Button entities for Intentional maintenance actions."""

from __future__ import annotations

from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DEFAULT_NAME, DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Intentional button entities."""
    engine = hass.data[DOMAIN][entry.entry_id]
    _cleanup_legacy_target_buttons(hass, entry)
    entities: list[ButtonEntity] = [
        IntentionalReloadButton(hass, entry),
        IntentionalClearManualOverridesButton(hass, engine, entry, None),
    ]
    async_add_entities(entities)


def _cleanup_legacy_target_buttons(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove old per-target clear buttons to avoid registry bloat."""
    registry = er.async_get(hass)
    prefix = f"{entry.entry_id}_clear_manual_"
    keep = f"{prefix}all"
    for entity_id, registry_entry in list(registry.entities.items()):
        if registry_entry.platform != DOMAIN:
            continue
        if registry_entry.domain != Platform.BUTTON:
            continue
        unique_id = registry_entry.unique_id
        if unique_id.startswith(prefix) and unique_id != keep:
            registry.async_remove(entity_id)


class IntentionalReloadButton(ButtonEntity):
    """Button that reloads stored Intentional rules."""

    _attr_has_entity_name = True
    _attr_name = "Reload rules"
    _attr_icon = "mdi:reload"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "reload_rules"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_reload_rules"

    @property
    def device_info(self) -> dict[str, Any]:
        return _device_info(self._entry)

    async def async_press(self) -> None:
        await self.hass.services.async_call(DOMAIN, "reload", blocking=True)


def _device_info(entry: ConfigEntry) -> dict[str, Any]:
    return {
        "identifiers": {(DOMAIN, entry.entry_id)},
        "name": DEFAULT_NAME,
        "manufacturer": "Intentional",
        "model": "Intent Engine",
    }


class IntentionalClearManualOverridesButton(ButtonEntity):
    """Button that clears manual/user intents."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:eraser"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "clear_manual_overrides"

    def __init__(
        self,
        hass: HomeAssistant,
        engine,
        entry: ConfigEntry,
        target: str | None,
    ) -> None:
        self.hass = hass
        self._engine = engine
        self._entry = entry
        self._target = target
        if target is not None:
            self._attr_entity_registry_enabled_default = False
        suffix = "all" if target is None else target
        self._attr_unique_id = f"{entry.entry_id}_clear_manual_{suffix}"
        self._attr_name = "Clear all manual overrides" if target is None else f"Clear manual override {target}"

    @property
    def device_info(self) -> dict[str, Any]:
        return _device_info(self._entry)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"target": self._target}

    async def async_press(self) -> None:
        self._engine.clear_user_intents(self._target)
        self.hass.bus.async_fire(f"{DOMAIN}_refresh", {"entry_id": self._entry.entry_id})
