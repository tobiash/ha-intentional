"""Generated intent field values.

Generators produce ordinary desired-state values while a rule remains active.
They are slower and discrete, unlike animations: a generator samples a value,
keeps it until its next due time, then samples again.
"""

from __future__ import annotations

import colorsys
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ValueGeneratorSpec:
    """Specification for generating one intent field value."""

    kind: str
    values: Sequence[Any] = field(default_factory=tuple)
    weights: Sequence[float] = field(default_factory=tuple)
    mode: str = "random"
    hue_min: float = 0.0
    hue_max: float = 360.0
    saturation_min: float = 35.0
    saturation_max: float = 100.0
    value_min: float = 35.0
    value_max: float = 100.0
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    every_min_ms: int = 0
    every_max_ms: int = 0
    transition_min_ms: int | None = None
    transition_max_ms: int | None = None

    def __post_init__(self) -> None:
        if self.kind not in {"sample", "walk", "weighted_sample", "gradient", "noise"}:
            raise ValueError(f"unknown generator kind {self.kind!r}")
        if self.kind in {"sample", "walk", "weighted_sample", "gradient"} and not self.values:
            raise ValueError(f"{self.kind} generator requires a non-empty `from` list")
        if self.kind == "weighted_sample" and self.weights and len(self.weights) != len(self.values):
            raise ValueError("weighted_sample `weights` length must match `from`")
        if self.kind == "weighted_sample" and self.weights:
            if any(weight < 0 for weight in self.weights):
                raise ValueError("weighted_sample `weights` must be non-negative")
            if not any(self.weights):
                raise ValueError("weighted_sample `weights` must contain a positive value")
        if self.kind == "noise" and self.minimum is not None and self.maximum is not None and self.maximum < self.minimum:
            raise ValueError("noise `max` must be >= `min`")
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
    next_due_ms = raw.get("next_due_ms")
    transition_ms = raw.get("transition_ms")
    if not _is_nonnegative_int(next_due_ms):
        return None
    if transition_ms is not None and not _is_nonnegative_int(transition_ms):
        return None
    state = GeneratedFieldState(
        value=raw.get("value"),
        next_due_ms=next_due_ms,
        transition_ms=transition_ms,
    )
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
    value = _generate_value(spec, rng, previous_value)
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


def _generate_value(spec: ValueGeneratorSpec, rng: random.Random, previous_value: Any) -> Any:
    if spec.kind == "sample":
        values = _without_previous(list(spec.values), previous_value)
        return rng.choice(values)
    if spec.kind == "weighted_sample":
        values = _without_previous(list(spec.values), previous_value)
        if not spec.weights:
            return rng.choice(values)
        weighted = [
            (value, weight)
            for value, weight in zip(spec.values, spec.weights, strict=True)
            if value in values
        ]
        return rng.choices(
            [value for value, _weight in weighted],
            weights=[weight for _value, weight in weighted],
            k=1,
        )[0]
    if spec.kind == "walk":
        return _walk_value(list(spec.values), previous_value, rng)
    if spec.kind == "gradient":
        return _gradient_value(list(spec.values), previous_value, rng, mode=spec.mode)
    if spec.kind == "noise":
        return _noise_value(spec, rng, previous_value)
    raise ValueError(f"unknown generator kind {spec.kind!r}")


def _without_previous(values: list[Any], previous_value: Any) -> list[Any]:
    if previous_value is not None and len(values) > 1:
        values = [value for value in values if value != previous_value]
    return values


def _walk_value(values: list[Any], previous_value: Any, rng: random.Random) -> Any:
    if previous_value not in values or len(values) <= 1:
        return rng.choice(values)
    index = values.index(previous_value)
    step = rng.choice((-1, 1))
    return values[(index + step) % len(values)]


def _gradient_value(values: list[Any], previous_value: Any, rng: random.Random, *, mode: str) -> Any:
    if previous_value is None or not _is_numeric_sequence(previous_value):
        return rng.choice(values)
    candidates = [value for value in values if value != previous_value]
    if not candidates:
        return previous_value
    numeric = [value for value in candidates if _is_numeric_sequence(value)]
    if not numeric:
        return rng.choice(candidates)
    if mode == "random":
        target = rng.choice(numeric)
    elif mode in {"nearest", "walk"}:
        target = min(numeric, key=lambda value: _distance(previous_value, value))
    else:
        raise ValueError(f"unknown gradient mode {mode!r}")
    return [round((float(a) + float(b)) / 2) for a, b in zip(previous_value, target, strict=True)]


def _noise_value(spec: ValueGeneratorSpec, rng: random.Random, previous_value: Any) -> Any:
    if spec.minimum is not None and spec.maximum is not None:
        value = rng.uniform(spec.minimum, spec.maximum)
        if spec.step:
            value = round(value / spec.step) * spec.step
        return int(value) if float(value).is_integer() else value
    hue = rng.uniform(spec.hue_min, spec.hue_max)
    saturation = rng.uniform(spec.saturation_min, spec.saturation_max) / 100
    value = rng.uniform(spec.value_min, spec.value_max) / 100
    red, green, blue = colorsys.hsv_to_rgb((hue % 360) / 360, saturation, value)
    return [round(red * 255), round(green * 255), round(blue * 255)]


def _is_numeric_sequence(value: Any) -> bool:
    return isinstance(value, list | tuple) and all(isinstance(item, int | float) for item in value)


def _distance(left: Sequence[Any], right: Sequence[Any]) -> float:
    if len(left) != len(right):
        return float("inf")
    return sum((float(a) - float(b)) ** 2 for a, b in zip(left, right, strict=True))


def parse_generator_spec(
    raw: Any,
    *,
    parse_duration: Callable[[Any], int],
) -> ValueGeneratorSpec:
    """Parse a YAML-loaded generator mapping into a spec."""
    if not isinstance(raw, dict):
        raise ValueError("generator must be a mapping")
    unknown = set(raw) - {
        "kind", "from", "weights", "every", "transition", "mode",
        "hue", "saturation", "value", "min", "max", "step",
    }
    if unknown:
        raise ValueError(f"unknown generator fields: {sorted(unknown)}")
    every_min_ms, every_max_ms = _parse_interval(raw.get("every"), parse_duration=parse_duration)
    transition_min_ms = None
    transition_max_ms = None
    if "transition" in raw:
        transition_min_ms, transition_max_ms = _parse_interval(raw.get("transition"), parse_duration=parse_duration)
    kind = str(raw.get("kind", "sample"))
    values = _parse_values(raw, kind)
    weights = _parse_weights(raw, values)
    hue_min, hue_max = _parse_numeric_range(raw.get("hue"), default_min=0, default_max=360)
    saturation_min, saturation_max = _parse_numeric_range(raw.get("saturation"), default_min=35, default_max=100)
    value_min, value_max = _parse_numeric_range(raw.get("value"), default_min=35, default_max=100)
    return ValueGeneratorSpec(
        kind=kind,
        values=tuple(values),
        weights=tuple(weights),
        mode=str(raw.get("mode", "random")),
        hue_min=hue_min,
        hue_max=hue_max,
        saturation_min=saturation_min,
        saturation_max=saturation_max,
        value_min=value_min,
        value_max=value_max,
        minimum=_optional_float(raw.get("min")),
        maximum=_optional_float(raw.get("max")),
        step=_optional_float(raw.get("step")),
        every_min_ms=every_min_ms,
        every_max_ms=every_max_ms,
        transition_min_ms=transition_min_ms,
        transition_max_ms=transition_max_ms,
    )


def _parse_values(raw: dict[str, Any], kind: str) -> list[Any]:
    if kind == "noise":
        values = raw.get("from", [])
        if values is None:
            return []
        if not isinstance(values, list):
            raise ValueError("noise generator `from`, when provided, must be a list")
        return values
    values = raw.get("from")
    if not isinstance(values, list):
        raise ValueError(f"{kind} generator requires `from` to be a list")
    if kind == "weighted_sample" and values and all(isinstance(item, dict) for item in values):
        return [item.get("value") for item in values]
    return values


def _parse_weights(raw: dict[str, Any], values: list[Any]) -> list[float]:
    explicit = raw.get("weights")
    if explicit is not None:
        if not isinstance(explicit, list):
            raise ValueError("generator `weights` must be a list")
        try:
            return [float(weight) for weight in explicit]
        except (TypeError, ValueError) as e:
            raise ValueError("generator `weights` must contain only numbers") from e
    from_values = raw.get("from")
    if isinstance(from_values, list) and from_values and all(isinstance(item, dict) for item in from_values):
        try:
            return [float(item.get("weight", 1)) for item in from_values]
        except (TypeError, ValueError) as e:
            raise ValueError("generator inline `weight` values must be numbers") from e
    return []


def _parse_numeric_range(raw: Any, *, default_min: float, default_max: float) -> tuple[float, float]:
    if raw is None:
        return default_min, default_max
    if not isinstance(raw, dict):
        raise ValueError("numeric range must be a mapping")
    unknown = set(raw) - {"min", "max"}
    if unknown:
        raise ValueError(f"unknown range fields: {sorted(unknown)}")
    return float(raw.get("min", default_min)), float(raw.get("max", default_max))


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


def _is_nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)
