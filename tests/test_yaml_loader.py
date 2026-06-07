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
        assert [rule.target for rule in rules] == ["light.living_room", "cover.blinds"]
        assert all(rule.when == 'input_boolean.movie_mode == "on"' for rule in rules)
        assert rules[0].set == {"brightness_pct": 15}
        assert rules[1].set == {"state": "closed"}

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
