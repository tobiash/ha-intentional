"""Animation specifications for time-varying intents.

When a rule's `emit` block includes an `animation:` key, the winning intent
carries an AnimationSpec. The engine's tick loop calls spec.evaluate(t_ms)
each frame to compute the current value to apply to the target.

Four kinds are supported:
- pulse: discrete list of values, linear-interpolated, looped N times or forever
- breath: smooth sine-wave between min and max
- cycle: smooth oscillation through a list of values (like pulse but sinusoidal)
- flash: single bright spike that decays to zero

Each kind has kind-specific timing parameters:
- pulse: duration_ms (one traversal of values)
- breath: period_ms (one full sine cycle)
- cycle: period_ms (one full traversal)
- flash: decay_ms (time to reach zero)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, NamedTuple, Optional, Sequence, Union


class AnimationKind(str, Enum):
    PULSE = "pulse"
    BREATH = "breath"
    CYCLE = "cycle"
    FLASH = "flash"


# Easing functions — applied to the inter-value interpolation step.
_EASING_FUNCS = {
    "linear": lambda x: x,
    "ease-in": lambda x: x * x,
    "ease-out": lambda x: 1 - (1 - x) * (1 - x),
    "ease-in-out": lambda x: 3 * x * x - 2 * x * x * x,
    "sine": lambda x: 0.5 - 0.5 * math.cos(math.pi * x),
}


class AnimationFrame(NamedTuple):
    """A single frame of animation output.

    Attributes
    ----------
    value
        The computed value for the parameter at time t_ms. Type matches
        the parameter (e.g. float for brightness_pct).
    finished
        True if the animation has completed (all repeats done). The engine
        should stop calling evaluate() and remove the intent on next tick.
    """

    value: Any
    finished: bool


@dataclass(frozen=True)
class AnimationSpec:
    """A time-varying intent value.

    Attributes
    ----------
    kind
        One of: pulse, breath, cycle, flash.
    parameter
        The intent field to animate, e.g. "brightness_pct", "color_temp_k".
        Must match a key in the intent's `set` dict.
    values
        For pulse and cycle: list of values to interpolate through.
        Ignored for breath and flash.
    min, max
        For breath only: oscillation bounds. min should be < max.
    peak
        For flash only: the starting peak value (decays to 0).
    duration_ms
        For pulse: time to traverse the values list once. Must be positive.
    period_ms
        For breath and cycle: one full oscillation period. Must be positive.
    decay_ms
        For flash: time to decay from peak to 0. Must be positive.
    repeat
        For pulse and flash: how many cycles to perform. Must be a
        positive int or the string "forever". Breath and cycle are
        inherently continuous and ignore this field.
    easing
        Easing function for the inter-value interpolation step. One of:
        linear, ease-in, ease-out, ease-in-out, sine. Cycle always uses
        sine regardless of this setting.
    """

    kind: str
    parameter: str
    values: Sequence[Any] = field(default_factory=list)
    min: Optional[float] = None
    max: Optional[float] = None
    peak: Optional[float] = None
    duration_ms: int = 0
    period_ms: int = 0
    decay_ms: int = 0
    repeat: Union[int, str] = 1
    easing: str = "linear"

    def __post_init__(self) -> None:
        # Validate kind
        try:
            AnimationKind(self.kind)
        except ValueError as e:
            valid = ", ".join(k.value for k in AnimationKind)
            raise ValueError(
                f"Unknown animation kind {self.kind!r}. Must be one of: {valid}"
            ) from e

        # Validate kind-specific timing
        if self.kind == "pulse":
            self._require_positive("duration_ms", self.duration_ms)
        elif self.kind == "breath":
            self._require_positive("period_ms", self.period_ms)
            if self.min is None or self.max is None:
                raise ValueError("breath animation requires both `min` and `max`")
            if self.min >= self.max:
                raise ValueError(
                    f"breath: min ({self.min}) must be less than max ({self.max})"
                )
        elif self.kind == "cycle":
            self._require_positive("period_ms", self.period_ms)
            if not self.values:
                raise ValueError("cycle animation requires a non-empty `values` list")
        elif self.kind == "flash":
            self._require_positive("decay_ms", self.decay_ms)
            if self.peak is None:
                raise ValueError("flash animation requires a `peak` value")

        # Validate repeat
        if isinstance(self.repeat, str):
            if self.repeat != "forever":
                raise ValueError(
                    f"repeat must be a positive int or 'forever', got {self.repeat!r}"
                )
        elif self.repeat < 1:
            raise ValueError(
                f"repeat must be a positive int or 'forever', got {self.repeat}"
            )

        # Validate easing
        if self.easing not in _EASING_FUNCS:
            valid = ", ".join(sorted(_EASING_FUNCS.keys()))
            raise ValueError(
                f"Unknown easing {self.easing!r}. Must be one of: {valid}"
            )

    @staticmethod
    def _require_positive(name: str, value: int) -> None:
        if value <= 0:
            raise ValueError(f"{name} must be positive, got {value}")

    # ── Evaluation ──────────────────────────────────────────────────

    def evaluate(self, t_ms: int) -> AnimationFrame:
        """Compute the animation value at the given time in milliseconds.

        t_ms is measured from the start of the animation (typically
        computed by the engine as `now - animation_started_at_ms`).
        """
        if self.kind == "pulse":
            return self._evaluate_pulse(t_ms)
        if self.kind == "breath":
            return self._evaluate_breath(t_ms)
        if self.kind == "cycle":
            return self._evaluate_cycle(t_ms)
        if self.kind == "flash":
            return self._evaluate_flash(t_ms)
        # Defensive: __post_init__ should have caught this
        raise ValueError(f"Unhandled animation kind: {self.kind}")

    def _evaluate_pulse(self, t_ms: int) -> AnimationFrame:
        n = len(self.values)
        if n == 0:
            return AnimationFrame(value=0, finished=True)
        if n == 1:
            return AnimationFrame(
                value=self.values[0], finished=t_ms >= self.duration_ms
            )

        cycle_pos_ms = t_ms % self.duration_ms
        cycle_index = t_ms // self.duration_ms
        cycle_progress = cycle_pos_ms / self.duration_ms

        last_index = n - 1
        position = cycle_progress * last_index
        lower = int(position)
        upper = min(lower + 1, last_index)
        frac = position - lower
        eased_frac = _EASING_FUNCS[self.easing](frac)

        value = _interpolate(self.values[lower], self.values[upper], eased_frac)
        finished = self.repeat != "forever" and cycle_index >= self.repeat
        return AnimationFrame(value=value, finished=finished)

    def _evaluate_breath(self, t_ms: int) -> AnimationFrame:
        # period_ms is the time for a full min→max→min cycle (the *visible*
        # period), which equals two cycles of the underlying sine wave.
        # We use cos(4π·phase) so that at phase=0 we're at min, at 0.25
        # we're at max, at 0.5 we're back to min, etc.
        phase = (t_ms % self.period_ms) / self.period_ms
        wave = -math.cos(4 * math.pi * phase)
        value = self.min + (self.max - self.min) * (wave + 1) / 2
        return AnimationFrame(value=value, finished=False)

    def _evaluate_cycle(self, t_ms: int) -> AnimationFrame:
        n = len(self.values)
        if n == 0:
            return AnimationFrame(value=0, finished=True)
        if n == 1:
            return AnimationFrame(value=self.values[0], finished=False)
        # Ping-pong traversal: first half of the period goes through
        # values in order, second half goes back. Sine easing on each
        # segment so peaks land exactly on the values.
        half_period = self.period_ms / 2
        if t_ms < half_period:
            # Forward: values[0] → values[n-1]
            segment_pos = t_ms / half_period  # 0..1
            return self._traverse(self.values, segment_pos)
        else:
            # Reverse: values[n-1] → values[0]
            segment_pos = (t_ms - half_period) / half_period
            return self._traverse(list(reversed(self.values)), segment_pos)

    @staticmethod
    def _traverse(values: Sequence[Any], pos: float) -> AnimationFrame:
        """Map position [0, 1] to a value along a sequence with sine easing."""
        n = len(values)
        last_index = n - 1
        position = pos * last_index
        lower = int(position)
        upper = min(lower + 1, last_index)
        frac = position - lower
        eased = _EASING_FUNCS["sine"](frac)
        return AnimationFrame(
            value=_interpolate(values[lower], values[upper], eased),
            finished=False,
        )

    def _evaluate_flash(self, t_ms: int) -> AnimationFrame:
        if t_ms >= self.decay_ms:
            if self.repeat == "forever":
                return AnimationFrame(value=0, finished=False)
            flash_repeats = max(1, self.repeat)
            if t_ms >= self.decay_ms * flash_repeats:
                return AnimationFrame(value=0, finished=True)
            return AnimationFrame(value=0, finished=False)
        progress = t_ms / self.decay_ms
        eased = math.cos(math.pi * progress / 2)  # 1 → 0, ease-out
        value = self.peak * eased
        return AnimationFrame(value=value, finished=False)


def _interpolate(a: Any, b: Any, t: float) -> Any:
    """Linear interpolation between two values. Supports numbers and equal-length lists."""
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a + (b - a) * t
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        return [_interpolate(ax, bx, t) for ax, bx in zip(a, b)]
    return a if t == 0 else b
