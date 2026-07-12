"""Button entities for Intentional maintenance actions."""

from __future__ import annotations

from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from ._engine.runtime import TickRuntime, runtime_key
from .const import DEFAULT_NAME, DOMAIN
from .lifecycle_writer import mark_lifecycle_mutated
from .publication import publication_key, publication_signal
from .room_controls import area_for_target, room_controls_for_engine, slugify_area_id


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
    room_entities = {
        area_id: IntentionalClearRoomOverridesButton(hass, engine, entry, area_id)
        for area_id in room_controls_for_engine(
            engine,
            lambda target: area_for_target(hass, target),
        )
    }
    entities.extend(room_entities.values())
    async_add_entities(entities)

    async def _on_publication(_changed: frozenset[str]) -> None:
        runtime = hass.data[DOMAIN].get(runtime_key(entry.entry_id))
        if isinstance(runtime, TickRuntime) and runtime.unloading:
            return
        current_room_ids = set(room_controls_for_engine(
            engine,
            lambda target: area_for_target(hass, target),
        ))
        for removed_id in set(room_entities) - current_room_ids:
            entity = room_entities.pop(removed_id)
            await entity.async_remove()
        new_entities = []
        for area_id in current_room_ids - set(room_entities):
            entity = IntentionalClearRoomOverridesButton(hass, engine, entry, area_id)
            room_entities[area_id] = entity
            new_entities.append(entity)
        if new_entities:
            async_add_entities(new_entities)

    entry.async_on_unload(
        async_dispatcher_connect(hass, publication_signal(entry.entry_id), _on_publication)
    )


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
        runtime = self.hass.data[DOMAIN].get(runtime_key(self._entry.entry_id))
        if isinstance(runtime, TickRuntime):
            async with runtime.mutation_lock:
                self._engine.clear_user_intents(self._target)
                runtime.advance_revision()
                mark_lifecycle_mutated(self.hass, self._entry.entry_id)
        else:
            self._engine.clear_user_intents(self._target)
        self.hass.data[DOMAIN][publication_key(self._entry.entry_id)].publish_if_changed()


class IntentionalClearRoomOverridesButton(ButtonEntity):
    """Button that clears manual/user intents for one Home Assistant area."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:eraser"
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
        self._attr_unique_id = f"{entry.entry_id}_area_{slugify_area_id(area_id)}_clear_manual"

    @property
    def device_info(self) -> dict[str, Any]:
        return _device_info(self._entry)

    @property
    def name(self) -> str | None:
        control = self._room_control()
        room_name = control.name if control is not None else self._area_id
        return f"Clear {room_name} manual overrides"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        control = self._room_control()
        if control is None:
            return {"area_id": self._area_id, "targets": []}
        return {
            "area_id": control.area_id,
            "area_name": control.name,
            "targets": sorted(control.targets),
            "manual_override_targets": sorted(control.manual_override_targets),
        }

    async def async_press(self) -> None:
        control = self._room_control()
        if control is None:
            return
        runtime = self.hass.data[DOMAIN].get(runtime_key(self._entry.entry_id))
        if isinstance(runtime, TickRuntime):
            async with runtime.mutation_lock:
                for target in control.targets | control.manual_override_targets:
                    self._engine.clear_user_intents(target)
                runtime.advance_revision()
                mark_lifecycle_mutated(self.hass, self._entry.entry_id)
        else:
            for target in control.targets | control.manual_override_targets:
                self._engine.clear_user_intents(target)
        self.hass.data[DOMAIN][publication_key(self._entry.entry_id)].publish_if_changed()

    def _room_control(self):
        return room_controls_for_engine(
            self._engine,
            lambda target: area_for_target(self.hass, target),
        ).get(self._area_id)
