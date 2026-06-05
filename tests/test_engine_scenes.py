"""Tests for scene support in the Engine.

The engine needs to expose scene rules separately from target-resolved
rules, because they take a different path in the integration layer:
- Target rules → compositor → light.turn_on with resolved value
- Scene rules → scene.turn_on with the scene entity_id

Scene rules can have modifiers (transition, ttl) but no set/cap/etc.
fields — the scene defines the values, the integration just activates it.
"""

from __future__ import annotations

from intentional.engine import Engine
from intentional.yaml_loader import Rule


def _scene_rule(id_: str, when: str, scene: str, **kwargs) -> Rule:
    return Rule(id=id_, when=when, scene=scene, **kwargs)


def _target_rule(id_: str, when: str, target: str, **kwargs) -> Rule:
    return Rule(id=id_, when=when, target=target, **kwargs)


class TestSceneRuleLifecycle:
    def test_scene_rule_emits_when_trigger_fires(self) -> None:
        engine = Engine()
        engine.load_rules([_scene_rule("movie", 'input_boolean.x == "on"', "scene.movie")])
        engine.update_state("input_boolean.x", "on")
        engine.evaluate_all()
        assert engine.list_active_scene_intents() == ["scene.movie"]

    def test_scene_rule_emits_intent_object(self) -> None:
        engine = Engine()
        engine.load_rules([_scene_rule("movie", 'input_boolean.x == "on"', "scene.movie")])
        engine.update_state("input_boolean.x", "on")
        engine.evaluate_all()
        intents = engine.list_active_scene_intents(return_intents=True)
        assert len(intents) == 1
        intent, scene_id = intents[0]
        assert scene_id == "scene.movie"
        assert intent.rule_id == "movie"

    def test_scene_rule_does_not_appear_in_target_intents(self) -> None:
        """A scene rule should NOT show up in list_active_intents for any target."""
        engine = Engine()
        engine.load_rules([_scene_rule("movie", 'input_boolean.x == "on"', "scene.movie")])
        engine.update_state("input_boolean.x", "on")
        engine.evaluate_all()
        # No target-resolved intents, only scene intents
        assert engine.list_active_intents("scene.movie") == []
        assert engine.list_active_intents("light.x") == []
        assert engine.list_active_scene_intents() == ["scene.movie"]

    def test_scene_rule_drops_when_trigger_stops(self) -> None:
        engine = Engine()
        engine.load_rules([_scene_rule("movie", 'input_boolean.x == "on"', "scene.movie")])
        engine.update_state("input_boolean.x", "on")
        engine.evaluate_all()
        assert len(engine.list_active_scene_intents()) == 1
        engine.update_state("input_boolean.x", "off")
        engine.evaluate_all()
        assert engine.list_active_scene_intents() == []

    def test_forced_scene_intent_ignores_when_until_ttl_expires(self) -> None:
        engine = Engine(clock_fn=lambda: 0)
        engine.load_rules([_scene_rule("movie", 'input_boolean.x == "on"', "scene.movie")])
        engine.update_state("input_boolean.x", "off")
        intent = engine.activate_scene_rule("movie", ttl_ms=1000)

        assert intent is not None
        assert intent.rule_id == "movie"
        assert intent.ignore_when is True

        engine.evaluate_all()
        assert engine.list_active_scene_intents() == ["scene.movie"]

        engine.advance_clock(1000)
        engine.evaluate_all()
        assert engine.list_active_scene_intents() == []

    def test_forced_scene_intent_uses_rule_ttl_by_default(self) -> None:
        engine = Engine(clock_fn=lambda: 0)
        engine.load_rules([
            _scene_rule(
                "movie",
                'input_boolean.x == "on"',
                "scene.movie",
                ttl_ms=5000,
            )
        ])

        intent = engine.activate_scene_rule("movie")

        assert intent is not None
        assert intent.ttl_ms == 5000

    def test_forced_scene_intent_rejects_missing_or_non_scene_rule(self) -> None:
        engine = Engine()
        engine.load_rules([
            _target_rule("light-rule", 'input_boolean.x == "on"', "light.x")
        ])

        assert engine.activate_scene_rule("missing") is None
        assert engine.activate_scene_rule("light-rule") is None


class TestMixedSceneAndTargetRules:
    def test_scene_and_target_rules_coexist(self) -> None:
        """A scene rule and a target rule with the same trigger both fire."""
        engine = Engine()
        engine.load_rules([
            _scene_rule("movie", 'input_boolean.x == "on"', "scene.movie"),
            _target_rule("cap", 'input_boolean.x == "on"', "light.living_room",
                         cap={"brightness_pct": 50}),
        ])
        engine.update_state("input_boolean.x", "on")
        engine.evaluate_all()
        # Both should be active
        assert engine.list_active_scene_intents() == ["scene.movie"]
        target_intents = engine.list_active_intents("light.living_room")
        assert len(target_intents) == 1
        assert target_intents[0].rule_id == "cap"

    def test_scene_rule_with_modifier_target_does_not_conflict(self) -> None:
        """Scene sets light via scene.turn_on; modifier rule caps that light.
        The two never meet in the compositor — but both fire from the same
        trigger."""
        engine = Engine()
        engine.load_rules([
            _scene_rule("movie", 'input_boolean.x == "on"', "scene.movie"),
            _target_rule("cap-lights", 'input_boolean.x == "on"', "light.living_room",
                         cap={"brightness_pct": 30}),
        ])
        engine.update_state("input_boolean.x", "on")
        engine.evaluate_all()
        # Scene: list says "activate scene.movie"
        # Cap rule: list says "cap light.living_room to 30%"
        # The cap's effect won't be visible until AFTER the scene runs,
        # so the integration layer must apply the cap after scene.turn_on
        # in a subsequent tick. (Implementation detail — see integration tests.)
        assert engine.list_active_scene_intents() == ["scene.movie"]
        cap_intents = engine.list_active_intents("light.living_room")
        assert len(cap_intents) == 1


class TestSceneActivationPlanning:
    def test_scene_activation_plan_returns_new_scene_service_call(self) -> None:
        from intentional.ha_adapter import scene_activation_plan

        engine = Engine()
        engine.load_rules([
            _scene_rule(
                "movie",
                'input_boolean.x == "on"',
                "scene.movie",
                transition_ms=3000,
            )
        ])
        engine.update_state("input_boolean.x", "on")
        engine.evaluate_all()

        calls, active, no_longer_active = scene_activation_plan(engine, set())

        assert calls == (
            (
                "scene",
                "turn_on",
                {"entity_id": "scene.movie", "transition": 3.0},
            ),
        )
        assert active == {"scene.movie"}
        assert no_longer_active == set()

    def test_scene_activation_plan_suppresses_already_active_scene(self) -> None:
        from intentional.ha_adapter import scene_activation_plan

        engine = Engine()
        engine.load_rules([
            _scene_rule("movie", 'input_boolean.x == "on"', "scene.movie")
        ])
        engine.update_state("input_boolean.x", "on")
        engine.evaluate_all()

        calls, active, no_longer_active = scene_activation_plan(
            engine,
            {"scene.movie"},
        )

        assert calls == ()
        assert active == {"scene.movie"}
        assert no_longer_active == set()

    def test_scene_activation_plan_reports_deactivated_scene(self) -> None:
        from intentional.ha_adapter import scene_activation_plan

        engine = Engine()

        calls, active, no_longer_active = scene_activation_plan(
            engine,
            {"scene.movie"},
        )

        assert calls == ()
        assert active == set()
        assert no_longer_active == {"scene.movie"}


class TestSceneRuleReload:
    def test_loading_scene_rule_replaces_previous(self) -> None:
        engine = Engine()
        engine.load_rules([_scene_rule("a", 'sensor.x == "on"', "scene.a")])
        engine.update_state("sensor.x", "on")
        engine.evaluate_all()
        assert engine.list_active_scene_intents() == ["scene.a"]
        # Reload with a different scene
        engine.load_rules([_scene_rule("b", 'sensor.x == "on"', "scene.b")])
        engine.evaluate_all()
        assert engine.list_active_scene_intents() == ["scene.b"]


class TestSceneRuleDiagnostics:
    def test_explain_works_for_scene_rules(self) -> None:
        engine = Engine()
        engine.load_rules([
            _scene_rule("movie", 'input_boolean.x == "on"', "scene.movie",
                        reason="Movie time"),
        ])
        engine.update_state("input_boolean.x", "on")
        engine.evaluate_all()
        explanation = engine.explain_scenes()
        assert "scene.movie" in explanation
        assert "movie" in explanation
        assert "Movie time" in explanation
