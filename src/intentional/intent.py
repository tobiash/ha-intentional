"""Intent data model for ha-intentional.

An Intent is a claim about how a target entity should be, with priority
metadata. The compositor (see compositor.py) resolves conflicts between
multiple active intents for the same target.

Design principles:
- Intent is a frozen dataclass: once created, an intent is immutable.
  This makes them safe to share across coroutines and trivial to reason about.
- Authority and confidence are the primary sort keys. created_at is a
  deterministic tiebreaker so max() selection is stable.
- All timestamps are in milliseconds since the Unix epoch (matches HA).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from intentional.animation import AnimationSpec
    from intentional.generation import ValueGeneratorSpec


def _now_ms() -> int:
    """Return the current time in milliseconds since the Unix epoch."""
    return int(time.time() * 1000)


def _copy_field_dict(value: dict[str, Any] | None) -> dict[str, Any]:
    """Defensive copy of a per-field modifier dict, handling None."""
    return dict(value) if value is not None else {}


class Authority(Enum):
    """Priority tier of an intent. Higher value = higher priority.

    The three tiers map to common sources of intent in home automation:
    - SENSOR: raw sensor readings (light level, temperature, time of day)
    - AUTOMATION: rules and scripts reacting to sensor or device state
    - USER: explicit user action (button press, voice command, dashboard toggle)

    Authorities are ordered: SENSOR < AUTOMATION < USER. This ordering is
    baked into the enum and used directly for intent comparison.
    """

    SENSOR = "sensor"
    AUTOMATION = "automation"
    USER = "user"

    @property
    def value_index(self) -> int:
        """Integer value suitable for comparison. Higher = more authority."""
        order = {"sensor": 10, "automation": 50, "user": 100}
        return order[self.value]

    def __lt__(self, other: Authority) -> bool:
        if not isinstance(other, Authority):
            return NotImplemented
        return self.value_index < other.value_index

    def __le__(self, other: Authority) -> bool:
        if not isinstance(other, Authority):
            return NotImplemented
        return self.value_index <= other.value_index

    def __gt__(self, other: Authority) -> bool:
        if not isinstance(other, Authority):
            return NotImplemented
        return self.value_index > other.value_index

    def __ge__(self, other: Authority) -> bool:
        if not isinstance(other, Authority):
            return NotImplemented
        return self.value_index >= other.value_index


@dataclass(frozen=True)
class Intent:
    """A claim about how a target entity should be, with priority metadata.

    Attributes
    ----------
    target
        The entity_id this intent applies to, e.g. "light.living_room".
    set
        Per-field absolute values. The highest-priority intent's `set` becomes
        the baseline; lower-priority `set` values are ignored unless
        `merge=True` and the fields don't overlap.
    merge
        If True, lower-priority intents can set fields that the winner didn't.
        Useful for "I only care about color_temp, you handle brightness."
    cap, floor, offset, multiply
        Per-field modifiers that compose across all intents (not just the
        winner). See compositor.py for the exact composition order.
    transition_ms
        How long, in milliseconds, to take getting to the target value.
        0 means "apply immediately." HA's light.turn_on supports this natively.
    easing
        Easing function for transitions: linear, ease-in, ease-out,
        ease-in-out, sine. HA doesn't natively support easing on light
        transitions, so the engine interpolates via the tick loop.
    authority
        Priority tier. Higher wins.
    confidence
        Float in [0.0, 1.0]. Within the same authority tier, higher wins.
    ttl_ms
        Time-to-live in milliseconds, or None for "until I explicitly retire."
        After this many ms past created_at_ms, the intent is treated as if
        it no longer exists.
    reason
        Human-readable explanation. Surfaces in the UI and logs.
    rule_id
        Identifier of the rule that emitted this intent. Used for debugging,
        blocking relationships, and event attribution. May be empty for
        ad-hoc user intents spawned outside a rule.
    ignore_when
        If True, the engine keeps this intent alive until its TTL expires even
        when the associated rule's ``when`` expression is false. Used for
        explicit service-triggered activations such as ``activate_scene``.
    created_at_ms
        When this intent was created. Used as a deterministic tiebreaker
        when authority and confidence are equal. Set automatically; pass
        explicitly only for testing.
    """

    target: str
    set: dict[str, Any] = field(default_factory=dict)
    withdraw: dict[str, Any] = field(default_factory=dict)
    merge: bool = False
    cap: dict[str, Any] = field(default_factory=dict)
    floor: dict[str, Any] = field(default_factory=dict)
    offset: dict[str, Any] = field(default_factory=dict)
    multiply: dict[str, Any] = field(default_factory=dict)
    transition_ms: int = 0
    transition_assert_ms: int | None = None
    transition_change_ms: int | None = None
    transition_withdraw_ms: int | None = None
    easing: str = "linear"
    authority: Authority = Authority.AUTOMATION
    confidence: float = 1.0
    ttl_ms: int | None = None
    reason: str = ""
    rule_id: str = ""
    ignore_when: bool = False
    selector_generated: bool = False
    created_at_ms: int = field(default_factory=_now_ms)
    animation: AnimationSpec | None = None
    generators: dict[str, ValueGeneratorSpec] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Defensive copy of mutable defaults to honor the frozen contract."""
        # frozen=True prevents attribute reassignment, but mutable values in
        # fields can still be mutated in place. We copy them here.
        object.__setattr__(self, "set", _copy_field_dict(self.set))
        object.__setattr__(self, "withdraw", _copy_field_dict(self.withdraw))
        object.__setattr__(self, "cap", _copy_field_dict(self.cap))
        object.__setattr__(self, "floor", _copy_field_dict(self.floor))
        object.__setattr__(self, "offset", _copy_field_dict(self.offset))
        object.__setattr__(self, "multiply", _copy_field_dict(self.multiply))
        object.__setattr__(self, "generators", dict(self.generators))

    # ── Priority / comparison ────────────────────────────────────────

    @property
    def priority(self) -> tuple[int, float, int, int]:
        """Sortable priority tuple. Higher tuple = higher priority.

        The compositor uses this with max() to pick the winning intent
        for any field that has competing `set` claims. The tuple is
        intentionally a plain tuple of primitives so it can be used as
        a sort key without invoking the dataclass.

        The final element is `id(self)` — the object's memory address.
        This guarantees a total ordering even when two intents are created
        in the same millisecond with the same confidence, which matters
        because `max()` is undefined on equal values. With `id()` as the
        final tiebreaker, two intents are never exactly equal.
        """
        return (self.authority.value_index, self.confidence, self.created_at_ms, id(self))

    def __lt__(self, other: Intent) -> bool:
        return self.priority < other.priority

    def __le__(self, other: Intent) -> bool:
        return self.priority <= other.priority

    def __gt__(self, other: Intent) -> bool:
        return self.priority > other.priority

    def __ge__(self, other: Intent) -> bool:
        return self.priority >= other.priority

    # ── Expiration ───────────────────────────────────────────────────

    def expires_at_ms(self) -> int | None:
        """Return the absolute timestamp at which this intent expires, or None."""
        if self.ttl_ms is None:
            return None
        return self.created_at_ms + self.ttl_ms

    def is_expired(self, *, into_the_future_ms: int | None = None) -> bool:
        """Return True if this intent has expired.

        Parameters
        ----------
        into_the_future_ms
            Optional override for "now" — used by tests to advance time
            deterministically. If None, uses the real current time.
        """
        if self.ttl_ms is None:
            return False
        if self.ttl_ms <= 0:
            return True
        now = into_the_future_ms if into_the_future_ms is not None else _now_ms()
        return now >= self.created_at_ms + self.ttl_ms

    # ── Debug representation ─────────────────────────────────────────

    def __repr__(self) -> str:
        bits = [f"target={self.target!r}", f"authority={self.authority.value!r}"]
        if self.rule_id:
            bits.append(f"rule={self.rule_id!r}")
        if self.ttl_ms is not None:
            bits.append(f"ttl={self.ttl_ms}ms")
        if self.set:
            bits.append(f"set={self.set!r}")
        return f"Intent({', '.join(bits)})"
