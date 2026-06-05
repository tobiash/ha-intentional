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
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .animation import AnimationSpec
from .intent import Authority

RuleDirFingerprint = tuple[tuple[str, int, int], ...]

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
    rule_id
        Unique identifier. Used for `blocks:` references and event attribution.
    when
        The trigger expression as a string. Engine evaluates this against
        the current state of referenced entities.
    for_ms
        Optional dwell time: the `when` expression must stay true for this
        long before the rule fires. Used directly for static dwell timing and
        as the fallback for dynamic entity-backed dwell timing.
    for_entity
        Optional entity_id whose numeric state controls the dwell time.
    for_entity_unit
        Unit for the numeric `for_entity` state: ms, s, m, or h.
    target
        The entity_id this rule's intents apply to. Empty string when the
        rule references a scene instead.
    scene
        Optional HA scene entity_id (e.g. "scene.movie"). When set, the
        rule activates this scene instead of operating through the compositor.
        Mutually exclusive with `target`.
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
    for_ms: int = 0
    for_entity: str | None = None
    for_entity_unit: str = "s"
    target: str = ""
    scene: str | None = None
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
    "id", "extends", "when", "for", "emit", "authority", "confidence", "reason", "blocks",
}
# Recognized fields in the emit block
_EMIT_FIELDS = {
    "target", "scene", "set", "cap", "floor", "offset", "multiply", "merge",
    "transition", "easing", "ttl", "animation",
}
# Recognized animation fields
_ANIMATION_FIELDS = {
    "kind", "parameter", "values", "min", "max", "peak",
    "duration", "period", "decay", "repeat", "easing",
}
# Recognized easing names
_VALID_EASINGS = {"linear", "ease-in", "ease-out", "ease-in-out", "sine"}
_MERGED_EMIT_DICTS = {"set", "cap", "floor", "offset", "multiply"}
_FOR_FIELDS = {"entity", "unit", "default"}
_FOR_UNITS = {"ms", "s", "m", "h"}


@dataclass(frozen=True)
class _RawRuleDef:
    raw: dict[str, Any]
    file: Path | None
    line: int | None


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

    # for
    for_ms, for_entity, for_entity_unit = _parse_for(
        raw.get("for"),
        f"Rule {rule_id!r}: for",
        file,
        line,
    )

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

    # target OR scene — mutually exclusive. A rule activates a HA scene
    # OR claims an intent for a specific target entity. The two paths are
    # different at the integration layer (scene.turn_on vs light.turn_on
    # with a resolved value).
    target = emit.get("target")
    scene = emit.get("scene")
    has_target = bool(target) and isinstance(target, str)
    has_scene = bool(scene) and isinstance(scene, str)

    if has_target and has_scene:
        raise RuleLoadError(
            f"Rule {rule_id!r}: `emit` has both `target` and `scene`. "
            f"They are mutually exclusive — use one or the other.",
            file=file, line=line,
        )
    if not has_target and not has_scene:
        raise RuleLoadError(
            f"Rule {rule_id!r}: `emit` must have either `target` (entity_id) "
            f"or `scene` (HA scene entity_id). One is required.",
            file=file, line=line,
        )

    if has_scene and not isinstance(scene, str):
        raise RuleLoadError(
            f"Rule {rule_id!r}: `scene` must be a string (HA scene entity_id), "
            f"got {type(scene).__name__}",
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
    ttl_ms = _parse_optional_duration(
        emit.get("ttl"),
        f"Rule {rule_id!r}: ttl",
        file,
        line,
        default=None,
    )

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
        for_ms=for_ms,
        for_entity=for_entity,
        for_entity_unit=for_entity_unit,
        target=target or "",
        scene=scene,
        set=_normalize_emit_mapping(emit.get("set", {})),
        cap=_normalize_emit_mapping(emit.get("cap", {})),
        floor=_normalize_emit_mapping(emit.get("floor", {})),
        offset=_normalize_emit_mapping(emit.get("offset", {})),
        multiply=_normalize_emit_mapping(emit.get("multiply", {})),
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


def _normalize_emit_mapping(raw: Any) -> dict[str, Any]:
    """Return an emit mapping with HA state booleans normalized to strings."""
    mapping = dict(raw)
    if mapping.get("state") is True:
        mapping["state"] = "on"
    elif mapping.get("state") is False:
        mapping["state"] = "off"
    return mapping


def _parse_optional_duration(
    value: Any,
    label: str,
    file: Path | None,
    line: int | None,
    *,
    default: int | None = 0,
) -> int | None:
    """Parse an optional duration field. Accepts int (ms) or string ('2s')."""
    if value is None:
        return default
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


def _parse_for(
    value: Any,
    label: str,
    file: Path | None,
    line: int | None,
) -> tuple[int, str | None, str]:
    """Parse top-level `for`, including dynamic entity-backed dwell timing."""
    if not isinstance(value, dict):
        return (
            _parse_optional_duration(value, label, file, line) or 0,
            None,
            "s",
        )

    unknown = set(value) - _FOR_FIELDS
    if unknown:
        raise RuleLoadError(
            f"{label}: unknown fields: {sorted(unknown)}. "
            f"Allowed: {sorted(_FOR_FIELDS)}",
            file=file,
            line=line,
        )

    entity = value.get("entity")
    if not entity or not isinstance(entity, str):
        raise RuleLoadError(
            f"{label}: dynamic dwell requires `entity` as a non-empty string",
            file=file,
            line=line,
        )

    unit = value.get("unit", "s")
    if unit not in _FOR_UNITS:
        raise RuleLoadError(
            f"{label}: `unit` must be one of {sorted(_FOR_UNITS)}, got {unit!r}",
            file=file,
            line=line,
        )

    default = value.get("default")
    if default is None:
        raise RuleLoadError(
            f"{label}: dynamic dwell requires `default` for unavailable entity states",
            file=file,
            line=line,
        )

    default_ms = _parse_optional_duration(
        default,
        f"{label}.default",
        file,
        line,
    )
    return default_ms or 0, entity, unit


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


# ── Inheritance resolution ───────────────────────────────────────────


def _load_raw_rule_defs_from_string(
    text: str,
    *,
    file: Path | None = None,
) -> list[_RawRuleDef]:
    """Parse YAML into raw rule definitions without schema resolution."""
    try:
        docs = list(yaml.safe_load_all(text))
    except yaml.YAMLError as e:
        line = getattr(e, "problem_mark", None)
        line_num = line.line + 1 if line else None
        raise RuleLoadError(f"YAML parse error: {e}", file=file, line=line_num) from e

    raw_rules: list[_RawRuleDef] = []
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
            if not isinstance(raw, dict):
                raise RuleLoadError(
                    f"Each rule must be a mapping, got {type(raw).__name__}",
                    file=file, line=line_num,
                )
            raw_rules.append(_RawRuleDef(raw=raw, file=file, line=line_num))

    return raw_rules


def _resolve_rule_inheritance(raw_rules: list[_RawRuleDef]) -> list[_RawRuleDef]:
    """Resolve `extends:` references into complete raw rule definitions."""
    by_id: dict[str, _RawRuleDef] = {}
    for raw_def in raw_rules:
        rule_id = raw_def.raw.get("id")
        if not rule_id or not isinstance(rule_id, str):
            continue
        if rule_id in by_id:
            existing = by_id[rule_id]
            raise RuleLoadError(
                f"Duplicate rule id {rule_id!r} "
                f"(first defined in {existing.file or '<inline>'})",
                file=raw_def.file,
                line=raw_def.line,
            )
        by_id[rule_id] = raw_def

    resolved: dict[str, dict[str, Any]] = {}

    def resolve(rule_id: str, stack: tuple[str, ...] = ()) -> dict[str, Any]:
        if rule_id in resolved:
            return deepcopy(resolved[rule_id])
        raw_def = by_id[rule_id]
        parent_id = raw_def.raw.get("extends")
        if parent_id is None:
            result = deepcopy(raw_def.raw)
            result.pop("extends", None)
            resolved[rule_id] = result
            return deepcopy(result)
        if not isinstance(parent_id, str) or not parent_id:
            raise RuleLoadError(
                f"Rule {rule_id!r}: `extends` must be a non-empty rule id string",
                file=raw_def.file,
                line=raw_def.line,
            )
        if parent_id not in by_id:
            raise RuleLoadError(
                f"Rule {rule_id!r}: extends unknown rule id {parent_id!r}",
                file=raw_def.file,
                line=raw_def.line,
            )
        if parent_id in stack:
            cycle = " -> ".join((*stack, rule_id, parent_id))
            raise RuleLoadError(
                f"Rule inheritance cycle: {cycle}",
                file=raw_def.file,
                line=raw_def.line,
            )
        parent = resolve(parent_id, (*stack, rule_id))
        result = _merge_rule_dicts(parent, raw_def.raw)
        result.pop("extends", None)
        resolved[rule_id] = result
        return deepcopy(result)

    output: list[_RawRuleDef] = []
    for raw_def in raw_rules:
        rule_id = raw_def.raw.get("id")
        if isinstance(rule_id, str) and rule_id:
            output.append(_RawRuleDef(raw=resolve(rule_id), file=raw_def.file, line=raw_def.line))
        else:
            output.append(raw_def)
    return output


def _merge_rule_dicts(parent: dict[str, Any], child: dict[str, Any]) -> dict[str, Any]:
    """Merge a child rule over its inherited parent rule."""
    result = deepcopy(parent)
    for key, value in child.items():
        if key == "extends":
            continue
        if key == "emit" and isinstance(value, dict) and isinstance(result.get("emit"), dict):
            result["emit"] = _merge_emit_dicts(result["emit"], value)
            continue
        result[key] = deepcopy(value)
    return result


def _merge_emit_dicts(parent: dict[str, Any], child: dict[str, Any]) -> dict[str, Any]:
    """Merge an inherited emit block, preserving per-field modifier maps."""
    result = deepcopy(parent)
    for key, value in child.items():
        if (
            key in _MERGED_EMIT_DICTS
            and isinstance(value, dict)
            and isinstance(result.get(key), dict)
        ):
            merged = deepcopy(result[key])
            merged.update(deepcopy(value))
            result[key] = merged
            continue
        result[key] = deepcopy(value)
    return result


def _validate_raw_rule_defs(raw_rules: list[_RawRuleDef]) -> list[Rule]:
    resolved_rules = _resolve_rule_inheritance(raw_rules)
    rules = [
        _validate_rule(raw_def.raw, file=raw_def.file, line=raw_def.line)
        for raw_def in resolved_rules
    ]

    # Check for duplicate IDs
    seen: dict[str, Rule] = {}
    for rule in rules:
        if rule.id in seen:
            existing = seen[rule.id]
            raise RuleLoadError(
                f"Duplicate rule id {rule.id!r} "
                f"(first defined in {existing.source_file or '<inline>'})",
                file=rule.source_file,
                line=rule.source_line,
            )
        seen[rule.id] = rule

    return rules


# ── Public API ───────────────────────────────────────────────────────


def load_rules_from_string(text: str, *, file: Path | None = None) -> list[Rule]:
    """Parse a YAML string into a list of validated Rule objects.

    Raises RuleLoadError on any parse or schema error.
    """
    return _validate_raw_rule_defs(_load_raw_rule_defs_from_string(text, file=file))


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

    raw_rules: list[_RawRuleDef] = []
    for path in rule_files:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as e:
            raise RuleLoadError(f"Could not read {path}: {e}", file=path) from e
        raw_rules.extend(_load_raw_rule_defs_from_string(text, file=path))

    return _validate_raw_rule_defs(raw_rules)


def rule_dir_fingerprint(directory: str | Path) -> RuleDirFingerprint:
    """Return a stable fingerprint of YAML rule files in a directory."""
    directory = Path(directory)
    if not directory.exists() or not directory.is_dir():
        return ()

    files = sorted(
        path for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() in (".yaml", ".yml")
    )
    fingerprint: list[tuple[str, int, int]] = []
    for path in files:
        try:
            stat = path.stat()
        except OSError:
            continue
        fingerprint.append((path.name, stat.st_mtime_ns, stat.st_size))
    return tuple(fingerprint)
