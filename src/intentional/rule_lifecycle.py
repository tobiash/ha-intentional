"""Rule lifecycle projection helpers.

This module owns the public vocabulary for authored-rule lifecycle phases and
timing summaries. Engine owns the runtime state; this module keeps the phase
ordering and optional timing merge rules local and reusable.
"""

PHASE_IDLE = "idle"
PHASE_WAITING = "waiting"
PHASE_ACTIVE = "active"
PHASE_HELD = "held"
PHASE_LINGERING = "lingering"

_PHASE_ORDER = {
    PHASE_ACTIVE: 4,
    PHASE_HELD: 3,
    PHASE_LINGERING: 2,
    PHASE_WAITING: 1,
    PHASE_IDLE: 0,
}


def rule_phase(
    rule_id: str,
    *,
    firing: set[str],
    for_remaining: dict[str, int],
    active_counts: dict[str, int],
    lingering_rules: set[str],
) -> str:
    """Return the lifecycle phase for one authored or expanded rule."""
    if rule_id in for_remaining:
        return PHASE_WAITING
    if rule_id in firing:
        return PHASE_ACTIVE
    if active_counts.get(rule_id, 0) <= 0:
        return PHASE_IDLE
    if rule_id in lingering_rules:
        return PHASE_LINGERING
    return PHASE_HELD


def dominant_phase(left: str, right: str) -> str:
    """Return the higher-leverage phase when grouping expanded rules."""
    return left if _PHASE_ORDER.get(left, 0) >= _PHASE_ORDER.get(right, 0) else right


def min_optional(left: int | None, right: int | None) -> int | None:
    """Return the lower present value, preserving None when neither side exists."""
    values = [value for value in (left, right) if value is not None]
    return min(values) if values else None
