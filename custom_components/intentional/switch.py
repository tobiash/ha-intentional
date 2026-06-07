"""Switch entities for enabling/disabling Intentional automation."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_RULE_DIR, DEFAULT_NAME, DEFAULT_RULE_DIR, DOMAIN
from .rule_files import _list_rules, _set_rule_enabled


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Intentional switch entities."""
    engine = hass.data[DOMAIN][entry.entry_id]
    rule_dir = entry.data.get(CONF_RULE_DIR, DEFAULT_RULE_DIR)
    rule_infos = await hass.async_add_executor_job(_list_rules, rule_dir)
    entities: list[SwitchEntity] = [IntentionalGlobalSwitch(hass, engine, entry)]
    entities.extend(
        IntentionalRuleSwitch(hass, entry, rule_dir, rule_info)
        for rule_info in rule_infos
    )
    async_add_entities(entities)


def _device_info(entry: ConfigEntry) -> dict[str, Any]:
    return {
        "identifiers": {(DOMAIN, entry.entry_id)},
        "name": DEFAULT_NAME,
        "manufacturer": "Intentional",
        "model": "Intent Engine",
    }


class IntentionalGlobalSwitch(SwitchEntity):
    """Global switch that enables/disables all Intentional automation."""

    _attr_has_entity_name = True
    _attr_name = "Automation enabled"
    _attr_icon = "mdi:power"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "global_enabled"

    def __init__(self, hass: HomeAssistant, engine, entry: ConfigEntry) -> None:
        self.hass = hass
        self._engine = engine
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_global_enabled"

    @property
    def device_info(self) -> dict[str, Any]:
        return _device_info(self._entry)

    @property
    def is_on(self) -> bool:
        return self._engine.is_enabled()

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._engine.set_enabled(True)
        self.async_write_ha_state()
        self.hass.bus.async_fire(f"{DOMAIN}_refresh", {"entry_id": self._entry.entry_id})

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._engine.set_enabled(False)
        self.async_write_ha_state()
        self.hass.bus.async_fire(f"{DOMAIN}_refresh", {"entry_id": self._entry.entry_id})


class IntentionalRuleSwitch(SwitchEntity):
    """Switch that persists one rule's top-level enabled flag."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:file-tree"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "rule_enabled"

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        rule_dir: str,
        rule_info: dict[str, Any],
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._rule_dir = rule_dir
        self._rule_id = str(rule_info["id"])
        self._filename = str(rule_info["filename"])
        self._enabled = bool(rule_info.get("enabled", True))
        self._attr_name = f"Rule {self._rule_id}"
        self._attr_unique_id = f"{entry.entry_id}_rule_{self._rule_id}"

    @property
    def device_info(self) -> dict[str, Any]:
        return _device_info(self._entry)

    @property
    def is_on(self) -> bool:
        return self._enabled

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"rule_id": self._rule_id, "filename": self._filename}

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set_enabled(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set_enabled(False)

    async def _set_enabled(self, enabled: bool) -> None:
        result = await self.hass.async_add_executor_job(
            _set_rule_enabled,
            self._rule_dir,
            self._rule_id,
            enabled,
        )
        if "error" in result:
            return
        self._enabled = enabled
        await self.hass.services.async_call(DOMAIN, "reload", blocking=True)
        self.async_write_ha_state()
