"""Native dashboard controls for opt-in, target-scoped explicit holds."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from homeassistant.components.button import ButtonEntity
from homeassistant.components.select import SelectEntity
from homeassistant.components.sensor import SensorEntity
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.restore_state import RestoreEntity

from ._engine.runtime import runtime_key
from .const import DEFAULT_NAME, DOMAIN
from .entity_lifecycle import RegistrationAwareEntity
from .publication import publication_signal

DURATIONS = {"15 minutes": 900, "30 minutes": 1800, "1 hour": 3600, "2 hours": 7200, "4 hours": 14400, "Until resumed": None}


def duration_key(entry_id, target):
    """Return the private duration-selection key (not an active hold)."""
    return f"{entry_id}:hold_duration:{target}"


async def setup_hold_entities(hass, entry, async_add_entities, factory):
    """Track opt-in targets across document saves without polling."""
    engine = hass.data[DOMAIN][entry.entry_id]
    entities = {}
    lock = asyncio.Lock()

    async def _refresh(_changed=None):
        targets = set(engine.hold_control_scopes())
        for target in set(entities) - targets:
            for entity in entities[target]:
                await entity.async_mark_removed()
        added = []
        for target in targets - set(entities):
            group = factory(target)
            entities[target] = group
            pending = set(group)
            for entity in group:
                def removed(target=target, group=group, entity=entity, pending=pending):
                    pending.discard(entity)
                    if not pending and entities.get(target) is group:
                        entities.pop(target, None)
                entity.set_removal_callback(removed)
            added.extend(group)
        for target in targets & set(entities):
            for entity in entities[target]:
                entity.mark_desired()
                if _changed is None or "global" in _changed or any(f"hold:{t}" in _changed for t in entity.targets):
                    entity.async_write_if_registered()
        if added:
            async_add_entities(added)

    async def refresh(changed=None):
        async with lock:
            runtime = hass.data[DOMAIN].get(runtime_key(entry.entry_id))
            if runtime is not None and runtime.unloading:
                return
            await _refresh(changed)

    await refresh()
    entry.async_on_unload(async_dispatcher_connect(hass, publication_signal(entry.entry_id), refresh))


class HoldEntity(RegistrationAwareEntity):
    """Shared identity and scope for a normal dashboard entity."""

    _attr_has_entity_name = True

    def __init__(self, hass, entry, target, kind):
        super().__init__()
        self.hass = hass
        self.entry = entry
        self.target = target
        self.engine = hass.data[DOMAIN][entry.entry_id]
        self._attr_unique_id = f"{entry.entry_id}_hold_{target}_{kind}"
        state = hass.states.get(target)
        label = target.removeprefix("scope:") if target.startswith("scope:") else state.name if state is not None else target
        self._attr_name = f"{label} {kind.replace('_', ' ')}"

    @property
    def device_info(self):
        return {"identifiers": {(DOMAIN, self.entry.entry_id)}, "name": DEFAULT_NAME,
                "manufacturer": "Intentional", "model": "Intent Engine"}

    @property
    def available(self):
        return self.target in self.engine.hold_control_scopes() and self.engine.is_enabled()

    @property
    def targets(self):
        return self.engine.hold_control_scopes().get(self.target, ())

    @property
    def extra_state_attributes(self):
        return {"scope": self.target, "targets": list(self.targets)}


class HoldDuration(HoldEntity, RestoreEntity, SelectEntity):
    """Select a duration without starting or modifying a hold."""

    _attr_options = list(DURATIONS)
    _attr_icon = "mdi:timer-outline"

    def __init__(self, hass, entry, target):
        super().__init__(hass, entry, target, "hold_duration")

    @property
    def current_option(self):
        return self.hass.data[DOMAIN].get(duration_key(self.entry.entry_id, self.target), "1 hour")

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        previous = await self.async_get_last_state()
        if previous is not None and previous.state in DURATIONS:
            self.hass.data[DOMAIN][duration_key(self.entry.entry_id, self.target)] = previous.state
        self.async_write_if_registered()

    async def async_select_option(self, option):
        if option not in DURATIONS:
            raise HomeAssistantError("Unknown hold duration")
        self.hass.data[DOMAIN][duration_key(self.entry.entry_id, self.target)] = option
        self.async_write_if_registered()


class HoldButton(HoldEntity, ButtonEntity):
    """Create an explicit hold or return the target to current rule demand."""

    _attr_icon = "mdi:hand-back-right"

    def __init__(self, hass, entry, target, action):
        self.action = action
        super().__init__(hass, entry, target, {"off": "hold_off", "current": "hold_current_settings", "resume": "resume_automation"}[action])

    async def async_press(self):
        if not self.available:
            raise HomeAssistantError("Intentional hold control is unavailable")
        if self.action == "resume":
            for target in self.targets:
                await self.hass.services.async_call(DOMAIN, "clear", {"target": target}, blocking=True, context=self._context)
            return
        payloads = {}
        for target in self.targets:
            state = self.hass.states.get(target)
            payload = {"state": "off"}
            if self.action == "current":
                if state is None or state.state not in {"on", "off"}:
                    raise HomeAssistantError("Cannot hold an unknown or unavailable light state")
                payload = {"state": state.state}
                if state.state == "on":
                    attrs = state.attributes
                    if attrs.get("brightness") is not None:
                        payload["brightness"] = attrs["brightness"]
                    mode = attrs.get("color_mode")
                    color = "color_temp_kelvin" if mode == "color_temp" else f"{mode}_color"
                    if attrs.get(color) is not None:
                        payload["color_temp_k" if color == "color_temp_kelvin" else color] = attrs[color]
            payloads[target] = payload
        seconds = DURATIONS[self.hass.data[DOMAIN].get(duration_key(self.entry.entry_id, self.target), "1 hour")]
        for target, payload in payloads.items():
            await self.hass.services.async_call(DOMAIN, "fire", {
                "target": target, "hold": True, "resume": "until_resumed" if seconds is None else "timed",
                **({"ttl": seconds} if seconds is not None else {}), **payload,
            }, blocking=True, context=self._context)


class HoldStatus(HoldEntity, SensorEntity):
    """Explain manual preference, persistent dismissals, and active authority."""

    _attr_icon = "mdi:hand-back-right-outline"

    def __init__(self, hass, entry, target):
        super().__init__(hass, entry, target, "manual_control_status")

    @property
    def native_value(self):
        states = set()
        for target in self.targets:
            intents = self.engine.list_active_user_intents(target)
            if intents:
                manual = intents[0]
                states.add("held_off" if manual.authority.value == "hold" and manual.set.get("state") == "off" else (
                    "held" if manual.authority.value == "hold" else "manual"))
            else:
                states.add("dismissed" if self.engine.dismissed_rule_ids(target) else "automatic")
        return next(iter(states)) if len(states) == 1 else "mixed"

    @property
    def extra_state_attributes(self):
        details = {}
        for target in self.targets:
            intents = self.engine.list_active_user_intents(target)
            manual = intents[0] if intents else None
            resolved = self.engine.resolve(target)
            winner = resolved.winning_intent if resolved else None
            details[target] = {"dismissed_rule_ids": self.engine.dismissed_rule_ids(target),
                "expires_at_ms": manual.expires_at_ms() if manual else None,
                "resume": manual.manual_resume if manual else None,
                "held_fields": dict(manual.set) if manual else {},
                "winning_rule_id": winner.rule_id if winner else None,
                "winning_authority": winner.authority.value if winner else None,
                "interrupted": bool(manual and winner and winner is not manual and winner.authority > manual.authority)}
        expiries = [detail["expires_at_ms"] for detail in details.values()]
        ends_at = datetime.fromtimestamp(max(expiries) / 1000, UTC).isoformat() if expiries and all(expiries) else None
        return {"scope": self.target, "targets": list(self.targets), "target_status": details,
                "ends_at": ends_at, "protected_rules_can_interrupt": True}
