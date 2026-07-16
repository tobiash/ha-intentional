"""Tests for the YAML rule loader.

The loader is the bridge between human-authored YAML and runtime rule
objects. It must:
- Parse YAML files into Rule objects
- Validate the schema strictly with clear error messages
- Support multi-document YAML (one file, many rules)
- Support rule inheritance (extends:) for DRY authoring
- Support time-duration shorthand: "2s" → 2000ms, "300ms" → 300ms
- Collect all errors from a file, not just the first one
- Be idempotent: load_rules() called twice gives the same result
"""

from __future__ import annotations

from pathlib import Path

import pytest

from intentional.yaml_loader import (
    RuleLoadError,
    load_rules,
    load_rules_from_string,
    parse_duration,
    rule_dir_fingerprint,
)

# ── Duration parsing ─────────────────────────────────────────────────


class TestDurationParsing:
    def test_milliseconds(self) -> None:
        assert parse_duration("500ms") == 500
        assert parse_duration("1ms") == 1

    def test_seconds(self) -> None:
        assert parse_duration("1s") == 1000
        assert parse_duration("2s") == 2000
        assert parse_duration("30s") == 30_000

    def test_minutes(self) -> None:
        assert parse_duration("1m") == 60_000
        assert parse_duration("5m") == 300_000

    def test_hours(self) -> None:
        assert parse_duration("1h") == 3_600_000
        assert parse_duration("2h") == 7_200_000

    def test_combined(self) -> None:
        assert parse_duration("1h30m") == 5_400_000
        assert parse_duration("1h30m15s") == 5_415_000

    def test_fractional_seconds(self) -> None:
        assert parse_duration("1.5s") == 1500
        assert parse_duration("0.5s") == 500
        assert parse_duration("2.25h") == 8_100_000

    def test_whitespace_tolerated(self) -> None:
        assert parse_duration("  2s  ") == 2000

    def test_invalid_unit_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_duration("5x")

    def test_negative_duration_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_duration("-1s")


# ── Rule schema validation ───────────────────────────────────────────


class TestRuleSchemaValidation:
    def test_document_target_safety_policy_is_owned_by_document(self) -> None:
        rules = load_rules_from_string("""
targets:
  lock.front_door:
    ownership: managed
    allowed_fields: [state]
    forbidden_automatic_states: [unlocked]
    unavailable: skip
    max_retries: 2
    user_authority:
      fields: [state]
      states: [unlocked]
rules:
  - id: secure-door
    while: {input_boolean.away: on}
    intent: {lock.front_door: {state: locked}}
""")

        policy = rules.target_policies["lock.front_door"]
        assert policy is not None
        assert policy.ownership == "managed"
        assert policy.allowed_fields == frozenset({"state"})
        assert policy.forbidden_automatic_states == frozenset({"unlocked"})
        assert policy.unavailable == "skip"
        assert policy.max_retries == 2
        assert policy.user_authority_states == frozenset({"unlocked"})

    def test_policy_without_explicit_rule_remains_in_document_registry(self) -> None:
        rules = load_rules_from_string("""
targets:
  light.dynamic:
    ownership: observe_only
rules: []
""")

        assert rules == []
        assert rules.target_policies["light.dynamic"].ownership == "observe_only"

    @pytest.mark.parametrize("field,value", [
        ("ownership", "exclusive"),
        ("unavailable", "retry"),
        ("max_retries", -1),
        ("allowed_fields", "state"),
    ])
    def test_document_target_safety_policy_rejects_noncanonical_values(self, field: str, value: object) -> None:
        import yaml

        contents = yaml.safe_dump({
            "targets": {"lock.front_door": {field: value}},
            "rules": [{"id": "secure", "when": "true", "emit": {"target": "lock.front_door", "set": {"state": "locked"}}}],
        })
        with pytest.raises(RuleLoadError):
            load_rules_from_string(contents)

    def test_vnext_observe_intent_rule_normalizes_to_rule(self) -> None:
        rules = load_rules_from_string("""
- id: living-room-tv
  observe:
    media_player.tv: on
  intent:
    light.living_room:
      color_temp_k: 2700
      brightness_pct:
        max: 40
""")

        assert len(rules) == 1
        assert rules[0].id == "living-room-tv"
        assert rules[0].when == 'media_player.tv == "on"'
        assert rules[0].target == "light.living_room"
        assert rules[0].set == {"color_temp_k": 2700}
        assert rules[0].cap == {"brightness_pct": 40}

    def test_document_targets_defaults_expand_to_baseline_rules(self) -> None:
        rules = load_rules_from_string("""
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

        assert [rule.id for rule in rules] == [
            "__target_default__:light.living_room",
            "living-room-presence",
        ]
        assert rules[0].when == "true"
        assert rules[0].target == "light.living_room"
        assert rules[0].set == {"state": "off"}
        assert rules[0].authority.value == "sensor"
        assert rules[0].confidence == 0.0

    def test_vnext_stable_for_aliases_after(self) -> None:
        rules = load_rules_from_string("""
- id: stable-tv
  observe:
    media_player.tv: playing
  stable_for: 10s
  intent:
    light.sofa:
      brightness_pct: 20
""")

        assert rules[0].for_ms == 10_000

    def test_vnext_observe_stable_for_aliases_observe_for(self) -> None:
        rules = load_rules_from_string("""
- id: stable-tv
  observe:
    media_player.tv: playing
    stable_for: 10s
  intent:
    light.sofa:
      brightness_pct: 20
""")

        assert rules[0].for_ms == 10_000

    def test_vnext_observe_supports_named_comparison(self) -> None:
        rules = load_rules_from_string("""
- id: brighten-when-dark
  observe:
    sensor.outdoor_light.illuminance:
      lt: 50
  intent:
    light.living_room:
      brightness_pct: 80
""")

        assert rules[0].when == "sensor.outdoor_light.illuminance < 50"
        assert rules[0].target == "light.living_room"
        assert rules[0].set == {"brightness_pct": 80}

    def test_vnext_observe_multiple_fields_are_implicit_all(self) -> None:
        rules = load_rules_from_string("""
- id: tv-when-dark
  observe:
    media_player.tv: on
    sensor.outdoor_light.illuminance:
      lt: 50
  intent:
    light.living_room:
      brightness_pct: 30
""")

        assert rules[0].when == 'media_player.tv == "on" and sensor.outdoor_light.illuminance < 50'

    def test_vnext_observe_for_normalizes_to_dwell_time(self) -> None:
        rules = load_rules_from_string("""
- id: door-left-open
  observe:
    binary_sensor.front_door: on
    for: 30s
  intent:
    light.entry:
      state: on
""")

        assert rules[0].when == 'binary_sensor.front_door == "on"'
        assert rules[0].for_ms == 30_000

    def test_lifecycle_while_after_hold_normalizes_to_rule(self) -> None:
        rules = load_rules_from_string("""
- id: living-room-settled
  while:
    binary_sensor.living_room_presence: on
    sensor.living_room_light:
      lt: 100
  after: 5m
  hold:
    while:
      binary_sensor.living_room_presence: on
    after: 10m
  intent:
    light.living_room:
      state: on
      brightness_pct: 60
""")

        assert rules[0].when == 'binary_sensor.living_room_presence == "on" and sensor.living_room_light < 100'
        assert rules[0].for_ms == 300_000
        assert rules[0].hold_when == 'binary_sensor.living_room_presence == "on"'
        assert rules[0].linger_ms == 600_000
        assert rules[0].target == "light.living_room"
        assert rules[0].set == {"state": "on", "brightness_pct": 60}

    def test_lifecycle_rejects_ambiguous_after_and_observe_for(self) -> None:
        with pytest.raises(RuleLoadError, match="Use either top-level `after` or `observe.for`"):
            load_rules_from_string("""
- id: ambiguous-dwell
  while:
    binary_sensor.living_room_presence: on
    for: 1m
  after: 5m
  intent:
    light.living_room:
      state: on
""")

    def test_lifecycle_rejects_ambiguous_hold_after_and_linger(self) -> None:
        with pytest.raises(RuleLoadError, match="Use either `hold.after` or target `linger`"):
            load_rules_from_string("""
- id: ambiguous-hold
  while:
    binary_sensor.living_room_presence: on
  hold:
    after: 5m
  intent:
    light.living_room:
      state: on
      linger: 1m
""")

    @pytest.mark.parametrize(
        "hold",
        [
            "after: 5m\n    after_when_stops: 10m",
            "after_when_stops: 10m\n    after: 5m",
        ],
    )
    def test_lifecycle_rejects_both_hold_after_aliases(self, hold: str) -> None:
        with pytest.raises(
            RuleLoadError,
            match="Use either `hold.after` or `hold.after_when_stops`",
        ):
            load_rules_from_string(f"""
- id: ambiguous-hold-alias
  while: {{binary_sensor.motion: on}}
  hold:
    {hold}
  intent:
    light.one: {{state: on}}
""")

    def test_lifecycle_hold_survives_multi_target_expansion(self) -> None:
        rules = load_rules_from_string("""
- id: living-room-evening
  while:
    binary_sensor.living_room_presence: on
    schedule.living_room_evening: on
  hold:
    while:
      binary_sensor.living_room_presence: on
    after: 5m
  intent:
    light.sofa:
      state: on
      brightness_pct: 60
    light.table:
      state: on
      brightness_pct: 40
""")

        assert [rule.id for rule in rules] == [
            "living-room-evening:light.sofa",
            "living-room-evening:light.table",
        ]
        assert {rule.target: rule.hold_when for rule in rules} == {
            "light.sofa": 'binary_sensor.living_room_presence == "on"',
            "light.table": 'binary_sensor.living_room_presence == "on"',
        }
        assert {rule.target: rule.linger_ms for rule in rules} == {
            "light.sofa": 300_000,
            "light.table": 300_000,
        }

    def test_dynamic_hold_after_parses_and_expands_immutably(self) -> None:
        rules = load_rules_from_string("""
- id: adaptive
  while: {binary_sensor.motion: on}
  hold:
    after:
      tiers:
        - {active_for: 0s, duration: 30s}
        - {active_for: 30m, duration: 10m}
      adjustments:
        - {from: "22:00", until: "06:00", add: 5m}
      max: 15m
  intent:
    light.one: {state: on}
    light.two: {state: on}
""")
        assert len(rules) == 2
        policy = rules[0].dynamic_hold_after
        assert policy == rules[1].dynamic_hold_after
        assert [(tier.active_for_ms, tier.duration_ms) for tier in policy.tiers] == [(0, 30_000), (1_800_000, 600_000)]
        assert policy.adjustments[0].add_ms == 300_000
        assert policy.max_ms == 900_000
        assert all(rule.linger_ms is None for rule in rules)

    @pytest.mark.parametrize("replacement", [
        "tiers: []\n      adjustments: []\n      max: 1m",
        "tiers: [{active_for: 1s, duration: 1m}]\n      adjustments: []\n      max: 1m",
        "tiers: [{active_for: 0s, duration: 1m}, {active_for: 0s, duration: 2m}]\n      adjustments: []\n      max: 2m",
        "tiers: [{active_for: 0s, duration: -1m}]\n      adjustments: []\n      max: 1m",
        "tiers: [{active_for: 0s, duration: 1m}]\n      adjustments: [{from: '2:00', until: '06:00', add: 1m}]\n      max: 1m",
        "tiers: [{active_for: 0s, duration: 1m}]\n      adjustments: []\n      max: 0s",
    ])
    def test_dynamic_hold_after_rejects_invalid_policy(self, replacement: str) -> None:
        with pytest.raises(RuleLoadError):
            load_rules_from_string(f"""
- id: invalid
  while: {{binary_sensor.motion: on}}
  hold:
    after:
      {replacement}
  intent:
    light.one: {{state: on}}
""")

    def test_dynamic_hold_after_rejects_duration_over_safe_bound(self) -> None:
        with pytest.raises(RuleLoadError, match="is too large"):
            load_rules_from_string("""
- id: overflow
  while: {binary_sensor.motion: on}
  hold:
    after:
      tiers: [{active_for: 0s, duration: 8784h}]
      adjustments: []
      max: 8784h
  intent:
    light.one: {state: on}
""")

    def test_lifecycle_hold_until_normalizes_release_condition(self) -> None:
        rules = load_rules_from_string("""
- id: living-room-dark
  while:
    binary_sensor.living_room_presence: on
    sensor.living_room_light:
      lt: 100
  hold:
    until:
      binary_sensor.living_room_presence: off
      for: 15m
  intent:
    light.living_room:
      state: on
""")

        assert rules[0].hold_until_when == 'binary_sensor.living_room_presence == "off"'
        assert rules[0].hold_until_for_ms == 900_000

    def test_rule_group_and_profile_metadata_loads(self) -> None:
        rules = load_rules_from_string("""
- id: living-room-pass-through
  group: living-room-lighting
  profile: pass-through
  while:
    binary_sensor.living_room_presence: on
  intent:
    light.living_room:
      brightness_pct: 20
""")

        assert rules[0].group == "living-room-lighting"
        assert rules[0].profile == "pass-through"

    def test_lifecycle_rejects_empty_hold_until(self) -> None:
        with pytest.raises(RuleLoadError, match="`hold.until` must contain at least one release condition"):
            load_rules_from_string("""
- id: empty-release
  while:
    binary_sensor.living_room_presence: on
  hold:
    until:
      for: 15m
  intent:
    light.living_room:
      state: on
""")

    def test_vnext_observe_any_normalizes_to_or_expression(self) -> None:
        rules = load_rules_from_string("""
- id: office-occupied
  observe:
    any:
      - binary_sensor.office_motion: on
      - binary_sensor.office_presence: on
  intent:
    light.office:
      state: on
""")

        assert rules[0].when == '(binary_sensor.office_motion == "on" or binary_sensor.office_presence == "on")'

    def test_vnext_observe_all_normalizes_to_and_expression(self) -> None:
        rules = load_rules_from_string("""
- id: tv-when-dark-explicit
  observe:
    all:
      - media_player.tv: on
      - sensor.outdoor_light.illuminance:
          lt: 50
  intent:
    light.living_room:
      brightness_pct: 30
""")

        assert rules[0].when == '(media_player.tv == "on" and sensor.outdoor_light.illuminance < 50)'

    def test_vnext_observe_not_wraps_one_observation(self) -> None:
        rules = load_rules_from_string("""
- id: not-guest-mode
  observe:
    not:
      input_boolean.guest_mode: on
  intent:
    light.hallway:
      state: on
""")

        assert rules[0].when == 'not (input_boolean.guest_mode == "on")'

    def test_vnext_observe_none_negates_many_observations(self) -> None:
        rules = load_rules_from_string("""
- id: neither-mode
  observe:
    none:
      - input_boolean.guest_mode: on
      - input_boolean.sleep_mode: on
  intent:
    light.hallway:
      state: on
""")

        assert rules[0].when == 'not (input_boolean.guest_mode == "on" or input_boolean.sleep_mode == "on")'

    def test_vnext_observe_changed_to_normalizes_to_state_change_pulse(self) -> None:
        rules = load_rules_from_string("""
- id: door-opened
  observe:
    changed:
      binary_sensor.front_door:
        to: on
  intent:
    light.entry:
      ttl: 2m
      state: on
""")

        assert rules[0].when == 'binary_sensor.front_door.changed == true and binary_sensor.front_door == "on"'
        assert rules[0].ttl_ms == 120_000

    def test_vnext_observe_happened_normalizes_to_event_pulse(self) -> None:
        rules = load_rules_from_string("""
- id: doorbell-message
  observe:
    happened:
      event.espnow_recv_doorbell:
        event_type: ringer
  intent:
    light.entry:
      ttl: 5s
      state: on
""")

        assert rules[0].when == 'event.espnow_recv_doorbell.triggered == true and event.espnow_recv_doorbell.event_type == "ringer"'
        assert rules[0].ttl_ms == 5_000

    def test_vnext_edge_intent_requires_ttl(self) -> None:
        with pytest.raises(RuleLoadError, match="edge-created intents require `ttl`"):
            load_rules_from_string("""
- id: door-opened
  observe:
    changed:
      binary_sensor.front_door:
        to: on
  intent:
    light.entry:
      state: on
""")

    def test_vnext_missing_observe_is_always_active(self) -> None:
        rules = load_rules_from_string("""
- id: hallway-base
  intent:
    light.hallway:
      brightness_pct:
        min: 3
""")

        assert rules[0].when == "true"
        assert rules[0].target == "light.hallway"
        assert rules[0].floor == {"brightness_pct": 3}

    def test_vnext_intent_target_transition_is_metadata(self) -> None:
        rules = load_rules_from_string("""
- id: hallway-on
  intent:
    light.hallway:
      transition: 1.5s
      state: on
""")

        assert rules[0].transition_ms == 1500
        assert rules[0].set == {"state": "on"}

    def test_vnext_intent_normalizes_yaml_boolean_effect_off(self) -> None:
        rules = load_rules_from_string("""
- id: office-soft-light
  observe:
    binary_sensor.office_occupancy: on
  intent:
    light.office:
      state: on
      effect: off
      brightness_pct: 40
""")

        assert rules[0].set == {
            "state": "on",
            "effect": "off",
            "brightness_pct": 40,
        }

    def test_vnext_intent_target_linger_is_metadata(self) -> None:
        rules = load_rules_from_string("""
- id: tv-linger
  observe:
    media_player.tv: on
  intent:
    light.living_room:
      linger: 2h
      brightness_pct: 30
""")

        assert rules[0].linger_ms == 7_200_000
        assert rules[0].set == {"brightness_pct": 30}

    def test_vnext_intent_target_rejects_ttl_with_linger(self) -> None:
        with pytest.raises(RuleLoadError, match="cannot use both `ttl` and `linger`"):
            load_rules_from_string("""
- id: invalid-lifecycle
  observe:
    media_player.tv: on
  intent:
    light.living_room:
      ttl: 1m
      linger: 2m
      brightness_pct: 30
""")

    def test_vnext_rule_metadata_is_parsed(self) -> None:
        rules = load_rules_from_string("""
- id: metadata-rule
  enabled: false
  labels: [living-room, test]
  notes: Created by an agent
  intent:
    light.living_room:
      state: on
""")

        assert rules[0].enabled is False
        assert rules[0].labels == ("living-room", "test")
        assert rules[0].notes == "Created by an agent"
        assert rules[0].when == "false"

    def test_vnext_intent_suppress_rules_normalizes_to_blocks(self) -> None:
        rules = load_rules_from_string("""
- id: focus-mode
  observe:
    input_boolean.focus_mode: on
  intent:
    suppress:
      rules:
        - phone-ring-pulse
    light.office:
      state: off
""")

        assert rules[0].target == "light.office"
        assert rules[0].blocks == ("phone-ring-pulse",)

    def test_vnext_intent_rejects_toggle_state(self) -> None:
        with pytest.raises(RuleLoadError, match="toggle.*effect"):
            load_rules_from_string("""
- id: toggle-light
  intent:
    light.office:
      state: toggle
""")

    def test_vnext_intent_rejects_remote_command_field(self) -> None:
        with pytest.raises(RuleLoadError, match="command.*effect"):
            load_rules_from_string("""
- id: remote-command
  intent:
    remote.android_tv:
      command: HOME
""")

    def test_vnext_inline_field_animation_parses_to_animation_spec(self) -> None:
        rules = load_rules_from_string("""
- id: pulse-led
  intent:
    light.monitor_back_led:
      ttl: 20s
      color_temp_k: 2700
      brightness_pct:
        animate:
          pulse: [0, 100, 0]
          duration: 2s
          repeat: 4
""")

        assert len(rules) == 1
        rule = rules[0]
        assert rule.target == "light.monitor_back_led"
        assert rule.set == {"color_temp_k": 2700, "brightness_pct": 0}
        assert rule.ttl_ms == 20_000
        assert rule.animation is not None
        assert rule.animation.kind == "pulse"
        assert rule.animation.parameter == "brightness_pct"
        assert rule.animation.values == [0, 100, 0]
        assert rule.animation.duration_ms == 2_000
        assert rule.animation.repeat == 4

    def test_vnext_inline_field_animation_rejects_multiple_animated_fields(self) -> None:
        with pytest.raises(RuleLoadError, match="One animated field"):
            load_rules_from_string("""
- id: too-many-animations
  intent:
    light.monitor_back_led:
      brightness_pct:
        animate:
          pulse: [0, 100, 0]
          duration: 2s
      color_temp_k:
        animate:
          pulse: [2200, 4000, 2200]
          duration: 2s
""")

    def test_vnext_apply_transition_policy_parses(self) -> None:
        rules = load_rules_from_string("""
- id: office-presence
  observe:
    binary_sensor.office_presence: on
  intent:
    light.office:
      state: on
      brightness_pct: 40
      apply:
        transition:
          assert: 2s
          change: 4s
          withdraw: 6s
""")

        assert rules[0].transition_assert_ms == 2_000
        assert rules[0].transition_change_ms == 4_000
        assert rules[0].transition_withdraw_ms == 6_000

    def test_vnext_effect_only_rule_parses_effect(self) -> None:
        rules = load_rules_from_string("""
- id: door-left-open-notify
  observe:
    binary_sensor.front_door: on
    for: 30s
  effect:
    service: notify.mobile_app_phone
    data:
      message: Door has been open for 30 seconds
""")

        assert len(rules) == 1
        assert rules[0].target == ""
        assert rules[0].effects[0].domain == "notify"
        assert rules[0].effects[0].service == "mobile_app_phone"
        assert rules[0].effects[0].data == {"message": "Door has been open for 30 seconds"}

    def test_vnext_multi_target_intent_expands_to_target_rules(self) -> None:
        rules = load_rules_from_string("""
- id: movie-mode
  observe:
    input_boolean.movie_mode: on
  intent:
    light.living_room:
      brightness_pct: 15
    cover.blinds:
      state: closed
""")

        assert [rule.id for rule in rules] == [
            "movie-mode:light.living_room",
            "movie-mode:cover.blinds",
        ]
        assert [rule.authored_rule_id for rule in rules] == ["movie-mode", "movie-mode"]
        assert [rule.target for rule in rules] == ["light.living_room", "cover.blinds"]
        assert all(rule.when == 'input_boolean.movie_mode == "on"' for rule in rules)
        assert rules[0].set == {"brightness_pct": 15}
        assert rules[1].set == {"state": "closed"}

    def test_multi_target_preserves_suppress_and_emits_effect_and_select_once(self) -> None:
        rules = load_rules_from_string("""
- id: movie-mode
  observe:
    input_boolean.movie_mode: on
  intent:
    suppress:
      rules: [daylight]
    select:
      - domain: light
        area: living_room
        brightness_pct: 20
    light.sofa:
      state: on
    cover.blinds:
      state: closed
  effect:
    service: notify.notify
    data: {message: Movie mode}
""")

        assert [rule.blocks for rule in rules] == [("daylight",), ("daylight",)]
        assert [len(rule.effects) for rule in rules] == [1, 0]
        assert [len(rule.intent_selectors) for rule in rules] == [1, 0]

    @pytest.mark.parametrize("modifier", ["set", "cap", "floor", "offset", "multiply"])
    def test_emit_modifier_must_be_mapping(self, modifier: str) -> None:
        with pytest.raises(RuleLoadError, match="modifier must be a mapping"):
            load_rules_from_string(f"""
- id: malformed
  when: "true"
  emit:
    target: light.office
    {modifier}: 42
""")

    def test_invalid_generator_weight_fails_as_rule_validation(self) -> None:
        with pytest.raises(RuleLoadError, match="invalid generator.*weights"):
            load_rules_from_string("""
- id: malformed-generator
  when: "true"
  emit:
    target: light.office
    generate:
      brightness_pct:
        kind: weighted_sample
        from: [10, 20]
        weights: [one, two]
        every: 1s
""")

    def test_minimal_valid_rule(self) -> None:
        rules = load_rules_from_string("""
- id: minimal
  when: sensor.x.state == "on"
  emit:
    target: light.y
""")
        assert len(rules) == 1
        assert rules[0].id == "minimal"
        assert rules[0].target == "light.y"

    def test_missing_id_raises(self) -> None:
        with pytest.raises(RuleLoadError) as exc:
            load_rules_from_string("""
- when: sensor.x.state == "on"
  emit:
    target: light.y
""")
        assert "id" in str(exc.value).lower()

    def test_duplicate_id_raises(self) -> None:
        with pytest.raises(RuleLoadError) as exc:
            load_rules_from_string("""
- id: dup
  when: sensor.x.state == "on"
  emit:
    target: light.y
- id: dup
  when: sensor.a.state == "on"
  emit:
    target: light.b
""")
        assert "duplicate" in str(exc.value).lower()

    def test_missing_target_raises(self) -> None:
        with pytest.raises(RuleLoadError) as exc:
            load_rules_from_string("""
- id: no-target
  when: sensor.x.state == "on"
  emit:
    set:
      brightness_pct: 50
""")
        assert "target" in str(exc.value).lower()

    def test_invalid_authority_raises(self) -> None:
        with pytest.raises(RuleLoadError) as exc:
            load_rules_from_string("""
- id: bad-auth
  when: sensor.x.state == "on"
  emit:
    target: light.y
    authority: god
""")
        assert "authority" in str(exc.value).lower()

    def test_confidence_out_of_range_raises(self) -> None:
        with pytest.raises(RuleLoadError):
            load_rules_from_string("""
- id: bad-conf
  when: sensor.x.state == "on"
  emit:
    target: light.y
    confidence: 1.5
""")

    def test_confidence_negative_raises(self) -> None:
        with pytest.raises(RuleLoadError):
            load_rules_from_string("""
- id: neg-conf
  when: sensor.x.state == "on"
  emit:
    target: light.y
    confidence: -0.1
""")


# ── YAML features ────────────────────────────────────────────────────


class TestYAMLFeatures:
    def test_vnext_mixed_document_rules_key_loads_rules(self) -> None:
        rules = load_rules_from_string("""
rules:
  - id: hallway-base
    intent:
      light.hallway:
        state: on
""")

        assert len(rules) == 1
        assert rules[0].id == "hallway-base"
        assert rules[0].when == "true"
        assert rules[0].target == "light.hallway"
        assert rules[0].set == {"state": "on"}

    def test_vnext_scene_include_expands_to_target_rules(self) -> None:
        rules = load_rules_from_string("""
scenes:
  movie:
    intent:
      light.living_room:
        brightness_pct: 15
      cover.blinds:
        state: closed

rules:
  - id: movie-mode
    observe:
      input_boolean.movie_mode: on
    intent:
      include: scene.movie
""")

        assert [rule.id for rule in rules] == [
            "movie-mode:light.living_room",
            "movie-mode:cover.blinds",
        ]
        assert [rule.target for rule in rules] == ["light.living_room", "cover.blinds"]
        assert rules[0].set == {"brightness_pct": 15}
        assert rules[1].set == {"state": "closed"}

    def test_vnext_scene_include_merges_inline_target_refinement(self) -> None:
        rules = load_rules_from_string("""
scenes:
  movie:
    intent:
      light.living_room:
        brightness_pct: 15
        color_temp_k: 2200

rules:
  - id: movie-mode
    intent:
      include: scene.movie
      light.living_room:
        brightness_pct:
          max: 10
""")

        assert len(rules) == 1
        assert rules[0].id == "movie-mode"
        assert rules[0].target == "light.living_room"
        assert rules[0].set == {"brightness_pct": 15, "color_temp_k": 2200}
        assert rules[0].cap == {"brightness_pct": 10}

    def test_vnext_scene_include_cycle_raises(self) -> None:
        with pytest.raises(RuleLoadError, match="Scene include cycle"):
            load_rules_from_string("""
scenes:
  a:
    intent:
      include: scene.b
  b:
    intent:
      include: scene.a

rules:
  - id: cycle
    intent:
      include: scene.a
""")

    def test_multiple_documents_in_one_file(self) -> None:
        rules = load_rules_from_string("""
- id: rule-a
  when: sensor.x.state == "on"
  emit:
    target: light.a

- id: rule-b
  when: sensor.y.state == "on"
  emit:
    target: light.b
""")
        assert len(rules) == 2
        assert rules[0].id == "rule-a"
        assert rules[1].id == "rule-b"

    def test_ttl_with_duration_shorthand(self) -> None:
        rules = load_rules_from_string("""
- id: with-ttl
  when: sensor.x.state == "on"
  emit:
    target: light.y
    ttl: 2h
""")
        assert rules[0].ttl_ms == 7_200_000

    def test_omitted_ttl_is_unbounded(self) -> None:
        rules = load_rules_from_string("""
- id: without-ttl
  when: sensor.x.state == "on"
  emit:
    target: light.y
""")
        assert rules[0].ttl_ms is None
        assert rules[0].for_ms == 0
        assert rules[0].transition_ms == 0

    def test_transition_with_duration_shorthand(self) -> None:
        rules = load_rules_from_string("""
- id: with-transition
  when: sensor.x.state == "on"
  emit:
    target: light.y
    transition: 1.5s
""")
        assert rules[0].transition_ms == 1500

    def test_for_with_duration_shorthand(self) -> None:
        rules = load_rules_from_string("""
- id: motion-held
  when: binary_sensor.motion == "on"
  for: 2m
  emit:
    target: light.hall
""")

        assert rules[0].for_ms == 120_000

    def test_for_can_use_entity_backed_dynamic_duration(self) -> None:
        rules = load_rules_from_string("""
- id: motion-held
  when: binary_sensor.motion == "on"
  for:
    entity: input_number.motion_off_delay
    unit: s
    default: 2m
  emit:
    target: light.hall
""")

        assert rules[0].for_ms == 120_000
        assert rules[0].for_entity == "input_number.motion_off_delay"
        assert rules[0].for_entity_unit == "s"

    def test_dynamic_for_requires_default(self) -> None:
        with pytest.raises(RuleLoadError, match="requires `default`"):
            load_rules_from_string("""
- id: motion-held
  when: binary_sensor.motion == "on"
  for:
    entity: input_number.motion_off_delay
  emit:
    target: light.hall
""")

    def test_blocked_rules_parsed(self) -> None:
        rules = load_rules_from_string("""
- id: blocker
  when: input_boolean.focus == "on"
  emit:
    target: light.x
  blocks: [rule-a, rule-b]
""")
        assert "rule-a" in rules[0].blocks
        assert "rule-b" in rules[0].blocks

    def test_animation_block_parsed(self) -> None:
        rules = load_rules_from_string("""
- id: pulse-notify
  when: binary_sensor.door == "on"
  emit:
    target: light.led
    animation:
      kind: pulse
      parameter: brightness_pct
      values: [0, 100, 0]
      duration: 2s
      repeat: 4
""")
        rule = rules[0]
        assert rule.animation is not None
        assert rule.animation.kind == "pulse"
        assert rule.animation.values == [0, 100, 0]
        assert rule.animation.duration_ms == 2000
        assert rule.animation.repeat == 4

    def test_all_modifiers_supported(self) -> None:
        rules = load_rules_from_string("""
- id: full-modifiers
  when: sensor.x.state == "on"
  emit:
    target: light.y
    set: { brightness_pct: 80 }
    cap: { brightness_pct: 95 }
    floor: { brightness_pct: 5 }
    offset: { brightness_pct: -10 }
    multiply: { brightness_pct: 0.9 }
    merge: true
""")
        rule = rules[0]
        assert rule.set == {"brightness_pct": 80}
        assert rule.cap == {"brightness_pct": 95}
        assert rule.floor == {"brightness_pct": 5}
        assert rule.offset == {"brightness_pct": -10}
        assert rule.multiply == {"brightness_pct": 0.9}
        assert rule.merge is True

    def test_unquoted_on_off_states_are_normalized(self) -> None:
        rules = load_rules_from_string("""
- id: yaml-bool-state
  when: sensor.x.state == "on"
  emit:
    target: light.y
    set: { state: on }
- id: yaml-bool-off-state
  when: sensor.x.state == "off"
  emit:
    target: light.z
    set: { state: off }
""")

        assert rules[0].set == {"state": "on"}
        assert rules[1].set == {"state": "off"}

    def test_extends_inherits_and_merges_emit_fields(self) -> None:
        rules = load_rules_from_string("""
- id: living-room-default
  when: sensor.dark == "on"
  emit:
    target: light.living_room
    set: { state: on, brightness_pct: 70, color_temp_k: 2700 }
    cap: { brightness_pct: 90 }
    transition: 1s
  authority: automation
  confidence: 0.5
  reason: "Default room lighting"

- id: living-room-tv
  extends: living-room-default
  when: media_player.tv == "on"
  emit:
    set: { brightness_pct: 25 }
    cap: { brightness_pct: 40 }
    ttl: 10m
  confidence: 0.9
  reason: "TV lighting"
""")

        rule = rules[1]
        assert rule.id == "living-room-tv"
        assert rule.when == 'media_player.tv == "on"'
        assert rule.target == "light.living_room"
        assert rule.set == {
            "state": "on",
            "brightness_pct": 25,
            "color_temp_k": 2700,
        }
        assert rule.cap == {"brightness_pct": 40}
        assert rule.transition_ms == 1000
        assert rule.ttl_ms == 600_000
        assert rule.for_ms == 0
        assert rule.confidence == 0.9
        assert rule.reason == "TV lighting"

    def test_extends_can_reference_rules_from_earlier_files(self, tmp_path: Path) -> None:
        (tmp_path / "01-base.yaml").write_text("""
- id: default-light
  when: sensor.dark == "on"
  emit:
    target: light.office
    set: { state: on, brightness_pct: 75 }
""")
        (tmp_path / "02-child.yaml").write_text("""
- id: focus-light
  extends: default-light
  when: input_boolean.focus == "on"
  emit:
    set: { brightness_pct: 95 }
""")

        rules = load_rules(tmp_path)

        assert rules[1].id == "focus-light"
        assert rules[1].target == "light.office"
        assert rules[1].set == {"state": "on", "brightness_pct": 95}

    def test_extends_missing_parent_raises(self) -> None:
        with pytest.raises(RuleLoadError) as exc:
            load_rules_from_string("""
- id: child
  extends: missing-parent
  when: sensor.x == "on"
  emit:
    target: light.x
""")

        assert "missing-parent" in str(exc.value)

    def test_extends_cycle_raises(self) -> None:
        with pytest.raises(RuleLoadError) as exc:
            load_rules_from_string("""
- id: a
  extends: b
  when: sensor.a == "on"
  emit:
    target: light.a
- id: b
  extends: a
  when: sensor.b == "on"
  emit:
    target: light.b
""")

        assert "cycle" in str(exc.value).lower()


# ── Error reporting ──────────────────────────────────────────────────


class TestErrorReporting:
    def test_yaml_syntax_error_includes_line(self) -> None:
        bad_yaml = """
- id: broken
  when: sensor.x.state == "on"
  emit:
    target: light.y
  bad_indent:
this is broken
"""
        with pytest.raises(RuleLoadError) as exc:
            load_rules_from_string(bad_yaml)
        # Error should mention the file or location
        assert "line" in str(exc.value).lower() or "broken" in str(exc.value).lower()

    def test_unknown_field_warns_or_errors(self) -> None:
        """Unknown fields in `emit` should be flagged — they indicate typos."""
        with pytest.raises(RuleLoadError) as exc:
            load_rules_from_string("""
- id: typo
  when: sensor.x.state == "on"
  emit:
    target: light.y
    sets: { brightness_pct: 50 }  # typo: should be `set`
""")
        assert "sets" in str(exc.value).lower() or "unknown" in str(exc.value).lower()


# ── File-based loading ───────────────────────────────────────────────


class TestFileLoading:
    def test_load_from_directory(self, tmp_path: Path) -> None:
        (tmp_path / "01-rules.yaml").write_text("""
- id: from-file
  when: sensor.x.state == "on"
  emit:
    target: light.y
""")
        rules = load_rules(tmp_path)
        assert len(rules) == 1
        assert rules[0].id == "from-file"

    def test_load_multiple_files_alphabetically(self, tmp_path: Path) -> None:
        (tmp_path / "02-second.yaml").write_text("""
- id: second
  when: sensor.x.state == "on"
  emit:
    target: light.b
""")
        (tmp_path / "01-first.yaml").write_text("""
- id: first
  when: sensor.x.state == "on"
  emit:
    target: light.a
""")
        rules = load_rules(tmp_path)
        assert rules[0].id == "first"
        assert rules[1].id == "second"

    def test_ignores_non_yaml_files(self, tmp_path: Path) -> None:
        (tmp_path / "README.md").write_text("# Notes")
        (tmp_path / "rules.yaml").write_text("""
- id: real
  when: sensor.x.state == "on"
  emit:
    target: light.y
""")
        rules = load_rules(tmp_path)
        assert len(rules) == 1
        assert rules[0].id == "real"

    def test_empty_directory_returns_empty_list(self, tmp_path: Path) -> None:
        assert load_rules(tmp_path) == []

    def test_nonexistent_directory_raises(self, tmp_path: Path) -> None:
        with pytest.raises(RuleLoadError):
            load_rules(tmp_path / "does-not-exist")

    def test_invalid_file_aborts_load(self, tmp_path: Path) -> None:
        (tmp_path / "good.yaml").write_text("""
- id: good
  when: sensor.x.state == "on"
  emit:
    target: light.y
""")
        (tmp_path / "bad.yaml").write_text("""
- id: bad
  this is: not: valid: yaml: at: all
""")
        with pytest.raises(RuleLoadError):
            load_rules(tmp_path)

    def test_rule_dir_fingerprint_tracks_yaml_files(self, tmp_path: Path) -> None:
        rule_file = tmp_path / "rules.yaml"
        rule_file.write_text("""
- id: first
  when: sensor.x == "on"
  emit:
    target: light.x
""")

        before = rule_dir_fingerprint(tmp_path)
        (tmp_path / "README.md").write_text("ignored")
        after_ignored = rule_dir_fingerprint(tmp_path)
        rule_file.write_text("""
- id: first
  when: sensor.x == "off"
  emit:
    target: light.x
""")
        after_edit = rule_dir_fingerprint(tmp_path)
        (tmp_path / "second.yml").write_text("""
- id: second
  when: sensor.y == "on"
  emit:
    target: light.y
""")
        after_add = rule_dir_fingerprint(tmp_path)

        assert before == after_ignored
        assert after_edit != before
        assert after_add != after_edit
        assert [name for name, _mtime, _size in after_add] == [
            "rules.yaml",
            "second.yml",
        ]

    def test_rule_dir_fingerprint_missing_directory_is_empty(self, tmp_path: Path) -> None:
        assert rule_dir_fingerprint(tmp_path / "missing") == ()
def test_semantic_observation_loads_target_first_group_and_dwell() -> None:
    from intentional.yaml_loader import load_rules_from_string

    rule = load_rules_from_string("""
- id: office-motion
  while: {motion: {detected: {area: office, behavior: any, for: 2s}}}
  emit: {target: light.office, set: {state: on}}
""")[0]

    assert rule.when == "true"
    assert rule.for_ms == 2000
    group = rule.observation_groups[0]
    assert (group.selector.purpose, group.selector.domain, group.selector.area) == ("motion", "binary_sensor", "office")


def test_semantic_observation_composes_with_ordinary_observation() -> None:
    rule = load_rules_from_string("""
- id: guarded-motion
  while:
    motion: {detected: {area: office}}
    input_boolean.enabled: {is: on}
  emit: {target: light.office, set: {state: on}}
""")[0]

    assert rule.when == 'input_boolean.enabled == "on"'
    assert len(rule.observation_groups) == 1


@pytest.mark.parametrize(
    "observation, message",
    [
        ("motion: {detected: {changed: true, for: 2s}}", "cannot be combined"),
        ("temperature: {above: {value: 21, changed: true}}", "only supported for binary"),
        ("motion: {detected: {area: []}}", "area.*non-empty string"),
        ("motion: {detected: {device: ''}}", "device.*non-empty string"),
        ("motion: {detected: {entity: 7}}", "entity.*non-empty string"),
    ],
)
def test_semantic_observation_rejects_unsafe_shapes(observation: str, message: str) -> None:
    with pytest.raises(RuleLoadError, match=message):
        load_rules_from_string(f"""
- id: invalid-semantic
  while: {{{observation}}}
  emit: {{target: light.office, set: {{state: on}}}}
""")


@pytest.mark.parametrize("hold_key", ["while", "until"])
def test_semantic_hold_rejects_clause_dwell(hold_key: str) -> None:
    with pytest.raises(RuleLoadError, match="not supported inside `hold`"):
        load_rules_from_string(f"""
- id: invalid-hold-dwell
  while: {{motion: {{detected: {{}}}}}}
  hold:
    {hold_key}: {{occupancy: {{occupied: {{for: 5s}}}}}}
  emit: {{target: light.office, set: {{state: on}}}}
""")


@pytest.mark.parametrize(
    ("location", "operator"),
    [
        ("while", "any"),
        ("while", "all"),
        ("while", "none"),
        ("while", "not"),
        ("hold.while", "any"),
        ("hold.until", "not"),
    ],
)
def test_semantic_purpose_rejects_ordinary_boolean_nesting(
    location: str, operator: str
) -> None:
    nested = (
        f"{{{operator}: {{motion: {{detected: {{}}}}}}}}"
        if operator == "not"
        else f"{{{operator}: [{{motion: {{detected: {{}}}}}}]}}"
    )
    if location == "while":
        main = nested
        hold = ""
    else:
        main = "{motion: {detected: {}}}"
        hold_key = location.partition(".")[2]
        hold = f"  hold:\n    {hold_key}: {nested}\n"

    with pytest.raises(
        RuleLoadError,
        match=rf"Semantic purpose `motion` cannot be nested inside ordinary `{operator}` in `{location}`",
    ):
        load_rules_from_string(
            f"- id: nested-semantic\n  while: {main}\n{hold}"
            "  emit: {target: light.office, set: {state: on}}\n"
        )


@pytest.mark.parametrize(
    ("purpose", "comparison"),
    [
        ("motion", "active"),
        ("occupancy", "vacant"),
        ("door", "clear"),
        ("window", "detected"),
        ("moisture", "clear"),
    ],
)
def test_semantic_binary_vocabulary_is_purpose_specific(
    purpose: str, comparison: str
) -> None:
    with pytest.raises(RuleLoadError, match=rf"Semantic `{purpose}` requires"):
        load_rules_from_string(
            f"- id: invalid-vocabulary\n  while: {{{purpose}: {{{comparison}: {{}}}}}}\n"
            "  emit: {target: light.office, set: {state: on}}\n"
        )


def test_dynamic_hold_after_bounds_collection_sizes() -> None:
    from intentional.yaml_loader import MAX_HOLD_AFTER_ADJUSTMENTS, MAX_HOLD_AFTER_TIERS

    tiers = "\n".join(
        f"        - active_for: {index}s\n          duration: 1m"
        for index in range(MAX_HOLD_AFTER_TIERS + 1)
    )
    adjustments = "\n".join(
        "        - {from: '00:00', until: '00:01', add: 1s}"
        for _ in range(MAX_HOLD_AFTER_ADJUSTMENTS + 1)
    )
    prefix = "- id: bounded\n  while: {binary_sensor.x: on}\n  hold:\n    after:\n"
    suffix = "\n  intent: {light.x: {state: on}}\n"

    with pytest.raises(RuleLoadError, match="tiers.*at most"):
        load_rules_from_string(prefix + f"      tiers:\n{tiers}\n      adjustments: []\n      max: 1h" + suffix)
    with pytest.raises(RuleLoadError, match="adjustments.*at most"):
        load_rules_from_string(prefix + f"      tiers: [{{active_for: 0s, duration: 1m}}]\n      adjustments:\n{adjustments}\n      max: 1h" + suffix)


def test_pulse_alert_requires_resolution_duration() -> None:
    with pytest.raises(RuleLoadError, match="pulse.*resolve_after"):
        load_rules_from_string("""
- id: doorbell
  when: binary_sensor.doorbell.changed == true
  alert:
    name: DoorbellPressed
    severity: info
    annotations: {summary: Doorbell pressed}
""")


def test_state_alert_rejects_pulse_resolution_duration() -> None:
    with pytest.raises(RuleLoadError, match="resolve_after.*pulse"):
        load_rules_from_string("""
- id: freezer
  while: {sensor.freezer_temperature: {gt: -10}}
  alert:
    name: FreezerTemperatureHigh
    severity: warning
    resolve_after: 5m
    annotations: {summary: Freezer is too warm}
""")


def test_pulse_alert_rejects_pending_duration() -> None:
    with pytest.raises(RuleLoadError, match="pulse.*for"):
        load_rules_from_string("""
- id: doorbell
  when: event.doorbell.triggered == true
  alert:
    name: DoorbellPressed
    severity: info
    for: 1s
    resolve_after: 5m
    annotations: {summary: Doorbell pressed}
""")


def test_alert_names_must_be_unique_within_rule() -> None:
    with pytest.raises(RuleLoadError, match="duplicate Alert name"):
        load_rules_from_string("""
- id: freezer
  while: {sensor.freezer_temperature: {gt: -10}}
  alert:
    - name: FreezerTemperatureHigh
      severity: warning
      annotations: {summary: Freezer is too warm}
    - name: FreezerTemperatureHigh
      severity: critical
      annotations: {summary: Freezer is critically warm}
""")


def test_alert_rejects_mixed_pulse_and_state_observation() -> None:
    with pytest.raises(RuleLoadError, match="mix pulse and state"):
        load_rules_from_string("""
- id: mixed
  when: button.doorbell.changed == true or binary_sensor.smoke == "on"
  alert:
    name: Emergency
    severity: critical
    resolve_after: 5m
    annotations: {summary: Emergency detected}
""")


def test_alert_parses_labels_annotations_staleness_and_escalations() -> None:
    alert = load_rules_from_string("""
- id: freezer
  while: {sensor.freezer_temperature: {gt: -10}}
  alert:
    name: FreezerTemperatureHigh
    severity: info
    stale_after: 5m
    labels: {area: kitchen, category: appliance}
    annotations:
      summary: Freezer is too warm
      description: Check the freezer door
    escalations:
      - {after: 30m, severity: warning}
      - {after: 2h, severity: critical}
""")[0].alerts[0]

    assert alert.labels == {"area": "kitchen", "category": "appliance"}
    assert alert.annotations == {
        "summary": "Freezer is too warm",
        "description": "Check the freezer door",
    }
    assert alert.stale_after_ms == 300_000
    assert [(step.after_ms, step.severity) for step in alert.escalations] == [
        (1_800_000, "warning"),
        (7_200_000, "critical"),
    ]


def test_alert_rejects_reserved_labels_and_content_overflow() -> None:
    with pytest.raises(RuleLoadError, match="reserved label"):
        load_rules_from_string("""
- id: freezer
  while: {binary_sensor.freezer: "on"}
  alert:
    name: FreezerOpen
    severity: warning
    labels: {severity: custom}
    annotations: {summary: Freezer is open}
""")
    with pytest.raises(RuleLoadError, match="at most 16 Alerts"):
        alerts = "\n".join(
            f"    - name: Alert{index}\n      severity: info\n      annotations: {{summary: Alert {index}}}"
            for index in range(17)
        )
        load_rules_from_string(
            f'- id: many\n  while: {{binary_sensor.test: "on"}}\n  alert:\n{alerts}\n'
        )


def test_document_rejects_more_than_256_alert_definitions() -> None:
    rules = "\n".join(
        f"""- id: rule-{index}
  while: {{binary_sensor.test_{index}: \"on\"}}
  alert:
    name: Alert{index}
    severity: info
    annotations: {{summary: Alert {index}}}"""
        for index in range(257)
    )

    with pytest.raises(RuleLoadError, match="at most 256 Alert definitions"):
        load_rules_from_string(rules)
