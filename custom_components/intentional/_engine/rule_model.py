"""Engine-facing authored rule model."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .animation import AnimationSpec
from .generation import ValueGeneratorSpec
from .intent import Authority
from .records import (
    AlertSpec,
    DynamicHoldAfter,
    Effect,
    HysteresisObservation,
    IntentSelector,
    ObservationGroup,
    ObserveSelector,
)

RuleDirFingerprint = tuple[tuple[str, int, int], ...]


class RuleLoadError(Exception):
    """Raised when a rule file or rule definition cannot be loaded."""

    def __init__(
        self,
        message: str,
        *,
        file: Path | None = None,
        line: int | None = None,
    ) -> None:
        parts = []
        if file is not None:
            parts.append(f"{file}")
        if line is not None:
            parts.append(f"line {line}")
        prefix = ": ".join(parts) if parts else "rule"
        super().__init__(f"{prefix}: {message}")
        self.file = file
        self.line = line


@dataclass(frozen=True)
class Rule:
    """A loaded rule, ready to be evaluated by the engine."""

    id: str
    when: str
    authored_rule_id: str = ""
    for_ms: int = 0
    for_entity: str | None = None
    for_entity_unit: str = "s"
    target: str = ""
    scene: str | None = None
    set: dict[str, Any] = field(default_factory=dict)
    withdraw: dict[str, Any] = field(default_factory=dict)
    cap: dict[str, Any] = field(default_factory=dict)
    floor: dict[str, Any] = field(default_factory=dict)
    offset: dict[str, Any] = field(default_factory=dict)
    multiply: dict[str, Any] = field(default_factory=dict)
    merge: bool = False
    transition_ms: int = 0
    transition_assert_ms: int | None = None
    transition_change_ms: int | None = None
    transition_withdraw_ms: int | None = None
    easing: str = "linear"
    ttl_ms: int | None = None
    manual_override_ttl_ms: int | None = None
    linger_ms: int | None = None
    dynamic_hold_after: DynamicHoldAfter | None = None
    hold_when: str | None = None
    hold_until_when: str | None = None
    hold_until_for_ms: int = 0
    authority: Authority = Authority.AUTOMATION
    confidence: float = 1.0
    reason: str = ""
    blocks: tuple[str, ...] = field(default_factory=tuple)
    animation: AnimationSpec | None = None
    generators: dict[str, ValueGeneratorSpec] = field(default_factory=dict)
    effects: tuple[Effect, ...] = field(default_factory=tuple)
    alerts: tuple[AlertSpec, ...] = field(default_factory=tuple)
    intent_selectors: tuple[IntentSelector, ...] = field(default_factory=tuple)
    observe_selectors: tuple[ObserveSelector, ...] = field(default_factory=tuple)
    observe_selector_mode: str = "any"
    observation_groups: tuple[ObservationGroup, ...] = field(default_factory=tuple)
    hysteresis: HysteresisObservation | None = None
    hold_observation_groups: tuple[ObservationGroup, ...] = field(default_factory=tuple)
    hold_until_observation_groups: tuple[ObservationGroup, ...] = field(default_factory=tuple)
    edge_created: bool = False
    enabled: bool = True
    labels: tuple[str, ...] = field(default_factory=tuple)
    group: str = ""
    profile: str = ""
    notes: str = ""
    source_file: Path | None = None
    source_line: int | None = None
