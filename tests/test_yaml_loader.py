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
