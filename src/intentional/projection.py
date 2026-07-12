"""Projection records for agent, UI, and simulation views."""

from __future__ import annotations

import unicodedata
from typing import Any, Protocol

from .adapter.matcher import service_plan_match
from .adapter.signer import service_plan_signature
from .adapter.translator import service_calls_for_resolved_target
from .compositor import resolve_intents
from .reconciliation import target_policy_denial

REDACTED = "[redacted]"

_SENSITIVE_FIELD_PARTS = frozenset(
    {
        "auth",
        "authorization",
        "code",
        "credential",
        "opaque",
        "password",
        "pin",
        "private",
        "secret",
        "token",
    }
)
_SERVICE_DATA_FIELDS = frozenset({"data", "service_data", "service_payload"})


def redact_sensitive(value: Any, *, _field: str | None = None) -> Any:
    """Recursively redact secrets while retaining projection structure."""
    if _field is not None and _is_sensitive_field(_field):
        return REDACTED
    if isinstance(value, dict):
        if set(value) >= {"code", "message"} and isinstance(value["code"], str):
            return {
                key: item if key == "code" else redact_sensitive(item, _field=key)
                for key, item in value.items()
            }
        projected_field = value.get("field")
        field_is_sensitive = isinstance(projected_field, str) and _is_sensitive_field(
            projected_field
        )
        return {
            key: (
                REDACTED
                if (field_is_sensitive and key in {"value", "from", "to"})
                or key in _SERVICE_DATA_FIELDS
                else item
                if key == "code" and "message" in value
                else redact_sensitive(item, _field=key)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_sensitive(item) for item in value]
    return value


def _is_sensitive_field(field: str) -> bool:
    normalized = "".join(character.lower() if character.isalnum() else "_" for character in field)
    parts = frozenset(part for part in normalized.split("_") if part)
    return bool(parts & _SENSITIVE_FIELD_PARTS) or "key" in parts


class SimulationEngine(Protocol):
    """Small Interface needed to project a simulated engine step."""

    def now_ms(self) -> int: ...

    def list_active_targets(self) -> tuple[str, ...]: ...

    def resolve(self, target: str) -> Any: ...

    def list_authored_rule_statuses(self) -> dict[str, dict[str, Any]]: ...


def target_projection(
    engine: SimulationEngine,
    target: str,
    *,
    actual_state: Any | None = None,
    reconciliation: Any | None = None,
) -> dict[str, Any]:
    """Project compositor, Rule, actual-state, and reconciliation facts."""
    now_ms = engine.now_ms()
    resolved = engine.resolve(target)
    intents = sorted(
        engine.list_active_intents(target), key=lambda item: item.priority, reverse=True
    )  # type: ignore[attr-defined]
    fields: list[dict[str, Any]] = []
    if resolved is not None:
        for field, value in sorted(resolved.value.items()):
            setters = [intent for intent in intents if field in intent.set]
            provider = max(setters, key=lambda item: item.priority) if setters else None
            modifiers = []
            for operation in ("cap", "floor", "offset", "multiply"):
                for intent in intents:
                    values = getattr(intent, operation)
                    if field in values:
                        modifiers.append(
                            {
                                "operation": operation,
                                "value": values[field],
                                "rule_id": intent.rule_id or None,
                                "authority": intent.authority.value,
                            }
                        )
            fields.append(
                {
                    "field": field,
                    "value": value,
                    "provider": _intent_ref(provider),
                    "losing_providers": [
                        _intent_ref(intent) for intent in setters if intent is not provider
                    ],
                    "modifiers": modifiers,
                }
            )

    calls = (
        ()
        if resolved is None
        else service_calls_for_resolved_target(
            target, dict(resolved.value), transition_ms=resolved.transition_ms
        )
    )
    match = "unknown"
    if calls and actual_state is not None:
        match = service_plan_match(service_plan_signature(calls), actual_state).value
    statuses = engine.list_rule_statuses()  # type: ignore[attr-defined]
    rules = []
    active_rule_ids = {intent.rule_id for intent in intents if intent.rule_id}
    for rule_id, status in statuses.items():
        if target not in status.get("targets", []):
            continue
        state = (
            "winning"
            if resolved and resolved.winning_intent and rule_id == resolved.winning_intent.rule_id
            else "losing"
        )
        if status.get("blocked_by"):
            state = "blocked"
        elif status.get("phase") == "waiting":
            state = "waiting"
        elif rule_id not in active_rule_ids:
            state = "inactive"
        rules.append({"rule_id": rule_id, "state": state, **status})

    manual = next(
        (intent for intent in intents if intent.authority.value == "user" and not intent.rule_id),
        None,
    )
    revealed = None
    if manual is not None:
        without_manual = resolve_intents(
            target,
            [intent for intent in intents if intent is not manual],
            into_the_future_ms=now_ms,
        )
        if without_manual is not None:
            revealed = {
                "value": dict(without_manual.value),
                "winner": _intent_ref(without_manual.winning_intent),
            }
    reconciliation_state = (
        reconciliation.projection_state(target, now_ms) if reconciliation is not None else {}
    )
    policy_denial = (
        None if resolved is None else target_policy_denial(engine, target, dict(resolved.value))
    )
    return redact_sensitive(
        {
            "target": target,
            "desired": None if resolved is None else dict(resolved.value),
            "fields": fields,
            "rules": rules,
            "actual": _actual_snapshot(actual_state),
            "service_plan": [
                {"domain": domain, "service": service, "data": data}
                for domain, service, data in calls
            ],
            "plan_match": match,
            "reconciliation": reconciliation_state,
            "target_policy": None
            if getattr(engine, "target_policy", lambda _target: None)(target) is None
            else engine.target_policy(target).as_dict(),  # type: ignore[attr-defined]
            "policy_denial": reconciliation_state.get("policy_denial") or policy_denial,
            "manual_override": None
            if manual is None
            else {
                "expires_at_ms": manual.expires_at_ms(),
                "remaining_ms": None
                if manual.expires_at_ms() is None
                else max(0, manual.expires_at_ms() - now_ms),
                "revealed_after_withdrawal": revealed,
            },
        }
    )


def _intent_ref(intent: Any | None) -> dict[str, Any] | None:
    if intent is None:
        return None
    return redact_sensitive(
        {
            "rule_id": intent.rule_id or None,
            "authority": intent.authority.value,
            "confidence": intent.confidence,
            "created_at_ms": intent.created_at_ms,
            "reason": intent.reason,
        }
    )


def _actual_snapshot(state: Any | None) -> dict[str, Any] | None:
    if state is None:
        return None
    safe_fields = {
        "brightness",
        "brightness_pct",
        "color_temp",
        "color_temp_kelvin",
        "current_position",
        "current_tilt_position",
        "fan_mode",
        "fan_speed",
        "hs_color",
        "hvac_mode",
        "mode",
        "operation_mode",
        "percentage",
        "position",
        "rgb_color",
        "source",
        "temperature",
        "target_humidity",
        "target_temperature",
        "tilt",
        "volume_level",
        "xy_color",
    }
    attributes = getattr(state, "attributes", {}) or {}
    return {
        "state": state.state,
        "attributes": {key: value for key, value in attributes.items() if key in safe_fields},
    }


def simulation_step(engine: SimulationEngine, *, index: int) -> dict[str, Any]:
    """Return a stable response record for one simulation timeline step."""
    resolved = []
    for target in engine.list_active_targets():
        result = engine.resolve(target)
        if result is not None:
            resolved.append({"target": target, "value": dict(result.value)})
    return {
        "index": index,
        "now_ms": engine.now_ms(),
        "active_targets": list(engine.list_active_targets()),
        "resolved_targets": resolved,
        "active_rules": active_rule_statuses(engine),
    }


def preview_targets(
    engine: SimulationEngine,
    *,
    actual_for_target: Any | None = None,
) -> list[dict[str, Any]]:
    """Return desired target values with optional actual-state diffs."""
    actual_for_target = actual_for_target or (lambda _target: None)
    previews = []
    for target in engine.list_active_targets():
        result = engine.resolve(target)
        if result is None:
            continue
        desired = dict(result.value)
        actual = actual_for_target(target)
        previews.append(
            {
                "target": target,
                "desired": desired,
                "actual": actual,
                "changes": _changes(desired, actual),
                "winning_rule_id": getattr(result.winning_intent, "rule_id", None),
                "reason": getattr(result.winning_intent, "reason", "")
                if result.winning_intent is not None
                else "",
                "policy_denial": target_policy_denial(engine, target, desired),
            }
        )
    return redact_sensitive(previews)


def explain_card(engine: SimulationEngine, *, target: str | None = None) -> dict[str, Any]:
    """Return Lovelace-card-friendly explain data."""
    targets = [target] if target else list(engine.list_active_targets())
    cards = []
    for current_target in targets:
        explain = engine.explain_target(current_target)  # type: ignore[attr-defined]
        winner = explain.get("winning_intent") or {}
        resolved = explain.get("resolved") or {}
        cards.append(
            {
                "target": current_target,
                "state": "active" if resolved else "idle",
                "desired": resolved.get("value"),
                "winning_rule_id": winner.get("rule_id"),
                "reason": winner.get("reason", ""),
                "active_intent_count": len(explain.get("active_intents") or []),
                "rules": explain.get("rules_for_target") or [],
                "intents": explain.get("active_intents") or [],
            }
        )
    return redact_sensitive({"targets": cards, "count": len(cards)})


def dashboard_cards(rooms: Any) -> dict[str, Any]:
    """Return a simple Lovelace entities-card layout for room controls."""
    cards = []
    for room in rooms.values():
        slug = _entity_slug(room.name)
        cards.append(
            {
                "type": "entities",
                "title": f"Intentional: {room.name}",
                "entities": [
                    f"sensor.intentional_{slug}_status",
                    f"switch.intentional_pause_{slug}_rules",
                    f"button.intentional_clear_{slug}_manual_overrides",
                ],
            }
        )
    return {"title": "Intentional Rooms", "cards": cards}


def _entity_slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return (
        "_".join(
            part
            for part in "".join(ch.lower() if ch.isalnum() else "_" for ch in normalized).split("_")
            if part
        )
        or "area"
    )


def active_rule_statuses(engine: SimulationEngine) -> list[dict[str, Any]]:
    """Return authored rule statuses that are active or still hold runtime intents."""
    return [
        status
        for status in engine.list_authored_rule_statuses().values()
        if status["active"] or status["active_intent_count"]
    ]


def _changes(desired: dict[str, Any], actual: Any) -> list[dict[str, Any]]:
    if not isinstance(actual, dict):
        return [
            {"field": field, "from": None, "to": value, "changed": True}
            for field, value in desired.items()
        ]
    changes = []
    attributes = actual.get("attributes") if isinstance(actual.get("attributes"), dict) else {}
    for field, value in desired.items():
        current = actual.get("state") if field == "state" else attributes.get(field)
        changes.append(
            {
                "field": field,
                "from": current,
                "to": value,
                "changed": current != value,
            }
        )
    return changes
