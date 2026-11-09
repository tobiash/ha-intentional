"""Manual control survives imperfect telemetry and has an explicit lifetime."""

from types import SimpleNamespace

import pytest

from intentional.engine import Engine
from intentional.reconciliation import Reconciliation
from intentional.target_policy import TargetPolicy
from intentional.yaml_loader import Rule, load_rules_from_string
from tests.test_reconciliation import _FakeAdapter, _NoContextTracker


@pytest.mark.asyncio
async def test_physical_off_does_not_require_brightness_to_have_converged():
    engine = Engine(clock_fn=lambda: 1000)
    engine.load_rules([Rule(id="on", when="true", target="light.test", set={"state": "on", "brightness_pct": 60})])
    engine.evaluate_all()
    adapter = _FakeAdapter()
    adapter.set_state("light.test", "on", {"brightness": 145})
    reconciler = Reconciliation(drift_override_ttl_ms=300000, drift_confirmation_ms=1500, service_failure_backoff_ms=30000)
    await reconciler.apply(engine, adapter, 1000)
    reconciler.on_state_delta(engine, adapter.get_state("light.test"), _NoContextTracker(), 4000)
    adapter.calls.clear()
    adapter.set_state("light.test", "off")
    reconciler.on_state_delta(engine, adapter.get_state("light.test"), _NoContextTracker(), 5000)
    await reconciler.tick(engine, adapter, _NoContextTracker(), 5100)
    assert adapter.calls == []
    events = await reconciler.tick(engine, adapter, _NoContextTracker(), 6600)
    assert any(e.kind == "drift_promoted" and e.details["set"] == {"state": "off"} for e in events)


def test_until_vacant_survives_restart_and_requires_fresh_stable_vacancy():
    clock = [1000]
    engine = Engine(clock_fn=lambda: clock[0])
    rules = load_rules_from_string('''
targets:
  light.test:
    manual_override:
      resume: until_vacant
      vacancy_entity: binary_sensor.presence
      vacancy_for: 30s
rules:
  - id: occupied
    while:
      binary_sensor.presence: on
    intent:
      light.test:
        state: on
''')
    engine.load_rules(rules)
    engine.update_state("binary_sensor.presence", "on")
    engine.evaluate_all()
    engine.emit_manual_intent("light.test", {"state": "off"}, ttl_ms=300000)
    clock[0] += 3600000
    engine.evaluate_all()
    assert engine.resolve("light.test").value["state"] == "off"
    restored = Engine(clock_fn=lambda: clock[0])
    restored.load_rules(rules)
    restored.import_lifecycle_records(engine.export_lifecycle_records())
    restored.update_state("binary_sensor.presence", "off")
    restored.evaluate_all()
    clock[0] += 29000
    restored.evaluate_all()
    assert restored.resolve("light.test") is not None
    restored.update_state("binary_sensor.presence", "unavailable")
    restored.evaluate_all()
    restored.update_state("binary_sensor.presence", "off")
    restored.evaluate_all()
    clock[0] += 30000
    restored.evaluate_all()
    assert restored.resolve("light.test") is None
    restored.update_state("binary_sensor.presence", "on")
    restored.evaluate_all()
    assert restored.resolve("light.test").value["state"] == "on"


def test_until_resumed_and_timed_policies():
    clock = [1000]
    engine = Engine(clock_fn=lambda: clock[0])
    engine.load_rules([], target_policies={"light.test": TargetPolicy(manual_resume="until_resumed")})
    engine.emit_manual_intent("light.test", {"state": "off"}, ttl_ms=100)
    clock[0] += 1000
    engine.evaluate_all()
    assert engine.resolve("light.test") is not None
    engine.clear_user_intents("light.test")
    assert engine.resolve("light.test") is None
    engine.emit_manual_intent("light.test", {"state": "on"}, ttl_ms=100, resume="timed")
    clock[0] += 100
    engine.evaluate_all()
    assert engine.resolve("light.test") is None


def test_manual_off_waits_for_retained_lighting_demand_to_end():
    clock = [1000]
    engine = Engine(clock_fn=lambda: clock[0])
    engine.load_rules([Rule(id="evening", when="input_boolean.evening == 'on'", target="light.test", set={"brightness_pct": 80})], target_policies={
        "light.test": TargetPolicy(manual_resume="until_vacant", vacancy_entity="binary_sensor.presence", vacancy_for_ms=30000),
    })
    engine.update_state("input_boolean.evening", "on")
    engine.update_state("binary_sensor.presence", "off")
    engine.evaluate_all()
    engine.emit_manual_intent("light.test", {"state": "off"})
    engine.evaluate_all()
    clock[0] += 60000
    engine.evaluate_all()
    assert engine.resolve("light.test").value["state"] == "off"
    engine.update_state("input_boolean.evening", "off")
    engine.evaluate_all()
    engine.evaluate_all()
    assert engine.resolve("light.test") is None


@pytest.mark.asyncio
async def test_power_only_detection_does_not_promote_brightness_telemetry():
    engine = Engine(clock_fn=lambda: 1000)
    engine.load_rules([Rule(id="on", when="true", target="light.test", set={"state": "on", "brightness_pct": 60})], target_policies={"light.test": TargetPolicy(manual_detection="power")})
    engine.evaluate_all()
    adapter = _FakeAdapter()
    adapter.set_state("light.test", "on", {"brightness": 153})
    reconciler = Reconciliation(drift_override_ttl_ms=300000, drift_confirmation_ms=1500, service_failure_backoff_ms=30000)
    await reconciler.apply(engine, adapter, 1000)
    adapter.set_state("light.test", "on", {"brightness": 140})
    for now in (4000, 6000, 10000):
        events = reconciler.on_state_delta(engine, adapter.get_state("light.test"), _NoContextTracker(), now)
        assert not any(e.kind == "drift_promoted" for e in events)
    assert reconciler.projection_state("light.test", 10000)["pending_drift"] is None


@pytest.mark.parametrize("config", [
    "{resume: until_vacant}",
    "{resume: until_vacant, vacancy_entity: sensor.temperature}",
    "{resume: until_resumed, duration: 10m}",
    "{resume: timed, duration: 0s}",
    "{resume: timed, duration: true}",
    "{resume: forever}",
    "{unattributed: guess}",
])
def test_invalid_resume_configuration_is_rejected(config):
    from intentional.rule_model import RuleLoadError
    with pytest.raises(RuleLoadError):
        load_rules_from_string(f"targets:\n  light.test:\n    manual_override: {config}\nrules: []\n")


@pytest.mark.asyncio
async def test_captured_command_feedback_cannot_replace_requested_off():
    engine = Engine(clock_fn=lambda: 1000)
    engine.load_rules([Rule(id="on", when="true", target="light.test", set={"state": "on"}, transition_ms=30000)])
    engine.evaluate_all()
    adapter = _FakeAdapter()
    adapter.set_state("light.test", "on")
    reconciler = Reconciliation(drift_override_ttl_ms=300000, drift_confirmation_ms=1500, service_failure_backoff_ms=30000)
    await reconciler.apply(engine, adapter, 1000)
    engine.emit_manual_intent("light.test", {"state": "off"})
    reconciler.manual_command_received("light.test", 1100, "command")
    await reconciler.apply(engine, adapter, 1100)
    adapter.set_state("light.test", "on", context=SimpleNamespace(id="command", parent_id=None, user_id="person"))
    for now in (1200, 4000, 40000):
        assert reconciler.on_state_delta(engine, adapter.get_state("light.test"), _NoContextTracker(), now) == []
    assert engine.resolve("light.test").value["state"] == "off"
