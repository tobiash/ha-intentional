"""Tests for the compositor.

The compositor is the heart of ha-intentional. Given a set of active intents
for a target, it produces a ResolvedIntent: the final value to apply, with
metadata about how it was reached.

These tests cover the resolution rules:
- Single intent: pass through
- set: highest-priority intent wins, others ignored (unless merge=True)
- cap: smallest cap wins (clamps from above)
- floor: largest floor wins (clamps from below)
- offset: all offsets sum
- multiply: all multiplies apply once to the offset-adjusted value (no compounding)
- Authority beats confidence beats recency
- merge=True allows lower-priority intents to fill in unset fields
- Expired intents are ignored
- Empty intent set returns None
- Device bounds (0-100 brightness etc.) clamp results
- Animations transfer from the winning intent
- TTL information is preserved
- Multiple targets resolve independently
"""

from __future__ import annotations

from intentional.compositor import (
    resolve_intents,
)
from intentional.intent import Authority, Intent

# ── Empty / single-intent cases ──────────────────────────────────────


class TestEmptyCases:
    def test_no_intents_returns_none(self) -> None:
        assert resolve_intents("light.x", []) is None

    def test_single_intent_passes_through(self) -> None:
        intent = Intent(target="light.x", set={"brightness_pct": 50})
        result = resolve_intents("light.x", [intent])
        assert result is not None
        assert result.value == {"brightness_pct": 50}
        assert result.winning_intent is intent

    def test_intents_for_other_target_are_ignored(self) -> None:
        intent = Intent(target="light.y", set={"brightness_pct": 50})
        assert resolve_intents("light.x", [intent]) is None

    def test_expired_intents_are_filtered(self) -> None:
        old = Intent(
            target="light.x", set={"brightness_pct": 50}, ttl_ms=1000
        )
        # Pretend it's 2 seconds in the future
        result = resolve_intents(
            "light.x", [old], into_the_future_ms=old.created_at_ms + 2000
        )
        assert result is None


# ── set: highest-priority intent wins ────────────────────────────────


class TestSetResolution:
    def test_user_authority_beats_automation(self) -> None:
        auto = Intent(
            target="light.x",
            set={"brightness_pct": 80},
            authority=Authority.AUTOMATION,
        )
        user = Intent(
            target="light.x",
            set={"brightness_pct": 20},
            authority=Authority.USER,
        )
        result = resolve_intents("light.x", [auto, user])
        assert result is not None
        assert result.value == {"brightness_pct": 20}
        assert result.winning_intent is user

    def test_within_authority_higher_confidence_wins(self) -> None:
        weak = Intent(
            target="light.x",
            set={"brightness_pct": 30},
            authority=Authority.AUTOMATION,
            confidence=0.3,
        )
        strong = Intent(
            target="light.x",
            set={"brightness_pct": 90},
            authority=Authority.AUTOMATION,
            confidence=0.9,
        )
        result = resolve_intents("light.x", [weak, strong])
        assert result is not None
        assert result.value == {"brightness_pct": 90}
        assert result.winning_intent is strong

    def test_merge_true_allows_other_intents_to_fill_fields(self) -> None:
        """Per-field merge: intents that set different fields all contribute.
        The merge flag is a hint (see Intent docstring) but doesn't gate
        per-field merging — that's always on.
        """
        brightness = Intent(
            target="light.x",
            set={"brightness_pct": 80},
            authority=Authority.AUTOMATION,
        )
        color = Intent(
            target="light.x",
            set={"color_temp_k": 2700},
            authority=Authority.AUTOMATION,
        )
        # No overlap on fields, so both contribute
        result = resolve_intents("light.x", [brightness, color])
        assert result.value == {"brightness_pct": 80, "color_temp_k": 2700}
        assert result is not None
        assert result.value == {"brightness_pct": 80, "color_temp_k": 2700}

    def test_merge_does_not_override_winning_intent_fields(self) -> None:
        """Even with merge=True, a higher-priority field wins."""
        higher = Intent(
            target="light.x",
            set={"brightness_pct": 100},
            authority=Authority.USER,
            merge=True,
        )
        lower = Intent(
            target="light.x",
            set={"brightness_pct": 30, "color_temp_k": 2700},
            authority=Authority.AUTOMATION,
            merge=True,
        )
        result = resolve_intents("light.x", [higher, lower])
        assert result is not None
        assert result.value["brightness_pct"] == 100  # higher wins
        assert result.value["color_temp_k"] == 2700  # lower fills in


# ── cap: smallest cap wins ───────────────────────────────────────────


class TestCapResolution:
    def test_cap_clamps_above_to_minimum(self) -> None:
        intent = Intent(target="light.x", set={"brightness_pct": 100})
        cap = Intent(target="light.x", cap={"brightness_pct": 40})
        result = resolve_intents("light.x", [intent, cap])
        assert result is not None
        assert result.value == {"brightness_pct": 40}

    def test_cap_below_set_value_does_nothing(self) -> None:
        intent = Intent(target="light.x", set={"brightness_pct": 30})
        cap = Intent(target="light.x", cap={"brightness_pct": 100})
        result = resolve_intents("light.x", [intent, cap])
        assert result is not None
        assert result.value == {"brightness_pct": 30}

    def test_smallest_cap_wins(self) -> None:
        intent = Intent(target="light.x", set={"brightness_pct": 100})
        big_cap = Intent(target="light.x", cap={"brightness_pct": 80})
        small_cap = Intent(target="light.x", cap={"brightness_pct": 40})
        result = resolve_intents("light.x", [intent, big_cap, small_cap])
        assert result is not None
        assert result.value == {"brightness_pct": 40}


# ── floor: largest floor wins ────────────────────────────────────────


class TestFloorResolution:
    def test_floor_clamps_below_to_maximum(self) -> None:
        intent = Intent(target="light.x", set={"brightness_pct": 10})
        floor = Intent(target="light.x", floor={"brightness_pct": 30})
        result = resolve_intents("light.x", [intent, floor])
        assert result is not None
        assert result.value == {"brightness_pct": 30}

    def test_floor_above_set_value_does_nothing(self) -> None:
        intent = Intent(target="light.x", set={"brightness_pct": 80})
        floor = Intent(target="light.x", floor={"brightness_pct": 10})
        result = resolve_intents("light.x", [intent, floor])
        assert result is not None
        assert result.value == {"brightness_pct": 80}

    def test_largest_floor_wins(self) -> None:
        intent = Intent(target="light.x", set={"brightness_pct": 0})
        small = Intent(target="light.x", floor={"brightness_pct": 5})
        large = Intent(target="light.x", floor={"brightness_pct": 30})
        result = resolve_intents("light.x", [intent, small, large])
        assert result is not None
        assert result.value == {"brightness_pct": 30}


# ── offset: all offsets sum ──────────────────────────────────────────


class TestOffsetResolution:
    def test_offset_subtracts(self) -> None:
        intent = Intent(target="light.x", set={"brightness_pct": 80})
        offset = Intent(target="light.x", offset={"brightness_pct": -20})
        result = resolve_intents("light.x", [intent, offset])
        assert result is not None
        assert result.value == {"brightness_pct": 60}

    def test_multiple_offsets_sum(self) -> None:
        intent = Intent(target="light.x", set={"brightness_pct": 100})
        o1 = Intent(target="light.x", offset={"brightness_pct": -20})
        o2 = Intent(target="light.x", offset={"brightness_pct": -10})
        result = resolve_intents("light.x", [intent, o1, o2])
        assert result is not None
        assert result.value == {"brightness_pct": 70}


# ── multiply: applied once after offset, no compounding ──────────────


class TestMultiplyResolution:
    def test_multiply_scales(self) -> None:
        intent = Intent(target="light.x", set={"brightness_pct": 100})
        mult = Intent(target="light.x", multiply={"brightness_pct": 0.5})
        result = resolve_intents("light.x", [intent, mult])
        assert result is not None
        assert result.value == {"brightness_pct": 50}

    def test_two_multiplies_apply_once_each(self) -> None:
        """0.6 * 0.5 = 0.3, not 0.6 * 0.6 = 0.36. We multiply each factor once."""
        intent = Intent(target="light.x", set={"brightness_pct": 100})
        m1 = Intent(target="light.x", multiply={"brightness_pct": 0.6})
        m2 = Intent(target="light.x", multiply={"brightness_pct": 0.5})
        result = resolve_intents("light.x", [intent, m1, m2])
        assert result is not None
        assert result.value == {"brightness_pct": 30}  # 100 * 0.6 * 0.5


# ── Composition order: cap → floor → offset → multiply → device bounds ──


class TestCompositionOrder:
    def test_cap_then_floor(self) -> None:
        """Cap above the floor, both apply."""
        intent = Intent(target="light.x", set={"brightness_pct": 50})
        cap = Intent(target="light.x", cap={"brightness_pct": 80})
        floor = Intent(target="light.x", floor={"brightness_pct": 20})
        result = resolve_intents("light.x", [intent, cap, floor])
        assert result is not None
        # 50 -> cap to 80 (no change) -> floor to 20 (no change) = 50
        assert result.value == {"brightness_pct": 50}

    def test_offset_applies_to_set(self) -> None:
        intent = Intent(target="light.x", set={"brightness_pct": 50})
        offset = Intent(target="light.x", offset={"brightness_pct": 10})
        cap = Intent(target="light.x", cap={"brightness_pct": 100})
        result = resolve_intents("light.x", [intent, offset, cap])
        assert result is not None
        # 50 -> offset to 60 -> cap to 100 (no change) = 60
        assert result.value == {"brightness_pct": 60}

    def test_offset_then_multiply(self) -> None:
        intent = Intent(target="light.x", set={"brightness_pct": 100})
        offset = Intent(target="light.x", offset={"brightness_pct": -20})
        mult = Intent(target="light.x", multiply={"brightness_pct": 0.5})
        result = resolve_intents("light.x", [intent, offset, mult])
        assert result is not None
        # 100 -> offset to 80 -> multiply by 0.5 = 40
        assert result.value == {"brightness_pct": 40}


# ── Device bounds ────────────────────────────────────────────────────


class TestDeviceBounds:
    def test_brightness_clamped_to_0_100(self) -> None:
        # If a rule author makes a mistake, we still clamp
        intent = Intent(target="light.x", set={"brightness_pct": 200})
        result = resolve_intents("light.x", [intent])
        assert result is not None
        assert result.value["brightness_pct"] == 100

        intent2 = Intent(target="light.x", set={"brightness_pct": -50})
        result2 = resolve_intents("light.x", [intent2])
        assert result2 is not None
        assert result2.value["brightness_pct"] == 0

    def test_caps_and_floors_re_apply_after_device_clamp(self) -> None:
        """If a multiplier pushed something out of bounds, the cap/floor catches it."""
        intent = Intent(target="light.x", set={"brightness_pct": 100})
        mult = Intent(target="light.x", multiply={"brightness_pct": 1.5})  # would be 150
        cap = Intent(target="light.x", cap={"brightness_pct": 80})
        result = resolve_intents("light.x", [intent, mult, cap])
        assert result is not None
        # 100 * 1.5 = 150 -> device clamp 100 -> cap 80 = 80
        assert result.value["brightness_pct"] == 80

    def test_unknown_field_passes_through(self) -> None:
        """Fields without a known bound are not clamped."""
        intent = Intent(target="light.x", set={"rgb_color": [255, 0, 128]})
        result = resolve_intents("light.x", [intent])
        assert result is not None
        assert result.value == {"rgb_color": [255, 0, 128]}


# ── The full TV scenario ─────────────────────────────────────────────


class TestTVScenario:
    """The classic scenario from the README: TV turns on, light dims."""

    def test_user_manual_set_capped_by_tv_rule(self) -> None:
        # User sets 100% manually
        user = Intent(
            target="light.living_room",
            set={"brightness_pct": 100},
            authority=Authority.USER,
        )
        # TV rule: cap at 40, set color temp
        tv = Intent(
            target="light.living_room",
            cap={"brightness_pct": 40},
            set={"color_temp_k": 2700},
            authority=Authority.AUTOMATION,
        )
        result = resolve_intents("light.living_room", [user, tv])
        assert result is not None
        # User's set wins for set: brightness=100, color_temp_k=2700
        # Then cap clamps brightness to 40
        assert result.value == {"brightness_pct": 40, "color_temp_k": 2700}

    def test_tv_off_restores_user_value(self) -> None:
        """When TV intent expires, user value is restored."""
        user = Intent(
            target="light.living_room",
            set={"brightness_pct": 100},
            authority=Authority.USER,
        )
        tv_expired = Intent(
            target="light.living_room",
            cap={"brightness_pct": 40},
            set={"color_temp_k": 2700},
            authority=Authority.AUTOMATION,
            ttl_ms=1000,
        )
        # Resolving with only the unexpired intents:
        result = resolve_intents(
            "light.living_room",
            [user, tv_expired],
            into_the_future_ms=tv_expired.created_at_ms + 2000,
        )
        assert result is not None
        # TV is filtered out → user value 100, no cap
        assert result.value == {"brightness_pct": 100}


# ── Animation and transition metadata ────────────────────────────────


class TestResolvedMetadata:
    def test_transition_from_winning_intent(self) -> None:
        intent = Intent(
            target="light.x",
            set={"brightness_pct": 50},
            transition_ms=1500,
            easing="ease-in-out",
        )
        result = resolve_intents("light.x", [intent])
        assert result is not None
        assert result.transition_ms == 1500
        assert result.easing == "ease-in-out"

    def test_animation_spec_preserved(self) -> None:
        from intentional.animation import AnimationSpec

        anim = AnimationSpec(kind="pulse", parameter="brightness_pct", values=[0, 100, 0], duration_ms=2000, repeat=4, easing="sine")
        intent = Intent(
            target="light.x",
            set={"brightness_pct": 50},
            animation=anim,
        )
        result = resolve_intents("light.x", [intent])
        assert result is not None
        assert result.animation is anim

    def test_no_intent_metadata_means_defaults(self) -> None:
        intent = Intent(target="light.x", set={"brightness_pct": 50})
        result = resolve_intents("light.x", [intent])
        assert result is not None
        assert result.transition_ms == 0
        assert result.easing == "linear"
        assert result.animation is None

    def test_ttl_remaining_reported(self) -> None:
        intent = Intent(
            target="light.x",
            set={"brightness_pct": 50},
            ttl_ms=10_000,
        )
        result = resolve_intents(
            "light.x", [intent], into_the_future_ms=intent.created_at_ms + 3000
        )
        assert result is not None
        # ~7000ms remaining (some clock drift tolerated in test)
        assert result.ttl_remaining_ms is not None
        assert 6_500 <= result.ttl_remaining_ms <= 7_500


# ── ResolvedIntent structure ─────────────────────────────────────────


class TestResolvedIntentStructure:
    def test_repr_includes_target(self) -> None:
        intent = Intent(target="light.x", set={"brightness_pct": 50})
        result = resolve_intents("light.x", [intent])
        assert result is not None
        assert "light.x" in repr(result)

    def test_target_field_set(self) -> None:
        intent = Intent(target="light.living_room", set={"brightness_pct": 50})
        result = resolve_intents("light.living_room", [intent])
        assert result is not None
        assert result.target == "light.living_room"

    def test_winning_intent_attribute(self) -> None:
        auto = Intent(
            target="light.x", set={"brightness_pct": 30}, authority=Authority.AUTOMATION
        )
        user = Intent(
            target="light.x", set={"brightness_pct": 80}, authority=Authority.USER
        )
        result = resolve_intents("light.x", [auto, user])
        assert result is not None
        assert result.winning_intent is user
