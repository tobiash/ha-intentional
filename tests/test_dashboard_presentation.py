"""Scoped facts must not confuse shared demand with local application."""

from types import SimpleNamespace

import pytest

from intentional.dashboard_presentation import dashboard_presentation
from intentional.engine import Engine
from intentional.yaml_loader import Rule


def test_shared_rule_scope_counts_only_local_intents_and_preserves_variant_requests():
    engine = Engine(clock_fn=lambda: 0)
    engine.load_rules([
        Rule(id="shared:a", authored_rule_id="shared", when="true", target="light.a",
             set={"brightness_pct": 80}, group="lighting", reason="Evening lighting"),
        Rule(id="shared:b", authored_rule_id="shared", when="true", target="light.b",
             set={"brightness_pct": 35}, group="lighting", reason="Evening lighting"),
        Rule(id="camera", when="true", target="switch.camera", set={"state": "on"},
             group="security"),
    ])
    engine.evaluate_all()
    result = dashboard_presentation(
        engine, area="living", domain="light", group="lighting",
        area_for_target=lambda t: "living" if t in {"light.a", "switch.camera"} else "office",
    )
    assert len(result["rules"]) == 1
    row = result["rules"][0]
    assert row["reason"] == "Evening lighting"
    assert row["scope"] == {
        "targets": ["light.a"], "outside_targets": ["light.b"],
        "coverage": "current_membership", "shared": True, "non_target_effects": False,
    }
    assert row["scoped_active_intent_count"] == 1
    assert {v["desired"]["set"]["brightness_pct"] for v in row["variants"]} == {35, 80}
    assert [t["target"] for t in result["targets"]] == ["light.a"]
    assert dashboard_presentation(engine, group="missing")["rules"] == []


@pytest.mark.parametrize("state", [None, "unknown", "unavailable", "off"])
def test_request_composition_observation_and_manual_ownership_are_separate(state):
    engine = Engine(clock_fn=lambda: 0)
    engine.load_rules([
        Rule(id="evening", when="true", target="light.a", set={"brightness_pct": 80}),
        Rule(id="shield", when="true", target="light.a", cap={"brightness_pct": 25}),
        Rule(id="disabled", when="false", target="light.a", set={"state": "off"}, enabled=False),
    ])
    engine.evaluate_all()
    actual = None if state is None else SimpleNamespace(entity_id="light.a", state=state, attributes={})
    result = dashboard_presentation(engine, actual_for_target=lambda _: actual)
    target = result["targets"][0]
    assert target["desired"]["brightness_pct"] == 25
    assert target["baseline_rule_ids"] == ["evening"]
    assert target["availability"] == ("missing" if state is None else "available" if state == "off" else state)
    assert target["actual"] == (None if actual is None else {"state": state, "attributes": {}})
    disabled = next(r for r in result["rules"] if r["rule_id"] == "disabled")
    assert disabled["variants"][0]["enabled"] is False
    assert disabled["scoped_active_intent_count"] == 0
    engine.emit_user_intent("light.a", {"brightness_pct": 60}, ttl_ms=5000)
    manual = dashboard_presentation(engine)["targets"][0]
    assert manual["baseline_rule_ids"] == []
    assert manual["manual_override"]["remaining_ms"] == 5000
    assert manual["fields"][0]["provider"]["authority"] == "user"
    assert manual["desired"]["brightness_pct"] == 25


def test_selector_membership_is_scoped_even_when_idle_and_exclusions_match_engine():
    from intentional.records import IntentSelector

    members = ["light.a", "light.b", "light.excluded"]
    engine = Engine(clock_fn=lambda: 0, selector_resolver=lambda _: members)
    engine.load_rules([Rule(
        id="selected", when="binary_sensor.motion == 'on'",
        intent_selectors=(IntentSelector(domain="light", exclude=("light.excluded",), set={"brightness_pct": 80}),),
    )])
    result = dashboard_presentation(engine, area="living", area_for_target=lambda t: "living" if t == "light.a" else "office")
    row = result["rules"][0]
    assert row["scope"]["targets"] == ["light.a"]
    assert row["scope"]["outside_targets"] == ["light.b"]
    assert row["scoped_active_intent_count"] == 0
    assert row["variants"][0]["requests"][0]["desired"] == {"set": {"brightness_pct": 80}}
    engine.update_state("binary_sensor.motion", "on")
    engine.evaluate_all()
    assert set(engine.list_active_targets()) == {"light.a", "light.b"}
    members.remove("light.b")
    engine.evaluate_all()
    assert dashboard_presentation(engine)["rules"][0]["scope"]["targets"] == ["light.a"]


def test_modifier_noop_competition_and_counterfactual_use_resolver():
    engine = Engine(clock_fn=lambda: 0)
    engine.load_rules([
        Rule(id="low", when="true", target="light.a", set={"brightness_pct": 35}, confidence=0.1),
        Rule(id="evening", when="true", target="light.a", set={"brightness_pct": 80}, confidence=0.9),
        Rule(id="noop", when="true", target="light.a", cap={"brightness_pct": 90}),
        Rule(id="shield", when="true", target="light.a", cap={"brightness_pct": 25}),
    ])
    engine.evaluate_all()
    rows = {r["rule_id"]: r for r in dashboard_presentation(engine)["rules"]}
    assert rows["low"]["contributions"][0]["state"] == "competing"
    assert rows["noop"]["contributions"][0]["state"] == "modifier_no_change"
    assert rows["shield"]["contributions"][0]["changed_fields_without_rule"] == ["brightness_pct"]
    assert rows["evening"]["contributions"][0]["baseline_fields"] == ["brightness_pct"]
    engine.emit_user_intent("light.a", {"brightness_pct": 60}, ttl_ms=5000)
    rows = {r["rule_id"]: r for r in dashboard_presentation(engine)["rules"]}
    assert rows["evening"]["contributions"][0]["manual_precedence_fields"] == ["brightness_pct"]
    engine.set_enabled(False)
    assert all(r["presentation_state"] == "global_off" for r in dashboard_presentation(engine)["rules"])


def test_lifecycle_facets_remain_separate_from_contribution_and_do_not_mutate_engine():
    engine = Engine(clock_fn=lambda: 0)
    engine.load_rules([
        Rule(id="held", when="binary_sensor.motion == 'on'", hold_when="true", target="light.a", set={"state": "on"}),
        Rule(id="waiting", when="true", for_ms=10000, target="light.b", set={"state": "on"}),
        Rule(id="blocker", when="true", blocks=("blocked",), target="light.c", set={"state": "off"}),
        Rule(id="blocked", when="true", target="light.c", set={"state": "on"}),
        Rule(id="paused", when="true", target="light.d", set={"state": "on"}),
    ])
    engine.set_rules_paused({"paused"}, True)
    engine.update_state("binary_sensor.motion", "on")
    engine.evaluate_all()
    engine.update_state("binary_sensor.motion", "off")
    engine.evaluate_all()
    before = engine.export_lifecycle_records()
    rows = {r["rule_id"]: r for r in dashboard_presentation(engine)["rules"]}
    assert rows["held"]["presentation_state"] == "held"
    assert rows["held"]["variants"][0]["condition_firing"] is False
    assert rows["held"]["contributions"][0]["baseline_fields"] == ["state"]
    assert rows["waiting"]["presentation_state"] == "waiting"
    assert rows["blocked"]["presentation_state"] == "blocked"
    assert rows["paused"]["presentation_state"] == "paused"
    assert engine.export_lifecycle_records() == before


def test_animation_counterfactual_is_unknown_not_a_false_modifier_effect():
    from intentional.animation import AnimationSpec

    engine = Engine(clock_fn=lambda: 0)
    engine.load_rules([
        Rule(id="animated", when="true", target="light.a", set={"brightness_pct": 80},
             confidence=1, animation=AnimationSpec(kind="pulse", parameter="brightness_pct", values=[10, 80], duration_ms=1000)),
        Rule(id="cap", when="true", target="light.a", cap={"brightness_pct": 90}, confidence=0.1),
    ])
    engine.evaluate_all()
    result = dashboard_presentation(engine)
    row = next(r for r in result["rules"] if r["rule_id"] == "cap")
    assert row["contributions"][0]["impact_known"] is False
    assert row["contributions"][0]["changed_fields_without_rule"] is None
    assert row["contributions"][0]["state"] == "modifier_unknown"
    assert result["targets"][0]["fields"][0]["provider"]["rule_id"] == "animated"


def test_scoped_resume_includes_orphan_manual_targets_but_group_scope_does_not():
    engine = Engine(clock_fn=lambda: 0)
    engine.emit_user_intent("light.orphan", {"state": "off"}, ttl_ms=5000)
    engine.emit_user_intent("switch.camera", {"state": "off"}, ttl_ms=5000)
    result = dashboard_presentation(engine, area="living", domain="light", area_for_target=lambda _: "living")
    assert result["rules"] == []
    assert result["capabilities"]["resume_targets"] == ["light.orphan"]
    assert dashboard_presentation(engine, group="lighting")["capabilities"]["resume_targets"] == []
