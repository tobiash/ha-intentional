"""VNext record types shared by loading, planning, effects, and selectors."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Effect:
    """A side-effect escape hatch emitted by an active rule."""

    domain: str
    service: str
    target: dict[str, Any] = field(default_factory=dict)
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class IntentSelector:
    """A selector that expands to target entity IDs at planning time."""

    domain: str | None = None
    area: str | None = None
    label: str | None = None
    exclude: tuple[str, ...] = field(default_factory=tuple)
    set: dict[str, Any] = field(default_factory=dict)
    cap: dict[str, Any] = field(default_factory=dict)
    floor: dict[str, Any] = field(default_factory=dict)
    offset: dict[str, Any] = field(default_factory=dict)
    multiply: dict[str, Any] = field(default_factory=dict)
    transition_ms: int = 0
    easing: str = "linear"
    ttl_ms: int | None = None
    linger_ms: int | None = None


@dataclass(frozen=True)
class ObserveSelector:
    """A selector-backed observation over dynamic entity sets."""

    domain: str | None = None
    area: str | None = None
    label: str | None = None
    exclude: tuple[str, ...] = field(default_factory=tuple)
    field: str = "state"
    operator: str = "is"
    value: Any = None
