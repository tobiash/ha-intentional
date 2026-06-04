"""Tests for scene support in rules.

A rule can EITHER have a `target` (and operate via the compositor) OR
reference a HA scene by entity_id (and activate that scene when the
rule fires). The two are mutually exclusive: scene rules don't go
through the compositor at all — the integration layer fires a
`scene.turn_on` call instead.

Design rationale: HA scenes are a battle-tested primitive for setting
many entities atomically. ha-intentional should reference them, not
reimplement them. The `scene:` field is the bridge.
"""

from __future__ import annotations

import pytest

from intentional.yaml_loader import RuleLoadError, load_rules_from_string

# ── Schema validation: scene is allowed, mutually exclusive with target


class TestSceneSchemaValidation:
    def test_rule_with_scene_only_passes(self) -> None:
        rules = load_rules_from_string("""
- id: movie-scene
  when: input_boolean.movie == "on"
  emit:
    scene: scene.movie
    ttl: 2h
  authority: user
""")
        assert len(rules) == 1
        assert rules[0].scene == "scene.movie"
        assert rules[0].target == ""  # default empty when scene is set
        assert rules[0].ttl_ms == 7_200_000  # ttl inside emit still works

    def test_rule_with_neither_scene_nor_target_raises(self) -> None:
        with pytest.raises(RuleLoadError) as exc:
            load_rules_from_string("""
- id: missing-both
  when: sensor.x == "on"
  emit:
    transition: 1s
""")
        assert "scene" in str(exc.value).lower() or "target" in str(exc.value).lower()

    def test_rule_with_both_scene_and_target_raises(self) -> None:
        with pytest.raises(RuleLoadError) as exc:
            load_rules_from_string("""
- id: conflict
  when: sensor.x == "on"
  emit:
    target: light.x
    scene: scene.movie
""")
        # Error message should help the user fix it
        msg = str(exc.value).lower()
        assert "scene" in msg or "target" in msg
        assert "mutually exclusive" in msg or "both" in msg

    def test_scene_field_must_be_string(self) -> None:
        with pytest.raises(RuleLoadError):
            load_rules_from_string("""
- id: bad-scene
  when: sensor.x == "on"
  emit:
    scene: 42
""")

    def test_empty_scene_string_raises(self) -> None:
        with pytest.raises(RuleLoadError):
            load_rules_from_string("""
- id: empty-scene
  when: sensor.x == "on"
  emit:
    scene: ""
""")


# ── Scene rules still get a Rule object with the scene attribute


class TestSceneRuleProperties:
    def test_scene_attribute_exposed(self) -> None:
        rules = load_rules_from_string("""
- id: bedtime
  when: input_boolean.bedtime == "on"
  emit:
    scene: scene.bedtime
""")
        assert rules[0].scene == "scene.bedtime"

    def test_target_default_empty_when_scene_set(self) -> None:
        rules = load_rules_from_string("""
- id: s
  when: sensor.x == "on"
  emit:
    scene: scene.s
""")
        assert rules[0].target == ""

    def test_scene_rule_carries_emit_metadata(self) -> None:
        """Transition, easing, ttl, etc. all apply to scene rules too."""
        rules = load_rules_from_string("""
- id: gradual-scene
  when: input_boolean.x == "on"
  emit:
    scene: scene.x
    transition: 3s
    easing: ease-in
    ttl: 1h
  authority: user
""")
        rule = rules[0]
        assert rule.transition_ms == 3000
        assert rule.easing == "ease-in"
        assert rule.ttl_ms == 3_600_000
        assert rule.authority.value == "user"

    def test_unknown_field_in_emit_still_rejected(self) -> None:
        """scene: doesn't loosen the rest of the schema validation."""
        with pytest.raises(RuleLoadError) as exc:
            load_rules_from_string("""
- id: typo
  when: sensor.x == "on"
  emit:
    scene: scene.x
    sets: { brightness_pct: 50 }   # typo
""")
        assert "sets" in str(exc.value).lower() or "unknown" in str(exc.value).lower()
