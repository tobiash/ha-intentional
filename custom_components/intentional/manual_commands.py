"""Capture explicit HA light commands before telemetry can lose attribution."""

from __future__ import annotations

from collections import OrderedDict
from typing import Any

from homeassistant.auth.permissions.const import POLICY_CONTROL
from homeassistant.core import callback
from homeassistant.helpers.target import TargetSelection, async_extract_referenced_entity_ids

from ._engine.ha_adapter import manual_set_from_service_data


class ManualCommands:
    """Expand light groups and commit user requests through the mutation lock."""

    def __init__(self, hass, engine, runtime, tracker, writer, reconciler, record) -> None:
        self.hass = hass
        self.engine = engine
        self.runtime = runtime
        self.tracker = tracker
        self.writer = writer
        self.reconciler = reconciler
        self.record = record
        self._seen: OrderedDict[tuple[str, str], None] = OrderedDict()

    @callback
    def receive(self, event) -> None:
        context = event.context
        if self.runtime.unloading or not self.engine.is_enabled() or not context.user_id or context.parent_id or self.tracker.owns_state(event):
            return
        domain, service = event.data.get("domain"), event.data.get("service")
        if domain not in {"light", "homeassistant"} or service not in {"turn_on", "turn_off", "toggle"}:
            return
        data = dict(event.data.get("service_data", {}))
        selected = async_extract_referenced_entity_ids(self.hass, TargetSelection(data), expand_group=False)
        roots = selected.referenced | selected.indirectly_referenced
        if data.get("entity_id") == "all":
            roots = set(self.hass.states.async_entity_ids("light"))
        managed = {r.target for r in self.engine.loaded_rules() if r.target} | set(self.engine.list_active_targets())
        requests: dict[str, dict[str, Any]] = {}
        for root in sorted(roots):
            state = self.hass.states.get(root)
            if state is None or not root.startswith(("light.", "group.")):
                continue
            power = "off" if service == "turn_off" or (service == "toggle" and state.state == "on") else "on"
            if service == "toggle" and state.last_changed > event.time_fired and state.state in {"on", "off"}:
                # A fast entity may have finished toggling before the event
                # listener runs. Capture its result instead of toggling twice.
                power = state.state
            payload = manual_set_from_service_data(data) if power == "on" else {}
            if "color_temp_kelvin" in data and power == "on":
                payload["color_temp_k"] = data["color_temp_kelvin"]
            payload["state"] = power
            if payload.get("brightness") == 0 or payload.get("brightness_pct") == 0:
                payload = {"state": "off"}
            for target in self._members(root, set()):
                if target not in managed or not target.startswith("light."):
                    continue
                policy = self.engine.target_policy(target)
                if policy is not None and (policy.ownership != "managed" or policy.dispatch != "apply"):
                    continue
                key = (context.id, target)
                if key in self._seen:
                    continue
                self._seen[key] = None
                requests[target] = dict(payload)
        while len(self._seen) > 1024:
            self._seen.popitem(last=False)
        if requests:
            self.runtime.pending_manual_commands += 1
            self.runtime.advance_revision()
            self.hass.async_create_task(self._commit(requests, context.id, context.user_id, roots))

    def _members(self, entity_id: str, seen: set[str]) -> set[str]:
        if entity_id in seen:
            return set()
        seen.add(entity_id)
        state = self.hass.states.get(entity_id)
        members = None if state is None else state.attributes.get("entity_id")
        if not isinstance(members, (list, tuple)):
            return {entity_id}
        result = {entity_id}
        for member in members:
            if isinstance(member, str):
                result.update(self._members(member, seen))
        return result

    async def _commit(self, requests: dict[str, dict[str, Any]], context_id: str, user_id: str, roots: set[str]) -> None:
        try:
            user = await self.hass.auth.async_get_user(user_id)
            if user is None or not user.is_active or any(not user.permissions.check_entity(root, POLICY_CONTROL) for root in roots):
                return
            async with self.runtime.mutation_lock:
                if self.runtime.unloading:
                    return
                for target, payload in requests.items():
                    if not user.permissions.check_entity(target, POLICY_CONTROL):
                        continue
                    self.engine.emit_manual_intent(
                        target, payload,
                        ttl_ms=self.reconciler.manual_override_ttl_ms(self.engine, target),
                        reason="Explicit HA user light command",
                    )
                    self.reconciler.manual_command_received(target, self.engine.now_ms(), context_id)
                    self.record(target)
                self.engine.evaluate_all()
                self.runtime.advance_revision()
                self.writer.mutated()
        finally:
            self.runtime.pending_manual_commands -= 1
