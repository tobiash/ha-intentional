"""Natural manual control: dismiss this activation versus hold future ones."""

import pytest

from intentional.engine import Engine
from intentional.intent import Authority
from intentional.target_policy import TargetPolicy
from intentional.yaml_loader import Rule, load_rules_from_string


def setup():
    engine = Engine(clock_fn=lambda: 1000)
    rules = [
        Rule(id="evening", when="schedule.evening == 'on'", target="light.test", set={"state": "on", "brightness_pct": 80}),
        Rule(id="presence", when="binary_sensor.presence == 'on'", target="light.test", set={"state": "on", "brightness_pct": 30}, confidence=0.5),
        Rule(id="bell", when="input_boolean.bell == 'on'", target="light.test", set={"state": "on", "rgb_color": [255, 0, 0]}, authority=Authority.IMPORTANT),
    ]
    engine.load_rules(rules, target_policies={"light.test": TargetPolicy(manual_resume="activation", hold_controls=True)})
    engine.update_state("schedule.evening", "on")
    engine.update_state("binary_sensor.presence", "on")
    engine.evaluate_all()
    return engine, rules


def test_off_dismisses_evening_but_allows_fresh_presence():
    engine, _ = setup()
    engine.emit_manual_intent("light.test", {"state": "off"})
    engine.evaluate_all()
    assert engine.resolve("light.test").value == {"state": "off"}
    assert engine.dismissed_rule_ids("light.test") == ["evening", "presence"]
    engine.update_state("binary_sensor.presence", "off")
    engine.evaluate_all()
    assert engine.resolve("light.test").value == {"state": "off"}
    assert engine.dismissed_rule_ids("light.test") == ["evening"]
    engine.update_state("binary_sensor.presence", "on")
    engine.evaluate_all()
    assert engine.resolve("light.test").value["brightness_pct"] == 30
    assert not engine.list_active_user_intents()
    assert engine.dismissed_rule_ids("light.test") == ["evening"]
    engine.update_state("schedule.evening", "off")
    engine.evaluate_all()
    assert not engine.dismissed_rule_ids("light.test")
    engine.update_state("schedule.evening", "on")
    engine.evaluate_all()
    assert engine.resolve("light.test").value["brightness_pct"] == 80


def test_important_interruption_preserves_manual_preference():
    engine, _ = setup()
    engine.emit_manual_intent("light.test", {"state": "on", "rgb_color": [100, 0, 255]})
    engine.update_state("input_boolean.bell", "on")
    engine.evaluate_all()
    assert engine.resolve("light.test").value["rgb_color"] == [255, 0, 0]
    engine.update_state("input_boolean.bell", "off")
    engine.evaluate_all()
    assert engine.resolve("light.test").value["rgb_color"] == [100, 0, 255]


def test_explicit_hold_covers_future_rules_and_reveals_current_demand_on_expiry():
    engine, _ = setup()
    engine.emit_manual_intent("light.test", {"state": "off"}, hold=True, resume="timed", ttl_ms=7200000)
    engine.update_state("input_boolean.bell", "on")
    engine.evaluate_all()
    assert engine.resolve("light.test").value["state"] == "off"
    assert engine.list_active_user_intents()[0].authority is Authority.HOLD
    engine.advance_clock(7200000)
    engine.evaluate_all()
    assert engine.resolve("light.test").winning_intent.rule_id == "bell"


def test_explicit_hold_protects_brightness_from_lower_modifiers_but_not_protected_bounds():
    from intentional.projection import target_projection
    engine = Engine(clock_fn=lambda: 1000)
    engine.load_rules([Rule(id="cap", when="true", target="light.test", cap={"brightness_pct": 20})])
    engine.evaluate_all()
    engine.emit_manual_intent("light.test", {"state": "on", "brightness_pct": 100}, hold=True, resume="until_resumed")
    assert engine.resolve("light.test").value["brightness_pct"] == 100
    assert target_projection(engine, "light.test")["fields"][0]["modifiers"] == []
    engine.add_rule(Rule(id="protected", when="true", target="light.test", cap={"brightness_pct": 60}, authority=Authority.PROTECTED))
    engine.evaluate_all()
    assert engine.resolve("light.test").value["brightness_pct"] == 60


def test_session_dismissals_survive_reload_and_restart():
    engine, rules = setup()
    engine.emit_manual_intent("light.test", {"state": "off"})
    engine.load_rules(rules, target_policies={"light.test": TargetPolicy(manual_resume="activation")})
    engine.evaluate_all()
    assert engine.resolve("light.test").value == {"state": "off"}
    restored, _ = setup()
    restored.import_lifecycle_records(engine.export_lifecycle_records())
    restored.evaluate_all()
    assert restored.resolve("light.test").value == {"state": "off"}
    restored.clear_user_intents("light.test")
    assert restored.resolve("light.test").value["state"] == "on"
    assert restored.dismissed_rule_ids("light.test") == []


def test_timed_hold_survives_restart_with_original_deadline():
    engine, rules = setup()
    engine.emit_manual_intent("light.test", {"state": "off"}, hold=True, resume="timed", ttl_ms=10000)
    restored = Engine(clock_fn=lambda: 5000)
    restored.load_rules(rules)
    restored.import_lifecycle_records(engine.export_lifecycle_records())
    restored.evaluate_all()
    assert restored.list_active_user_intents()[0].expires_at_ms() == 11000
    restored.advance_clock(6000)
    restored.evaluate_all()
    assert not restored.list_active_user_intents()


def test_dismissals_are_target_scoped_and_include_hidden_contributions():
    engine, rules = setup()
    engine.add_rule(Rule(id="other", when="true", target="light.other", set={"state": "on"}))
    engine.evaluate_all()
    engine.emit_manual_intent("light.test", {"state": "off"})
    assert engine.resolve("light.other").value["state"] == "on"
    assert engine.dismissed_rule_ids("light.test") == ["evening", "presence"]


def test_session_respects_retention_and_releases_when_all_contributions_end():
    engine = Engine(clock_fn=lambda: 1000)
    engine.load_rules([Rule(id="presence", when="binary_sensor.presence == 'on'", target="light.test", set={"state": "on"}, linger_ms=30000)], target_policies={"light.test": TargetPolicy(manual_resume="activation")})
    engine.update_state("binary_sensor.presence", "on")
    engine.evaluate_all()
    engine.emit_manual_intent("light.test", {"state": "on", "brightness_pct": 100})
    engine.update_state("binary_sensor.presence", "off")
    engine.evaluate_all()
    engine.advance_clock(29000)
    engine.evaluate_all()
    assert engine.resolve("light.test").value["brightness_pct"] == 100
    engine.advance_clock(1000)
    engine.evaluate_all()
    assert engine.resolve("light.test") is None


def test_policy_parses_and_old_default_remains_timed():
    rules = load_rules_from_string("targets:\n  light.test:\n    manual_override: {resume: activation, hold_controls: true}\nrules: []")
    engine = Engine()
    engine.load_rules(rules)
    assert engine.hold_control_targets() == ("light.test",)
    assert engine.target_policy("light.test").manual_resume == "activation"
    assert TargetPolicy().manual_resume == "timed"


def test_native_brightness_hold_does_not_leak_percentage_or_its_modifiers():
    engine, _ = setup()
    engine.add_rule(Rule(id="cap", when="true", target="light.test", cap={"brightness_pct": 10}))
    engine.evaluate_all()
    engine.emit_manual_intent("light.test", {"state": "on", "brightness": 255}, hold=True)
    assert engine.resolve("light.test").value == {"state": "on", "brightness": 255}


def test_dismissals_remain_explainable_without_manual_preference():
    from intentional.projection import target_projection
    engine, _ = setup()
    engine.emit_manual_intent("light.test", {"state": "off"})
    engine.update_state("binary_sensor.presence", "off")
    engine.evaluate_all()
    engine.update_state("binary_sensor.presence", "on")
    engine.evaluate_all()
    projection = target_projection(engine, "light.test")
    assert projection["dismissed_rule_ids"] == ["evening"]
    assert next(r for r in projection["rules"] if r["rule_id"] == "evening")["state"] == "dismissed"


def test_restart_dwell_is_not_a_new_activation():
    rules = [Rule(id="presence", when="binary_sensor.presence == 'on'", for_ms=2000, target="light.test", set={"state": "on"})]
    engine = Engine(clock_fn=lambda: 1000)
    engine.load_rules(rules, target_policies={"light.test": TargetPolicy(manual_resume="activation")})
    engine.update_state("binary_sensor.presence", "on")
    engine.evaluate_all()
    engine.advance_clock(2000)
    engine.evaluate_all()
    engine.emit_manual_intent("light.test", {"state": "off"})
    restored = Engine(clock_fn=lambda: 4000)
    restored.load_rules(rules)
    restored.update_state("binary_sensor.presence", "on")
    restored.import_lifecycle_records(engine.export_lifecycle_records())
    restored.evaluate_all()
    restored.advance_clock(2000)
    restored.evaluate_all()
    assert restored.resolve("light.test").value == {"state": "off"}
    assert restored.dismissed_rule_ids("light.test") == ["presence"]


def test_no_active_contribution_uses_timed_fallback_not_instant_resume():
    engine = Engine(clock_fn=lambda: 1000)
    engine.load_rules([], target_policies={"light.test": TargetPolicy(manual_resume="activation")})
    engine.emit_manual_intent("light.test", {"state": "on"}, ttl_ms=60000)
    engine.evaluate_all()
    assert engine.resolve("light.test").value == {"state": "on"}
    engine.advance_clock(60000)
    engine.evaluate_all()
    assert engine.resolve("light.test") is None


def test_named_control_scope_groups_targets_without_extra_per_lamp_controls():
    engine = Engine()
    engine.load_rules([], target_policies={
        "light.one": TargetPolicy(hold_controls="Living room lights"),
        "light.two": TargetPolicy(hold_controls="Living room lights"),
        "light.office": TargetPolicy(hold_controls=True),
    })
    assert engine.hold_control_scopes() == {
        "scope:Living room lights": ("light.one", "light.two"), "light.office": ("light.office",),
    }


@pytest.mark.parametrize("policy", ["{hold_controls: 42}", "{hold_controls: ''}", "{resume: activation, duration: 1h}"])
def test_invalid_control_policy_rejected(policy):
    from intentional.rule_model import RuleLoadError
    with pytest.raises(RuleLoadError):
        load_rules_from_string(f"targets:\n  light.test:\n    manual_override: {policy}\nrules: []")
