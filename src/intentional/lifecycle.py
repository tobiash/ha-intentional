"""Lifecycle record persistence for active intents and effect activation state."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from intentional.animation import AnimationSpec
from intentional.generation import (
    GeneratedFieldState,
    ValueGeneratorSpec,
    generated_field_state_from_record,
    generated_field_state_to_record,
)
from intentional.intent import Authority, Intent


def export_lifecycle_records(
    intents: list[Intent],
    active_effect_rule_ids: set[str],
    generated_fields: dict[tuple[str, str], GeneratedFieldState] | None = None,
    *,
    now_ms: int,
) -> dict[str, Any]:
    """Return restart-safe lifecycle records for active runtime claims."""
    return {
        "version": 1,
        "intents": [
            intent_to_lifecycle_record(intent)
            for intent in intents
            if not intent.is_expired(into_the_future_ms=now_ms)
            and (intent.ttl_ms is not None or intent.ignore_when or intent.authority is Authority.USER)
        ],
        "active_effect_rule_ids": sorted(active_effect_rule_ids),
        "generated_fields": [
            generated_field_state_to_record(rule_id, field_name, state)
            for (rule_id, field_name), state in sorted((generated_fields or {}).items())
            if state.next_due_ms > now_ms
        ],
    }


def restore_lifecycle_intents(
    records: dict[str, Any] | None,
    *,
    now_ms: int,
    known_rule_ids: set[str],
) -> tuple[list[Intent], set[str], dict[tuple[str, str], GeneratedFieldState]]:
    """Restore non-expired lifecycle records that still belong to loaded rules."""
    if not records:
        return [], set(), {}
    restored: list[Intent] = []
    for raw in records.get("intents", []):
        intent = intent_from_lifecycle_record(raw)
        if intent is None or intent.is_expired(into_the_future_ms=now_ms):
            continue
        if intent.rule_id and intent.rule_id not in known_rule_ids:
            continue
        restored.append(intent)
    active_effect_rule_ids = {
        rule_id for rule_id in records.get("active_effect_rule_ids", [])
        if rule_id in known_rule_ids
    }
    generated_fields: dict[tuple[str, str], GeneratedFieldState] = {}
    for raw in records.get("generated_fields", []):
        restored_state = generated_field_state_from_record(raw)
        if restored_state is None:
            continue
        key, state = restored_state
        rule_id, _field_name = key
        if rule_id in known_rule_ids and state.next_due_ms > now_ms:
            generated_fields[key] = state
    return restored, active_effect_rule_ids, generated_fields


def intent_to_lifecycle_record(intent: Intent) -> dict[str, Any]:
    """Serialize one Intent into storage-safe primitives."""
    return {
        "target": intent.target,
        "set": dict(intent.set),
        "merge": intent.merge,
        "cap": dict(intent.cap),
        "floor": dict(intent.floor),
        "offset": dict(intent.offset),
        "multiply": dict(intent.multiply),
        "transition_ms": intent.transition_ms,
        "transition_assert_ms": intent.transition_assert_ms,
        "transition_change_ms": intent.transition_change_ms,
        "transition_withdraw_ms": intent.transition_withdraw_ms,
        "easing": intent.easing,
        "authority": intent.authority.value,
        "confidence": intent.confidence,
        "ttl_ms": intent.ttl_ms,
        "reason": intent.reason,
        "rule_id": intent.rule_id,
        "ignore_when": intent.ignore_when,
        "created_at_ms": intent.created_at_ms,
        "animation": asdict(intent.animation) if intent.animation is not None else None,
        "generators": {
            field_name: asdict(spec)
            for field_name, spec in intent.generators.items()
        },
    }


def intent_from_lifecycle_record(raw: Any) -> Intent | None:
    """Deserialize one lifecycle record, returning None for invalid data."""
    if not isinstance(raw, dict):
        return None
    try:
        authority = Authority(raw.get("authority", Authority.AUTOMATION.value))
    except ValueError:
        return None
    animation_raw = raw.get("animation")
    animation = None
    if isinstance(animation_raw, dict):
        try:
            animation = AnimationSpec(**animation_raw)
        except (TypeError, ValueError):
            animation = None
    generators: dict[str, ValueGeneratorSpec] = {}
    generators_raw = raw.get("generators")
    if isinstance(generators_raw, dict):
        for field_name, spec_raw in generators_raw.items():
            if not isinstance(field_name, str) or not isinstance(spec_raw, dict):
                continue
            try:
                generators[field_name] = ValueGeneratorSpec(**spec_raw)
            except (TypeError, ValueError):
                continue
    return Intent(
        target=str(raw.get("target", "")),
        set=dict(raw.get("set") or {}),
        merge=bool(raw.get("merge", False)),
        cap=dict(raw.get("cap") or {}),
        floor=dict(raw.get("floor") or {}),
        offset=dict(raw.get("offset") or {}),
        multiply=dict(raw.get("multiply") or {}),
        transition_ms=int(raw.get("transition_ms") or 0),
        transition_assert_ms=_optional_int(raw.get("transition_assert_ms")),
        transition_change_ms=_optional_int(raw.get("transition_change_ms")),
        transition_withdraw_ms=_optional_int(raw.get("transition_withdraw_ms")),
        easing=str(raw.get("easing", "linear")),
        authority=authority,
        confidence=float(raw.get("confidence", 1.0)),
        ttl_ms=raw.get("ttl_ms"),
        reason=str(raw.get("reason", "")),
        rule_id=str(raw.get("rule_id", "")),
        ignore_when=bool(raw.get("ignore_when", False)),
        created_at_ms=int(raw.get("created_at_ms") or 0),
        animation=animation,
        generators=generators,
    )


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)
