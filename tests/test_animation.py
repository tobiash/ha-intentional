"""Tests for the animation system.

Animations are time-varying intent values. The compositor resolves to a
static baseline; when the winning intent has an animation, the engine
uses AnimationSpec to compute a per-frame value during the tick loop.

The four supported kinds:
- pulse: discrete list of values, linear-interpolated, looped N times or forever
- breath: smooth sine-wave between min and max
- cycle: smooth oscillation across a list of values
- flash: single bright spike with exponential decay

These tests cover the math of each kind, parameter validation, and the
end-of-animation signaling.
"""

from __future__ import annotations

import pytest

from intentional.animation import AnimationFrame, AnimationSpec

# ── AnimationSpec construction and validation ────────────────────────


class TestAnimationSpecConstruction:
    def test_pulse_minimal(self) -> None:
        spec = AnimationSpec(
            kind="pulse", parameter="brightness_pct",
            values=[0, 100, 0], duration_ms=2000,
        )
        assert spec.kind == "pulse"
        assert spec.parameter == "brightness_pct"
        assert spec.values == [0, 100, 0]
        assert spec.duration_ms == 2000
        assert spec.repeat == 1
        assert spec.easing == "linear"

    def test_all_kinds_accepted(self) -> None:
        for kind, kwargs in [
            ("pulse", {"values": [0, 100], "duration_ms": 1000}),
            ("breath", {"min": 0, "max": 100, "period_ms": 1000}),
            ("cycle", {"values": [0, 100], "period_ms": 1000}),
            ("flash", {"peak": 100, "decay_ms": 500}),
        ]:
            spec = AnimationSpec(kind=kind, parameter="x", **kwargs)
            assert spec.kind == kind

    def test_unknown_kind_raises(self) -> None:
        with pytest.raises(ValueError):
            AnimationSpec(kind="wiggle", parameter="x")

    def test_negative_duration_rejected(self) -> None:
        with pytest.raises(ValueError):
            AnimationSpec(kind="pulse", parameter="x", values=[0, 100], duration_ms=-100)

    def test_zero_duration_rejected(self) -> None:
        with pytest.raises(ValueError):
            AnimationSpec(kind="pulse", parameter="x", values=[0, 100], duration_ms=0)

    def test_repeat_must_be_positive_or_forever(self) -> None:
        with pytest.raises(ValueError):
            AnimationSpec(kind="pulse", parameter="x", values=[0, 100], duration_ms=100, repeat=0)
        with pytest.raises(ValueError):
            AnimationSpec(kind="pulse", parameter="x", values=[0, 100], duration_ms=100, repeat=-1)

    def test_repeat_forever_is_valid(self) -> None:
        spec = AnimationSpec(kind="pulse", parameter="x", values=[0, 100], duration_ms=1000, repeat="forever")
        assert spec.repeat == "forever"

    def test_unknown_easing_rejected(self) -> None:
        with pytest.raises(ValueError):
            AnimationSpec(kind="pulse", parameter="x", values=[0, 100], duration_ms=100, easing="bouncy")


# ── Pulse animation: linear interpolation through values ─────────────


class TestPulseAnimation:
    def test_pulse_returns_first_value_at_t_zero(self) -> None:
        spec = AnimationSpec(
            kind="pulse", parameter="brightness_pct",
            values=[0, 100, 0], duration_ms=3000, easing="linear",
        )
        frame = spec.evaluate(t_ms=0)
        assert frame.value == 0
        assert frame.finished is False

    def test_pulse_returns_middle_value_at_third_of_duration(self) -> None:
        """Linear interpolation: at 1/3 duration, position 0.333 maps to
        value index 0.667, lerp(0, 100, 0.667) ≈ 67."""
        spec = AnimationSpec(
            kind="pulse", parameter="brightness_pct",
            values=[0, 100, 0], duration_ms=3000, easing="linear",
        )
        frame = spec.evaluate(t_ms=1000)
        assert frame.value == pytest.approx(66.67, abs=0.1)

    def test_pulse_returns_last_value_at_end_of_duration(self) -> None:
        spec = AnimationSpec(
            kind="pulse", parameter="brightness_pct",
            values=[0, 100, 0], duration_ms=3000, easing="linear",
        )
        frame = spec.evaluate(t_ms=3000)
        assert frame.value == 0

    def test_pulse_repeats_with_repeat_count(self) -> None:
        spec = AnimationSpec(
            kind="pulse", parameter="brightness_pct",
            values=[0, 100, 0], duration_ms=2000, repeat=3, easing="linear",
        )
        # After 3 full cycles (6000ms), animation finishes
        assert spec.evaluate(t_ms=6000).finished is True
        # Mid-second-cycle: should be near peak (100)
        assert spec.evaluate(t_ms=3000).value == pytest.approx(100, abs=0.1)

    def test_pulse_forever_never_finishes(self) -> None:
        spec = AnimationSpec(
            kind="pulse", parameter="brightness_pct",
            values=[0, 100, 0], duration_ms=2000, repeat="forever",
        )
        assert spec.evaluate(t_ms=10_000_000).finished is False


# ── Breath animation: smooth sine between min and max ────────────────


class TestBreathAnimation:
    def test_breath_min_at_start(self) -> None:
        spec = AnimationSpec(
            kind="breath", parameter="brightness_pct",
            min=10, max=80, period_ms=4000,
        )
        frame = spec.evaluate(t_ms=0)
        assert frame.value == pytest.approx(10, abs=0.01)

    def test_breath_max_at_quarter_period(self) -> None:
        spec = AnimationSpec(
            kind="breath", parameter="brightness_pct",
            min=10, max=80, period_ms=4000,
        )
        frame = spec.evaluate(t_ms=1000)
        assert frame.value == pytest.approx(80, abs=0.01)

    def test_breath_mid_at_half_period(self) -> None:
        spec = AnimationSpec(
            kind="breath", parameter="brightness_pct",
            min=10, max=80, period_ms=4000,
        )
        frame = spec.evaluate(t_ms=2000)
        assert frame.value == pytest.approx(10, abs=0.01)

    def test_breath_period_oscillation_is_smooth(self) -> None:
        """Values at successive 100ms intervals should not jump wildly."""
        spec = AnimationSpec(
            kind="breath", parameter="brightness_pct",
            min=0, max=100, period_ms=2000,
        )
        values = [spec.evaluate(t_ms=t).value for t in range(0, 2000, 100)]
        for i in range(1, len(values)):
            # Threshold chosen to allow the maximum rate of change at the
            # steepest part of the sine wave (around the zero-crossing).
            # With 100ms steps on a 1000ms half-period (2000ms visible
            # period), the peak rate of change is ~31.5 per 100ms.
            assert abs(values[i] - values[i - 1]) < 32


# ── Cycle animation: ping-pong through values with sine easing ───────


class TestCycleAnimation:
    def test_cycle_first_value_at_t_zero(self) -> None:
        spec = AnimationSpec(
            kind="cycle", parameter="color_temp_k",
            values=[2200, 6500], period_ms=2000,
        )
        assert spec.evaluate(t_ms=0).value == pytest.approx(2200, abs=1)

    def test_cycle_through_two_values(self) -> None:
        spec = AnimationSpec(
            kind="cycle", parameter="color_temp_k",
            values=[2200, 6500], period_ms=2000,
        )
        # At half-period, peak at the second value
        frame = spec.evaluate(t_ms=1000)
        assert frame.value == pytest.approx(6500, abs=1)

    def test_cycle_returns_to_start_at_full_period(self) -> None:
        spec = AnimationSpec(
            kind="cycle", parameter="color_temp_k",
            values=[2200, 6500], period_ms=2000,
        )
        assert spec.evaluate(t_ms=2000).value == pytest.approx(2200, abs=1)


# ── Flash animation: single spike with decay ─────────────────────────


class TestFlashAnimation:
    def test_flash_peak_at_start(self) -> None:
        spec = AnimationSpec(
            kind="flash", parameter="brightness_pct",
            peak=100, decay_ms=800,
        )
        frame = spec.evaluate(t_ms=0)
        assert frame.value == 100

    def test_flash_decays_to_zero(self) -> None:
        spec = AnimationSpec(
            kind="flash", parameter="brightness_pct",
            peak=100, decay_ms=800,
        )
        # After decay time, value should be near 0
        frame = spec.evaluate(t_ms=800)
        assert frame.value < 5

    def test_flash_finishes_after_decay(self) -> None:
        spec = AnimationSpec(
            kind="flash", parameter="brightness_pct",
            peak=100, decay_ms=500, repeat=1,
        )
        assert spec.evaluate(t_ms=400).finished is False
        assert spec.evaluate(t_ms=500).finished is True
        assert spec.evaluate(t_ms=10_000).finished is True


# ── AnimationFrame ───────────────────────────────────────────────────


class TestAnimationFrame:
    def test_frame_holds_value_and_finished_flag(self) -> None:
        f = AnimationFrame(value=42, finished=False)
        assert f.value == 42
        assert f.finished is False

    def test_frame_is_named_tuple(self) -> None:
        """AnimationFrame is a NamedTuple — supports tuple unpacking."""
        f = AnimationFrame(value=10, finished=True)
        v, done = f
        assert v == 10
        assert done is True
