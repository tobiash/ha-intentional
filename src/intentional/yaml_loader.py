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

from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from intentional.animation import AnimationSpec
from intentional.capabilities import vnext_intent_policy_error
from intentional.durations import parse_duration
from intentional.generation import ValueGeneratorSpec, parse_generator_spec
from intentional.intent import Authority
from intentional.records import Effect, IntentSelector, ObserveSelector
from intentional.rule_model import Rule, RuleDirFingerprint, RuleLoadError
from intentional.target_policy import TargetPolicy

# ── Schema validation ────────────────────────────────────────────────


# Recognized top-level fields in a rule
_RULE_TOP_LEVEL = {
    "id", "extends", "when", "observe", "while", "for", "after", "stable_for", "hold", "emit", "intent", "effect", "authority", "confidence", "reason", "blocks", "enabled", "labels", "group", "profile", "notes", "edge_created",
}
# Recognized fields in the emit block
_EMIT_FIELDS = {
    "target", "scene", "set", "cap", "floor", "offset", "multiply", "merge", "generate",
    "transition", "easing", "ttl", "linger", "animation", "apply",
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
    scenes: dict[str, Any] = field(default_factory=dict)
    authored_rule_id: str | None = None
    target_policies: dict[str, TargetPolicy] = field(default_factory=dict)


class RuleSet(list[Rule]):
    """Validated Rules plus document-owned Target policies."""

    def __init__(self, rules: list[Rule], target_policies: dict[str, TargetPolicy]) -> None:
        super().__init__(rules)
        self.target_policies = dict(target_policies)


class _RawRuleDefs(list[_RawRuleDef]):
    def __init__(self) -> None:
        super().__init__()
        self.target_policies: dict[str, TargetPolicy] = {}


def _validate_rule(
    raw: dict[str, Any],
    *,
    file: Path | None = None,
    line: int | None = None,
    authored_rule_id: str | None = None,
    target_policies: dict[str, TargetPolicy] | None = None,
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

    raw = _normalize_vnext_rule(raw, file=file, line=line)

    # id
    rule_id = raw.get("id")
    if not rule_id or not isinstance(rule_id, str):
        raise RuleLoadError("Missing or invalid `id` (must be a non-empty string)", file=file, line=line)

    # when
    when = raw.get("when")
    if not when or not isinstance(when, str):
        raise RuleLoadError(f"Rule {rule_id!r}: missing or invalid `when` (must be a non-empty string)", file=file, line=line)

    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise RuleLoadError(f"Rule {rule_id!r}: `enabled` must be a boolean", file=file, line=line)

    labels_raw = raw.get("labels", [])
    if labels_raw is None:
        labels_raw = []
    if not isinstance(labels_raw, list) or not all(isinstance(label, str) for label in labels_raw):
        raise RuleLoadError(f"Rule {rule_id!r}: `labels` must be a list of strings", file=file, line=line)

    notes = raw.get("notes", "")
    if not isinstance(notes, str):
        raise RuleLoadError(f"Rule {rule_id!r}: `notes` must be a string", file=file, line=line)

    group = raw.get("group", "")
    if group is None:
        group = ""
    if not isinstance(group, str):
        raise RuleLoadError(f"Rule {rule_id!r}: `group` must be a string", file=file, line=line)

    profile = raw.get("profile", "")
    if profile is None:
        profile = ""
    if not isinstance(profile, str):
        raise RuleLoadError(f"Rule {rule_id!r}: `profile` must be a string", file=file, line=line)

    # for
    for_ms, for_entity, for_entity_unit = _parse_for(
        raw.get("for"),
        f"Rule {rule_id!r}: for",
        file,
        line,
    )

    # emit
    emit = raw.get("emit")
    effects = _parse_effects(raw.get("effect"), rule_id, file, line)
    intent_selectors = _parse_intent_selectors(raw.get("intent"), rule_id, file, line)
    observe_selectors, observe_selector_mode = _parse_observe_selectors(raw.get("observe"), rule_id, file, line)
    if emit is None and (effects or intent_selectors):
        emit = {"target": "__intentional_effect_only__"}
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
    effect_only = target == "__intentional_effect_only__"
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
    transition_assert_ms, transition_change_ms, transition_withdraw_ms = _parse_apply_transitions(
        emit.get("apply"),
        rule_id,
        file,
        line,
    )
    ttl_ms = _parse_optional_duration(
        emit.get("ttl"),
        f"Rule {rule_id!r}: ttl",
        file,
        line,
        default=None,
    )
    hold_when = _parse_hold_when(raw.get("hold"), rule_id, file, line)
    hold_until_when, hold_until_for_ms = _parse_hold_until(raw.get("hold"), rule_id, file, line)
    linger_ms = _parse_optional_duration(
        emit.get("linger"),
        f"Rule {rule_id!r}: linger",
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
    generators = _parse_generators(emit.get("generate"), rule_id, file, line)

    emit_mappings = {
        name: _normalize_emit_mapping(emit.get(name, {}))
        for name in _MERGED_EMIT_DICTS
    }

    return Rule(
        id=rule_id,
        when=when,
        authored_rule_id=authored_rule_id or rule_id,
        for_ms=for_ms,
        for_entity=for_entity,
        for_entity_unit=for_entity_unit,
        target="" if effect_only else target or "",
        scene=scene,
        set=emit_mappings["set"],
        cap=emit_mappings["cap"],
        floor=emit_mappings["floor"],
        offset=emit_mappings["offset"],
        multiply=emit_mappings["multiply"],
        merge=bool(emit.get("merge", False)),
        transition_ms=transition_ms,
        transition_assert_ms=transition_assert_ms,
        transition_change_ms=transition_change_ms,
        transition_withdraw_ms=transition_withdraw_ms,
        easing=easing,
        ttl_ms=ttl_ms,
        linger_ms=linger_ms,
        hold_when=hold_when,
        hold_until_when=hold_until_when,
        hold_until_for_ms=hold_until_for_ms,
        authority=authority,
        confidence=float(confidence),
        reason=str(raw.get("reason", "")),
        blocks=tuple(blocks_raw),
        animation=animation,
        generators=generators,
        effects=effects,
        intent_selectors=intent_selectors,
        observe_selectors=observe_selectors,
        observe_selector_mode=observe_selector_mode,
        edge_created=bool(raw.get("edge_created", False)),
        enabled=enabled,
        labels=tuple(labels_raw),
        group=group,
        profile=profile,
        notes=notes,
        source_file=file,
        source_line=line,
    )


def _normalize_emit_mapping(raw: Any) -> dict[str, Any]:
    """Return an emit mapping with YAML-coerced HA strings normalized."""
    if not isinstance(raw, dict):
        raise RuleLoadError(
            f"Intent modifier must be a mapping, got {type(raw).__name__}"
        )
    mapping = dict(raw)
    if mapping.get("state") is True:
        mapping["state"] = "on"
    elif mapping.get("state") is False:
        mapping["state"] = "off"
    if mapping.get("effect") is True:
        mapping["effect"] = "on"
    elif mapping.get("effect") is False:
        mapping["effect"] = "off"
    return mapping


def _normalize_vnext_rule(
    raw: dict[str, Any],
    *,
    file: Path | None,
    line: int | None,
) -> dict[str, Any]:
    """Normalize the first VNext observe/intent shape to the existing Rule schema."""
    if "while" not in raw and "observe" not in raw and "intent" not in raw:
        return raw

    normalized = dict(raw)
    if "observe" in normalized and "while" in normalized:
        raise RuleLoadError("Use either `while` or `observe`, not both", file=file, line=line)
    if "observe" not in normalized and "while" in normalized:
        normalized["observe"] = normalized["while"]
    if normalized.get("enabled") is False:
        normalized["when"] = "false"
    if "when" not in normalized and "observe" in normalized:
        normalized["when"] = _observe_to_when(normalized["observe"], file=file, line=line)
    elif "when" not in normalized and "intent" in normalized:
        normalized["when"] = "true"
    observe_has_for = isinstance(normalized.get("observe"), dict) and (
        "for" in normalized["observe"] or "stable_for" in normalized["observe"]
    )
    stability_fields = [field for field in ("after", "stable_for") if field in normalized]
    if stability_fields and observe_has_for:
        if "after" in normalized and isinstance(normalized.get("observe"), dict) and "for" in normalized["observe"]:
            raise RuleLoadError("Use either top-level `after` or `observe.for`, not both", file=file, line=line)
        raise RuleLoadError("Use only one stability guard: top-level `after`, top-level `stable_for`, `observe.for`, or `observe.stable_for`", file=file, line=line)
    if "after" in normalized and "stable_for" in normalized:
        raise RuleLoadError("Use either top-level `after` or `stable_for`, not both", file=file, line=line)
    if "for" not in normalized and isinstance(normalized.get("observe"), dict):
        if "for" in normalized["observe"] and "stable_for" in normalized["observe"]:
            raise RuleLoadError("Use either `observe.for` or `observe.stable_for`, not both", file=file, line=line)
        observe_for = normalized["observe"].get("for", normalized["observe"].get("stable_for"))
        if observe_for is not None:
            normalized["for"] = observe_for
    if "for" not in normalized and "after" in normalized:
        normalized["for"] = normalized["after"]
    if "for" not in normalized and "stable_for" in normalized:
        normalized["for"] = normalized["stable_for"]
    if "emit" not in normalized and "intent" in normalized:
        _normalize_vnext_suppression(normalized, file=file, line=line)
        intent = normalized["intent"]
        explicit_targets = [
            key for key in intent
            if key not in {"include", "select", "suppress"}
        ] if isinstance(intent, dict) else []
        if not explicit_targets and isinstance(intent, dict) and "select" in intent:
            normalized["emit"] = {"target": "__intentional_effect_only__"}
        else:
            normalized["emit"] = _intent_to_emit(intent, file=file, line=line)
    _normalize_hold_linger(normalized, file=file, line=line)
    if "intent" in normalized and _observe_contains_edge(normalized.get("observe")):
        normalized["edge_created"] = True
        emit = normalized.get("emit")
        if isinstance(emit, dict) and "ttl" not in emit:
            raise RuleLoadError("VNext edge-created intents require `ttl`", file=file, line=line)
    emit = normalized.get("emit")
    if isinstance(emit, dict) and "ttl" in emit and "linger" in emit:
        raise RuleLoadError("VNext target lifecycle cannot use both `ttl` and `linger`", file=file, line=line)
    return normalized


def _normalize_hold_linger(
    raw: dict[str, Any],
    *,
    file: Path | None,
    line: int | None,
) -> None:
    hold = raw.get("hold")
    if hold is None:
        return
    if not isinstance(hold, dict):
        raise RuleLoadError("`hold` must be a mapping", file=file, line=line)
    unknown = set(hold) - {"while", "until", "after", "after_when_stops"}
    if unknown:
        raise RuleLoadError(
            f"`hold` does not support fields {sorted(unknown)} yet",
            file=file,
            line=line,
        )
    hold_after = hold.get("after_when_stops", hold.get("after"))
    emit = raw.get("emit")
    if hold_after is None or not isinstance(emit, dict):
        return
    if "linger" in emit:
        raise RuleLoadError("Use either `hold.after` or target `linger`, not both", file=file, line=line)
    emit["linger"] = hold_after


def _parse_hold_when(
    hold: Any,
    rule_id: str,
    file: Path | None,
    line: int | None,
) -> str | None:
    if hold is None:
        return None
    if not isinstance(hold, dict):
        raise RuleLoadError(f"Rule {rule_id!r}: `hold` must be a mapping", file=file, line=line)
    hold_while = hold.get("while")
    if hold_while is None:
        return None
    return _observe_to_when(hold_while, file=file, line=line)


def _parse_hold_until(
    hold: Any,
    rule_id: str,
    file: Path | None,
    line: int | None,
) -> tuple[str | None, int]:
    if hold is None:
        return None, 0
    if not isinstance(hold, dict):
        raise RuleLoadError(f"Rule {rule_id!r}: `hold` must be a mapping", file=file, line=line)
    hold_until = hold.get("until")
    if hold_until is None:
        return None, 0
    if not isinstance(hold_until, dict):
        raise RuleLoadError(f"Rule {rule_id!r}: `hold.until` must be a mapping", file=file, line=line)
    release_observe = {
        key: value
        for key, value in hold_until.items()
        if key != "for"
    }
    if not release_observe:
        raise RuleLoadError(
            f"Rule {rule_id!r}: `hold.until` must contain at least one release condition",
            file=file,
            line=line,
        )
    release_when = _observe_to_when(release_observe, file=file, line=line)
    release_for_ms, release_for_entity, _release_for_entity_unit = _parse_for(
        hold_until.get("for"),
        f"Rule {rule_id!r}: hold.until.for",
        file,
        line,
    )
    if release_for_entity is not None:
        raise RuleLoadError(
            f"Rule {rule_id!r}: `hold.until.for` does not support entity-backed durations yet",
            file=file,
            line=line,
        )
    return release_when, release_for_ms


def _normalize_vnext_suppression(
    raw: dict[str, Any],
    *,
    file: Path | None,
    line: int | None,
) -> None:
    intent = raw.get("intent")
    if not isinstance(intent, dict) or "suppress" not in intent:
        return
    suppress = intent["suppress"]
    if not isinstance(suppress, dict):
        raise RuleLoadError("VNext `intent.suppress` must be a mapping", file=file, line=line)
    rules = suppress.get("rules", [])
    if not isinstance(rules, list) or not all(isinstance(rule_id, str) for rule_id in rules):
        raise RuleLoadError("VNext `intent.suppress.rules` must be a list of rule IDs", file=file, line=line)
    raw["blocks"] = [*raw.get("blocks", []), *rules]


def _observe_contains_edge(observe: Any) -> bool:
    if not isinstance(observe, dict):
        return False
    if "changed" in observe or "happened" in observe:
        return True
    for key in ("all", "any", "none"):
        items = observe.get(key)
        if isinstance(items, list) and any(_observe_contains_edge(item) for item in items):
            return True
    return _observe_contains_edge(observe.get("not"))


def _observe_to_when(
    observe: Any,
    *,
    file: Path | None,
    line: int | None,
) -> str:
    """Convert a compact level observation to the current when expression."""
    if not isinstance(observe, dict):
        raise RuleLoadError("`observe` must be a mapping", file=file, line=line)
    if set(observe) == {"any"}:
        return _observe_group_to_when("any", observe["any"], file=file, line=line)
    if set(observe) == {"all"}:
        return _observe_group_to_when("all", observe["all"], file=file, line=line)
    if set(observe) == {"none"}:
        return f"not {_observe_group_to_when('any', observe['none'], file=file, line=line)}"
    if set(observe) == {"not"}:
        return f"not ({_observe_to_when(observe['not'], file=file, line=line)})"
    if set(observe) == {"changed"}:
        return _changed_observe_to_when(observe["changed"], file=file, line=line)
    if set(observe) == {"happened"}:
        return _happened_observe_to_when(observe["happened"], file=file, line=line)
    if set(observe) == {"select"}:
        return "true"
    fields = [key for key in observe if key not in {"for", "stable_for", "select"}]
    if not fields:
        raise RuleLoadError(
            "VNext `observe` must contain at least one observed field",
            file=file,
            line=line,
        )
    return " and ".join(
        _observe_field_to_when(field, observe[field], file=file, line=line)
        for field in fields
    )


def _observe_group_to_when(
    operator: str,
    items: Any,
    *,
    file: Path | None,
    line: int | None,
) -> str:
    """Convert a grouped observation to the current boolean expression syntax."""
    if not isinstance(items, list) or not items:
        raise RuleLoadError(
            f"VNext `observe.{operator}` must be a non-empty list",
            file=file,
            line=line,
        )
    joiner = " or " if operator == "any" else " and "
    return "(" + joiner.join(
        _observe_to_when(item, file=file, line=line)
        for item in items
    ) + ")"


def _changed_observe_to_when(
    changed: Any,
    *,
    file: Path | None,
    line: int | None,
) -> str:
    if not isinstance(changed, dict) or len(changed) != 1:
        raise RuleLoadError("VNext `observe.changed` must contain exactly one entity", file=file, line=line)
    entity_id, spec = next(iter(changed.items()))
    if not isinstance(entity_id, str) or not entity_id:
        raise RuleLoadError("VNext `observe.changed` entity must be a non-empty string", file=file, line=line)
    if spec is None:
        spec = {}
    if not isinstance(spec, dict):
        raise RuleLoadError("VNext `observe.changed` spec must be a mapping", file=file, line=line)
    unknown = set(spec) - {"to", "within"}
    if unknown:
        raise RuleLoadError(
            f"VNext `observe.changed` does not support fields {sorted(unknown)} yet",
            file=file,
            line=line,
        )
    parts = [f"{entity_id}.changed == true"]
    if "to" in spec:
        parts.append(f"{entity_id} == {_when_literal(spec['to'])}")
    return " and ".join(parts)


def _happened_observe_to_when(
    happened: Any,
    *,
    file: Path | None,
    line: int | None,
) -> str:
    if not isinstance(happened, dict) or len(happened) != 1:
        raise RuleLoadError("VNext `observe.happened` must contain exactly one event entity", file=file, line=line)
    entity_id, spec = next(iter(happened.items()))
    if not isinstance(entity_id, str) or not entity_id.startswith("event."):
        raise RuleLoadError("VNext `observe.happened` currently supports event.* entities", file=file, line=line)
    if spec is None:
        spec = {}
    if not isinstance(spec, dict):
        raise RuleLoadError("VNext `observe.happened` spec must be a mapping", file=file, line=line)
    parts = [f"{entity_id}.triggered == true"]
    for observed_field, value in spec.items():
        if observed_field == "within":
            continue
        parts.append(f"{entity_id}.{observed_field} == {_when_literal(value)}")
    return " and ".join(parts)


def _observe_field_to_when(
    field: str,
    expected: Any,
    *,
    file: Path | None,
    line: int | None,
) -> str:
    """Convert one observed field predicate to a current when expression."""
    if isinstance(expected, dict):
        if len(expected) != 1:
            raise RuleLoadError(
                "VNext `observe` comparisons must contain exactly one operator",
                file=file,
                line=line,
        )
        operator, expected = next(iter(expected.items()))
        return f"{field} {_observe_operator_to_when(operator, file=file, line=line)} {_when_literal(expected)}"
    return f"{field} == {_when_literal(expected)}"


def _observe_operator_to_when(
    operator: str,
    *,
    file: Path | None,
    line: int | None,
) -> str:
    operators = {
        "is": "==",
        "is_not": "!=",
        "lt": "<",
        "lte": "<=",
        "gt": ">",
        "gte": ">=",
    }
    try:
        return operators[operator]
    except KeyError as e:
        raise RuleLoadError(
            f"VNext `observe` does not support operator {operator!r} yet",
            file=file,
            line=line,
        ) from e


def _when_literal(value: Any) -> str:
    if value is True:
        return '"on"'
    if value is False:
        return '"off"'
    if isinstance(value, str):
        return repr(value).replace("'", '"')
    return repr(value)


def _intent_to_emit(
    intent: Any,
    *,
    file: Path | None,
    line: int | None,
) -> dict[str, Any]:
    """Convert a single VNext target intent to the current emit schema."""
    if not isinstance(intent, dict):
        raise RuleLoadError("`intent` must be a mapping", file=file, line=line)
    target_items = [
        (key, value)
        for key, value in intent.items()
        if key not in {"include", "select", "suppress"}
    ]
    if len(target_items) != 1:
        raise RuleLoadError(
            "VNext `intent` currently supports exactly one explicit target",
            file=file,
            line=line,
        )
    target, fields = target_items[0]
    if not isinstance(target, str) or not target:
        raise RuleLoadError("VNext `intent` target must be a non-empty string", file=file, line=line)
    if not isinstance(fields, dict):
        raise RuleLoadError(
            f"VNext `intent` for {target!r} must be a mapping",
            file=file,
            line=line,
        )

    return _intent_fields_to_emit(target, fields, file=file, line=line)


def _intent_fields_to_emit(
    target: str,
    fields: dict[str, Any],
    *,
    file: Path | None,
    line: int | None,
) -> dict[str, Any]:
    """Normalize VNext target fields to the current emit schema."""

    emit: dict[str, Any] = {"target": target, "set": {}, "cap": {}, "floor": {}, "offset": {}, "multiply": {}, "generate": {}}
    for intent_field, value in fields.items():
        if intent_field == "apply":
            emit["apply"] = value
            continue
        _validate_vnext_intent_field(target, intent_field, value, file=file, line=line)
        if intent_field in {"ttl", "linger", "transition", "easing"}:
            emit[intent_field] = value
            continue
        if isinstance(value, dict) and set(value) & {"value", "min", "max", "offset", "multiply", "animate", "generate"}:
            if "animate" in value:
                if "animation" in emit:
                    raise RuleLoadError("One animated field per VNext target is supported", file=file, line=line)
                emit["animation"] = _vnext_inline_animation_to_legacy(intent_field, value["animate"], file=file, line=line)
                if "value" not in value:
                    initial = _vnext_animation_initial_value(emit["animation"])
                    if initial is not None:
                        emit["set"][intent_field] = initial
            if "value" in value:
                emit["set"][intent_field] = value["value"]
            if "max" in value:
                emit["cap"][intent_field] = value["max"]
            if "min" in value:
                emit["floor"][intent_field] = value["min"]
            if "offset" in value:
                emit["offset"][intent_field] = value["offset"]
            if "multiply" in value:
                emit["multiply"][intent_field] = value["multiply"]
            if "generate" in value:
                emit["generate"][intent_field] = value["generate"]
            continue
        emit["set"][intent_field] = value

    return {key: value for key, value in emit.items() if key == "target" or value}


def _vnext_inline_animation_to_legacy(
    field: str,
    raw: Any,
    *,
    file: Path | None,
    line: int | None,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise RuleLoadError("VNext inline `animate` must be a mapping", file=file, line=line)
    animation = dict(raw)
    animation["parameter"] = field
    if "kind" in animation:
        return animation

    kind_keys = set(animation) & {"pulse", "cycle", "breath", "flash"}
    if len(kind_keys) != 1:
        raise RuleLoadError(
            "VNext inline `animate` must specify exactly one of pulse, cycle, breath, or flash",
            file=file,
            line=line,
        )
    kind = next(iter(kind_keys))
    kind_value = animation.pop(kind)
    animation["kind"] = kind
    if kind in {"pulse", "cycle"}:
        animation["values"] = kind_value
    elif kind == "breath":
        if isinstance(kind_value, list | tuple) and len(kind_value) == 2:
            animation["min"], animation["max"] = kind_value
        elif isinstance(kind_value, dict):
            animation.update(kind_value)
        else:
            raise RuleLoadError("VNext breath animation must be [min, max] or a mapping", file=file, line=line)
    elif kind == "flash":
        animation["peak"] = kind_value
    return animation


def _vnext_animation_initial_value(animation: dict[str, Any]) -> Any:
    kind = animation.get("kind")
    if kind in {"pulse", "cycle"}:
        values = animation.get("values")
        if isinstance(values, list | tuple) and values:
            return values[0]
    if kind == "breath":
        return animation.get("min")
    if kind == "flash":
        return animation.get("peak")
    return None


def _validate_vnext_intent_field(
    target: str,
    field: str,
    value: Any,
    *,
    file: Path | None,
    line: int | None,
) -> None:
    message = vnext_intent_policy_error(target, field, value)
    if message is not None:
        raise RuleLoadError(message, file=file, line=line)


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
        except (TypeError, ValueError) as e:
            raise RuleLoadError(f"{label}: {e}", file=file, line=line) from e
    raise RuleLoadError(
        f"{label} must be an integer (ms) or a duration string, got {type(value).__name__}",
        file=file, line=line,
    )


def _parse_apply_transitions(
    raw: Any,
    rule_id: str,
    file: Path | None,
    line: int | None,
) -> tuple[int | None, int | None, int | None]:
    if raw is None:
        return None, None, None
    if not isinstance(raw, dict):
        raise RuleLoadError(f"Rule {rule_id!r}: apply must be a mapping", file=file, line=line)
    unknown = set(raw) - {"transition"}
    if unknown:
        raise RuleLoadError(
            f"Rule {rule_id!r}: unknown fields in apply: {sorted(unknown)}. Allowed: ['transition']",
            file=file,
            line=line,
        )
    transition = raw.get("transition")
    if transition is None:
        return None, None, None
    if not isinstance(transition, dict):
        raise RuleLoadError(f"Rule {rule_id!r}: apply.transition must be a mapping", file=file, line=line)
    unknown_transition = set(transition) - {"assert", "change", "withdraw"}
    if unknown_transition:
        raise RuleLoadError(
            f"Rule {rule_id!r}: unknown fields in apply.transition: {sorted(unknown_transition)}. "
            "Allowed: ['assert', 'change', 'withdraw']",
            file=file,
            line=line,
        )
    return (
        _parse_optional_duration(transition.get("assert"), f"Rule {rule_id!r}: apply.transition.assert", file, line, default=None),
        _parse_optional_duration(transition.get("change"), f"Rule {rule_id!r}: apply.transition.change", file, line, default=None),
        _parse_optional_duration(transition.get("withdraw"), f"Rule {rule_id!r}: apply.transition.withdraw", file, line, default=None),
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


def _parse_generators(
    raw: Any,
    rule_id: str,
    file: Path | None,
    line: int | None,
) -> dict[str, ValueGeneratorSpec]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise RuleLoadError(
            f"Rule {rule_id!r}: `generate` must be a mapping",
            file=file,
            line=line,
        )
    generators: dict[str, ValueGeneratorSpec] = {}
    for field_name, spec_raw in raw.items():
        if not isinstance(field_name, str) or not field_name:
            raise RuleLoadError(
                f"Rule {rule_id!r}: generated field names must be non-empty strings",
                file=file,
                line=line,
            )
        try:
            generators[field_name] = parse_generator_spec(
                spec_raw,
                parse_duration=_parse_generator_duration,
            )
        except ValueError as e:
            raise RuleLoadError(
                f"Rule {rule_id!r}: invalid generator for {field_name!r}: {e}",
                file=file,
                line=line,
            ) from e
    return generators


def _parse_generator_duration(value: Any) -> int:
    if isinstance(value, int):
        if value <= 0:
            raise ValueError(f"duration must be positive, got {value}")
        return value
    if isinstance(value, str):
        return parse_duration(value)
    raise ValueError(f"duration must be an integer (ms) or duration string, got {type(value).__name__}")


def _parse_effects(
    raw: Any,
    rule_id: str,
    file: Path | None,
    line: int | None,
) -> tuple[Effect, ...]:
    if raw is None:
        return ()
    raw_effects = raw if isinstance(raw, list) else [raw]
    if not isinstance(raw_effects, list):
        raise RuleLoadError(f"Rule {rule_id!r}: `effect` must be a mapping or list", file=file, line=line)

    effects: list[Effect] = []
    for effect in raw_effects:
        if not isinstance(effect, dict):
            raise RuleLoadError(f"Rule {rule_id!r}: each `effect` must be a mapping", file=file, line=line)
        unknown = set(effect) - {"service", "target", "data"}
        if unknown:
            raise RuleLoadError(
                f"Rule {rule_id!r}: unknown fields in `effect`: {sorted(unknown)}",
                file=file,
                line=line,
            )
        service_name = effect.get("service")
        if not isinstance(service_name, str) or "." not in service_name:
            raise RuleLoadError(
                f"Rule {rule_id!r}: `effect.service` must be a domain.service string",
                file=file,
                line=line,
            )
        domain, service = service_name.split(".", 1)
        target = effect.get("target", {})
        data = effect.get("data", {})
        if not isinstance(target, dict):
            raise RuleLoadError(f"Rule {rule_id!r}: `effect.target` must be a mapping", file=file, line=line)
        if not isinstance(data, dict):
            raise RuleLoadError(f"Rule {rule_id!r}: `effect.data` must be a mapping", file=file, line=line)
        effects.append(Effect(domain=domain, service=service, target=dict(target), data=dict(data)))
    return tuple(effects)


def _parse_intent_selectors(
    intent: Any,
    rule_id: str,
    file: Path | None,
    line: int | None,
) -> tuple[IntentSelector, ...]:
    if not isinstance(intent, dict) or "select" not in intent:
        return ()
    raw_selectors = intent["select"]
    if not isinstance(raw_selectors, list):
        raise RuleLoadError(f"Rule {rule_id!r}: `intent.select` must be a list", file=file, line=line)
    selectors: list[IntentSelector] = []
    for raw_selector in raw_selectors:
        if not isinstance(raw_selector, dict):
            raise RuleLoadError(f"Rule {rule_id!r}: selector entries must be mappings", file=file, line=line)
        selectors.append(_parse_intent_selector(raw_selector, rule_id, file, line))
    return tuple(selectors)


def _parse_observe_selectors(
    observe: Any,
    rule_id: str,
    file: Path | None,
    line: int | None,
) -> tuple[tuple[ObserveSelector, ...], str]:
    if not isinstance(observe, dict) or "select" not in observe:
        return (), "any"
    raw_select = observe["select"]
    if not isinstance(raw_select, dict):
        raise RuleLoadError(f"Rule {rule_id!r}: `observe.select` must be a mapping", file=file, line=line)
    mode = raw_select.get("mode", "any")
    if mode not in {"any", "all", "none"}:
        raise RuleLoadError(f"Rule {rule_id!r}: `observe.select.mode` must be any, all, or none", file=file, line=line)
    raw_entities = raw_select.get("entities")
    if not isinstance(raw_entities, list) or not raw_entities:
        raise RuleLoadError(f"Rule {rule_id!r}: `observe.select.entities` must be a non-empty list", file=file, line=line)
    return tuple(
        _parse_observe_selector(raw_selector, rule_id, file, line)
        for raw_selector in raw_entities
    ), mode


def _parse_observe_selector(
    raw: Any,
    rule_id: str,
    file: Path | None,
    line: int | None,
) -> ObserveSelector:
    if not isinstance(raw, dict):
        raise RuleLoadError(f"Rule {rule_id!r}: `observe.select.entities` entries must be mappings", file=file, line=line)
    selector_keys = {"domain", "area", "label", "exclude"}
    domain = raw.get("domain")
    area = raw.get("area")
    label = raw.get("label")
    if domain is not None and not isinstance(domain, str):
        raise RuleLoadError(f"Rule {rule_id!r}: observe selector `domain` must be a string", file=file, line=line)
    if area is not None and not isinstance(area, str):
        raise RuleLoadError(f"Rule {rule_id!r}: observe selector `area` must be a string", file=file, line=line)
    if label is not None and not isinstance(label, str):
        raise RuleLoadError(f"Rule {rule_id!r}: observe selector `label` must be a string", file=file, line=line)
    if domain is None and area is None and label is None:
        raise RuleLoadError(f"Rule {rule_id!r}: observe selector requires domain, area, or label", file=file, line=line)
    exclude_raw = raw.get("exclude", [])
    if not isinstance(exclude_raw, list) or not all(isinstance(item, str) for item in exclude_raw):
        raise RuleLoadError(f"Rule {rule_id!r}: observe selector `exclude` must be a list of entity IDs", file=file, line=line)
    comparisons = {key: value for key, value in raw.items() if key not in selector_keys}
    if not comparisons:
        comparisons = {"state": "on"}
    if len(comparisons) != 1:
        raise RuleLoadError(f"Rule {rule_id!r}: observe selector supports one comparison field", file=file, line=line)
    field, value = next(iter(comparisons.items()))
    operator = "is"
    expected = value
    if isinstance(value, dict):
        if len(value) != 1:
            raise RuleLoadError(f"Rule {rule_id!r}: observe selector comparison must contain one operator", file=file, line=line)
        operator, expected = next(iter(value.items()))
        _observe_operator_to_when(operator, file=file, line=line)
    return ObserveSelector(
        domain=domain,
        area=area,
        label=label,
        exclude=tuple(exclude_raw),
        field=str(field),
        operator=operator,
        value=_observe_selector_expected_value(expected),
    )


def _observe_selector_expected_value(value: Any) -> Any:
    if value is True:
        return "on"
    if value is False:
        return "off"
    return value


def _parse_intent_selector(
    raw: dict[str, Any],
    rule_id: str,
    file: Path | None,
    line: int | None,
) -> IntentSelector:
    selector_keys = {"domain", "area", "label", "exclude"}
    unknown_filters = {key for key in selector_keys if key in raw and raw[key] is None}
    if unknown_filters:
        raise RuleLoadError(f"Rule {rule_id!r}: selector filters cannot be null", file=file, line=line)
    domain = raw.get("domain")
    area = raw.get("area")
    label = raw.get("label")
    if domain is not None and not isinstance(domain, str):
        raise RuleLoadError(f"Rule {rule_id!r}: selector `domain` must be a string", file=file, line=line)
    if area is not None and not isinstance(area, str):
        raise RuleLoadError(f"Rule {rule_id!r}: selector `area` must be a string", file=file, line=line)
    if label is not None and not isinstance(label, str):
        raise RuleLoadError(f"Rule {rule_id!r}: selector `label` must be a string", file=file, line=line)
    if not any((domain, area, label)):
        raise RuleLoadError(f"Rule {rule_id!r}: selector requires domain, area, or label", file=file, line=line)
    exclude = raw.get("exclude", [])
    if exclude is None:
        exclude = []
    if not isinstance(exclude, list) or not all(isinstance(entity_id, str) for entity_id in exclude):
        raise RuleLoadError(f"Rule {rule_id!r}: selector `exclude` must be a list of entity IDs", file=file, line=line)

    fields = {key: value for key, value in raw.items() if key not in selector_keys}
    emit = _intent_fields_to_emit("<selector>", fields, file=file, line=line)
    return IntentSelector(
        domain=domain,
        area=area,
        label=label,
        exclude=tuple(exclude),
        set=_normalize_emit_mapping(emit.get("set", {})),
        cap=dict(emit.get("cap", {})),
        floor=dict(emit.get("floor", {})),
        offset=dict(emit.get("offset", {})),
        multiply=dict(emit.get("multiply", {})),
        transition_ms=_parse_optional_duration(emit.get("transition"), f"Rule {rule_id!r}: selector.transition", file, line) or 0,
        easing=str(emit.get("easing", "linear")),
        ttl_ms=_parse_optional_duration(emit.get("ttl"), f"Rule {rule_id!r}: selector.ttl", file, line, default=None),
        linger_ms=_parse_optional_duration(emit.get("linger"), f"Rule {rule_id!r}: selector.linger", file, line, default=None),
    )


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

    raw_rules = _RawRuleDefs()
    all_scenes: dict[str, Any] = {}
    all_target_policies: dict[str, TargetPolicy] = {}
    for doc in docs:
        if doc is None:
            continue  # empty document
        if isinstance(doc, dict):
            unknown = set(doc) - {"rules", "scenes", "targets"}
            if unknown:
                raise RuleLoadError(
                    f"Unknown top-level document fields: {sorted(unknown)}. "
                    "Allowed: ['rules', 'scenes', 'targets']",
                    file=file,
                )
            doc_scenes = doc.get("scenes", {})
            if doc_scenes is None:
                doc_scenes = {}
            if not isinstance(doc_scenes, dict):
                raise RuleLoadError("Document `scenes` must be a mapping", file=file)
            for scene_id, scene in doc_scenes.items():
                if scene_id in all_scenes:
                    raise RuleLoadError(f"Duplicate scene id {scene_id!r}", file=file)
                all_scenes[scene_id] = scene
            target_policies = _target_policies_from_document(doc.get("targets", {}), file=file)
            for target, policy in target_policies.items():
                if target in all_target_policies and all_target_policies[target] != policy:
                    raise RuleLoadError(f"Conflicting Target policy for {target!r}", file=file)
                all_target_policies[target] = policy
                raw_rules.target_policies[target] = policy
            raw_rules.extend(
                _target_default_rule_defs_from_document(
                    doc.get("targets", {}),
                    file=file,
                    scenes=all_scenes,
                )
            )
            doc_rules = doc.get("rules", [])
            if not isinstance(doc_rules, list):
                raise RuleLoadError("Document `rules` must be a list", file=file)
            doc = doc_rules
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
            raw_rules.append(_RawRuleDef(
                raw=raw, file=file, line=line_num, scenes=all_scenes,
                target_policies=dict(all_target_policies),
            ))

    for raw_def in raw_rules:
        raw_def.target_policies.update(all_target_policies)

    return raw_rules


_TARGET_POLICY_FIELDS = {
    "default", "ownership", "allowed_fields", "forbidden_automatic_states",
    "unavailable", "max_retries", "user_authority",
}


def _target_policies_from_document(raw_targets: Any, *, file: Path | None) -> dict[str, TargetPolicy]:
    if raw_targets is None:
        return {}
    if not isinstance(raw_targets, dict):
        raise RuleLoadError("Document `targets` must be a mapping", file=file)
    result = {}
    for target, raw in raw_targets.items():
        if not isinstance(target, str) or not target or "." not in target:
            raise RuleLoadError("Document `targets` keys must be entity IDs", file=file)
        if not isinstance(raw, dict):
            raise RuleLoadError(f"Target policy for {target!r} must be a mapping", file=file)
        unknown = set(raw) - _TARGET_POLICY_FIELDS
        if unknown:
            raise RuleLoadError(
                f"Target policy for {target!r} has unknown fields: {sorted(unknown)}. "
                f"Allowed: {sorted(_TARGET_POLICY_FIELDS)}", file=file,
            )
        ownership = raw.get("ownership", "managed")
        if ownership not in {"managed", "opportunistic", "observe_only"}:
            raise RuleLoadError(f"Target {target!r}: `ownership` must be managed, opportunistic, or observe_only", file=file)
        unavailable = raw.get("unavailable", "allow")
        if unavailable not in {"allow", "skip"}:
            raise RuleLoadError(f"Target {target!r}: `unavailable` must be allow or skip", file=file)
        allowed_fields = _string_set(raw.get("allowed_fields"), target, "allowed_fields", file, optional=True)
        forbidden = _string_set(raw.get("forbidden_automatic_states", []), target, "forbidden_automatic_states", file)
        user = raw.get("user_authority", {})
        if not isinstance(user, dict) or set(user) - {"fields", "states"}:
            raise RuleLoadError(f"Target {target!r}: `user_authority` must contain only fields and states", file=file)
        max_retries = raw.get("max_retries")
        if max_retries is not None and (not isinstance(max_retries, int) or isinstance(max_retries, bool) or max_retries < 0):
            raise RuleLoadError(f"Target {target!r}: `max_retries` must be a non-negative integer", file=file)
        result[target] = TargetPolicy(
            ownership=ownership,
            allowed_fields=allowed_fields,
            forbidden_automatic_states=forbidden or frozenset(),
            unavailable=unavailable,
            max_retries=max_retries,
            user_authority_fields=_string_set(user.get("fields", []), target, "user_authority.fields", file) or frozenset(),
            user_authority_states=_string_set(user.get("states", []), target, "user_authority.states", file) or frozenset(),
        )
    return result


def _string_set(value: Any, target: str, field: str, file: Path | None, *, optional: bool = False) -> frozenset[str] | None:
    if value is None and optional:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise RuleLoadError(f"Target {target!r}: `{field}` must be a list of non-empty strings", file=file)
    return frozenset(value)


def _target_default_rule_defs_from_document(
    raw_targets: Any,
    *,
    file: Path | None,
    scenes: dict[str, Any],
) -> list[_RawRuleDef]:
    if raw_targets is None:
        return []
    if not isinstance(raw_targets, dict):
        raise RuleLoadError("Document `targets` must be a mapping", file=file)
    defaults: list[_RawRuleDef] = []
    for target, policy in raw_targets.items():
        if not isinstance(target, str) or not target:
            raise RuleLoadError("Document `targets` keys must be non-empty entity IDs", file=file)
        if not isinstance(policy, dict):
            raise RuleLoadError(f"Target policy for {target!r} must be a mapping", file=file)
        unknown = set(policy) - _TARGET_POLICY_FIELDS
        if unknown:
            raise RuleLoadError(
                f"Target policy for {target!r} has unknown fields: {sorted(unknown)}. "
                f"Allowed: {sorted(_TARGET_POLICY_FIELDS)}",
                file=file,
            )
        default = policy.get("default")
        if default is None:
            continue
        if isinstance(default, str):
            default = {"state": default}
        if isinstance(default, bool):
            default = {"state": default}
        if not isinstance(default, dict):
            raise RuleLoadError(f"Target default for {target!r} must be a mapping or scalar state", file=file)
        defaults.append(_RawRuleDef(
            raw={
                "id": f"__target_default__:{target}",
                "when": "true",
                "emit": {
                    "target": target,
                    "set": _normalize_emit_mapping(default),
                },
                "authority": Authority.SENSOR.value,
                "confidence": 0.0,
                "reason": f"Default state for {target}",
                "group": "target-defaults",
                "profile": "default",
            },
            file=file,
            line=None,
            scenes=scenes,
        ))
    return defaults


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
            output.append(_RawRuleDef(
                raw=resolve(rule_id),
                file=raw_def.file,
                line=raw_def.line,
                authored_rule_id=raw_def.authored_rule_id,
                target_policies=raw_def.target_policies,
            ))
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
    scenes: dict[str, Any] = {}
    for raw_def in raw_rules:
        for scene_id, scene in raw_def.scenes.items():
            if scene_id in scenes and scenes[scene_id] != scene:
                raise RuleLoadError(f"Duplicate scene id {scene_id!r}", file=raw_def.file)
            scenes[scene_id] = scene
    resolved_rules = _resolve_rule_inheritance(raw_rules)
    resolved_rules = [
        _expand_vnext_scene_includes(raw_def, scenes)
        for raw_def in resolved_rules
    ]
    expanded_rules = []
    for raw_def in resolved_rules:
        expanded_rules.extend(_expand_vnext_multi_target_rule(raw_def))
    rules = [
        _validate_rule(
            raw_def.raw,
            file=raw_def.file,
            line=raw_def.line,
            authored_rule_id=raw_def.authored_rule_id,
            target_policies=raw_def.target_policies,
        )
        for raw_def in expanded_rules
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


def _expand_vnext_scene_includes(
    raw_def: _RawRuleDef,
    scenes: dict[str, Any],
) -> _RawRuleDef:
    raw = raw_def.raw
    intent = raw.get("intent")
    if not isinstance(intent, dict) or "include" not in intent:
        return raw_def

    expanded_intent = _expand_intent_includes(intent, scenes, stack=())
    expanded = deepcopy(raw)
    expanded["intent"] = expanded_intent
    return _RawRuleDef(
        raw=expanded,
        file=raw_def.file,
        line=raw_def.line,
        scenes=raw_def.scenes,
            authored_rule_id=raw_def.authored_rule_id,
            target_policies=raw_def.target_policies,
    )


def _expand_intent_includes(
    intent: dict[str, Any],
    scenes: dict[str, Any],
    *,
    stack: tuple[str, ...],
) -> dict[str, Any]:
    includes = intent.get("include", [])
    if isinstance(includes, str):
        includes = [includes]
    if not isinstance(includes, list) or not all(isinstance(item, str) for item in includes):
        raise RuleLoadError("VNext `intent.include` must be a scene id string or list of scene ids")

    result: dict[str, Any] = {}
    for include in includes:
        scene_id = include.removeprefix("scene.")
        if scene_id in stack:
            cycle = " -> ".join((*stack, scene_id))
            raise RuleLoadError(f"Scene include cycle: {cycle}")
        scene = scenes.get(scene_id)
        if not isinstance(scene, dict) or not isinstance(scene.get("intent"), dict):
            raise RuleLoadError(f"Unknown or invalid scene include {include!r}")
        scene_intent = _expand_intent_includes(scene["intent"], scenes, stack=(*stack, scene_id))
        result = _merge_vnext_intents(result, scene_intent)

    inline_intent = {
        key: deepcopy(value)
        for key, value in intent.items()
        if key != "include"
    }
    return _merge_vnext_intents(result, inline_intent)


def _merge_vnext_intents(parent: dict[str, Any], child: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(parent)
    for target, fields in child.items():
        if target in result and isinstance(result[target], dict) and isinstance(fields, dict):
            result[target] = _merge_vnext_target_fields(result[target], fields)
        else:
            result[target] = deepcopy(fields)
    return result


def _merge_vnext_target_fields(parent: dict[str, Any], child: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(parent)
    for intent_field, value in child.items():
        if intent_field in result:
            result[intent_field] = _merge_vnext_field_value(result[intent_field], value)
        else:
            result[intent_field] = deepcopy(value)
    return result


def _merge_vnext_field_value(parent: Any, child: Any) -> Any:
    operator_keys = {"value", "min", "max", "offset", "multiply", "animate"}
    parent_is_operator = isinstance(parent, dict) and bool(set(parent) & operator_keys)
    child_is_operator = isinstance(child, dict) and bool(set(child) & operator_keys)
    if parent_is_operator and child_is_operator:
        merged = deepcopy(parent)
        merged.update(deepcopy(child))
        return merged
    if child_is_operator and not parent_is_operator and "value" not in child:
        merged = {"value": deepcopy(parent)}
        merged.update(deepcopy(child))
        return merged
    return deepcopy(child)


def _expand_vnext_multi_target_rule(raw_def: _RawRuleDef) -> list[_RawRuleDef]:
    """Expand a VNext multi-target intent into one current Rule per target."""
    raw = raw_def.raw
    if "emit" in raw or "intent" not in raw or not isinstance(raw.get("intent"), dict):
        return [raw_def]
    intent = raw["intent"]
    target_keys = [
        key for key in intent
        if key not in {"include", "select", "suppress"}
    ]
    if len(target_keys) <= 1:
        return [raw_def]

    expanded: list[_RawRuleDef] = []
    rule_id = raw.get("id")
    for target in target_keys:
        item = deepcopy(raw)
        item["id"] = f"{rule_id}:{target}"
        item_intent = {target: deepcopy(intent[target])}
        if "suppress" in intent:
            item_intent["suppress"] = deepcopy(intent["suppress"])
        if not expanded and "select" in intent:
            item_intent["select"] = deepcopy(intent["select"])
        item["intent"] = item_intent
        if expanded:
            item.pop("effect", None)
        expanded.append(_RawRuleDef(
            raw=item,
            file=raw_def.file,
            line=raw_def.line,
            authored_rule_id=rule_id if isinstance(rule_id, str) else None,
            target_policies=raw_def.target_policies,
        ))
    return expanded


# ── Public API ───────────────────────────────────────────────────────


def load_rules_from_string(text: str, *, file: Path | None = None) -> RuleSet:
    """Parse a YAML string into a list of validated Rule objects.

    Raises RuleLoadError on any parse or schema error.
    """
    raw_rules = _load_raw_rule_defs_from_string(text, file=file)
    policies = _policies_from_raw_defs(raw_rules)
    return RuleSet(_validate_raw_rule_defs(raw_rules), policies)


def load_rules(directory: str | Path) -> RuleSet:
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

    raw_rules = _RawRuleDefs()
    for path in rule_files:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as e:
            raise RuleLoadError(f"Could not read {path}: {e}", file=path) from e
        loaded = _load_raw_rule_defs_from_string(text, file=path)
        for target, policy in loaded.target_policies.items():
            existing = raw_rules.target_policies.get(target)
            if existing is not None and existing != policy:
                raise RuleLoadError(f"Conflicting Target policy for {target!r}", file=path)
            raw_rules.target_policies[target] = policy
        raw_rules.extend(loaded)

    return RuleSet(_validate_raw_rule_defs(raw_rules), _policies_from_raw_defs(raw_rules))


def _policies_from_raw_defs(raw_rules: list[_RawRuleDef]) -> dict[str, TargetPolicy]:
    policies: dict[str, TargetPolicy] = dict(getattr(raw_rules, "target_policies", {}))
    for raw_def in raw_rules:
        for target, policy in raw_def.target_policies.items():
            existing = policies.get(target)
            if existing is not None and existing != policy:
                raise RuleLoadError(f"Conflicting Target policy for {target!r}", file=raw_def.file)
            policies[target] = policy
    return policies


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
