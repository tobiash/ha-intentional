"""Switch entities for enabling/disabling Intentional automation."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_RULE_DIR, DEFAULT_NAME, DEFAULT_RULE_DIR, DOMAIN
from .room_controls import area_for_target, room_controls_for_engine, slugify_area_id
from .rule_files import _list_rules, _set_rule_enabled
from .rule_store import StorageRuleStore, rule_store_key


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Intentional switch entities."""
    engine = hass.data[DOMAIN][entry.entry_id]
    rule_store = hass.data[DOMAIN].get(rule_store_key(entry.entry_id))
    rule_dir = entry.data.get(CONF_RULE_DIR, DEFAULT_RULE_DIR)
    if isinstance(rule_store, StorageRuleStore):
        rule_infos = rule_store.list_rules()
    else:
        rule_infos = await hass.async_add_executor_job(_list_rules, rule_dir)
    _cleanup_stale_rule_switches(hass, entry, rule_infos)
    entities: list[SwitchEntity] = [IntentionalGlobalSwitch(hass, engine, entry)]
    rule_entities = {
        str(rule_info["id"]): IntentionalRuleSwitch(
            hass,
            entry,
            rule_dir,
            rule_info,
            rule_store,
            engine,
        )
        for rule_info in rule_infos
    }
    entities.extend(rule_entities.values())
    room_entities = {
        area_id: IntentionalRoomPauseSwitch(hass, engine, entry, area_id)
        for area_id in room_controls_for_engine(
            engine,
            lambda target: area_for_target(hass, target),
        )
    }
    entities.extend(room_entities.values())
    async_add_entities(entities)

    async def _on_refresh(event) -> None:
        if event.data.get("entry_id") != entry.entry_id:
            return
        current_infos = (
            rule_store.list_rules()
            if isinstance(rule_store, StorageRuleStore)
            else await hass.async_add_executor_job(_list_rules, rule_dir)
        )
        _cleanup_stale_rule_switches(hass, entry, current_infos)
        current_ids = {
            str(rule_info["id"])
            for rule_info in current_infos
            if isinstance(rule_info.get("id"), str)
        }
        for removed_id in set(rule_entities) - current_ids:
            entity = rule_entities.pop(removed_id)
            await entity.async_remove()
        new_entities = []
        for rule_info in current_infos:
            rule_id = str(rule_info["id"])
            if rule_id in rule_entities:
                rule_entities[rule_id].update_rule_info(rule_info)
                rule_entities[rule_id].async_write_ha_state()
                continue
            entity = IntentionalRuleSwitch(
                hass,
                entry,
                rule_dir,
                rule_info,
                rule_store,
                engine,
            )
            rule_entities[rule_id] = entity
            new_entities.append(entity)
        current_room_ids = set(room_controls_for_engine(
            engine,
            lambda target: area_for_target(hass, target),
        ))
        for removed_id in set(room_entities) - current_room_ids:
            entity = room_entities.pop(removed_id)
            await entity.async_remove()
        for area_id in current_room_ids - set(room_entities):
            entity = IntentionalRoomPauseSwitch(hass, engine, entry, area_id)
            room_entities[area_id] = entity
            new_entities.append(entity)
        for area_id in current_room_ids & set(room_entities):
            room_entities[area_id].async_write_ha_state()
        if new_entities:
            async_add_entities(new_entities)

    entry.async_on_unload(
        hass.bus.async_listen(f"{DOMAIN}_refresh", _on_refresh)
    )


def _cleanup_stale_rule_switches(
    hass: HomeAssistant,
    entry: ConfigEntry,
    rule_infos: list[dict[str, Any]],
) -> None:
    """Remove registry entries for authored rules that no longer exist."""
    registry = er.async_get(hass)
    prefix = f"{entry.entry_id}_rule_"
    current_unique_ids = {
        f"{prefix}{rule_info['id']}"
        for rule_info in rule_infos
        if isinstance(rule_info.get("id"), str)
    }
    for entity_id, registry_entry in list(registry.entities.items()):
        if registry_entry.platform != DOMAIN:
            continue
        if registry_entry.domain != Platform.SWITCH:
            continue
        unique_id = registry_entry.unique_id
        if unique_id.startswith(prefix) and unique_id not in current_unique_ids:
            registry.async_remove(entity_id)


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
        rule_store: StorageRuleStore | None = None,
        engine=None,
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._rule_dir = rule_dir
        self._rule_store = rule_store
        self._engine = engine
        self._rule_id = str(rule_info["id"])
        self._filename = str(rule_info["filename"])
        self._enabled = bool(rule_info.get("enabled", True))
        self._attr_name = f"Rule {self._rule_id}"
        self._attr_unique_id = f"{entry.entry_id}_rule_{self._rule_id}"

    def update_rule_info(self, rule_info: dict[str, Any]) -> None:
        """Refresh rule-file metadata after reload."""
        self._filename = str(rule_info["filename"])
        self._enabled = bool(rule_info.get("enabled", True))

    @property
    def device_info(self) -> dict[str, Any]:
        return _device_info(self._entry)

    @property
    def is_on(self) -> bool:
        return self._enabled

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "rule_id": self._rule_id,
            "filename": self._filename,
            "source": "storage" if self._rule_store is not None else "file",
            **self._rule_status_attributes(),
        }

    def _rule_status_attributes(self) -> dict[str, Any]:
        if self._engine is None:
            return {}
        if hasattr(self._engine, "list_authored_rule_statuses"):
            status = self._engine.list_authored_rule_statuses().get(self._rule_id, {})
        else:
            status = self._engine.list_rule_statuses().get(self._rule_id, {})
        return {
            key: value
            for key, value in status.items()
            if key != "rule_id"
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set_enabled(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set_enabled(False)

    async def _set_enabled(self, enabled: bool) -> None:
        if self._rule_store is not None:
            result = await self._rule_store.async_set_rule_enabled(self._rule_id, enabled)
        else:
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


class IntentionalRoomPauseSwitch(SwitchEntity):
    """Switch that pauses all rules targeting one Home Assistant area."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:pause-circle"
    _attr_entity_category = EntityCategory.CONFIG

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
        self._attr_unique_id = f"{entry.entry_id}_area_{slugify_area_id(area_id)}_paused"

    @property
    def device_info(self) -> dict[str, Any]:
        return _device_info(self._entry)

    @property
    def name(self) -> str | None:
        control = self._room_control()
        room_name = control.name if control is not None else self._area_id
        return f"Pause {room_name} rules"

    @property
    def is_on(self) -> bool:
        control = self._room_control()
        return bool(control and control.paused)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        control = self._room_control()
        if control is None:
            return {"area_id": self._area_id, "rule_ids": []}
        return {
            "area_id": control.area_id,
            "area_name": control.name,
            "rule_ids": sorted(control.rule_ids),
            "paused_rule_ids": sorted(control.paused_rule_ids),
            "active_rule_ids": sorted(control.active_rule_ids),
            "targets": sorted(control.targets),
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        control = self._room_control()
        if control is None:
            return
        self._engine.set_rules_paused(control.rule_ids, True)
        self.hass.bus.async_fire(f"{DOMAIN}_refresh", {"entry_id": self._entry.entry_id})
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        control = self._room_control()
        if control is None:
            return
        self._engine.set_rules_paused(control.rule_ids, False)
        self.hass.bus.async_fire(f"{DOMAIN}_refresh", {"entry_id": self._entry.entry_id})
        self.async_write_ha_state()

    def _room_control(self):
        return room_controls_for_engine(
            self._engine,
            lambda target: area_for_target(self.hass, target),
        ).get(self._area_id)
