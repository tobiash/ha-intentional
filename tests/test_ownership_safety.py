from __future__ import annotations

from types import SimpleNamespace

import pytest

from intentional.engine import Engine
from intentional.intent import Authority, Intent
from intentional.reconciliation import Reconciliation
from intentional.target_policy import TargetPolicy
from intentional.yaml_loader import load_rules_from_string


class Adapter:
    def __init__(self, state: str = "off", **attributes: object) -> None:
        self.state = SimpleNamespace(
            entity_id="light.test",
            state=state,
            attributes=attributes,
            context=SimpleNamespace(user_id=None),
        )
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def get_state(self, entity_id: str) -> object:
        return self.state

    async def async_call(self, domain: str, service: str, data: dict, *, context: object) -> None:
        self.calls.append((domain, service, data))

    def new_context(self) -> object:
        return object()


def reconciler() -> Reconciliation:
    return Reconciliation(
        drift_override_ttl_ms=1000,
        drift_confirmation_ms=0,
        service_failure_backoff_ms=10,
    )


def test_field_wrapper_parses_withdrawal_without_changing_modifiers() -> None:
    rules = load_rules_from_string("""
- id: safe
  while: {input_boolean.active: on}
  intent:
    light.test:
      state: {value: on, withdraw: adopt}
      brightness_pct: {value: 80, withdraw: 20, max: 90}
      color_temp_k: {max: 3000}
""")
    assert rules[0].set == {"state": "on", "brightness_pct": 80}
    assert rules[0].withdraw == {"state": "adopt", "brightness_pct": 20}
    assert rules[0].cap == {"brightness_pct": 90, "color_temp_k": 3000}


def test_modifier_only_intent_never_seeds_a_baseline() -> None:
    for operation, value in (("cap", 40), ("floor", 40), ("offset", 10), ("multiply", 0.5)):
        engine = Engine(clock_fn=lambda: 0)
        engine._active_intents = [Intent("light.test", **{operation: {"brightness_pct": value}})]
        resolved = engine.resolve("light.test")
        assert resolved.value == {}
        assert resolved.field_providers == {}


def test_modifiers_and_physical_bounds_apply_when_baseline_exists() -> None:
    engine = Engine(clock_fn=lambda: 0)
    engine._active_intents = [
        Intent("light.test", {"brightness_pct": 90}),
        Intent("light.test", cap={"brightness_pct": 80}),
        Intent("light.test", floor={"brightness_pct": 20}),
        Intent("light.test", offset={"brightness_pct": 50}),
        Intent("light.test", multiply={"brightness_pct": 2}),
    ]
    assert engine.resolve("light.test").value == {"brightness_pct": 80}


@pytest.mark.asyncio
async def test_lower_provider_reveal_suppresses_field_withdrawal() -> None:
    engine = Engine(clock_fn=lambda: 0)
    high = Intent("light.test", {"brightness_pct": 80}, withdraw={"brightness_pct": 10}, authority=Authority.USER)
    low = Intent("light.test", {"brightness_pct": 40})
    engine._active_intents = [high, low]
    adapter = Adapter("on", brightness_pct=20)
    reconciliation = reconciler()
    await reconciliation.apply(engine, adapter, 0)
    adapter.calls.clear()
    engine._active_intents = [low]
    await reconciliation.apply(engine, adapter, 1)
    assert adapter.calls[-1][2]["brightness_pct"] == 40


@pytest.mark.asyncio
async def test_adoption_is_captured_before_matching_skip_and_survives_restart() -> None:
    engine = Engine(clock_fn=lambda: 0)
    engine._active_intents = [Intent("light.test", {"brightness_pct": 80}, withdraw={"brightness_pct": "adopt"})]
    adapter = Adapter("on", brightness_pct=80)
    reconciliation = reconciler()
    await reconciliation.apply(engine, adapter, 0)
    records = reconciliation.export_pending_withdraws(engine)
    assert records[0]["field_ownership"]["brightness_pct"]["value"] == 80
    restored = reconciler()
    restored.restore_pending_withdraws({"pending_withdraws": records})
    engine._active_intents = []
    adapter.state.attributes["brightness_pct"] = 10
    adapter.calls.clear()
    await restored.apply(engine, adapter, 1)
    assert adapter.calls[-1][2]["brightness_pct"] == 80


@pytest.mark.asyncio
async def test_shadow_projects_without_service_or_applied_ownership() -> None:
    engine = Engine(clock_fn=lambda: 0)
    engine._active_intents = [Intent("light.test", {"state": "on"}, withdraw={"state": "off"})]
    engine._target_policies = {"light.test": TargetPolicy(dispatch="shadow")}
    adapter = Adapter()
    reconciliation = reconciler()
    events = await reconciliation.apply(engine, adapter, 0)
    assert adapter.calls == []
    assert [event.kind for event in events] == ["service_would_apply"]
    assert reconciliation.projection_state("light.test", 0)["last_applied"] is None
    assert reconciliation.pending_withdraw_targets() == ()
    assert reconciliation.export_pending_withdraws(engine) == []
    assert await reconciliation.apply(engine, adapter, 1) == []


@pytest.mark.asyncio
async def test_shadow_to_apply_adopts_actual_at_transition_time() -> None:
    engine = Engine(clock_fn=lambda: 0)
    engine._active_intents = [
        Intent("light.test", {"brightness_pct": 80}, withdraw={"brightness_pct": "adopt"})
    ]
    engine._target_policies = {"light.test": TargetPolicy(dispatch="shadow")}
    adapter = Adapter("on", brightness_pct=20)
    reconciliation = reconciler()

    await reconciliation.apply(engine, adapter, 0)
    adapter.state.attributes["brightness_pct"] = 35
    await reconciliation.apply(engine, adapter, 1)
    assert reconciliation.export_pending_withdraws(engine) == []

    engine._target_policies = {"light.test": TargetPolicy(dispatch="apply")}
    await reconciliation.apply(engine, adapter, 2)
    engine._active_intents = []
    adapter.state.attributes["brightness_pct"] = 80
    adapter.calls.clear()
    await reconciliation.apply(engine, adapter, 3)

    assert adapter.calls[-1][2]["brightness_pct"] == 35


def test_hysteresis_boundaries_dwell_and_restart() -> None:
    rules = load_rules_from_string("""
- id: warm
  while:
    sensor.temperature:
      enter: {gte: 25}
      exit: {lt: 20}
  after: 1s
  intent: {light.test: {state: on}}
""")
    engine = Engine(clock_fn=lambda: 0)
    engine.load_rules(rules)
    engine.update_state("sensor.temperature", 25)
    engine.evaluate_all()
    assert not engine.has_active_target("light.test")
    engine.advance_clock(1000)
    engine.evaluate_all()
    assert engine.has_active_target("light.test")
    records = engine.export_lifecycle_records()
    restored = Engine(clock_fn=lambda: 1000)
    restored.load_rules(rules)
    restored.update_state("sensor.temperature", 22)
    restored.import_lifecycle_records(records)
    restored.evaluate_all()
    assert restored.has_active_target("light.test")
    restored.update_state("sensor.temperature", 19.9)
    restored.evaluate_all()
    assert not restored.has_active_target("light.test")


def test_disabled_restore_preserves_hysteresis_latch_through_deadband() -> None:
    rules = load_rules_from_string("""
- id: warm
  while:
    sensor.temperature:
      enter: {gte: 25}
      exit: {lt: 20}
  intent: {light.test: {state: on}}
""")
    source = Engine(clock_fn=lambda: 0)
    source.load_rules(rules)
    source.update_state("sensor.temperature", 26)
    source.evaluate_all()
    records = source.export_lifecycle_records()
    records["enabled"] = False

    restored = Engine(clock_fn=lambda: 0)
    restored.load_rules(rules)
    restored.update_state("sensor.temperature", 22)
    restored.import_lifecycle_records(records)
    assert restored.is_enabled() is False

    restored.set_enabled(True)
    restored.evaluate_all()
    assert restored.has_active_target("light.test")
