"""Generated intent field values.

Generators produce ordinary desired-state values while a rule remains active.
They are slower and discrete, unlike animations: a generator samples a value,
keeps it until its next due time, then samples again.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ValueGeneratorSpec:
    """Specification for generating one intent field value."""

    kind: str
    values: Sequence[Any] = field(default_factory=tuple)
    every_min_ms: int = 0
    every_max_ms: int = 0
    transition_min_ms: int | None = None
    transition_max_ms: int | None = None

    def __post_init__(self) -> None:
        if self.kind != "sample":
            raise ValueError(f"unknown generator kind {self.kind!r}")
        if not self.values:
            raise ValueError("sample generator requires a non-empty `from` list")
        if self.every_min_ms <= 0:
            raise ValueError("generator every interval must be positive")
        if self.every_max_ms < self.every_min_ms:
            raise ValueError("generator every.max must be >= every.min")


@dataclass(frozen=True)
class GeneratedFieldState:
    """Runtime sample state for one generated field."""

    value: Any
    next_due_ms: int
    transition_ms: int | None = None


def generated_field_state_to_record(
    rule_id: str,
    field_name: str,
    state: GeneratedFieldState,
) -> dict[str, Any]:
    """Serialize generated field state to lifecycle storage."""
    return {
        "rule_id": rule_id,
        "field": field_name,
        "value": state.value,
        "next_due_ms": state.next_due_ms,
        "transition_ms": state.transition_ms,
    }


def generated_field_state_from_record(raw: Any) -> tuple[tuple[str, str], GeneratedFieldState] | None:
    """Restore generated field state from lifecycle storage."""
    if not isinstance(raw, dict):
        return None
    rule_id = raw.get("rule_id")
    field_name = raw.get("field")
    if not isinstance(rule_id, str) or not isinstance(field_name, str):
        return None
    try:
        state = GeneratedFieldState(
            value=raw.get("value"),
            next_due_ms=int(raw.get("next_due_ms")),
            transition_ms=_optional_int(raw.get("transition_ms")),
        )
    except (TypeError, ValueError):
        return None
    return (rule_id, field_name), state


def sample_generated_field(
    spec: ValueGeneratorSpec,
    *,
    now_ms: int,
    seed: str,
    previous_value: Any = None,
) -> GeneratedFieldState:
    """Sample a generated field and schedule its next sample."""
    rng = random.Random(seed)
    values = list(spec.values)
    if previous_value is not None and len(values) > 1:
        values = [value for value in values if value != previous_value]
    value = rng.choice(values)
    interval_ms = rng.randint(spec.every_min_ms, spec.every_max_ms)
    transition_ms = None
    if spec.transition_min_ms is not None:
        transition_max = spec.transition_max_ms or spec.transition_min_ms
        transition_ms = rng.randint(spec.transition_min_ms, transition_max)
    return GeneratedFieldState(
        value=value,
        next_due_ms=now_ms + interval_ms,
        transition_ms=transition_ms,
    )


def parse_generator_spec(
    raw: Any,
    *,
    parse_duration: Callable[[Any], int],
) -> ValueGeneratorSpec:
    """Parse a YAML-loaded generator mapping into a spec."""
    if not isinstance(raw, dict):
        raise ValueError("generator must be a mapping")
    unknown = set(raw) - {"kind", "from", "every", "transition"}
    if unknown:
        raise ValueError(f"unknown generator fields: {sorted(unknown)}")
    every_min_ms, every_max_ms = _parse_interval(raw.get("every"), parse_duration=parse_duration)
    transition_min_ms = None
    transition_max_ms = None
    if "transition" in raw:
        transition_min_ms, transition_max_ms = _parse_interval(raw.get("transition"), parse_duration=parse_duration)
    values = raw.get("from")
    if not isinstance(values, list):
        raise ValueError("sample generator requires `from` to be a list")
    return ValueGeneratorSpec(
        kind=str(raw.get("kind", "sample")),
        values=tuple(values),
        every_min_ms=every_min_ms,
        every_max_ms=every_max_ms,
        transition_min_ms=transition_min_ms,
        transition_max_ms=transition_max_ms,
    )


def _parse_interval(
    raw: Any,
    *,
    parse_duration: Callable[[Any], int],
) -> tuple[int, int]:
    if isinstance(raw, dict):
        unknown = set(raw) - {"min", "max"}
        if unknown:
            raise ValueError(f"unknown interval fields: {sorted(unknown)}")
        if "min" not in raw or "max" not in raw:
            raise ValueError("interval mapping requires `min` and `max`")
        return parse_duration(raw["min"]), parse_duration(raw["max"])
    duration_ms = parse_duration(raw)
    return duration_ms, duration_ms


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)
