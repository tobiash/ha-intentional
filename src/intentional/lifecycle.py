"""Lifecycle record persistence for active intents and effect activation state."""

from __future__ import annotations

import math
from dataclasses import asdict, replace
from typing import Any

from intentional.animation import AnimationSpec
from intentional.generation import (
    GeneratedFieldState,
    ValueGeneratorSpec,
    generated_field_state_from_record,
    generated_field_state_to_record,
)
from intentional.intent import Authority, Intent
from intentional.records import EffectOutboxRecord


def export_lifecycle_records(
    intents: list[Intent],
    active_effect_rule_ids: set[str],
    generated_fields: dict[tuple[str, str], GeneratedFieldState] | None = None,
    effect_outbox: list[EffectOutboxRecord] | None = None,
    rule_fingerprints: dict[str, str] | None = None,
    *,
    now_ms: int,
) -> dict[str, Any]:
    """Return restart-safe lifecycle records for active runtime claims."""
    return {
        "version": 2,
        "intents": [
            intent_to_lifecycle_record(
                intent,
                rule_fingerprint=(rule_fingerprints or {}).get(intent.rule_id),
            )
            for intent in intents
            if not intent.is_expired(into_the_future_ms=now_ms)
            and (
                intent.rule_id
                or intent.ttl_ms is not None
                or intent.ignore_when
                or intent.authority is Authority.USER
            )
        ],
        "active_effect_rule_ids": sorted(active_effect_rule_ids),
        "generated_fields": [
            generated_field_state_to_record(rule_id, field_name, state)
            for (rule_id, field_name), state in sorted((generated_fields or {}).items())
            if state.next_due_ms > now_ms
        ],
        "effect_outbox": [asdict(record) for record in (effect_outbox or [])],
    }


def restore_lifecycle_intents(
    records: dict[str, Any] | None,
    *,
    now_ms: int,
    known_rule_ids: set[str],
    rule_fingerprints: dict[str, str] | None = None,
) -> tuple[list[Intent], set[str], dict[tuple[str, str], GeneratedFieldState]]:
    """Restore non-expired lifecycle records that still belong to loaded rules."""
    version = lifecycle_version(records)
    if version is None:
        return [], set(), {}
    restored: list[Intent] = []
    for raw in _record_list(records, "intents"):
        intent = intent_from_lifecycle_record(raw)
        if intent is None:
            continue
        if intent.ttl_ms is not None and now_ms < intent.created_at_ms:
            intent = replace(intent, created_at_ms=now_ms)
        if intent.is_expired(into_the_future_ms=now_ms):
            continue
        if intent.rule_id:
            if intent.rule_id not in known_rule_ids:
                continue
            if version == 2:
                expected = (rule_fingerprints or {}).get(intent.rule_id)
                if not expected or raw.get("rule_fingerprint") != expected:
                    continue
        restored.append(intent)
    active_effect_rule_ids = {
        rule_id
        for rule_id in _record_list(records, "active_effect_rule_ids")
        if isinstance(rule_id, str) and rule_id in known_rule_ids
    }
    generated_fields: dict[tuple[str, str], GeneratedFieldState] = {}
    for raw in _record_list(records, "generated_fields"):
        restored_state = generated_field_state_from_record(raw)
        if restored_state is None:
            continue
        key, state = restored_state
        rule_id, _field_name = key
        if rule_id in known_rule_ids and state.next_due_ms > now_ms:
            generated_fields[key] = state
    return restored, active_effect_rule_ids, generated_fields


def restore_effect_outbox(records: dict[str, Any] | None) -> list[EffectOutboxRecord]:
    """Restore valid Effect records, including entries from removed Rules."""
    if lifecycle_version(records) is None:
        return []
    restored: list[EffectOutboxRecord] = []
    for raw in _record_list(records, "effect_outbox"):
        if not isinstance(raw, dict):
            continue
        try:
            record = EffectOutboxRecord(
                activation_id=_required_string(raw, "activation_id"),
                rule_id=_required_string(raw, "rule_id"),
                rule_fingerprint=_required_string(raw, "rule_fingerprint"),
                effect_index=_nonnegative_int(raw["effect_index"]),
                domain=_required_string(raw, "domain"),
                service=_required_string(raw, "service"),
                target=_string_dict(raw.get("target", {})),
                data=_string_dict(raw.get("data", {})),
                attempts=_nonnegative_int(raw.get("attempts", 0)),
                next_retry_ms=_nonnegative_int(raw.get("next_retry_ms", 0)),
                acknowledged_at_ms=_optional_nonnegative_int(raw.get("acknowledged_at_ms")),
                dead_lettered_at_ms=_optional_nonnegative_int(raw.get("dead_lettered_at_ms")),
                last_error=_optional_string(raw.get("last_error")),
            )
            if record.acknowledged_at_ms is None:
                restored.append(record)
        except (KeyError, TypeError, ValueError):
            continue
    live = [record for record in restored if record.dead_lettered_at_ms is None]
    dead = sorted(
        (record for record in restored if record.dead_lettered_at_ms is not None),
        key=lambda record: record.dead_lettered_at_ms or 0,
    )[-100:]
    return live + dead


def intent_to_lifecycle_record(
    intent: Intent, *, rule_fingerprint: str | None = None
) -> dict[str, Any]:
    """Serialize one Intent into storage-safe primitives."""
    record = {
        "target": intent.target,
        "set": dict(intent.set),
        "withdraw": dict(intent.withdraw),
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
        "selector_generated": intent.selector_generated,
        "created_at_ms": intent.created_at_ms,
        "animation": asdict(intent.animation) if intent.animation is not None else None,
        "generators": {field_name: asdict(spec) for field_name, spec in intent.generators.items()},
    }
    if intent.rule_id:
        record["rule_fingerprint"] = rule_fingerprint
    return record


def intent_from_lifecycle_record(raw: Any) -> Intent | None:
    """Deserialize one lifecycle record, returning None for invalid data."""
    if not isinstance(raw, dict):
        return None
    try:
        authority_raw = raw.get("authority", Authority.AUTOMATION.value)
        if not isinstance(authority_raw, str):
            return None
        authority = Authority(authority_raw)
        animation_raw = raw.get("animation")
        if animation_raw is not None and not isinstance(animation_raw, dict):
            return None
        animation = AnimationSpec(**animation_raw) if animation_raw is not None else None
        generators_raw = raw.get("generators", {})
        if not isinstance(generators_raw, dict):
            return None
        generators: dict[str, ValueGeneratorSpec] = {}
        for field_name, spec_raw in generators_raw.items():
            if not isinstance(field_name, str) or not isinstance(spec_raw, dict):
                return None
            generators[field_name] = ValueGeneratorSpec(**spec_raw)
        confidence = raw.get("confidence", 1.0)
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            return None
        confidence = float(confidence)
        if not math.isfinite(confidence):
            return None
        return Intent(
            target=_string(raw.get("target", "")),
            set=_string_dict(raw.get("set", {})),
            withdraw=_string_dict(raw.get("withdraw", {})),
            merge=_bool(raw.get("merge", False)),
            cap=_string_dict(raw.get("cap", {})),
            floor=_string_dict(raw.get("floor", {})),
            offset=_string_dict(raw.get("offset", {})),
            multiply=_string_dict(raw.get("multiply", {})),
            transition_ms=_nonnegative_int(raw.get("transition_ms", 0)),
            transition_assert_ms=_optional_nonnegative_int(raw.get("transition_assert_ms")),
            transition_change_ms=_optional_nonnegative_int(raw.get("transition_change_ms")),
            transition_withdraw_ms=_optional_nonnegative_int(raw.get("transition_withdraw_ms")),
            easing=_string(raw.get("easing", "linear")),
            authority=authority,
            confidence=confidence,
            ttl_ms=_optional_nonnegative_int(raw.get("ttl_ms")),
            reason=_string(raw.get("reason", "")),
            rule_id=_string(raw.get("rule_id", "")),
            ignore_when=_bool(raw.get("ignore_when", False)),
            selector_generated=_bool(raw.get("selector_generated", False)),
            created_at_ms=_nonnegative_int(raw.get("created_at_ms", 0)),
            animation=animation,
            generators=generators,
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _record_list(records: dict[str, Any], key: str) -> list[Any]:
    value = records.get(key, [])
    return value if isinstance(value, list) else []


def _string(value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError
    return value


def _required_string(raw: dict[str, Any], key: str) -> str:
    value = _string(raw[key])
    if not value:
        raise ValueError
    return value


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    return _string(value)[:500]


def _bool(value: Any) -> bool:
    if not isinstance(value, bool):
        raise TypeError
    return value


def _nonnegative_int(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise TypeError
    return value


def _optional_nonnegative_int(value: Any) -> int | None:
    if value is None:
        return None
    return _nonnegative_int(value)


def _string_dict(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise TypeError
    return dict(value)


def lifecycle_version(records: Any) -> int | None:
    """Return a supported lifecycle schema version, rejecting malformed input."""
    if not isinstance(records, dict):
        return None
    version = records.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version not in (1, 2):
        return None
    return version
