"""YAML rule loader for ha-intentional.

Loads rule files from a directory, validates them, and produces Rule objects
that the engine can use to spawn Intents when triggers fire.

YAML rule format:

    - id: rule-name                    # required, unique across all files
      when: "expression"               # required, see "when" below
      emit:                            # required, what to claim
        target: light.living_room
        set: { brightness_pct: 80 }    # absolute values
        cap: { brightness_pct: 40 }    # ceiling
        floor: { brightness_pct: 5 }   # floor
        offset: { brightness_pct: -10 }
        multiply: { brightness_pct: 0.9 }
        merge: false
        transition: 1.5s
        easing: ease-in-out
        ttl: 2h
        animation:
          kind: pulse
          parameter: brightness_pct
          values: [0, 100, 0]
          duration: 2s
          repeat: 4
      authority: automation            # sensor | automation | user
      confidence: 0.9                  # 0.0 .. 1.0
      reason: "Dark outside"           # human-readable
      blocks: [other-rule-id]          # suppress these rules when active

The "when" expression is a string that's evaluated at runtime by the engine
against the current state of referenced entities. The loader does not
evaluate it — it just records it for the engine.

Duration shorthand (used for transition, ttl, animation timing):
- 500ms, 1ms
- 1s, 30s
- 5m, 1h
- 1h30m15s (combined)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from intentional.animation import AnimationSpec
from intentional.intent import Authority

# ── Errors ───────────────────────────────────────────────────────────


class RuleLoadError(Exception):
    """Raised when a rule file or rule definition cannot be loaded.

    Includes the source file and line number when available.
    """

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


# ── Duration parsing ─────────────────────────────────────────────────


_DURATION_RE = re.compile(
    r"^\s*"
    r"(?:(?P<hours>\d+(?:\.\d+)?)h)?"
    r"(?:(?P<minutes>\d+(?:\.\d+)?)m(?!s))?"      # minutes, not milliseconds
    r"(?:(?P<seconds>\d+(?:\.\d+)?)s)?"
    r"(?:(?P<millis>\d+)ms)?"
    r"\s*$"
)


def parse_duration(s: str) -> int:
    """Parse a duration string like '1h30m' or '500ms' into milliseconds.

    Supported units: ms, s, m (minutes), h (hours). Can be combined: '1h30m15s'.
    Returns the total duration in milliseconds.
    """
    if not isinstance(s, str):
        raise ValueError(f"Duration must be a string, got {type(s).__name__}")
    m = _DURATION_RE.match(s)
    if not m or not any(m.groupdict().values()):
        raise ValueError(
            f"Invalid duration {s!r}. Use forms like '500ms', '2s', '5m', '1h', '1h30m15s'."
        )
    hours = float(m.group("hours") or 0)
    minutes = float(m.group("minutes") or 0)
    seconds = float(m.group("seconds") or 0)
    millis = int(m.group("millis") or 0)
    total = int(hours * 3_600_000 + minutes * 60_000 + seconds * 1000 + millis)
    if total < 0:
        raise ValueError(f"Duration must be non-negative, got {s!r}")
    return total


# ── Rule dataclass ───────────────────────────────────────────────────


@dataclass(frozen=True)
class Rule:
    """A loaded rule, ready to be evaluated by the engine.

    Attributes
    ----------
    id
        Unique identifier. Used for `blocks:` references and event attribution.
    when
        The trigger expression as a string. Engine evaluates this against
        the current state of referenced entities.
    target
        The entity_id this rule's intents apply to.
    set, cap, floor, offset, multiply
        Per-field modifiers, all dicts.
    merge
        Hint flag; currently informational only (compositor uses per-field
        merge regardless).
    transition_ms
        Smooth transition duration, 0 for instant.
    easing
        Easing function name.
    ttl_ms
        Time-to-live in milliseconds, or None for "until rule withdraws."
    authority
        Priority tier.
    confidence
        0.0 .. 1.0 priority within the authority tier.
    reason
        Human-readable explanation, surfaced in the UI.
    blocks
        List of rule IDs to suppress when this rule is active.
    animation
        Optional time-varying value spec.
    source_file
        Path to the YAML file this rule was loaded from. For error reporting.
    source_line
        Line number in the source file. For error reporting.
    """

    id: str
    when: str
    target: str
    set: dict[str, Any] = field(default_factory=dict)
    cap: dict[str, Any] = field(default_factory=dict)
    floor: dict[str, Any] = field(default_factory=dict)
    offset: dict[str, Any] = field(default_factory=dict)
    multiply: dict[str, Any] = field(default_factory=dict)
    merge: bool = False
    transition_ms: int = 0
    easing: str = "linear"
    ttl_ms: int | None = None
    authority: Authority = Authority.AUTOMATION
    confidence: float = 1.0
    reason: str = ""
    blocks: tuple[str, ...] = field(default_factory=tuple)
    animation: AnimationSpec | None = None
    source_file: Path | None = None
    source_line: int | None = None


# ── Schema validation ────────────────────────────────────────────────


# Recognized top-level fields in a rule
_RULE_TOP_LEVEL = {
    "id", "when", "emit", "authority", "confidence", "reason", "blocks",
}
# Recognized fields in the emit block
_EMIT_FIELDS = {
    "target", "set", "cap", "floor", "offset", "multiply", "merge",
    "transition", "easing", "ttl", "animation",
}
# Recognized animation fields
_ANIMATION_FIELDS = {
    "kind", "parameter", "values", "min", "max", "peak",
    "duration", "period", "decay", "repeat", "easing",
}
# Recognized easing names
_VALID_EASINGS = {"linear", "ease-in", "ease-out", "ease-in-out", "sine"}


def _validate_rule(
    raw: dict[str, Any],
    *,
    file: Path | None = None,
    line: int | None = None,
) -> Rule:
    """Validate a single rule dict and return a Rule object.

    Raises RuleLoadError on any schema violation.
    """
    if not isinstance(raw, dict):
        raise RuleLoadError(
            f"Each rule must be a mapping, got {type(raw).__name__}",
            file=file, line=line,
        )

    # Unknown top-level fields
    unknown = set(raw.keys()) - _RULE_TOP_LEVEL
    if unknown:
        raise RuleLoadError(
            f"Unknown top-level fields: {sorted(unknown)}. "
            f"Allowed: {sorted(_RULE_TOP_LEVEL)}",
            file=file, line=line,
        )

    # id
    rule_id = raw.get("id")
    if not rule_id or not isinstance(rule_id, str):
        raise RuleLoadError("Missing or invalid `id` (must be a non-empty string)", file=file, line=line)

    # when
    when = raw.get("when")
    if not when or not isinstance(when, str):
        raise RuleLoadError(f"Rule {rule_id!r}: missing or invalid `when` (must be a non-empty string)", file=file, line=line)

    # emit
    emit = raw.get("emit")
    if not isinstance(emit, dict):
        raise RuleLoadError(f"Rule {rule_id!r}: missing or invalid `emit` block", file=file, line=line)

    unknown_emit = set(emit.keys()) - _EMIT_FIELDS
    if unknown_emit:
        raise RuleLoadError(
            f"Rule {rule_id!r}: unknown fields in `emit`: {sorted(unknown_emit)}. "
            f"Allowed: {sorted(_EMIT_FIELDS)}",
            file=file, line=line,
        )

    # target
    target = emit.get("target")
    if not target or not isinstance(target, str):
        raise RuleLoadError(
            f"Rule {rule_id!r}: missing or invalid `target` in `emit` (must be a non-empty entity_id)",
            file=file, line=line,
        )

    # authority
    auth_str = raw.get("authority", "automation")
    if not isinstance(auth_str, str):
        raise RuleLoadError(
            f"Rule {rule_id!r}: `authority` must be a string, got {type(auth_str).__name__}",
            file=file, line=line,
        )
    try:
        authority = Authority(auth_str)
    except ValueError as e:
        valid = ", ".join(a.value for a in Authority)
        raise RuleLoadError(
            f"Rule {rule_id!r}: unknown `authority` {auth_str!r}. Must be one of: {valid}",
            file=file, line=line,
        ) from e

    # confidence
    confidence = raw.get("confidence", 1.0)
    if not isinstance(confidence, (int, float)) or not 0.0 <= confidence <= 1.0:
        raise RuleLoadError(
            f"Rule {rule_id!r}: `confidence` must be a number in [0.0, 1.0], got {confidence!r}",
            file=file, line=line,
        )

    # easing
    easing = emit.get("easing", "linear")
    if easing not in _VALID_EASINGS:
        raise RuleLoadError(
            f"Rule {rule_id!r}: unknown `easing` {easing!r}. Must be one of: {sorted(_VALID_EASINGS)}",
            file=file, line=line,
        )

    # durations
    transition_ms = _parse_optional_duration(emit.get("transition"), f"Rule {rule_id!r}: transition", file, line)
    ttl_ms = _parse_optional_duration(emit.get("ttl"), f"Rule {rule_id!r}: ttl", file, line)

    # blocks
    blocks_raw = raw.get("blocks", [])
    if blocks_raw is None:
        blocks_raw = []
    if not isinstance(blocks_raw, list) or not all(isinstance(b, str) for b in blocks_raw):
        raise RuleLoadError(
            f"Rule {rule_id!r}: `blocks` must be a list of rule-id strings",
            file=file, line=line,
        )

    # animation
    animation = _parse_animation(emit.get("animation"), rule_id, file, line)

    return Rule(
        id=rule_id,
        when=when,
        target=target,
        set=dict(emit.get("set", {})),
        cap=dict(emit.get("cap", {})),
        floor=dict(emit.get("floor", {})),
        offset=dict(emit.get("offset", {})),
        multiply=dict(emit.get("multiply", {})),
        merge=bool(emit.get("merge", False)),
        transition_ms=transition_ms,
        easing=easing,
        ttl_ms=ttl_ms,
        authority=authority,
        confidence=float(confidence),
        reason=str(raw.get("reason", "")),
        blocks=tuple(blocks_raw),
        animation=animation,
        source_file=file,
        source_line=line,
    )


def _parse_optional_duration(
    value: Any,
    label: str,
    file: Path | None,
    line: int | None,
) -> int:
    """Parse an optional duration field. Accepts int (ms) or string ('2s')."""
    if value is None:
        return 0
    if isinstance(value, int):
        if value < 0:
            raise RuleLoadError(f"{label} must be non-negative, got {value}", file=file, line=line)
        return value
    if isinstance(value, str):
        try:
            return parse_duration(value)
        except ValueError as e:
            raise RuleLoadError(f"{label}: {e}", file=file, line=line) from e
    raise RuleLoadError(
        f"{label} must be an integer (ms) or a duration string, got {type(value).__name__}",
        file=file, line=line,
    )


def _parse_animation(
    raw: Any,
    rule_id: str,
    file: Path | None,
    line: int | None,
) -> AnimationSpec | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise RuleLoadError(
            f"Rule {rule_id!r}: `animation` must be a mapping, got {type(raw).__name__}",
            file=file, line=line,
        )

    unknown = set(raw.keys()) - _ANIMATION_FIELDS
    if unknown:
        raise RuleLoadError(
            f"Rule {rule_id!r}: unknown fields in `animation`: {sorted(unknown)}. "
            f"Allowed: {sorted(_ANIMATION_FIELDS)}",
            file=file, line=line,
        )

    kind = raw.get("kind")
    parameter = raw.get("parameter")
    if not kind or not parameter:
        raise RuleLoadError(
            f"Rule {rule_id!r}: animation requires both `kind` and `parameter`",
            file=file, line=line,
        )

    kwargs: dict[str, Any] = {"kind": kind, "parameter": parameter}
    if "values" in raw:
        kwargs["values"] = raw["values"]
    if "min" in raw:
        kwargs["min"] = raw["min"]
    if "max" in raw:
        kwargs["max"] = raw["max"]
    if "peak" in raw:
        kwargs["peak"] = raw["peak"]
    if "duration" in raw:
        kwargs["duration_ms"] = _parse_optional_duration(
            raw["duration"], f"Rule {rule_id!r}: animation.duration", file, line
        )
    if "period" in raw:
        kwargs["period_ms"] = _parse_optional_duration(
            raw["period"], f"Rule {rule_id!r}: animation.period", file, line
        )
    if "decay" in raw:
        kwargs["decay_ms"] = _parse_optional_duration(
            raw["decay"], f"Rule {rule_id!r}: animation.decay", file, line
        )
    if "repeat" in raw:
        kwargs["repeat"] = raw["repeat"]
    if "easing" in raw:
        kwargs["easing"] = raw["easing"]

    try:
        return AnimationSpec(**kwargs)
    except (ValueError, TypeError) as e:
        raise RuleLoadError(
            f"Rule {rule_id!r}: invalid animation: {e}",
            file=file, line=line,
        ) from e


# ── Public API ───────────────────────────────────────────────────────


def load_rules_from_string(text: str, *, file: Path | None = None) -> list[Rule]:
    """Parse a YAML string into a list of validated Rule objects.

    Raises RuleLoadError on any parse or schema error.
    """
    try:
        docs = list(yaml.safe_load_all(text))
    except yaml.YAMLError as e:
        line = getattr(e, "problem_mark", None)
        line_num = line.line + 1 if line else None
        raise RuleLoadError(f"YAML parse error: {e}", file=file, line=line_num) from e

    # Collect all rule objects across documents
    rules: list[Rule] = []
    for doc in docs:
        if doc is None:
            continue  # empty document
        if not isinstance(doc, list):
            raise RuleLoadError(
                f"Each YAML document must be a list of rules, got {type(doc).__name__}",
                file=file,
            )
        for i, raw in enumerate(doc):
            line_num = i + 1  # approximate; YAML doesn't preserve original lines per item
            rules.append(_validate_rule(raw, file=file, line=line_num))

    # Check for duplicate IDs
    seen: dict[str, Rule] = {}
    for rule in rules:
        if rule.id in seen:
            existing = seen[rule.id]
            raise RuleLoadError(
                f"Duplicate rule id {rule.id!r} "
                f"(first defined in {existing.source_file or '<inline>'})",
                file=file,
            )
        seen[rule.id] = rule

    return rules


def load_rules(directory: str | Path) -> list[Rule]:
    """Load all rule files from a directory, in alphabetical order.

    Files must end in .yaml or .yml. Other files (README, .md) are ignored.
    """
    directory = Path(directory)
    if not directory.exists():
        raise RuleLoadError(f"Rule directory does not exist: {directory}")
    if not directory.is_dir():
        raise RuleLoadError(f"Path is not a directory: {directory}")

    rule_files = sorted(
        f for f in directory.iterdir()
        if f.is_file() and f.suffix.lower() in (".yaml", ".yml")
    )

    all_rules: list[Rule] = []
    for path in rule_files:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as e:
            raise RuleLoadError(f"Could not read {path}: {e}", file=path) from e
        all_rules.extend(load_rules_from_string(text, file=path))

    return all_rules
