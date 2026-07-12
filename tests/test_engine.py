"""Tests for the engine orchestrator.

The engine ties together rules, state, the compositor, and animation timing.
It exposes a small API:

- update_state(entity_id, value): inject a state change
- list_active_intents(target): get current intents for a target
- resolve(target): get the ResolvedIntent for a target
- evaluate_all(): re-evaluate all rules' `when` clauses, emit/drop intents
- tick(): drive animation frames and TTL expiry
- load_rules(rules): replace the rule set

These tests use a mock state store and mock notifier (so we can verify
the engine emits intents when triggers fire) without depending on HA.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from intentional.engine import Engine
from intentional.intent import Authority
from intentional.target_policy import TargetPolicy
from intentional.yaml_loader import Rule, load_rules_from_string

_DYNAMIC_HOLD_YAML = """
- id: adaptive
  while: {binary_sensor.motion: on}
  hold:
    after:
      tiers:
        - {active_for: 0s, duration: 30s}
        - {active_for: 30m, duration: 10m}
      adjustments:
        - {from: "22:00", until: "06:00", add: 5m}
        - {from: "00:00", until: "00:00", add: -1m}
      max: 15m
  intent:
    light.one: {state: on}
"""


def test_dynamic_hold_freezes_tier_adjustment_and_survives_restart_reload() -> None:
    now = [0]
    rules = load_rules_from_string(_DYNAMIC_HOLD_YAML)
    engine = Engine(clock_fn=lambda: now[0])
    engine.load_rules(rules)
    engine.set_time_of_day("night", clock="21:59")
    engine.update_state("binary_sensor.motion", "on")
    engine.evaluate_all()
    now[0] = 1_800_000
    engine.evaluate_all()
    engine.set_time_of_day("night", clock="22:00")
    engine.update_state("binary_sensor.motion", "off")
    engine.evaluate_all()
    status = engine.list_rule_statuses()["adaptive"]["hold_after"]
    assert status == {
        "frozen": True, "active_for_ms": 1_800_000,
        "tier": {"index": 1, "threshold_ms": 1_800_000, "base_duration_ms": 600_000},
        "adjustment": {"index": 0, "from": "22:00", "until": "06:00", "add_ms": 300_000},
        "max_ms": 900_000, "unclamped_duration_ms": 900_000, "duration_ms": 900_000,
        "started_at_ms": 1_800_000, "expires_at_ms": 2_700_000, "remaining_ms": 900_000,
    }
    engine.set_time_of_day("day", clock="12:00")
    now[0] += 100_000
    engine.load_rules(rules)
    engine.evaluate_all()
    assert engine.list_rule_statuses()["adaptive"]["hold_after"]["expires_at_ms"] == 2_700_000
    records = engine.export_lifecycle_records()
    restarted = Engine(clock_fn=lambda: now[0])
    restarted.load_rules(rules)
    restarted.import_lifecycle_records(records)
    assert restarted.list_rule_statuses()["adaptive"]["hold_after"]["remaining_ms"] == 800_000


def test_dynamic_hold_boundaries_first_match_and_zero_withdrawal() -> None:
    yaml = _DYNAMIC_HOLD_YAML.replace("duration: 30s", "duration: 30s").replace("add: 5m", "add: -1m")
    engine = Engine(clock_fn=lambda: 0)
    engine.load_rules(load_rules_from_string(yaml))
    engine.set_time_of_day("night", clock="06:00")
    engine.update_state("binary_sensor.motion", "on")
    engine.evaluate_all()
    engine.update_state("binary_sensor.motion", "off")
    engine.evaluate_all()
    assert engine.list_active_intents("light.one") == []


def test_dynamic_hold_reactivation_starts_fresh_and_can_freeze_again() -> None:
    now = [0]
    engine = Engine(clock_fn=lambda: now[0])
    engine.load_rules(load_rules_from_string(_DYNAMIC_HOLD_YAML.replace("add: -1m", "add: 1m")))
    engine.set_time_of_day("day", clock="12:00")
    engine.update_state("binary_sensor.motion", "on")
    engine.evaluate_all()

    now[0] = 1_800_000
    engine.set_time_of_day("night", clock="22:00")
    engine.update_state("binary_sensor.motion", "off")
    engine.evaluate_all()
    lingering = engine.list_active_intents("light.one")[0]
    assert lingering.ignore_when is True
    assert lingering.expires_at_ms() == 2_700_000

    now[0] = 1_810_000
    engine.update_state("binary_sensor.motion", "on")
    engine.evaluate_all()
    active = engine.list_active_intents("light.one")[0]
    assert active.ignore_when is False
    assert active.ttl_ms is None
    assert active.created_at_ms == now[0]
    assert engine.list_rule_statuses()["adaptive"]["hold_after"] is None

    now[0] = 1_820_000
    engine.set_time_of_day("day", clock="12:00")
    engine.update_state("binary_sensor.motion", "off")
    engine.evaluate_all()
    second = engine.list_rule_statuses()["adaptive"]["hold_after"]
    assert second["active_for_ms"] == 10_000
    assert second["tier"]["index"] == 0
    assert second["adjustment"] == {
        "index": 1,
        "from": "00:00",
        "until": "00:00",
        "add_ms": 60_000,
    }
    assert second["duration_ms"] == 90_000
    second_intent = engine.list_active_intents("light.one")[0]
    assert second["expires_at_ms"] == second_intent.expires_at_ms()


def test_dynamic_hold_selector_reactivation_refreshes_current_membership_once() -> None:
    now = [0]
    selected = ["light.one", "light.two"]
    engine = Engine(
        clock_fn=lambda: now[0],
        selector_resolver=lambda _selector: list(selected),
    )
    engine.load_rules(load_rules_from_string("""
- id: adaptive-selector
  while: {binary_sensor.motion: on}
  hold:
    after:
      tiers:
        - {active_for: 0s, duration: 30s}
      adjustments: []
      max: 30s
  intent:
    select:
      - domain: light
        state: on
"""))
    engine.update_state("binary_sensor.motion", "on")
    engine.evaluate_all()

    now[0] = 10_000
    engine.update_state("binary_sensor.motion", "off")
    engine.evaluate_all()
    assert {
        intent.target
        for target in selected
        for intent in engine.list_active_intents(target)
    } == set(selected)

    selected[:] = ["light.two", "light.three"]
    now[0] = 20_000
    engine.update_state("binary_sensor.motion", "on")
    engine.evaluate_all()
    engine.evaluate_all()

    records = engine.export_lifecycle_records()["intents"]
    generated = [record for record in records if record["selector_generated"]]
    assert [(record["target"], record["created_at_ms"]) for record in generated] == [
        ("light.two", now[0]),
        ("light.three", now[0]),
    ]
    assert all(record["target"] for record in records)
    assert engine.resolve("light.one") is None
    assert engine.resolve("light.two").value == {"state": "on"}
    assert engine.resolve("light.three").value == {"state": "on"}


def test_dynamic_hold_provenance_expiry_matches_intent_expiry() -> None:
    now = [9_007_199_254_740_000]
    engine = Engine(clock_fn=lambda: now[0])
    engine.load_rules(load_rules_from_string(_DYNAMIC_HOLD_YAML))
    engine.set_time_of_day("night", clock="22:00")
    engine.update_state("binary_sensor.motion", "on")
    engine.evaluate_all()
    engine.update_state("binary_sensor.motion", "off")
    engine.evaluate_all()

    intent = engine.list_active_intents("light.one")[0]
    status = engine.list_rule_statuses()["adaptive"]["hold_after"]
    assert status["expires_at_ms"] == intent.expires_at_ms()


def test_unchanged_reload_preserves_held_intent() -> None:
    rule = Rule(id="held", when="binary_sensor.motion == 'on'",
                hold_when="input_boolean.keep == 'on'", target="light.one",
                set={"state": "on"})
    engine = Engine(clock_fn=lambda: 1_000)
    engine.load_rules([rule])
    engine.update_state("binary_sensor.motion", "on")
    engine.update_state("input_boolean.keep", "on")
    engine.evaluate_all()
    engine.update_state("binary_sensor.motion", "off")
    engine.evaluate_all()
    held = engine.list_active_intents("light.one")[0]

    engine.load_rules([replace(rule, source_file=Path("moved.yaml"), source_line=4)])

    assert engine.list_active_intents("light.one") == [held]


def test_restart_restores_holds_with_false_starting_conditions() -> None:
    rules = [
        Rule(id="while-held", when="binary_sensor.motion == 'on'",
             hold_when="input_boolean.keep == 'on'", target="light.one", set={"state": "on"}),
        Rule(id="until-held", when="binary_sensor.door == 'on'",
             hold_until_when="input_boolean.release == 'on'", target="light.two", set={"state": "on"}),
    ]
    engine = Engine(clock_fn=lambda: 1_000)
    engine.load_rules(rules)
    for entity, value in (("binary_sensor.motion", "on"), ("input_boolean.keep", "on"),
                          ("binary_sensor.door", "on"), ("input_boolean.release", "off")):
        engine.update_state(entity, value)
    engine.evaluate_all()
    engine.update_state("binary_sensor.motion", "off")
    engine.update_state("binary_sensor.door", "off")
    engine.evaluate_all()

    restarted = Engine(clock_fn=lambda: 1_100)
    restarted.load_rules(rules)
    for key, value in engine.state.items():
        entity, _, field = key.rpartition(".")
        restarted.update_state(entity, value, field=field)
    restarted.import_lifecycle_records(engine.export_lifecycle_records())
    restarted.evaluate_all()

    assert {
        intent.rule_id
        for target in ("light.one", "light.two")
        for intent in restarted.list_active_intents(target)
    } == {"while-held", "until-held"}


def test_version_one_static_linger_and_hold_restore_without_fingerprints() -> None:
    rules = [
        Rule(id="linger", when="binary_sensor.a == 'on'", target="light.one",
             set={"state": "on"}, linger_ms=60_000),
        Rule(id="held", when="binary_sensor.b == 'on'", hold_when="input_boolean.keep == 'on'",
             target="light.two", set={"state": "on"}),
    ]
    source = Engine(clock_fn=lambda: 1_000)
    source.load_rules(rules)
    source.update_state("binary_sensor.a", "on")
    source.update_state("binary_sensor.b", "on")
    source.update_state("input_boolean.keep", "on")
    source.evaluate_all()
    source.update_state("binary_sensor.a", "off")
    source.update_state("binary_sensor.b", "off")
    source.evaluate_all()
    records = source.export_lifecycle_records()
    records["version"] = 1
    for intent in records["intents"]:
        intent.pop("rule_fingerprint", None)

    restored = Engine(clock_fn=lambda: 2_000)
    restored.load_rules(rules)
    restored.import_lifecycle_records(records)

    assert restored.resolve("light.one") is not None
    assert restored.resolve("light.two") is not None


def test_version_two_changed_static_rule_is_not_restored() -> None:
    rule = Rule(id="linger", when="binary_sensor.a == 'on'", target="light.one",
                set={"state": "on"}, linger_ms=60_000)
    source = Engine(clock_fn=lambda: 1_000)
    source.load_rules([rule])
    source.update_state("binary_sensor.a", "on")
    source.evaluate_all()
    source.update_state("binary_sensor.a", "off")
    source.evaluate_all()

    restored = Engine(clock_fn=lambda: 2_000)
    restored.load_rules([replace(rule, set={"state": "off"})])
    restored.import_lifecycle_records(source.export_lifecycle_records())

    assert restored.resolve("light.one") is None


def test_lifecycle_restore_rejects_unsupported_or_malformed_version() -> None:
    for version in (None, True, "2", 0, 3):
        restored = Engine(clock_fn=lambda: 1_000)
        restored.import_lifecycle_records({"version": version, "enabled": False})
        assert restored.is_enabled() is True


def test_disable_and_pause_immediately_end_dynamic_hold_lifecycle() -> None:
    for stop in (
        lambda engine: engine.set_enabled(False),
        lambda engine: engine.set_rule_paused("adaptive", True),
        lambda engine: engine.set_label_paused("room", True),
    ):
        rule = replace(load_rules_from_string(_DYNAMIC_HOLD_YAML)[0], labels=("room",))
        engine = Engine(clock_fn=lambda: 1_000)
        engine.load_rules([rule])
        engine.update_state("binary_sensor.motion", "on")
        engine.evaluate_all()
        stop(engine)

        assert engine._rule_active_since == {}
        assert engine._frozen_hold_after == {}


def test_disabled_restore_does_not_resurrect_dynamic_hold_activation() -> None:
    rules = load_rules_from_string(_DYNAMIC_HOLD_YAML)
    source = Engine(clock_fn=lambda: 1_000)
    source.load_rules(rules)
    source.update_state("binary_sensor.motion", "on")
    source.evaluate_all()
    records = source.export_lifecycle_records()
    records["enabled"] = False

    restored = Engine(clock_fn=lambda: 2_000)
    restored.load_rules(rules)
    restored.import_lifecycle_records(records)

    assert restored.is_enabled() is False
    assert restored._rule_active_since == {}
    assert restored._frozen_hold_after == {}


def test_restart_rejects_retained_intent_and_frozen_provenance_for_changed_rule() -> None:
    rules = load_rules_from_string(_DYNAMIC_HOLD_YAML)
    engine = Engine(clock_fn=lambda: 0)
    engine.load_rules(rules)
    engine.set_time_of_day("night", clock="22:00")
    engine.update_state("binary_sensor.motion", "on")
    engine.evaluate_all()
    engine.update_state("binary_sensor.motion", "off")
    engine.evaluate_all()
    records = engine.export_lifecycle_records()

    restarted = Engine(clock_fn=lambda: 0)
    restarted.load_rules([replace(rules[0], set={"state": "off"})])
    restarted.import_lifecycle_records(records)

    assert restarted.list_active_intents("light.one") == []
    assert restarted.list_rule_statuses()["adaptive"]["hold_after"] is None


def test_rule_activation_restore_rejects_malformed_records() -> None:
    rules = load_rules_from_string(_DYNAMIC_HOLD_YAML)
    source = Engine(clock_fn=lambda: 1_000)
    source.load_rules(rules)
    source.update_state("binary_sensor.motion", "on")
    source.evaluate_all()
    valid = source.export_lifecycle_records()["rule_activations"][0]

    for activations in (None, {}, "bad", [None], [{**valid, "active_since_ms": True}]):
        restored = Engine(clock_fn=lambda: 1_000)
        restored.load_rules(rules)
        restored.import_lifecycle_records({"version": 2, "rule_activations": activations})
        assert restored._rule_active_since == {}

    source._freeze_dynamic_hold("adaptive", rules[0], 1_000)
    frozen_record = source.export_lifecycle_records()["rule_activations"][0]
    for corruption in (
        {"duration_ms": -1}, {"tier_threshold_ms": 1, "active_for_ms": 0},
        {"expires_at_ms": 999}, {"adjustment_index": 0, "adjustment_from": "bad"},
        {"adjustment_index": None, "adjustment_from": "22:00"},
        {"unclamped_duration_ms": 123},
    ):
        raw = dict(frozen_record)
        raw["frozen_hold_after"] = {**raw["frozen_hold_after"], **corruption}
        restored = Engine(clock_fn=lambda: 1_000)
        restored.load_rules(rules)
        restored.import_lifecycle_records({"version": 2, "rule_activations": [raw]})
        assert restored._frozen_hold_after == {}


def test_backward_wall_clock_clamps_elapsed_and_preserves_timestamp() -> None:
    now = [1_000]
    rules = load_rules_from_string(_DYNAMIC_HOLD_YAML)
    engine = Engine(clock_fn=lambda: now[0])
    engine.load_rules(rules)
    engine.update_state("binary_sensor.motion", "on")
    engine.evaluate_all()
    records = engine.export_lifecycle_records()

    now[0] = 500
    restarted = Engine(clock_fn=lambda: now[0])
    restarted.load_rules(rules)
    restarted.import_lifecycle_records(records)
    restarted.update_state("binary_sensor.motion", "off")
    restarted.evaluate_all()

    assert restarted._rule_active_since["adaptive"] == 1_000
    assert restarted.list_rule_statuses()["adaptive"]["hold_after"]["active_for_ms"] == 0


def test_active_dynamic_lifetime_survives_restart_before_condition_stops() -> None:
    now = [0]
    rules = load_rules_from_string(_DYNAMIC_HOLD_YAML)
    engine = Engine(clock_fn=lambda: now[0])
    engine.load_rules(rules)
    engine.update_state("binary_sensor.motion", "on")
    engine.evaluate_all()
    now[0] = 1_800_000

    restarted = Engine(clock_fn=lambda: now[0])
    restarted.load_rules(rules)
    restarted.update_state("binary_sensor.motion", "on")
    restarted.import_lifecycle_records(engine.export_lifecycle_records())
    restarted.evaluate_all()
    restarted.update_state("binary_sensor.motion", "off")
    restarted.evaluate_all()

    status = restarted.list_rule_statuses()["adaptive"]["hold_after"]
    assert status["active_for_ms"] == 1_800_000
    assert status["tier"]["index"] == 1


def test_target_policy_is_replaced_independently_of_rule_lifecycle_identity() -> None:
    engine = Engine(clock_fn=lambda: 0)
    original = Rule(
        id="door",
        when="true",
        target="lock.front",
        set={"state": "locked"},
    )
    engine.load_rules([original], target_policies={"lock.front": TargetPolicy(max_retries=1)})
    engine.evaluate_all()
    assert engine.active_intent_count() == 1

    changed = Rule(
        id="door",
        when="true",
        target="lock.front",
        set={"state": "locked"},
    )
    engine.load_rules([changed], target_policies={"lock.front": TargetPolicy(max_retries=2)})

    assert engine.active_intent_count() == 0
    assert engine.target_policy("lock.front").max_retries == 2


def test_reload_fingerprint_ignores_source_location_and_preserves_runtime_memory() -> None:
    engine = Engine()
    original = Rule(
        id="stable",
        when="input_boolean.ready == 'on'",
        target="light.desk",
        set={"state": "on"},
        source_file=Path("old.yaml"),
        source_line=1,
    )
    engine.load_rules([original])
    engine._condition_true_since["stable"] = 10
    engine._active_effect_rule_ids.add("stable")
    engine._generated_fields[("stable", "brightness_pct")] = object()

    engine.load_rules([replace(original, source_file=Path("moved.yaml"), source_line=20)])

    assert engine._condition_true_since == {"stable": 10}
    assert engine._active_effect_rule_ids == {"stable"}
    assert set(engine._generated_fields) == {("stable", "brightness_pct")}


def test_reload_semantic_change_resets_rule_runtime_memory() -> None:
    engine = Engine()
    original = Rule(
        id="changed",
        when="input_boolean.ready == 'on'",
        target="light.desk",
        set={"state": "on"},
    )
    engine.load_rules([original])
    engine._condition_true_since["changed"] = 10
    engine._active_effect_rule_ids.add("changed")
    engine._generated_fields[("changed", "brightness_pct")] = object()

    engine.load_rules([replace(original, set={"state": "off"})])

    assert engine._condition_true_since == {}
    assert engine._active_effect_rule_ids == set()
    assert engine._generated_fields == {}


def _rule(id_: str, when: str, target: str = "light.x", **kwargs: Any) -> Rule:
    """Helper to build a minimal Rule for tests."""
    return Rule(
        id=id_,
        when=when,
        target=target,
        **kwargs,
    )


# ── State management ─────────────────────────────────────────────────


class TestStateManagement:
    def test_initial_state_empty(self) -> None:
        engine = Engine()
        assert engine.state == {}

    def test_update_state_stores_value(self) -> None:
        engine = Engine()
        engine.update_state("sensor.x", "on")
        assert engine.state["sensor.x.state"] == "on"

    def test_update_state_with_explicit_field(self) -> None:
        engine = Engine()
        engine.update_state("light.x", {"brightness": 200}, field="attributes")
        assert engine.state["light.x.attributes"] == {"brightness": 200}

    def test_state_change_emits_event(self) -> None:
        engine = Engine()
        events: list[tuple[str, Any]] = []
        engine.on_state_change(lambda entity, value: events.append((entity, value)))
        engine.update_state("sensor.x", "on")
        assert events == [("sensor.x", "on")]

    def test_remove_state_removes_all_entity_fields(self) -> None:
        engine = Engine()
        events: list[tuple[str, Any]] = []
        engine.on_state_change(lambda entity, value: events.append((entity, value)))
        engine.update_state("sensor.x", "on")
        engine.update_state("sensor.x", 21, field="temperature")
        engine.update_state("sensor.xy", "on")

        engine.remove_state("sensor.x")

        assert engine.state == {"sensor.xy.state": "on"}
        assert events[-1] == ("sensor.x", None)


# ── Rule evaluation ──────────────────────────────────────────────────


class TestRuleEvaluation:
    def test_authored_rule_statuses_group_expanded_multi_target_rules(self) -> None:
        from intentional.yaml_loader import load_rules_from_string

        engine = Engine(clock_fn=lambda: 1000)
        engine.load_rules(
            load_rules_from_string("""
- id: living-room-presence
  observe:
    binary_sensor.living_room_presence: on
  intent:
    light.sofa:
      state: on
    light.table:
      state: on
""")
        )
        engine.update_state("binary_sensor.living_room_presence", "on")
        engine.evaluate_all()

        statuses = engine.list_authored_rule_statuses()

        assert set(statuses) == {"living-room-presence"}
        assert statuses["living-room-presence"]["active"] is True
        assert statuses["living-room-presence"]["active_intent_count"] == 2
        assert statuses["living-room-presence"]["targets"] == [
            "light.sofa",
            "light.table",
        ]

    def test_world_model_separates_authored_and_active_rules(self) -> None:
        from intentional.yaml_loader import load_rules_from_string

        engine = Engine(clock_fn=lambda: 1000)
        engine.load_rules(
            load_rules_from_string("""
- id: living-room-presence
  observe:
    binary_sensor.living_room_presence: on
  intent:
    light.sofa:
      state: on
""")
        )

        world = engine.world_model()

        assert world["authored_rules"][0]["rule_id"] == "living-room-presence"
        assert world["active_rules"] == []

    def test_target_default_resolves_when_no_stronger_intent_is_active(self) -> None:
        from intentional.yaml_loader import load_rules_from_string

        engine = Engine(clock_fn=lambda: 1000)
        engine.load_rules(
            load_rules_from_string("""
targets:
  light.living_room:
    default:
      state: off
rules:
  - id: living-room-presence
    observe:
      binary_sensor.living_room_presence: on
    intent:
      light.living_room:
        state: on
        brightness_pct: 80
""")
        )
        engine.evaluate_all()

        assert engine.resolve("light.living_room").value == {"state": "off"}

        engine.update_state("binary_sensor.living_room_presence", "on")
        engine.evaluate_all()

        assert engine.resolve("light.living_room").value == {
            "state": "on",
            "brightness_pct": 80,
        }

    def test_rule_status_active_includes_lingering_intents(self) -> None:
        from intentional.yaml_loader import load_rules_from_string

        engine = Engine(clock_fn=lambda: 1000)
        engine.load_rules(
            load_rules_from_string("""
- id: living-room-presence
  observe:
    binary_sensor.living_room_presence: on
  intent:
    light.sofa:
      state: on
      linger: 2m
""")
        )
        engine.update_state("binary_sensor.living_room_presence", "on")
        engine.evaluate_all()
        engine.update_state("binary_sensor.living_room_presence", "off")
        engine.evaluate_all()

        status = engine.list_authored_rule_statuses()["living-room-presence"]

        assert status["active"] is True
        assert status["condition_firing"] is False
        assert status["active_intent_count"] == 1

    def test_global_disable_withdraws_active_rule_intents(self) -> None:
        from intentional.yaml_loader import load_rules_from_string

        engine = Engine(clock_fn=lambda: 1000)
        engine.load_rules(
            load_rules_from_string("""
- id: office-light
  observe:
    binary_sensor.office_occupancy: on
  intent:
    light.office:
      state: on
""")
        )
        engine.update_state("binary_sensor.office_occupancy", "on")
        engine.evaluate_all()
        assert engine.resolve("light.office") is not None

        engine.set_enabled(False)
        engine.evaluate_all()

        assert engine.is_enabled() is False
        assert engine.resolve("light.office") is None
        assert engine.active_intent_count() == 0

    def test_global_disable_state_survives_lifecycle_restore(self) -> None:
        engine = Engine(clock_fn=lambda: 1000)
        engine.set_enabled(False)
        records = engine.export_lifecycle_records()

        restored = Engine(clock_fn=lambda: 1000)
        restored.import_lifecycle_records(records)

        assert records["enabled"] is False
        assert restored.is_enabled() is False

    def test_generated_sample_field_resolves_to_one_allowed_value(self) -> None:
        from intentional.yaml_loader import load_rules_from_string

        engine = Engine(clock_fn=lambda: 1000)
        engine.load_rules(
            load_rules_from_string("""
- id: monitor-backlight-random
  observe:
    binary_sensor.office_occupancy: on
  intent:
    light.monitor_backlight:
      state: on
      rgb_color:
        generate:
          kind: sample
          from:
            - [255, 120, 40]
            - [120, 40, 255]
          every: 2m
""")
        )

        engine.update_state("binary_sensor.office_occupancy", "on")
        engine.evaluate_all()

        resolved = engine.resolve("light.monitor_backlight")

        assert resolved is not None
        assert resolved.value["state"] == "on"
        assert resolved.value["rgb_color"] in ([255, 120, 40], [120, 40, 255])

    def test_generated_sample_field_resamples_after_interval(self) -> None:
        from intentional.yaml_loader import load_rules_from_string

        engine = Engine(clock_fn=lambda: 1000)
        engine.load_rules(
            load_rules_from_string("""
- id: monitor-backlight-random
  observe:
    binary_sensor.office_occupancy: on
  intent:
    light.monitor_backlight:
      state: on
      rgb_color:
        generate:
          kind: sample
          from:
            - [255, 120, 40]
            - [120, 40, 255]
          every: 2m
""")
        )
        engine.update_state("binary_sensor.office_occupancy", "on")
        engine.evaluate_all()
        first = engine.resolve("light.monitor_backlight").value["rgb_color"]

        engine.advance_clock(119_000)
        engine.evaluate_all()
        before_due = engine.resolve("light.monitor_backlight").value["rgb_color"]
        engine.advance_clock(1_000)
        engine.evaluate_all()
        after_due = engine.resolve("light.monitor_backlight").value["rgb_color"]

        assert before_due == first
        assert after_due != first
        assert after_due in ([255, 120, 40], [120, 40, 255])

    def test_generated_sample_field_can_set_transition(self) -> None:
        from intentional.yaml_loader import load_rules_from_string

        engine = Engine(clock_fn=lambda: 1000)
        engine.load_rules(
            load_rules_from_string("""
- id: monitor-backlight-random
  observe:
    binary_sensor.office_occupancy: on
  intent:
    light.monitor_backlight:
      rgb_color:
        generate:
          kind: sample
          from:
            - [255, 120, 40]
            - [120, 40, 255]
          every: 2m
          transition: 7s
""")
        )

        engine.update_state("binary_sensor.office_occupancy", "on")
        engine.evaluate_all()
        resolved = engine.resolve("light.monitor_backlight")

        assert resolved.transition_ms == 7000

    def test_generated_sample_field_survives_lifecycle_restore(self) -> None:
        from intentional.yaml_loader import load_rules_from_string

        rules = load_rules_from_string("""
- id: monitor-backlight-random
  observe:
    binary_sensor.office_occupancy: on
  intent:
    light.monitor_backlight:
      rgb_color:
        generate:
          kind: sample
          from:
            - [255, 120, 40]
            - [120, 40, 255]
          every: 2m
""")
        engine = Engine(clock_fn=lambda: 1000)
        engine.load_rules(rules)
        engine.update_state("binary_sensor.office_occupancy", "on")
        engine.evaluate_all()
        first = engine.resolve("light.monitor_backlight").value["rgb_color"]

        records = engine.export_lifecycle_records()
        assert records["generated_fields"] == [
            {
                "rule_id": "monitor-backlight-random",
                "field": "rgb_color",
                "value": first,
                "next_due_ms": 121000,
                "transition_ms": None,
            }
        ]

        restored = Engine(clock_fn=lambda: 61_000)
        restored.load_rules(rules)
        restored.import_lifecycle_records(records)
        restored.update_state("binary_sensor.office_occupancy", "on")
        restored.evaluate_all()

        assert restored.resolve("light.monitor_backlight").value["rgb_color"] == first

    def test_intent_selector_expands_to_resolved_targets(self) -> None:
        from intentional.yaml_loader import load_rules_from_string

        def resolve_selector(selector):
            assert selector.domain == "light"
            assert selector.area == "living_room"
            return ["light.floor_lamp", "light.table_lamp"]

        engine = Engine(clock_fn=lambda: 1000, selector_resolver=resolve_selector)
        engine.load_rules(
            load_rules_from_string("""
- id: living-room-off
  intent:
    select:
      - domain: light
        area: living_room
        state: off
""")
        )

        engine.evaluate_all()

        assert engine.resolve("light.floor_lamp").value == {"state": "off"}
        assert engine.resolve("light.table_lamp").value == {"state": "off"}

    def test_active_intent_selector_refreshes_changed_targets(self) -> None:
        from intentional.yaml_loader import load_rules_from_string

        selected = ["light.floor_lamp"]
        engine = Engine(
            clock_fn=lambda: 1000,
            selector_resolver=lambda _selector: list(selected),
        )
        engine.load_rules(
            load_rules_from_string("""
- id: living-room-off
  intent:
    select:
      - domain: light
        state: off
""")
        )
        engine.evaluate_all()
        selected[:] = ["light.table_lamp"]

        engine.evaluate_all()

        assert engine.resolve("light.floor_lamp") is None
        assert engine.resolve("light.table_lamp").value == {"state": "off"}

    def test_overlapping_intent_selectors_spawn_one_intent_per_target(self) -> None:
        from intentional.yaml_loader import load_rules_from_string

        now = 1_000
        engine = Engine(
            clock_fn=lambda: now,
            selector_resolver=lambda _selector: ["light.floor_lamp"],
        )
        engine.load_rules(
            load_rules_from_string("""
- id: base-brightness
  intent:
    light.floor_lamp:
      brightness_pct: 100
- id: overlapping-dimmers
  intent:
    select:
      - domain: light
        area: living_room
        brightness_pct: {offset: 10, multiply: 0.5}
        ttl: 10s
      - domain: light
        label: dimmable
        brightness_pct: {offset: 10, multiply: 0.5}
        ttl: 10s
""")
        )

        engine.evaluate_all()

        intents = [
            intent
            for intent in engine.list_active_intents("light.floor_lamp")
            if intent.selector_generated
        ]
        assert len(intents) == 1
        assert intents[0].selector_generated is True
        assert intents[0].created_at_ms == now
        assert intents[0].offset == {"brightness_pct": 10}
        assert intents[0].multiply == {"brightness_pct": 0.5}
        assert engine.resolve("light.floor_lamp").value["brightness_pct"] == 55

        now = 2_000
        engine.evaluate_all()

        refreshed = [
            intent
            for intent in engine.list_active_intents("light.floor_lamp")
            if intent.selector_generated
        ]
        assert len(refreshed) == 1
        assert refreshed[0].created_at_ms == 1_000
        records = engine.export_lifecycle_records()
        matching_records = [
            record
            for record in records["intents"]
            if record["target"] == "light.floor_lamp" and record["selector_generated"]
        ]
        assert len(matching_records) == 1
        assert matching_records[0]["selector_generated"] is True

    def test_selector_refresh_preserves_explicit_target_intent_and_lifecycle(self) -> None:
        from intentional.yaml_loader import load_rules_from_string

        selected = ["light.floor_lamp"]
        now = 1_000
        engine = Engine(
            clock_fn=lambda: now,
            selector_resolver=lambda _selector: list(selected),
        )
        engine.load_rules(
            load_rules_from_string("""
- id: living-room-off
  while:
    input_boolean.active: on
  intent:
    select:
      - domain: light
        state: off
        ttl: 10s
    light.table_lamp:
      state: off
      ttl: 10s
""")
        )
        engine.update_state("input_boolean.active", "on")
        engine.evaluate_all()
        targets = ("light.table_lamp", "light.floor_lamp", "light.ceiling")
        original = {
            intent.target: intent.created_at_ms
            for target in targets
            for intent in engine.list_active_intents(target)
        }
        selected[:] = ["light.floor_lamp", "light.ceiling"]
        now = 2_000

        engine.evaluate_all()

        active = {
            intent.target: intent
            for target in targets
            for intent in engine.list_active_intents(target)
        }
        assert set(active) == {"light.table_lamp", "light.floor_lamp", "light.ceiling"}
        assert active["light.table_lamp"].created_at_ms == original["light.table_lamp"]
        assert active["light.floor_lamp"].created_at_ms == original["light.floor_lamp"]
        assert active["light.ceiling"].created_at_ms == now
        records = engine.export_lifecycle_records()
        generated = {
            record["target"]: record["selector_generated"] for record in records["intents"]
        }
        assert generated == {
            "light.table_lamp": False,
            "light.floor_lamp": True,
            "light.ceiling": True,
        }

    def test_observe_select_any_fires_when_selected_entity_matches(self) -> None:
        from intentional.yaml_loader import load_rules_from_string

        def resolve_selector(selector):
            assert selector.domain == "binary_sensor"
            assert selector.label == "motion"
            return ["binary_sensor.kitchen_motion", "binary_sensor.hall_motion"]

        engine = Engine(clock_fn=lambda: 1000, selector_resolver=resolve_selector)
        engine.load_rules(
            load_rules_from_string("""
- id: selected-motion
  observe:
    select:
      mode: any
      entities:
        - domain: binary_sensor
          label: motion
          state: on
  intent:
    light.hallway:
      state: on
""")
        )

        engine.update_state("binary_sensor.kitchen_motion", "off")
        engine.update_state("binary_sensor.hall_motion", "on")
        engine.evaluate_all()

        assert engine.resolve("light.hallway").value == {"state": "on"}

    def test_observe_select_all_requires_every_selected_entity_to_match(self) -> None:
        from intentional.yaml_loader import load_rules_from_string

        engine = Engine(
            clock_fn=lambda: 1000,
            selector_resolver=lambda _selector: ["binary_sensor.a", "binary_sensor.b"],
        )
        engine.load_rules(
            load_rules_from_string("""
- id: all-motion
  observe:
    select:
      mode: all
      entities:
        - domain: binary_sensor
          label: motion
          state: on
  intent:
    light.hallway:
      state: on
""")
        )

        engine.update_state("binary_sensor.a", "on")
        engine.update_state("binary_sensor.b", "off")
        engine.evaluate_all()
        assert engine.resolve("light.hallway") is None

        engine.update_state("binary_sensor.b", "on")
        engine.evaluate_all()
        assert engine.resolve("light.hallway").value == {"state": "on"}

    def test_observe_select_none_fires_when_no_selected_entity_matches(self) -> None:
        from intentional.yaml_loader import load_rules_from_string

        engine = Engine(
            clock_fn=lambda: 1000,
            selector_resolver=lambda _selector: ["binary_sensor.a", "binary_sensor.b"],
        )
        engine.load_rules(
            load_rules_from_string("""
- id: no-motion
  observe:
    select:
      mode: none
      entities:
        - domain: binary_sensor
          label: motion
          state: on
  intent:
    light.hallway:
      state: off
""")
        )

        engine.update_state("binary_sensor.a", "off")
        engine.update_state("binary_sensor.b", "off")
        engine.evaluate_all()
        assert engine.resolve("light.hallway").value == {"state": "off"}

        engine.update_state("binary_sensor.b", "on")
        engine.evaluate_all()
        assert engine.resolve("light.hallway") is None

    def test_world_model_includes_observe_selector_diagnostics(self) -> None:
        from intentional.yaml_loader import load_rules_from_string

        engine = Engine(
            clock_fn=lambda: 1000,
            selector_resolver=lambda _selector: ["binary_sensor.a", "binary_sensor.b"],
        )
        engine.load_rules(
            load_rules_from_string("""
- id: selected-motion
  observe:
    select:
      mode: any
      entities:
        - domain: binary_sensor
          label: motion
          state: on
  intent:
    light.hallway:
      state: on
""")
        )
        engine.update_state("binary_sensor.a", "off")
        engine.update_state("binary_sensor.b", "on")
        engine.evaluate_all()

        diagnostics = engine.world_model()["selector_diagnostics"]

        assert diagnostics == [
            {
                "rule_id": "selected-motion",
                "phase": "while",
                "mode": "any",
                "selector": {
                    "domain": "binary_sensor",
                    "area": None,
                    "label": "motion",
                    "exclude": [],
                },
                "matches": [
                    {
                        "target": "binary_sensor.a",
                        "matched": False,
                        "actual": "off",
                        "expected": "on",
                    },
                    {
                        "target": "binary_sensor.b",
                        "matched": True,
                        "actual": "on",
                        "expected": "on",
                    },
                ],
            }
        ]

    def test_world_model_labels_semantic_hold_selector_phases(self) -> None:
        from intentional.yaml_loader import load_rules_from_string

        engine = Engine(selector_resolver=lambda selector: [f"binary_sensor.{selector.purpose}"])
        engine.load_rules(load_rules_from_string("""
- id: held-light
  while: {motion: {detected: {area: office}}}
  hold:
    while: {occupancy: {occupied: {area: office}}}
    until: {door: {open: {area: office}}}
  intent: {light.office: {state: on}}
"""))

        diagnostics = engine.world_model()["selector_diagnostics"]

        assert [(item["selector"]["purpose"], item["phase"]) for item in diagnostics] == [
            ("motion", "while"),
            ("occupancy", "hold.while"),
            ("door", "hold.until"),
        ]

    def test_semantic_and_legacy_observation_selectors_are_anded(self) -> None:
        from intentional.records import ObservationGroup, ObserveSelector
        from intentional.rule_model import Rule
        from intentional.selectors import observe_selectors_fire

        semantic = ObserveSelector(purpose="motion", value="on")
        legacy = ObserveSelector(label="doors", value="on")
        state = {
            "binary_sensor.motion.state": "on",
            "binary_sensor.door_a.state": "off",
            "binary_sensor.door_b.state": "off",
        }
        memberships = {
            "motion": ["binary_sensor.motion"],
            "doors": ["binary_sensor.door_a", "binary_sensor.door_b"],
        }
        def resolver(selected):
            return memberships[selected.purpose or selected.label]

        for mode, expected in (("any", False), ("all", False), ("none", True)):
            authored = Rule(
                id=f"mixed-{mode}",
                when="true",
                observe_selectors=(legacy,),
                observe_selector_mode=mode,
                observation_groups=(ObservationGroup(semantic),),
            )
            assert observe_selectors_fire(authored, state, resolver) is expected

        state["binary_sensor.motion.state"] = "off"
        none_rule = Rule(
            id="mixed-none",
            when="true",
            observe_selectors=(legacy,),
            observe_selector_mode="none",
            observation_groups=(ObservationGroup(semantic),),
        )
        assert not observe_selectors_fire(none_rule, state, resolver)

    def test_level_intent_lingers_after_observation_stops(self) -> None:
        from intentional.yaml_loader import load_rules_from_string

        engine = Engine(clock_fn=lambda: 1000)
        engine.load_rules(
            load_rules_from_string("""
- id: tv-linger
  observe:
    media_player.tv: on
  intent:
    light.living_room:
      linger: 2m
      brightness_pct: 30
""")
        )

        engine.update_state("media_player.tv", "on")
        engine.evaluate_all()
        assert engine.resolve("light.living_room").value == {"brightness_pct": 30}

        engine.update_state("media_player.tv", "off")
        engine.evaluate_all()
        assert engine.resolve("light.living_room").value == {"brightness_pct": 30}

        engine.advance_clock(120_000)
        engine.evaluate_all()
        assert engine.resolve("light.living_room") is None

    def test_hold_while_retains_intent_before_linger_starts(self) -> None:
        from intentional.yaml_loader import load_rules_from_string

        engine = Engine(clock_fn=lambda: 1000)
        engine.load_rules(
            load_rules_from_string("""
- id: evening-occupied
  while:
    binary_sensor.living_room_presence: on
    input_boolean.evening_mode: on
  hold:
    while:
      binary_sensor.living_room_presence: on
    after: 1m
  intent:
    light.living_room:
      brightness_pct: 70
""")
        )

        engine.update_state("binary_sensor.living_room_presence", "on")
        engine.update_state("input_boolean.evening_mode", "on")
        engine.evaluate_all()
        assert engine.resolve("light.living_room").value == {"brightness_pct": 70}

        engine.update_state("input_boolean.evening_mode", "off")
        engine.evaluate_all()
        assert engine.resolve("light.living_room").value == {"brightness_pct": 70}

        engine.advance_clock(5 * 60_000)
        engine.evaluate_all()
        assert engine.resolve("light.living_room").value == {"brightness_pct": 70}

        engine.update_state("binary_sensor.living_room_presence", "off")
        engine.evaluate_all()
        assert engine.resolve("light.living_room").value == {"brightness_pct": 70}

        engine.advance_clock(60_000)
        engine.evaluate_all()
        assert engine.resolve("light.living_room") is None

    def test_hold_until_requires_stable_release_condition(self) -> None:
        from intentional.yaml_loader import load_rules_from_string

        engine = Engine(clock_fn=lambda: 1000)
        engine.load_rules(
            load_rules_from_string("""
- id: living-room-dark
  group: living-room-lighting
  profile: stable-presence
  while:
    binary_sensor.living_room_presence: on
  hold:
    until:
      binary_sensor.living_room_presence: off
      for: 15m
  intent:
    light.living_room:
      state: on
""")
        )

        engine.update_state("binary_sensor.living_room_presence", "on")
        engine.evaluate_all()
        assert engine.resolve("light.living_room").value == {"state": "on"}

        engine.update_state("binary_sensor.living_room_presence", "off")
        engine.evaluate_all()
        engine.advance_clock(14 * 60_000)
        engine.evaluate_all()
        assert engine.resolve("light.living_room").value == {"state": "on"}

        engine.update_state("binary_sensor.living_room_presence", "on")
        engine.evaluate_all()
        engine.update_state("binary_sensor.living_room_presence", "off")
        engine.evaluate_all()
        engine.advance_clock(14 * 60_000)
        engine.evaluate_all()
        assert engine.resolve("light.living_room").value == {"state": "on"}

        engine.advance_clock(60_000)
        engine.evaluate_all()
        assert engine.resolve("light.living_room") is None

    def test_rule_status_reports_lifecycle_phase_and_timings(self) -> None:
        from intentional.yaml_loader import load_rules_from_string

        engine = Engine(clock_fn=lambda: 1000)
        engine.load_rules(
            load_rules_from_string("""
- id: living-room-dark
  group: living-room-lighting
  profile: stable-presence
  while:
    binary_sensor.living_room_presence: on
  hold:
    until:
      binary_sensor.living_room_presence: off
      for: 15m
  intent:
    light.living_room:
      state: on
""")
        )

        assert engine.list_rule_statuses()["living-room-dark"]["phase"] == "idle"

        engine.update_state("binary_sensor.living_room_presence", "on")
        engine.evaluate_all()
        engine.advance_clock(120_000)
        active = engine.list_rule_statuses()["living-room-dark"]
        assert active["group"] == "living-room-lighting"
        assert active["profile"] == "stable-presence"
        assert active["phase"] == "active"
        assert active["active_for_ms"] == 120_000
        assert active["condition_active_for_ms"] == 120_000
        assert active["held_for_ms"] is None

        engine.update_state("binary_sensor.living_room_presence", "off")
        engine.evaluate_all()
        engine.advance_clock(60_000)
        held = engine.list_rule_statuses()["living-room-dark"]
        assert held["phase"] == "held"
        assert held["active_for_ms"] == 180_000
        assert held["condition_active_for_ms"] is None
        assert held["held_for_ms"] == 60_000

    def test_lifecycle_records_restore_edge_ttl_intent(self) -> None:
        from intentional.yaml_loader import load_rules_from_string

        rules = load_rules_from_string("""
- id: door-pulse
  observe:
    changed:
      binary_sensor.front_door:
        to: on
  intent:
    light.entry:
      ttl: 5s
      state: on
""")
        engine = Engine(clock_fn=lambda: 1000)
        engine.load_rules(rules)
        engine.update_state("binary_sensor.front_door", True, field="changed")
        engine.update_state("binary_sensor.front_door", "on")
        engine.evaluate_all()

        records = engine.export_lifecycle_records()
        restored = Engine(clock_fn=lambda: 3000)
        restored.load_rules(rules)
        restored.import_lifecycle_records(records)

        assert restored.resolve("light.entry").value == {"state": "on"}

        expired = Engine(clock_fn=lambda: 6000)
        expired.load_rules(rules)
        expired.import_lifecycle_records(records)
        assert expired.resolve("light.entry") is None

    def test_lifecycle_records_restore_manual_override(self) -> None:
        engine = Engine(clock_fn=lambda: 1000)
        engine.emit_user_intent(
            "light.desk",
            {"brightness_pct": 25},
            ttl_ms=5_000,
            reason="Manual HA state change",
        )

        records = engine.export_lifecycle_records()
        restored = Engine(clock_fn=lambda: 3000)
        restored.import_lifecycle_records(records)

        assert restored.resolve("light.desk").value == {"brightness_pct": 25}
        assert restored.list_active_intents("light.desk")[0].authority is Authority.USER

        expired = Engine(clock_fn=lambda: 6000)
        expired.import_lifecycle_records(records)
        assert expired.resolve("light.desk") is None

    def test_lifecycle_records_restore_lingering_intent(self) -> None:
        from intentional.yaml_loader import load_rules_from_string

        rules = load_rules_from_string("""
- id: tv-linger
  observe:
    media_player.tv: on
  intent:
    light.living_room:
      linger: 2m
      brightness_pct: 30
""")
        engine = Engine(clock_fn=lambda: 1000)
        engine.load_rules(rules)
        engine.update_state("media_player.tv", "on")
        engine.evaluate_all()
        engine.update_state("media_player.tv", "off")
        engine.evaluate_all()

        records = engine.export_lifecycle_records()
        restored = Engine(clock_fn=lambda: 61_000)
        restored.load_rules(rules)
        restored.import_lifecycle_records(records)
        assert restored.resolve("light.living_room").value == {"brightness_pct": 30}

        expired = Engine(clock_fn=lambda: 121_000)
        expired.load_rules(rules)
        expired.import_lifecycle_records(records)
        assert expired.resolve("light.living_room") is None

    def test_effect_emits_once_per_observation_activation(self) -> None:
        from intentional.yaml_loader import load_rules_from_string

        engine = Engine(clock_fn=lambda: 1000)
        engine.load_rules(
            load_rules_from_string("""
- id: door-left-open-notify
  observe:
    binary_sensor.front_door: on
  effect:
    service: notify.mobile_app_phone
    data:
      message: Door open
""")
        )

        engine.update_state("binary_sensor.front_door", "on")
        engine.evaluate_all()
        first = engine.due_effects()
        assert [effect.service for effect in first] == ["mobile_app_phone"]

        engine.evaluate_all()
        assert len(engine.due_effects()) == 1
        assert engine.acknowledge_effect(first[0].activation_id, first[0].effect_index)

        engine.update_state("binary_sensor.front_door", "off")
        engine.evaluate_all()
        engine.update_state("binary_sensor.front_door", "on")
        engine.evaluate_all()
        assert [effect.service for effect in engine.due_effects()] == ["mobile_app_phone"]

    def test_lifecycle_records_restore_active_effect_dedupe_state(self) -> None:
        from intentional.yaml_loader import load_rules_from_string

        rules = load_rules_from_string("""
- id: door-left-open-notify
  observe:
    binary_sensor.front_door: on
  effect:
    service: notify.mobile_app_phone
    data:
      message: Door open
""")
        engine = Engine(clock_fn=lambda: 1000)
        engine.load_rules(rules)
        engine.update_state("binary_sensor.front_door", "on")
        engine.evaluate_all()
        pending = engine.due_effects()
        assert pending
        engine.acknowledge_effect(pending[0].activation_id, pending[0].effect_index)

        restored = Engine(clock_fn=lambda: 2000)
        restored.load_rules(rules)
        restored.import_lifecycle_records(engine.export_lifecycle_records())
        restored.update_state("binary_sensor.front_door", "on")
        restored.evaluate_all()
        assert restored.due_effects() == []

        restored.update_state("binary_sensor.front_door", "off")
        restored.evaluate_all()
        restored.update_state("binary_sensor.front_door", "on")
        restored.evaluate_all()
        assert [effect.service for effect in restored.due_effects()] == ["mobile_app_phone"]

    def test_effect_outbox_restores_crash_boundaries_and_retries(self) -> None:
        from intentional.yaml_loader import load_rules_from_string

        now = [1_000]
        rules = load_rules_from_string("""
- id: notify
  observe: {binary_sensor.door: on}
  effect:
    - service: notify.first
      data: {message: first}
    - service: notify.second
      data: {message: second}
""")
        engine = Engine(clock_fn=lambda: now[0])
        engine.load_rules(rules)
        engine.update_state("binary_sensor.door", "on")
        engine.evaluate_all()

        queued = engine.due_effects()
        assert len(queued) == 2
        assert len({record.activation_id for record in queued}) == 1
        assert [record.effect_index for record in queued] == [0, 1]

        attempted = engine.begin_effect_attempt(queued[0].activation_id, 0)
        assert attempted is not None
        assert attempted.attempts == 1
        crashed = Engine(clock_fn=lambda: now[0])
        crashed.load_rules(rules)
        crashed.import_lifecycle_records(engine.export_lifecycle_records())
        assert [record.effect_index for record in crashed.due_effects()] == [1]

        now[0] += 1_000
        assert [record.effect_index for record in crashed.due_effects()] == [0, 1]
        retried = crashed.begin_effect_attempt(queued[0].activation_id, 0)
        assert retried is not None
        assert retried.attempts == 2
        assert retried.next_retry_ms == now[0] + 2_000

    def test_queued_effect_survives_rule_deactivation_and_definition_change(self) -> None:
        from intentional.yaml_loader import load_rules_from_string

        engine = Engine(clock_fn=lambda: 1_000)
        original = load_rules_from_string("""
- id: notify
  observe: {binary_sensor.door: on}
  effect: {service: notify.phone, data: {message: old}}
""")
        engine.load_rules(original)
        engine.update_state("binary_sensor.door", "on")
        engine.evaluate_all()
        old = engine.due_effects()[0]

        engine.update_state("binary_sensor.door", "off")
        engine.evaluate_all()
        changed = load_rules_from_string("""
- id: notify
  observe: {binary_sensor.door: on}
  effect: {service: notify.phone, data: {message: new}}
""")
        engine.load_rules(changed)
        assert engine.due_effects() == [old]

        engine.update_state("binary_sensor.door", "on")
        engine.evaluate_all()
        queued = engine.due_effects()
        assert [record.data["message"] for record in queued] == ["old", "new"]
        assert queued[0].rule_fingerprint != queued[1].rule_fingerprint

    def test_acknowledged_effects_compact_and_restart_keeps_activation_suppressed(self) -> None:
        from intentional.yaml_loader import load_rules_from_string

        rules = load_rules_from_string("""
- id: notify
  observe: {binary_sensor.door: on}
  effect: {service: notify.phone}
""")
        engine = Engine(clock_fn=lambda: 1_000)
        engine.load_rules(rules)
        for _ in range(250):
            engine.update_state("binary_sensor.door", "on")
            engine.evaluate_all()
            record = engine.due_effects()[0]
            assert engine.acknowledge_effect(record.activation_id, record.effect_index)
            engine.update_state("binary_sensor.door", "off")
            engine.evaluate_all()

        engine.update_state("binary_sensor.door", "on")
        engine.evaluate_all()
        record = engine.due_effects()[0]
        engine.acknowledge_effect(record.activation_id, record.effect_index)
        assert engine.list_effect_outbox() == []

        restarted = Engine(clock_fn=lambda: 1_000)
        restarted.load_rules(rules)
        restarted.import_lifecycle_records(engine.export_lifecycle_records())
        restarted.update_state("binary_sensor.door", "on")
        restarted.evaluate_all()
        assert restarted.due_effects() == []

    def test_effect_failures_dead_letter_at_bounded_retry_ceiling(self) -> None:
        from intentional.engine import EFFECT_MAX_ATTEMPTS
        from intentional.yaml_loader import load_rules_from_string

        now = [0]
        engine = Engine(clock_fn=lambda: now[0])
        engine.load_rules(
            load_rules_from_string("""
- id: notify
  observe: {binary_sensor.door: on}
  effect: {service: notify.phone}
""")
        )
        engine.update_state("binary_sensor.door", "on")
        engine.evaluate_all()
        record = engine.due_effects()[0]
        for attempt in range(EFFECT_MAX_ATTEMPTS):
            attempted = engine.begin_effect_attempt(record.activation_id, record.effect_index)
            assert attempted is not None
            terminal = engine.fail_effect(record.activation_id, record.effect_index, "x" * 1000)
            assert terminal is (attempt == EFFECT_MAX_ATTEMPTS - 1)
            now[0] = attempted.next_retry_ms

        assert engine.due_effects() == []
        dead = engine.list_effect_outbox()[0]
        assert dead.dead_lettered_at_ms is not None
        assert len(dead.last_error or "") == 500

    def test_world_model_exposes_desired_records_and_conditions(self) -> None:
        engine = Engine(clock_fn=lambda: 1000)
        engine.load_rules(
            [
                _rule(
                    "desk-on",
                    'input_boolean.work == "on"',
                    target="light.desk",
                    set={"state": "on", "brightness_pct": 60},
                    reason="Work mode",
                )
            ]
        )
        engine.update_state("input_boolean.work", "on")
        engine.evaluate_all()

        world = engine.world_model()

        assert world["dsl_version"] == "vnext-draft"
        assert world["desired_records"] == [
            {
                "target": "light.desk",
                "desired": {"state": "on", "brightness_pct": 60},
                "rule_id": "desk-on",
                "reason": "Work mode",
                "conditions": [{"type": "DesiredResolved", "status": "true"}],
            }
        ]
        assert world["lifecycle"] == engine.export_lifecycle_records()

    def test_templated_intent_value_renders_from_engine_state(self) -> None:
        from intentional.yaml_loader import load_rules_from_string

        engine = Engine(clock_fn=lambda: 1000)
        engine.load_rules(
            load_rules_from_string("""
- id: adaptive-brightness
  observe:
    input_boolean.work: on
  intent:
    light.desk:
      brightness_pct: "{{ states('input_number.target_brightness') | int }}"
""")
        )
        engine.update_state("input_boolean.work", "on")
        engine.update_state("input_number.target_brightness", "42")
        engine.evaluate_all()

        assert engine.resolve("light.desk").value == {"brightness_pct": 42}

    def test_templated_effect_data_renders_when_effect_is_emitted(self) -> None:
        from intentional.yaml_loader import load_rules_from_string

        engine = Engine(clock_fn=lambda: 1000)
        engine.load_rules(
            load_rules_from_string("""
- id: notify-temp
  observe:
    binary_sensor.temp_alarm: on
  effect:
    service: notify.mobile_app_phone
    data:
      message: "Temp is {{ states('sensor.room_temp') }}"
""")
        )
        engine.update_state("binary_sensor.temp_alarm", "on")
        engine.update_state("sensor.room_temp", "24")
        engine.evaluate_all()

        effects = engine.due_effects()
        assert effects[0].data == {"message": "Temp is 24"}

    def test_evaluate_emits_intent_when_trigger_fires(self) -> None:
        engine = Engine()
        rule = _rule("r1", 'sensor.x.state == "on"')
        engine.load_rules([rule])
        engine.update_state("sensor.x", "on")
        engine.evaluate_all()
        intents = engine.list_active_intents("light.x")
        assert len(intents) == 1
        assert intents[0].rule_id == "r1"
        assert intents[0].authority is Authority.AUTOMATION

    def test_evaluate_does_not_emit_when_trigger_does_not_fire(self) -> None:
        engine = Engine()
        rule = _rule("r1", 'sensor.x.state == "on"')
        engine.load_rules([rule])
        engine.update_state("sensor.x", "off")
        engine.evaluate_all()
        assert engine.list_active_intents("light.x") == []

    def test_evaluate_re_emits_when_state_changes(self) -> None:
        engine = Engine()
        rule = _rule("r1", 'sensor.x.state == "on"')
        engine.load_rules([rule])
        engine.update_state("sensor.x", "on")
        engine.evaluate_all()
        assert len(engine.list_active_intents("light.x")) == 1

        engine.update_state("sensor.x", "off")
        engine.evaluate_all()
        assert len(engine.list_active_intents("light.x")) == 0

    def test_evaluate_handles_complex_when_expression(self) -> None:
        engine = Engine()
        rule = _rule(
            "r1",
            'sensor.x.state == "on" and sensor.y.state == "ready"',
        )
        engine.load_rules([rule])
        engine.update_state("sensor.x", "on")
        engine.update_state("sensor.y", "ready")
        engine.evaluate_all()
        assert len(engine.list_active_intents("light.x")) == 1

        engine.update_state("sensor.y", "not_ready")
        engine.evaluate_all()
        assert len(engine.list_active_intents("light.x")) == 0

    def test_evaluate_with_time_of_day(self) -> None:
        engine = Engine()
        rule = _rule("r1", 'time_of_day == "night"')
        engine.load_rules([rule])
        engine.set_time_of_day("night")
        engine.evaluate_all()
        assert len(engine.list_active_intents("light.x")) == 1
        engine.set_time_of_day("day")
        engine.evaluate_all()
        assert len(engine.list_active_intents("light.x")) == 0

    def test_for_delays_rule_until_condition_stays_true(self) -> None:
        engine = Engine(clock_fn=lambda: 0)
        rule = _rule(
            "motion-held",
            'binary_sensor.motion == "on"',
            for_ms=5_000,
        )
        engine.load_rules([rule])
        engine.update_state("binary_sensor.motion", "on")

        engine.evaluate_all()
        assert engine.list_active_intents("light.x") == []

        engine.advance_clock(4_999)
        engine.evaluate_all()
        assert engine.list_active_intents("light.x") == []

        engine.advance_clock(1)
        engine.evaluate_all()
        intents = engine.list_active_intents("light.x")
        assert len(intents) == 1
        assert intents[0].rule_id == "motion-held"

    def test_for_uses_dynamic_entity_duration(self) -> None:
        engine = Engine(clock_fn=lambda: 0)
        rule = _rule(
            "motion-held",
            'binary_sensor.motion == "on"',
            for_ms=120_000,
            for_entity="input_number.motion_off_delay",
            for_entity_unit="s",
        )
        engine.load_rules([rule])
        engine.update_state("input_number.motion_off_delay", "10")
        engine.update_state("binary_sensor.motion", "on")

        engine.evaluate_all()
        engine.advance_clock(9_999)
        engine.evaluate_all()
        assert engine.list_active_intents("light.x") == []

        engine.advance_clock(1)
        engine.evaluate_all()
        assert len(engine.list_active_intents("light.x")) == 1

    def test_for_uses_default_when_dynamic_entity_is_unavailable(self) -> None:
        engine = Engine(clock_fn=lambda: 0)
        rule = _rule(
            "motion-held",
            'binary_sensor.motion == "on"',
            for_ms=5_000,
            for_entity="input_number.motion_off_delay",
            for_entity_unit="s",
        )
        engine.load_rules([rule])
        engine.update_state("input_number.motion_off_delay", "unavailable")
        engine.update_state("binary_sensor.motion", "on")

        engine.evaluate_all()
        engine.advance_clock(4_999)
        engine.evaluate_all()
        assert engine.list_active_intents("light.x") == []

        engine.advance_clock(1)
        engine.evaluate_all()
        assert len(engine.list_active_intents("light.x")) == 1

    def test_for_resets_when_condition_goes_false(self) -> None:
        engine = Engine(clock_fn=lambda: 0)
        rule = _rule("motion-held", 'binary_sensor.motion == "on"', for_ms=5_000)
        engine.load_rules([rule])
        engine.update_state("binary_sensor.motion", "on")
        engine.evaluate_all()
        engine.advance_clock(4_000)
        engine.evaluate_all()

        engine.update_state("binary_sensor.motion", "off")
        engine.evaluate_all()
        engine.update_state("binary_sensor.motion", "on")
        engine.evaluate_all()
        engine.advance_clock(4_999)
        engine.evaluate_all()

        assert engine.list_active_intents("light.x") == []

        engine.advance_clock(1)
        engine.evaluate_all()
        assert len(engine.list_active_intents("light.x")) == 1

    def test_blocks_suppresses_firing_rule(self) -> None:
        engine = Engine()
        engine.load_rules(
            [
                _rule(
                    "movie-mode",
                    'input_boolean.movie == "on"',
                    target="light.room",
                    set={"brightness_pct": 20},
                    blocks=("ambient",),
                ),
                _rule(
                    "ambient",
                    'sensor.dark == "on"',
                    target="light.room",
                    set={"brightness_pct": 80},
                ),
            ]
        )
        engine.update_state("input_boolean.movie", "on")
        engine.update_state("sensor.dark", "on")

        engine.evaluate_all()

        active_rule_ids = {intent.rule_id for intent in engine.list_active_intents("light.room")}
        assert active_rule_ids == {"movie-mode"}
        resolved = engine.resolve("light.room")
        assert resolved is not None
        assert resolved.value == {"brightness_pct": 20}

    def test_blocks_authored_id_suppresses_all_expansions_without_prefix_collision(self) -> None:
        from intentional.yaml_loader import load_rules_from_string

        engine = Engine()
        engine.load_rules(
            load_rules_from_string("""
- id: movie-mode
  while:
    input_boolean.active: on
  intent:
    suppress:
      rules: [ambient]
    light.movie:
      state: on
- id: ambient
  while:
    input_boolean.active: on
  intent:
    light.sofa:
      state: on
    light.table:
      state: on
- id: ambient-extra
  while:
    input_boolean.active: on
  intent:
    light.extra:
      state: on
- id: ambient:manual
  while:
    input_boolean.active: on
  intent:
    light.manual:
      state: on
""")
        )
        engine.update_state("input_boolean.active", "on")

        engine.evaluate_all()

        assert engine.resolve("light.sofa") is None
        assert engine.resolve("light.table") is None
        assert engine.resolve("light.extra") is not None
        assert engine.resolve("light.manual") is not None

    def test_blocks_withdraws_previously_active_intent(self) -> None:
        engine = Engine()
        engine.load_rules(
            [
                _rule(
                    "movie-mode",
                    'input_boolean.movie == "on"',
                    target="light.room",
                    set={"brightness_pct": 20},
                    blocks=("ambient",),
                ),
                _rule(
                    "ambient",
                    'sensor.dark == "on"',
                    target="light.room",
                    set={"brightness_pct": 80},
                ),
            ]
        )
        engine.update_state("sensor.dark", "on")
        engine.evaluate_all()
        assert {intent.rule_id for intent in engine.list_active_intents("light.room")} == {
            "ambient"
        }

        engine.update_state("input_boolean.movie", "on")
        engine.evaluate_all()

        assert {intent.rule_id for intent in engine.list_active_intents("light.room")} == {
            "movie-mode"
        }


# ── Resolution ───────────────────────────────────────────────────────


class TestResolution:
    def test_resolve_combines_active_intents(self) -> None:
        engine = Engine()
        engine.load_rules(
            [
                _rule("dark", 'sensor.x.state == "on"', set={"brightness_pct": 80}),
                _rule("focus", 'sensor.y.state == "on"', cap={"brightness_pct": 40}),
            ]
        )
        engine.update_state("sensor.x", "on")
        engine.update_state("sensor.y", "on")
        engine.evaluate_all()
        resolved = engine.resolve("light.x")
        assert resolved is not None
        assert resolved.value == {"brightness_pct": 40}


# ── Diagnostics ─────────────────────────────────────────────────────


class TestStructuredDiagnostics:
    def test_explain_target_reports_compositor_winner_not_insertion_order(self) -> None:
        engine = Engine(clock_fn=lambda: 1000)
        engine.load_rules(
            [
                _rule(
                    "low-priority",
                    'sensor.low == "on"',
                    set={"brightness_pct": 20},
                    confidence=0.2,
                ),
                _rule(
                    "high-priority",
                    'sensor.high == "on"',
                    set={"brightness_pct": 80},
                    confidence=0.9,
                ),
            ]
        )
        engine.update_state("sensor.low", "on")
        engine.update_state("sensor.high", "on")
        engine.evaluate_all()

        explanation = engine.explain_target("light.x")

        assert explanation["resolved"]["value"] == {"brightness_pct": 80}
        assert explanation["active_intents"][0]["rule_id"] == "high-priority"
        assert explanation["active_intents"][1]["rule_id"] == "low-priority"
        assert explanation["winning_intent"]["rule_id"] == "high-priority"

    def test_explain_target_reports_blocked_firing_rules(self) -> None:
        engine = Engine(clock_fn=lambda: 0)
        engine.load_rules(
            [
                _rule(
                    "movie-mode",
                    'input_boolean.movie == "on"',
                    target="light.room",
                    set={"brightness_pct": 20},
                    blocks=("ambient",),
                ),
                _rule(
                    "ambient",
                    'sensor.dark == "on"',
                    target="light.room",
                    set={"brightness_pct": 80},
                ),
            ]
        )
        engine.update_state("input_boolean.movie", "on")
        engine.update_state("sensor.dark", "on")
        engine.evaluate_all()

        explanation = engine.explain_target("light.room")

        assert explanation["rules_for_target"] == [
            {
                "rule_id": "movie-mode",
                "firing": True,
                "condition_firing": True,
                "blocked_by": [],
                "for_remaining_ms": None,
                "phase": "active",
                "active_for_ms": 0,
                "condition_active_for_ms": 0,
                "held_for_ms": None,
                "group": "",
                "profile": "",
            },
            {
                "rule_id": "ambient",
                "firing": False,
                "condition_firing": True,
                "blocked_by": ["movie-mode"],
                "for_remaining_ms": None,
                "phase": "idle",
                "active_for_ms": None,
                "condition_active_for_ms": 0,
                "held_for_ms": None,
                "group": "",
                "profile": "",
            },
        ]
        assert explanation["winning_intent"]["rule_id"] == "movie-mode"

    def test_explain_target_reports_for_remaining(self) -> None:
        engine = Engine(clock_fn=lambda: 0)
        engine.load_rules(
            [
                _rule(
                    "motion-held",
                    'binary_sensor.motion == "on"',
                    target="light.hall",
                    for_ms=5_000,
                    set={"state": "on"},
                )
            ]
        )
        engine.update_state("binary_sensor.motion", "on")
        engine.evaluate_all()
        engine.advance_clock(2_000)

        explanation = engine.explain_target("light.hall")

        assert explanation["resolved"] is None
        assert explanation["rules_for_target"] == [
            {
                "rule_id": "motion-held",
                "firing": False,
                "condition_firing": True,
                "blocked_by": [],
                "for_remaining_ms": 3_000,
                "phase": "waiting",
                "active_for_ms": None,
                "condition_active_for_ms": 2_000,
                "held_for_ms": None,
                "group": "",
                "profile": "",
            }
        ]

    def test_list_rule_statuses_reports_rule_entity_attributes(self) -> None:
        engine = Engine()
        engine.load_rules(
            [
                _rule(
                    "office-light",
                    'binary_sensor.office == "on"',
                    target="light.office",
                    set={"state": "on", "brightness_pct": 40},
                    confidence=0.6,
                    reason="Office occupied",
                    labels=("office", "light"),
                )
            ]
        )
        engine.update_state("binary_sensor.office", "on")
        engine.evaluate_all()

        status = engine.list_rule_statuses()["office-light"]

        assert status["enabled"] is True
        assert status["active"] is True
        assert status["condition_firing"] is True
        assert status["active_intent_count"] == 1
        assert status["targets"] == ["light.office"]
        assert status["desired"] == {"set": {"state": "on", "brightness_pct": 40}}
        assert status["authority"] == "automation"
        assert status["confidence"] == 0.6
        assert status["reason"] == "Office occupied"
        assert status["labels"] == ["office", "light"]


# ── TTL expiry ───────────────────────────────────────────────────────


class TestTTLExpiry:
    def test_intent_expires_after_ttl(self) -> None:
        engine = Engine()
        rule = _rule("r1", 'sensor.x.state == "on"', ttl_ms=1000)
        engine.load_rules([rule])
        engine.update_state("sensor.x", "on")
        engine.evaluate_all()
        assert len(engine.list_active_intents("light.x")) == 1
        # Advance the engine's clock past the TTL
        engine.advance_clock(1500)
        engine.evaluate_all()
        assert len(engine.list_active_intents("light.x")) == 0

    def test_intent_with_no_ttl_persists(self) -> None:
        engine = Engine()
        rule = _rule("r1", 'sensor.x.state == "on"', ttl_ms=None)
        engine.load_rules([rule])
        engine.update_state("sensor.x", "on")
        engine.evaluate_all()
        engine.advance_clock(10_000_000)
        engine.evaluate_all()
        assert len(engine.list_active_intents("light.x")) == 1


# ── Animation ────────────────────────────────────────────────────────


class TestAnimationTick:
    def test_resolve_applies_current_animation_frame(self) -> None:
        engine = Engine(clock_fn=lambda: 0)
        rule = _rule(
            "r1",
            'sensor.x.state == "on"',
            set={"brightness_pct": 0, "color_temp_k": 2700},
            animation=_make_pulse_anim(),
        )
        engine.load_rules([rule])
        engine.update_state("sensor.x", "on")
        engine.evaluate_all()

        resolved = engine.resolve("light.x")
        assert resolved is not None
        assert resolved.animation is not None
        assert resolved.value == {"brightness_pct": 0, "color_temp_k": 2700}

        engine.advance_clock(500)
        resolved = engine.resolve("light.x")
        assert resolved is not None
        assert resolved.value["brightness_pct"] == 50
        assert resolved.value["color_temp_k"] == 2700

    def test_resolve_uses_finished_animation_frame(self) -> None:
        engine = Engine(clock_fn=lambda: 0)
        rule = _rule(
            "r1",
            'sensor.x.state == "on"',
            set={"brightness_pct": 0},
            animation=_make_pulse_anim(repeat=1),
        )
        engine.load_rules([rule])
        engine.update_state("sensor.x", "on")
        engine.evaluate_all()
        engine.advance_clock(2500)

        resolved = engine.resolve("light.x")

        assert resolved is not None
        assert resolved.value == {"brightness_pct": 50}


# ── Manual intent injection ─────────────────────────────────────────


class TestManualIntents:
    def test_emit_user_intent(self) -> None:
        engine = Engine()
        engine.emit_user_intent(
            target="light.x",
            set={"brightness_pct": 100},
            ttl_ms=2000,
        )
        assert len(engine.list_active_intents("light.x")) == 1
        resolved = engine.resolve("light.x")
        assert resolved is not None
        assert resolved.winning_intent.authority is Authority.USER
        assert resolved.value == {"brightness_pct": 100}

    def test_emit_user_intent_replaces_existing_manual_intent_for_target(self) -> None:
        engine = Engine()
        engine.emit_user_intent(
            target="light.x",
            set={"brightness_pct": 100},
            ttl_ms=2000,
            reason="Manual HA state change",
        )
        engine.emit_user_intent(
            target="light.x",
            set={"brightness_pct": 40},
            ttl_ms=2000,
            reason="Manual HA state change",
        )

        intents = engine.list_active_intents("light.x")
        assert len(intents) == 1
        assert intents[0].authority is Authority.USER
        assert engine.resolve("light.x").value == {"brightness_pct": 40}

    def test_clear_user_intents_only_removes_manual_intents(self) -> None:
        engine = Engine()
        engine.load_rules([_rule("r1", 'sensor.x.state == "on"', set={"state": "on"})])
        engine.update_state("sensor.x", "on")
        engine.evaluate_all()
        engine.emit_user_intent("light.x", {"brightness_pct": 100})
        engine.emit_user_intent("light.y", {"state": "off"})

        assert engine.clear_user_intents("light.x") == 1

        assert [intent.authority for intent in engine.list_active_intents("light.x")] == [
            Authority.AUTOMATION
        ]
        assert len(engine.list_active_intents("light.y")) == 1

        assert engine.clear_user_intents() == 1

        assert [intent.authority for intent in engine.list_active_intents("light.x")] == [
            Authority.AUTOMATION
        ]
        assert engine.list_active_intents("light.y") == []

    def test_paused_rule_does_not_emit_or_keep_intents(self) -> None:
        engine = Engine()
        engine.load_rules(
            [
                _rule("living-room", 'binary_sensor.presence.state == "on"'),
                _rule("kitchen", 'binary_sensor.presence.state == "on"', target="light.kitchen"),
            ]
        )
        engine.update_state("binary_sensor.presence", "on")
        engine.evaluate_all()

        engine.set_rule_paused("living-room", True)
        engine.evaluate_all()

        assert engine.resolve("light.x") is None
        assert engine.resolve("light.kitchen") is not None
        assert engine.list_authored_rule_statuses()["living-room"]["paused"] is True

    def test_paused_rule_ids_are_persisted(self) -> None:
        engine = Engine()
        engine.load_rules([_rule("living-room", 'binary_sensor.presence.state == "on"')])
        engine.set_rule_paused("living-room", True)

        restored = Engine()
        restored.load_rules([_rule("living-room", 'binary_sensor.presence.state == "on"')])
        restored.import_lifecycle_records(engine.export_lifecycle_records())

        assert restored.is_rule_paused("living-room") is True


# ── Rule reload ─────────────────────────────────────────────────────


class TestRuleReload:
    def test_load_rules_replaces_set(self) -> None:
        engine = Engine()
        engine.load_rules([_rule("r1", 'sensor.x.state == "on"')])
        engine.update_state("sensor.x", "on")
        engine.evaluate_all()
        assert len(engine.list_active_intents("light.x")) == 1
        # Reload with a different rule set
        engine.load_rules([_rule("r2", 'sensor.x.state == "off"')])
        engine.evaluate_all()
        # r1's intent should be gone (rule no longer loaded)
        # r2's intent is not active because sensor.x is "on"
        assert len(engine.list_active_intents("light.x")) == 0

    def test_load_rules_recreates_active_intent_from_current_rule_definition(self) -> None:
        engine = Engine()
        engine.load_rules([_rule("r1", 'sensor.x.state == "on"', target="light.old")])
        engine.update_state("sensor.x", "on")
        engine.evaluate_all()
        assert engine.resolve("light.old").value == {}

        engine.load_rules(
            [
                _rule(
                    "r1",
                    'sensor.x.state == "on"',
                    target="light.new",
                    set={"brightness_pct": 40},
                )
            ]
        )
        engine.evaluate_all()

        assert engine.resolve("light.old") is None
        assert engine.resolve("light.new").value == {"brightness_pct": 40}


# ── Diagnostics ─────────────────────────────────────────────────────


class TestDiagnostics:
    def test_public_counts_and_target_lists_exclude_expired_intents(self) -> None:
        engine = Engine(clock_fn=lambda: 1000)
        engine.load_rules(
            [
                _rule("light-rule", 'sensor.x.state == "on"', target="light.x"),
                _rule("switch-rule", 'sensor.y.state == "on"', target="switch.y"),
            ]
        )
        engine.update_state("sensor.x", "on")
        engine.evaluate_all()
        engine.emit_user_intent(
            target="light.manual",
            set={"state": "on"},
            ttl_ms=1000,
        )

        assert engine.rule_count() == 2
        assert engine.list_known_targets() == ("light.x", "switch.y")
        assert engine.list_active_targets() == ("light.manual", "light.x")
        assert engine.has_active_target("light.x") is True
        assert engine.has_active_target("switch.y") is False
        assert engine.active_intent_count() == 2

        engine.advance_clock(1000)

        assert engine.list_active_targets() == ("light.x",)
        assert engine.has_active_target("light.manual") is False
        assert engine.active_intent_count() == 1

    def test_explain_returns_reason_chain(self) -> None:
        engine = Engine()
        engine.load_rules(
            [
                _rule(
                    "dark",
                    'sensor.x.state == "on"',
                    reason="Dark outside",
                    set={"brightness_pct": 80},
                ),
            ]
        )
        engine.update_state("sensor.x", "on")
        engine.evaluate_all()
        explanation = engine.explain("light.x")
        assert "light.x" in explanation
        assert "dark" in explanation
        assert "Dark outside" in explanation


def _make_pulse_anim(repeat: int = 1) -> Any:
    from intentional.animation import AnimationSpec

    return AnimationSpec(
        kind="pulse",
        parameter="brightness_pct",
        values=[0, 100, 0],
        duration_ms=2000,
        repeat=repeat,
    )
def test_semantic_groups_are_independent_and_support_edges() -> None:
    from intentional.engine import Engine
    from intentional.yaml_loader import load_rules_from_string

    rules = load_rules_from_string("""
- id: safe-motion
  while:
    motion: {detected: {area: office, changed: true}}
    door: {closed: {area: office, behavior: all}}
  emit: {target: light.office, set: {state: on}}
""")
    memberships = {"motion": ["binary_sensor.motion"], "door": ["binary_sensor.door"]}
    engine = Engine(clock_fn=lambda: 0, selector_resolver=lambda selector: memberships[selector.purpose])
    engine.load_rules(rules)
    engine.update_state("binary_sensor.motion", "on")
    engine.update_state("binary_sensor.motion", True, field="changed")
    engine.update_state("binary_sensor.door", "off")
    engine.evaluate_all()

    assert engine.resolve("light.office") is not None


def test_semantic_and_ordinary_observations_must_both_match() -> None:
    from intentional.yaml_loader import load_rules_from_string

    rules = load_rules_from_string("""
- id: guarded-motion
  while:
    motion: {detected: {area: office}}
    input_boolean.enabled: {is: on}
  emit: {target: light.office, set: {state: on}}
""")
    engine = Engine(
        clock_fn=lambda: 0,
        selector_resolver=lambda _selector: ["binary_sensor.motion"],
    )
    engine.load_rules(rules)
    engine.update_state("binary_sensor.motion", "on")
    engine.update_state("input_boolean.enabled", "off")
    engine.evaluate_all()
    assert engine.resolve("light.office") is None

    engine.update_state("input_boolean.enabled", "on")
    engine.evaluate_all()
    assert engine.resolve("light.office") is not None


def test_unavailable_only_forces_semantic_selector_nonmatch() -> None:
    from intentional.records import ObserveSelector
    from intentional.selectors import observe_selector_target_matches

    state = {"sensor.temperature.state": "unavailable"}
    legacy = ObserveSelector(operator="is_not", value="on")
    semantic = ObserveSelector(purpose="temperature", operator="is_not", value=21)

    assert observe_selector_target_matches(state, "sensor.temperature", legacy)
    assert not observe_selector_target_matches(state, "sensor.temperature", semantic)
