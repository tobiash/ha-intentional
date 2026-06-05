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

from typing import Any

from intentional.engine import Engine
from intentional.intent import Authority
from intentional.yaml_loader import Rule


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


# ── Rule evaluation ──────────────────────────────────────────────────


class TestRuleEvaluation:
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
        engine.load_rules([
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
        ])
        engine.update_state("input_boolean.movie", "on")
        engine.update_state("sensor.dark", "on")

        engine.evaluate_all()

        active_rule_ids = {
            intent.rule_id for intent in engine.list_active_intents("light.room")
        }
        assert active_rule_ids == {"movie-mode"}
        resolved = engine.resolve("light.room")
        assert resolved is not None
        assert resolved.value == {"brightness_pct": 20}

    def test_blocks_withdraws_previously_active_intent(self) -> None:
        engine = Engine()
        engine.load_rules([
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
        ])
        engine.update_state("sensor.dark", "on")
        engine.evaluate_all()
        assert {
            intent.rule_id for intent in engine.list_active_intents("light.room")
        } == {"ambient"}

        engine.update_state("input_boolean.movie", "on")
        engine.evaluate_all()

        assert {
            intent.rule_id for intent in engine.list_active_intents("light.room")
        } == {"movie-mode"}


# ── Resolution ───────────────────────────────────────────────────────


class TestResolution:
    def test_resolve_combines_active_intents(self) -> None:
        engine = Engine()
        engine.load_rules([
            _rule("dark", 'sensor.x.state == "on"', set={"brightness_pct": 80}),
            _rule("focus", 'sensor.y.state == "on"', cap={"brightness_pct": 40}),
        ])
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
        engine.load_rules([
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
        ])
        engine.update_state("sensor.low", "on")
        engine.update_state("sensor.high", "on")
        engine.evaluate_all()

        explanation = engine.explain_target("light.x")

        assert explanation["resolved"]["value"] == {"brightness_pct": 80}
        assert explanation["active_intents"][0]["rule_id"] == "high-priority"
        assert explanation["active_intents"][1]["rule_id"] == "low-priority"
        assert explanation["winning_intent"]["rule_id"] == "high-priority"

    def test_explain_target_reports_blocked_firing_rules(self) -> None:
        engine = Engine()
        engine.load_rules([
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
        ])
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
            },
            {
                "rule_id": "ambient",
                "firing": False,
                "condition_firing": True,
                "blocked_by": ["movie-mode"],
                "for_remaining_ms": None,
            },
        ]
        assert explanation["winning_intent"]["rule_id"] == "movie-mode"

    def test_explain_target_reports_for_remaining(self) -> None:
        engine = Engine(clock_fn=lambda: 0)
        engine.load_rules([
            _rule(
                "motion-held",
                'binary_sensor.motion == "on"',
                target="light.hall",
                for_ms=5_000,
                set={"state": "on"},
            )
        ])
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
            }
        ]


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


# ── Diagnostics ─────────────────────────────────────────────────────


class TestDiagnostics:
    def test_public_counts_and_target_lists_exclude_expired_intents(self) -> None:
        engine = Engine(clock_fn=lambda: 1000)
        engine.load_rules([
            _rule("light-rule", 'sensor.x.state == "on"', target="light.x"),
            _rule("switch-rule", 'sensor.y.state == "on"', target="switch.y"),
        ])
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
        engine.load_rules([
            _rule("dark", 'sensor.x.state == "on"', reason="Dark outside",
                  set={"brightness_pct": 80}),
        ])
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
