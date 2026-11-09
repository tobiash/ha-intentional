"""Real HA service events preserve group user intent independently of telemetry."""

from types import SimpleNamespace

import pytest

pytest.importorskip("homeassistant")

from homeassistant.core import Context

from custom_components.intentional._engine.engine import Engine
from custom_components.intentional._engine.reconciliation import Reconciliation
from custom_components.intentional._engine.runtime import TickRuntime
from custom_components.intentional._engine.target_policy import TargetPolicy
from custom_components.intentional._engine.yaml_loader import Rule
from custom_components.intentional.manual_commands import ManualCommands
from custom_components.intentional.runtime_context import IntentionalContextTracker


def test_command_capture_imports_without_removed_service_helper(monkeypatch):
    import importlib

    from homeassistant.helpers import service

    from custom_components.intentional import manual_commands

    monkeypatch.delattr(service, "async_extract_referenced_entity_ids", raising=False)
    importlib.reload(manual_commands)


@pytest.mark.asyncio
async def test_group_toggle_captures_off_and_fences_dispatch_while_lock_is_busy(hass, hass_admin_user):
    engine = Engine(clock_fn=lambda: 1000)
    engine.load_rules([
        Rule(id="one", when="true", target="light.one", set={"state": "on", "brightness_pct": 60}),
        Rule(id="two", when="true", target="light.two", set={"state": "on"}),
    ], target_policies={"light.one": TargetPolicy(manual_resume="until_resumed")})
    engine.evaluate_all()
    hass.states.async_set("light.one", "on", {"brightness": 145})
    hass.states.async_set("light.two", "off")
    hass.states.async_set("light.room", "on", {"entity_id": ["light.one", "light.two"]})
    runtime = TickRuntime(tick_interval_ms=100)
    tracker = IntentionalContextTracker()
    reconciler = Reconciliation(drift_override_ttl_ms=300000, drift_confirmation_ms=1500, service_failure_backoff_ms=30000)
    records = []
    commands = ManualCommands(hass, engine, runtime, tracker, SimpleNamespace(mutated=lambda: None), reconciler, records.append)
    unsubscribe = hass.bus.async_listen("call_service", commands.receive)

    async def toggle(call):
        # Like a group, forwarding reuses the same context. It must not
        # replace or extend overrides captured from the original request.
        await hass.services.async_call("light", "turn_off", {"entity_id": ["light.one", "light.two"]}, context=call.context, blocking=True)
        hass.states.async_set("light.room", "off", {"entity_id": ["light.one", "light.two"]})

    async def turn_off(call):
        for entity_id in call.data["entity_id"]:
            hass.states.async_set(entity_id, "off")  # deliberately loses context

    hass.services.async_register("light", "toggle", toggle)
    hass.services.async_register("light", "turn_off", turn_off)
    await runtime.mutation_lock.acquire()
    try:
        await hass.services.async_call("light", "toggle", {"entity_id": "light.room"}, context=Context(user_id=hass_admin_user.id), blocking=True)
        # Let synchronous event listeners enqueue the lock-blocked commit.
        import asyncio
        await asyncio.sleep(0)
        assert runtime.pending_manual_commands == 1
        assert not runtime.is_revision(runtime.revision)
    finally:
        runtime.mutation_lock.release()
    await hass.async_block_till_done()
    assert engine.resolve("light.one").value["state"] == "off"
    assert engine.resolve("light.two").value["state"] == "off"
    assert engine.resolve("light.one").winning_intent.manual_resume == "until_resumed"
    assert sorted(records) == ["light.one", "light.two"]
    assert runtime.is_revision(runtime.revision)
    unsubscribe()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["automation", "own", "unattributed"])
async def test_non_user_commands_do_not_create_manual_overrides(hass, kind):
    engine = Engine()
    engine.load_rules([Rule(id="one", when="true", target="light.one", set={"state": "on"})])
    engine.evaluate_all()
    hass.states.async_set("light.one", "on")
    tracker = IntentionalContextTracker()
    context = {"automation": Context(user_id="person", parent_id="automation"), "own": tracker.new_context(), "unattributed": Context()}[kind]
    commands = ManualCommands(hass, engine, TickRuntime(tick_interval_ms=100), tracker, SimpleNamespace(mutated=lambda: None), None, lambda _: None)
    commands.receive(SimpleNamespace(context=context, data={"domain": "light", "service": "turn_off", "service_data": {"entity_id": "light.one"}}))
    await hass.async_block_till_done()
    assert engine.resolve("light.one").winning_intent.rule_id == "one"
