"""Button entities for Intentional maintenance actions."""

from __future__ import annotations

from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
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
    entities: list[ButtonEntity] = [
        IntentionalClearManualOverridesButton(hass, engine, entry, None)
    ]
    entities.extend(
        IntentionalClearManualOverridesButton(hass, engine, entry, target)
        for target in engine.list_known_targets()
    )
    async_add_entities(entities)


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
