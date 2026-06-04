"""The compositor: resolves a set of active intents into a single ResolvedIntent.

This is the heart of ha-intentional. Given a list of intents for a target
entity, the compositor produces the final value to apply, applying the
following rules in order:

1. **set**: The highest-priority intent's `set` is the baseline. Lower
   intents can only contribute fields if they have `merge: true` AND the
   winning intent didn't set that field.
2. **cap**: All caps apply (smallest cap wins, clamps from above).
3. **floor**: All floors apply (largest floor wins, clamps from below).
4. **offset**: All offsets sum and apply to the baseline.
5. **multiply**: All multiplies apply *once each* to the post-offset value.
   Two `multiply: 0.5` rules produce 0.25, not 0.5. (Counterintuitive; we
   considered stacking but settled on "no compounding" for predictability.)
6. **Device bounds**: Hard physical limits (e.g. brightness 0-100) clamp
   the result.
7. **cap/floor re-apply**: After device bounds, caps and floors are applied
   once more, so e.g. a multiplier that pushed past 100 still gets caught
   by a cap of 80.

The compositor is a pure function — no I/O, no state, no async. This makes
it trivially testable. The engine wraps it with lifecycle and state.

Authority ordering:
- Authority (sensor < automation < user) is the primary sort key.
- Confidence is the secondary key within an authority.
- created_at is the tertiary key.
- id(intent) is the final tiebreaker to guarantee total ordering.

A note on cap vs floor vs set vs modifiers: the design is that `set`
*competes* (winner takes all per field, unless merge=True), but
cap/floor/offset/multiply *accumulate* across all intents. This is the
key insight that makes modifier-based rules feel intuitive: multiple
rules can each contribute "this should be capped at X" or "this should
be dimmed by 20" without overriding each other.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .animation import AnimationSpec
from .intent import Intent

# Device-specific physical bounds applied after modifiers.
# Fields not in this table pass through unclamped.
DEVICE_BOUNDS: dict[str, tuple[float, float]] = {
    "brightness_pct": (0, 100),
    "brightness": (0, 255),       # HA native brightness scale
    "volume_level": (0, 1),
    "color_temp_k": (1000, 10000),  # sane range, devices vary
    "color_temp_mired": (50, 500),
}


@dataclass(frozen=True)
class ResolvedIntent:
    """The output of the compositor: a final value to apply, with metadata.

    Attributes
    ----------
    target
        The entity_id this resolved intent applies to.
    value
        The final field-value map to apply. Empty if no intents.
    winning_intent
        The intent whose `set` won the priority competition. Useful for
        "why is this on?" debugging and for selecting transition/easing/
        animation metadata.
    transition_ms, easing, animation
        Carried over from the winning intent. The engine uses these to
        apply the value with a smooth transition or run an animation.
    ttl_remaining_ms
        Milliseconds until the winning intent expires, or None if it has
        no TTL. The engine can use this to schedule a "graceful" exit.
    all_active_intents
        All intents that participated in the resolution (filtered for
        expiration and target match). Useful for diagnostics.
    """

    target: str
    value: dict[str, Any]
    winning_intent: Intent | None
    transition_ms: int = 0
    easing: str = "linear"
    animation: AnimationSpec | None = None
    ttl_remaining_ms: int | None = None
    all_active_intents: tuple[Intent, ...] = field(default_factory=tuple)


def resolve_intents(
    target: str,
    intents: list[Intent],
    *,
    into_the_future_ms: int | None = None,
) -> ResolvedIntent | None:
    """Resolve a set of active intents for a target into a ResolvedIntent.

    Returns None if there are no live intents for this target.

    Parameters
    ----------
    target
        The entity_id to resolve. Intents for other targets are ignored.
    intents
        The full set of active intents across all targets. The function
        filters internally by target and expiration.
    into_the_future_ms
        Optional clock override for testing. If provided, used as "now"
        for expiration checks. If None, uses real wall-clock time.
    """
    # Filter: target match + not expired
    active = [
        i for i in intents
        if i.target == target and not i.is_expired(into_the_future_ms=into_the_future_ms)
    ]
    if not active:
        return None

    # Pick the winner: highest authority → highest confidence → newest → id.
    # This is used for transition/easing/animation metadata.
    winner = max(active, key=lambda i: i.priority)

    # Build the baseline using PER-FIELD priority selection. For each
    # field, the highest-priority intent that explicitly sets it contributes
    # its value. This means:
    # - User intent sets brightness_pct=100, TV intent sets color_temp_k=2700
    # - Result: brightness_pct=100 (user wins) AND color_temp_k=2700
    #   (TV wins, user doesn't contest it)
    # - If two intents set the same field, higher priority wins; ties break
    #   on confidence → recency → id.
    #
    # Note: the Intent.merge flag does NOT affect set-block merging. Set
    # blocks always merge per-field. The merge flag is reserved for
    # potential future behavior (e.g. disabling modifier composition).
    set_providers = [i for i in active if i.set]
    if not set_providers:
        baseline: dict[str, Any] = {}
    else:
        # Collect all fields anyone wants to set
        all_fields: set[str] = set()
        for intent in set_providers:
            all_fields.update(intent.set.keys())

        # For each field, find the highest-priority provider
        baseline = {}
        for field_name in all_fields:
            providers = [i for i in set_providers if field_name in i.set]
            chosen = max(providers, key=lambda i: i.priority)
            baseline[field_name] = chosen.set[field_name]

    # Apply cap: smallest cap wins
    result = dict(baseline)
    for intent in active:
        for field_name, cap_value in intent.cap.items():
            if field_name in result:
                result[field_name] = _min_clamp(result[field_name], cap_value)
            else:
                # If the field doesn't exist yet, the cap seeds it as a value.
                # This handles "ambient_max" rules that say "no value should
                # exceed 60%" without themselves setting a value.
                result[field_name] = cap_value

    # Apply floor: largest floor wins
    for intent in active:
        for field_name, floor_value in intent.floor.items():
            if field_name in result:
                result[field_name] = _max_clamp(result[field_name], floor_value)

    # Apply offset: all offsets sum
    for intent in active:
        for field_name, delta in intent.offset.items():
            if field_name in result:
                result[field_name] = _add(result[field_name], delta)

    # Apply multiply: each multiply applies once to the post-offset value
    for intent in active:
        for field_name, factor in intent.multiply.items():
            if field_name in result:
                result[field_name] = _multiply(result[field_name], factor)

    # Apply device bounds
    for field_name, (lo, hi) in DEVICE_BOUNDS.items():
        if field_name in result:
            result[field_name] = _clamp(result[field_name], lo, hi)

    # Re-apply caps and floors once more, in case bounds let values through
    # that the caps/floors should still catch.
    for intent in active:
        for field_name, cap_value in intent.cap.items():
            if field_name in result:
                result[field_name] = _min_clamp(result[field_name], cap_value)
        for field_name, floor_value in intent.floor.items():
            if field_name in result:
                result[field_name] = _max_clamp(result[field_name], floor_value)

    # Compute TTL remaining for the winning intent
    ttl_remaining_ms: int | None = None
    if winner.ttl_ms is not None:
        now = into_the_future_ms if into_the_future_ms is not None else _now_ms()
        expires_at = winner.created_at_ms + winner.ttl_ms
        ttl_remaining_ms = max(0, expires_at - now)

    return ResolvedIntent(
        target=target,
        value=result,
        winning_intent=winner,
        transition_ms=winner.transition_ms,
        easing=winner.easing,
        animation=winner.animation,
        ttl_remaining_ms=ttl_remaining_ms,
        all_active_intents=tuple(active),
    )


# ── Numeric helpers ──────────────────────────────────────────────────


def _now_ms() -> int:
    """Return current time in milliseconds since the Unix epoch."""
    import time
    return int(time.time() * 1000)


def _min_clamp(current: Any, ceiling: Any) -> Any:
    """Clamp current to be at most ceiling. Supports numeric, list, and string."""
    if isinstance(current, (int, float)) and isinstance(ceiling, (int, float)):
        return min(current, ceiling)
    if isinstance(current, list) and isinstance(ceiling, list):
        return [_min_clamp(c, k) for c, k in zip(current, ceiling, strict=False)]
    return min(current, ceiling) if current <= ceiling else current


def _max_clamp(current: Any, floor: Any) -> Any:
    """Clamp current to be at least floor. Supports numeric, list, and string."""
    if isinstance(current, (int, float)) and isinstance(floor, (int, float)):
        return max(current, floor)
    if isinstance(current, list) and isinstance(floor, list):
        return [_max_clamp(c, f) for c, f in zip(current, floor, strict=False)]
    return max(current, floor) if current >= floor else current


def _clamp(value: Any, lo: float, hi: float) -> Any:
    """Clamp a value to [lo, hi]. Supports numeric and equal-length lists."""
    if isinstance(value, (int, float)):
        return max(lo, min(hi, value))
    if isinstance(value, list):
        return [_clamp(v, lo, hi) for v in value]
    return value


def _add(current: Any, delta: Any) -> Any:
    """Add a delta to a value. Supports numeric and equal-length lists."""
    if isinstance(current, (int, float)) and isinstance(delta, (int, float)):
        return current + delta
    if isinstance(current, list) and isinstance(delta, list):
        return [_add(c, d) for c, d in zip(current, delta, strict=False)]
    return current


def _multiply(value: Any, factor: Any) -> Any:
    """Multiply a value by a factor. Supports numeric and equal-length lists."""
    if isinstance(value, (int, float)) and isinstance(factor, (int, float)):
        return value * factor
    if isinstance(value, list) and isinstance(factor, list):
        return [_multiply(v, f) for v, f in zip(value, factor, strict=False)]
    return value
