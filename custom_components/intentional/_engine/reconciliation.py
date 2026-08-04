"""Reconciliation: decide, apply, and promote drift.

Per ADR-0001, this module owns the decide + apply + drift-promotion policy
and the five mutable dicts it reads (``last_applied``, ``last_resolved``,
``drift_suppressed_until``, ``drift_candidates``, ``service_failure_backoff``).
It deliberately does NOT own HA state ingest or rule evaluation; those stay in
the integration and run before this module is called.

The module is pure-read on everything except its own dicts: HA I/O goes through
an injected :class:`HAAdapter` and context attribution through a
:class:`ContextTracker`. Decisions and observations are returned as
:class:`ReconciliationEvent` values so the integration can translate them to
diagnostics without this module knowing about HA's diagnostic sink.
"""

from __future__ import annotations

import json
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any, Protocol, runtime_checkable

from .adapter import FrozenValue, ServicePlanSignature
from .adapter.extractor import manual_set_from_state_object
from .adapter.matcher import (
    ServicePlanMatch,
    service_plan_match,
    service_plan_matches_state,
)
from .adapter.signer import _freeze_signature_value, service_plan_signature
from .adapter.translator import service_calls_for_resolved_target


def reconciliation_key(entry_id: str) -> str:
    """Return the hass.data key for one config entry's Reconciliation state."""
    return f"{entry_id}:reconciliation"


# --------------------------------------------------------------------------- #
# Collaborators injected by the integration
# --------------------------------------------------------------------------- #


@runtime_checkable
class HAAdapter(Protocol):
    """The HA-facing operations Reconciliation needs."""

    def get_state(self, entity_id: str) -> Any: ...

    async def async_call(
        self, domain: str, service: str, data: dict[str, Any], *, context: Any
    ) -> None: ...

    def new_context(self) -> Any: ...


@runtime_checkable
class ContextTracker(Protocol):
    """Attribution for HA state changes owned by Intentional."""

    def owns_state(self, state: Any) -> bool: ...


# --------------------------------------------------------------------------- #
# Events
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ReconciliationEvent:
    """A single reconciliation observation the integration translates to diagnostics."""

    kind: str
    target: str = ""
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _RetryState:
    failures: int
    retry_at_ms: int
    signature: ServicePlanSignature


_RECONCILIATION_STATE_VERSION = 2
_MAX_PERSISTED_TARGETS = 256


# --------------------------------------------------------------------------- #
# Resolved target state (moved from the integration)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ResolvedTargetState:
    value: dict[str, Any]
    winner_key: tuple[str, int] | None
    winner_confidence: float | None
    transition_ms: int
    transition_withdraw_ms: int | None
    withdraw_value: dict[str, Any] | None = None
    ownership: str = "managed"
    field_ownership: dict[str, Any] = field(default_factory=dict)
    dispatch: str = "apply"


WITHDRAW_TO_OFF_DOMAINS = frozenset({"light", "switch", "input_boolean", "fan", "siren"})


# --------------------------------------------------------------------------- #
# Pure helpers (moved from the integration)
# --------------------------------------------------------------------------- #


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _transition_ms_for_resolved_change(
    previous: ResolvedTargetState | None,
    resolved: Any,
) -> int:
    winner = resolved.winning_intent
    if winner is None:
        return resolved.transition_ms
    if previous is None or previous.winner_key != (winner.rule_id, winner.created_at_ms):
        if (
            previous is not None
            and previous.winner_confidence is not None
            and winner.confidence < previous.winner_confidence
            and previous.transition_withdraw_ms is not None
        ):
            return previous.transition_withdraw_ms
        return (
            winner.transition_assert_ms
            if winner.transition_assert_ms is not None
            else resolved.transition_ms
        )
    return (
        winner.transition_change_ms
        if winner.transition_change_ms is not None
        else resolved.transition_ms
    )


def _default_withdraw_value(target: str, previous: ResolvedTargetState) -> dict[str, Any] | None:
    if previous.withdraw_value is not None:
        return dict(previous.withdraw_value)
    domain, sep, _object_id = target.partition(".")
    if not sep or domain not in WITHDRAW_TO_OFF_DOMAINS:
        return None
    if previous.value.get("state") != "on":
        return None
    return {"state": "off"}


def _normalized_actual_fields(state: Any | None) -> dict[str, Any]:
    if state is None or _state_is_unavailable(state):
        return {}
    return manual_set_from_state_object(state)


def _owned_withdrawals(previous: ResolvedTargetState | None) -> dict[str, Any]:
    if previous is None:
        return {}
    result: dict[str, Any] = {}
    for field_name, record in previous.field_ownership.items():
        if not isinstance(record, dict):
            continue
        if record.get("policy") in {"literal", "adopt"} and "value" in record:
            result[field_name] = record["value"]
    return result


def _has_explicit_withdrawal(previous: ResolvedTargetState) -> bool:
    return any(
        isinstance(record, dict) and record.get("policy") in {"literal", "adopt"}
        for record in previous.field_ownership.values()
    )


def _orphaned_withdrawals(
    previous: ResolvedTargetState | None, current_fields: set[str]
) -> dict[str, Any]:
    if previous is None:
        return {}
    return {
        field_name: value
        for field_name, value in _owned_withdrawals(previous).items()
        if field_name not in current_fields
    }


def _withdraw_value_for_service_plan(
    target: str,
    calls: tuple[tuple[str, str, dict[str, Any]], ...] | None,
) -> dict[str, Any] | None:
    domain, sep, _object_id = target.partition(".")
    if not sep or domain not in WITHDRAW_TO_OFF_DOMAINS or calls is None:
        return None
    for call_domain, service, service_data in calls:
        if call_domain != domain:
            continue
        if service == "turn_on" and service_data.get("entity_id") == target:
            return {"state": "off"}
    return None


def _last_applied_is_withdraw_signature(
    target: str,
    previous: ResolvedTargetState | None,
    last_applied: dict[str, ServicePlanSignature],
) -> bool:
    if previous is None:
        return False
    withdraw_value = _default_withdraw_value(target, previous)
    if withdraw_value is None:
        return False
    transition_ms = (
        previous.transition_withdraw_ms
        if previous.transition_withdraw_ms is not None
        else previous.transition_ms
    )
    calls = service_calls_for_resolved_target(
        target,
        withdraw_value,
        transition_ms=transition_ms,
    )
    if not calls:
        return False
    return last_applied.get(target) == service_plan_signature(calls)


def _state_has_user_context(state: Any) -> bool:
    context = getattr(state, "context", None)
    return getattr(context, "user_id", None) is not None


def _service_call_is_idempotent(service: str) -> bool:
    return service not in {"toggle", "media_next_track", "media_previous_track"}


def _redact_call_data(data: Any) -> dict[str, Any]:
    """Keep call diagnostics useful without retaining credentials or large payloads."""
    if not isinstance(data, dict):
        return {}
    sensitive = {"auth", "authorization", "code", "key", "password", "pin", "secret", "token"}
    remaining = 2048

    def bounded(value: Any, *, key: str = "", depth: int = 0) -> Any:
        nonlocal remaining
        normalized = key.lower().replace("-", "_")
        if any(part in normalized.split("_") for part in sensitive):
            return "[redacted]"
        if depth >= 6 or remaining <= 0:
            return "[truncated]"
        if isinstance(value, str):
            encoded = value.encode("utf-8")[: min(200, remaining)]
            remaining -= len(encoded)
            return encoded.decode("utf-8", errors="ignore")
        if isinstance(value, (bool, int, float)) or value is None:
            remaining -= min(remaining, len(str(value).encode()))
            return value
        if isinstance(value, dict):
            return {
                str(child_key)[:80]: bounded(child, key=str(child_key), depth=depth + 1)
                for child_key, child in list(value.items())[:32]
                if remaining > 0
            }
        if isinstance(value, (list, tuple)):
            return [bounded(child, depth=depth + 1) for child in value[:16] if remaining > 0]
        return "[bounded]"

    result = bounded(data)
    encoded = json.dumps(result, ensure_ascii=True, separators=(",", ":")).encode()
    if len(encoded) > 4096:
        return {
            "preview": encoded[:3000].decode("ascii", errors="ignore"),
            "truncated": True,
        }
    return result


def _truncate_utf8(value: Any, limit: int) -> str | None:
    encoded = str(value or "").encode("utf-8")[:limit]
    return encoded.decode("utf-8", errors="ignore") or None


_SAFETY_REDUCING_SERVICES = frozenset(
    {
        "turn_off",
        "lock",
        "close",
        "close_cover",
        "stop",
        "disarm",
    }
)


def _is_safety_reducing_plan(calls: tuple[tuple[str, str, dict[str, Any]], ...]) -> bool:
    """Classify only explicit de-energising/securing plans as safety reducing."""
    return bool(calls) and all(service in _SAFETY_REDUCING_SERVICES for _, service, _ in calls)


def _build_resolved_target_state(
    resolved: Any,
    calls: tuple[tuple[str, str, dict[str, Any]], ...] | None = None,
    *,
    ownership: str = "managed",
    previous: ResolvedTargetState | None = None,
    actual: Any | None = None,
    dispatch: str = "apply",
) -> ResolvedTargetState:
    winner = resolved.winning_intent
    field_ownership: dict[str, Any] = {}
    actual_fields = _normalized_actual_fields(actual)
    for field_name, provider in getattr(resolved, "field_providers", {}).items():
        old = (previous.field_ownership if previous is not None else {}).get(field_name)
        policy = provider.withdraw.get(field_name)
        if isinstance(old, dict) and old.get("policy") == "adopt":
            field_ownership[field_name] = dict(old)
        elif policy == "adopt":
            if field_name in actual_fields:
                field_ownership[field_name] = {"policy": "adopt", "value": actual_fields[field_name]}
        else:
            field_ownership[field_name] = {"policy": "literal", "value": policy} if field_name in provider.withdraw else {"policy": None}
    return ResolvedTargetState(
        value=dict(resolved.value),
        winner_key=(winner.rule_id, winner.created_at_ms) if winner is not None else None,
        winner_confidence=winner.confidence if winner is not None else None,
        transition_ms=resolved.transition_ms,
        transition_withdraw_ms=winner.transition_withdraw_ms if winner is not None else None,
        withdraw_value=_withdraw_value_for_service_plan(resolved.target, calls),
        ownership=ownership,
        field_ownership=field_ownership,
        dispatch=dispatch,
    )


def _build_partially_applied_target_state(
    resolved: Any,
    calls: tuple[tuple[str, str, dict[str, Any]], ...],
    *,
    ownership: str,
    previous: ResolvedTargetState | None,
    actual: Any | None,
) -> ResolvedTargetState:
    """Retain only ownership established by the successful plan prefix."""
    state = _build_resolved_target_state(
        resolved, calls, ownership=ownership, previous=previous, actual=actual
    )
    applied_fields: set[str] = set()
    for _domain, service, data in calls:
        applied_fields.update(key for key in data if key not in {"entity_id", "transition"})
        if service in {"turn_on", "turn_off", "lock", "unlock", "open_cover", "close_cover"}:
            applied_fields.add("state")
    return ResolvedTargetState(
        value={key: value for key, value in state.value.items() if key in applied_fields},
        winner_key=state.winner_key,
        winner_confidence=state.winner_confidence,
        transition_ms=state.transition_ms,
        transition_withdraw_ms=state.transition_withdraw_ms,
        withdraw_value=_withdraw_value_for_service_plan(resolved.target, calls),
        ownership=state.ownership,
        field_ownership={
            key: value for key, value in state.field_ownership.items() if key in applied_fields
        },
        dispatch=state.dispatch,
    )


def _resolved_target_state_from_record(
    raw: dict[str, Any],
    *,
    value_key: str,
) -> tuple[str, ResolvedTargetState] | None:
    target = raw.get("target")
    value = raw.get(value_key)
    if not isinstance(target, str) or not isinstance(value, dict):
        return None
    winner_key_raw = raw.get("winner_key")
    winner_key = None
    if (
        isinstance(winner_key_raw, list | tuple)
        and len(winner_key_raw) == 2
        and isinstance(winner_key_raw[0], str)
    ):
        try:
            winner_key = (winner_key_raw[0], int(winner_key_raw[1]))
        except (TypeError, ValueError):
            winner_key = None
    elif value_key == "set":
        rule_id = raw.get("rule_id")
        created_at_ms = raw.get("created_at_ms")
        if isinstance(rule_id, str):
            try:
                winner_key = (rule_id, int(created_at_ms or 0))
            except (TypeError, ValueError):
                winner_key = None
    try:
        return target, ResolvedTargetState(
            value=dict(value),
            winner_key=winner_key,
            winner_confidence=_optional_float(
                raw.get("winner_confidence")
                if "winner_confidence" in raw
                else raw.get("confidence")
            ),
            transition_ms=int(raw.get("transition_ms") or 0),
            transition_withdraw_ms=_optional_int(raw.get("transition_withdraw_ms")),
            withdraw_value=dict(raw["withdraw_value"])
            if isinstance(raw.get("withdraw_value"), dict)
            else None,
            ownership=str(raw.get("ownership", "managed")),
            field_ownership=dict(raw.get("field_ownership", {}))
            if isinstance(raw.get("field_ownership", {}), dict)
            else {},
            dispatch=str(raw.get("dispatch", "apply")),
        )
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Drift classification (internal to Reconciliation)
# --------------------------------------------------------------------------- #


def classify_state_drift(
    engine: Any,
    last_applied: dict[str, ServicePlanSignature],
    state: Any,
    *,
    ttl_ms: int,
    now_ms: int | None = None,
    drift_suppressed_until: dict[str, int] | None = None,
    drift_candidates: dict[str, tuple[int, FrozenValue]] | None = None,
    confirmation_ms: int = 0,
    reason: str = "Manual HA state change",
) -> dict[str, Any] | None:
    """Classify a state change and return the override payload, or None.

    Does NOT emit the intent; the caller applies the returned payload.
    """
    entity_id = state.entity_id
    if drift_suppressed_until is not None:
        suppress_until = drift_suppressed_until.get(entity_id)
        if suppress_until is not None:
            if now_ms is None:
                raise ValueError("now_ms is required with drift_suppressed_until")
            context = getattr(state, "context", None)
            if getattr(context, "user_id", None) is not None:
                drift_suppressed_until.pop(entity_id, None)
            elif now_ms < suppress_until:
                if drift_candidates is not None:
                    drift_candidates.pop(entity_id, None)
                return None
            else:
                drift_suppressed_until.pop(entity_id, None)
    plan = last_applied.get(entity_id)
    if plan is None:
        if drift_candidates is not None:
            drift_candidates.pop(entity_id, None)
        return None
    if service_plan_match(plan, state) is not ServicePlanMatch.MISMATCH:
        if drift_candidates is not None:
            drift_candidates.pop(entity_id, None)
        return None
    if not engine.has_active_target(entity_id):
        if drift_candidates is not None:
            drift_candidates.pop(entity_id, None)
        return None
    if _state_change_looks_like_ignored_activation(plan, state):
        if drift_candidates is not None:
            drift_candidates.pop(entity_id, None)
        last_applied.pop(entity_id, None)
        return None
    set_dict = manual_set_from_state_object(state)
    if not set_dict:
        if drift_candidates is not None:
            drift_candidates.pop(entity_id, None)
        return None
    if drift_candidates is not None and confirmation_ms > 0:
        if now_ms is None:
            raise ValueError("now_ms is required with drift_candidates")
        candidate_signature = _freeze_signature_value(set_dict)
        candidate = drift_candidates.get(entity_id)
        if candidate is None or candidate[1] != candidate_signature:
            drift_candidates[entity_id] = (now_ms, candidate_signature)
            return None
        first_seen_ms, _signature = candidate
        if now_ms - first_seen_ms < confirmation_ms:
            return None
        drift_candidates.pop(entity_id, None)
    last_applied.pop(entity_id, None)
    return {"target": entity_id, "set": set_dict, "ttl_ms": ttl_ms, "reason": reason}


def _detected_override_ttl_ms(engine: Any, target: str, default_ttl_ms: int) -> int:
    """Return the longest configured TTL among current resolved contributors."""
    resolved = engine.resolve(target)
    if resolved is None:
        return default_ttl_ms
    contributors = {
        id(provider): provider
        for provider in getattr(resolved, "field_providers", {}).values()
    }
    resolved_fields = set(resolved.value)
    for intent in getattr(resolved, "all_active_intents", ()):
        if any(
            resolved_fields.intersection(getattr(intent, operation, {}))
            for operation in ("cap", "floor", "offset", "multiply")
        ):
            contributors[id(intent)] = intent
    if not contributors:
        return default_ttl_ms
    return max(
        intent.manual_override_ttl_ms
        if intent.manual_override_ttl_ms is not None
        else default_ttl_ms
        for intent in contributors.values()
    )


def invalidate_service_plan_for_state_change(
    last_applied: dict[str, ServicePlanSignature],
    state: Any,
) -> bool:
    """Forget a cached service plan when HA reports conflicting state."""
    entity_id = state.entity_id
    plan = last_applied.get(entity_id)
    if plan is None:
        return False
    if service_plan_match(plan, state) is not ServicePlanMatch.MISMATCH:
        return False
    last_applied.pop(entity_id, None)
    return True


def _state_change_looks_like_ignored_activation(
    plan: ServicePlanSignature,
    state: Any,
) -> bool:
    """True when HA still reports off after an Intentional turn_on call."""
    if str(getattr(state, "state", "")) != "off":
        return False
    context = getattr(state, "context", None)
    if getattr(context, "user_id", None) is not None:
        return False
    return any(domain == "light" and service == "turn_on" for domain, service, _data in plan)


def pending_drift_targets(drift_candidates: dict[str, tuple[int, FrozenValue]]) -> tuple[str, ...]:
    """Return targets with unconfirmed drift observations."""
    return tuple(sorted(drift_candidates))


def clear_pending_state_drift(
    drift_candidates: dict[str, tuple[int, FrozenValue]],
    target: str,
) -> None:
    """Forget any unconfirmed drift observation for a target."""
    drift_candidates.pop(target, None)


# --------------------------------------------------------------------------- #
# Reconciliation
# --------------------------------------------------------------------------- #


class Reconciliation:
    """Decide which service plans to call, what to suppress, and what drift to promote."""

    def __init__(
        self,
        *,
        drift_override_ttl_ms: int,
        drift_confirmation_ms: int,
        service_failure_backoff_ms: int,
        drift_transition_grace_ms: int = 2_000,
        availability_recovery_grace_ms: int = 30_000,
        service_failure_backoff_max_ms: int | None = None,
        retry_jitter_fn: Callable[[str, int, int], int] | None = None,
    ) -> None:
        self._drift_override_ttl_ms = drift_override_ttl_ms
        self._drift_confirmation_ms = drift_confirmation_ms
        self._service_failure_backoff_ms = service_failure_backoff_ms
        self._service_failure_backoff_max_ms = (
            service_failure_backoff_max_ms
            if service_failure_backoff_max_ms is not None
            else service_failure_backoff_ms * 16
        )
        if self._service_failure_backoff_max_ms < service_failure_backoff_ms:
            raise ValueError("service_failure_backoff_max_ms must be at least the base backoff")
        self._retry_jitter_fn = retry_jitter_fn or (lambda _target, _attempt, delay: delay)
        self._drift_transition_grace_ms = drift_transition_grace_ms
        self._availability_recovery_grace_ms = availability_recovery_grace_ms

        self._last_applied: dict[str, ServicePlanSignature] = {}
        self._last_resolved: dict[str, ResolvedTargetState] = {}
        self._drift_suppressed_until: dict[str, int] = {}
        self._drift_candidates: dict[str, Any] = {}
        self._unavailable_targets: set[str] = set()
        self._availability_recovery_until: dict[str, int] = {}
        self._service_failure_backoff: dict[str, _RetryState] = {}
        self._service_plan_progress: dict[str, tuple[ServicePlanSignature, int]] = {}
        self._policy_denials: dict[str, dict[str, Any]] = {}
        self._shadow_plans: dict[str, dict[str, Any]] = {}
        self._attempt_history: deque[dict[str, Any]] = deque(maxlen=256)
        self._churn_events: deque[tuple[int, str, str, str | None]] = deque(maxlen=4096)
        self._churn_targets: OrderedDict[str, None] = OrderedDict()
        self._churn_rules: OrderedDict[str, None] = OrderedDict()
        self._desired_signatures: dict[str, ServicePlanSignature] = {}
        self._target_winners: dict[str, tuple[str, int] | None] = {}
        self._rule_visibility: dict[str, bool] = {}
        self._rule_visibility_since: dict[str, int] = {}
        self._rule_active_since: dict[str, int] = {}
        self._rule_winning_ms: dict[str, int] = {}
        self._rule_observed_at: dict[str, int] = {}
        self._last_reconciliation_now_ms: int | None = None

    def projection_state(self, target: str, now_ms: int) -> dict[str, Any]:
        """Return read-only reconciliation facts for Target explanation."""
        retry = self._service_failure_backoff.get(target)
        candidate = self._drift_candidates.get(target)
        suppressed_until = self._drift_suppressed_until.get(target)
        progress = self._service_plan_progress.get(target)
        return {
            "owned": target in self._last_resolved,
            "last_applied": self._last_applied.get(target),
            "pending_drift": None
            if candidate is None
            else {
                "first_seen_ms": candidate[0],
                "age_ms": max(0, now_ms - candidate[0]),
            },
            "transition_suppressed_until_ms": suppressed_until,
            "transition_suppressed": suppressed_until is not None and now_ms < suppressed_until,
            "retry": None
            if retry is None
            else {
                "failures": retry.failures,
                "retry_at_ms": retry.retry_at_ms,
                "remaining_ms": max(0, retry.retry_at_ms - now_ms),
            },
            "service_plan_progress": None if progress is None else {"next_call_index": progress[1]},
            "policy_denial": self._policy_denials.get(target),
            "shadow": self._shadow_plans.get(target),
            "recent_history": self.recent_history(target=target, limit=20),
        }

    def recent_history(self, *, target: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """Return bounded, already-redacted service-plan attempt records."""
        records = [record for record in self._attempt_history if target is None or record["target"] == target]
        bounded_limit = max(0, min(limit, 256))
        return [] if bounded_limit == 0 else records[-bounded_limit:]

    def churn_status(self, now_ms: int) -> dict[str, Any]:
        """Return compact rolling 5 minute/hour churn counters."""
        self._expire_churn(now_ms)
        five_minute = self._summarize_churn(now_ms - 300_000)
        hour = self._summarize_churn(now_ms - 3_600_000)
        target_counts: dict[str, int] = {}
        for timestamp, _kind, target, _rule_id in self._churn_events:
            if timestamp >= now_ms - 300_000 and target:
                target_counts[target] = target_counts.get(target, 0) + 1
        high = [
            {"target": target, "events_5m": count}
            for target, count in sorted(target_counts.items(), key=lambda item: (-item[1], item[0]))[:10]
            if count >= 10
        ]
        return {"status": "high_churn" if high else "ok", "five_minute": five_minute, "hour": hour, "high_churn_targets": high}

    def rule_shadowing_status(self, now_ms: int) -> dict[str, dict[str, Any]]:
        """Return active versus output-winning duration counters."""
        return {
            rule_id: {
                "fully_shadowed": not self._rule_visibility[rule_id],
                "active_duration_ms": max(0, now_ms - self._rule_active_since.get(rule_id, now_ms)),
                "winning_duration_ms": self._rule_winning_ms.get(rule_id, 0) + (
                    max(0, now_ms - self._rule_observed_at.get(rule_id, now_ms))
                    if self._rule_visibility[rule_id] else 0
                ),
            }
            for rule_id in sorted(self._rule_visibility)
        }

    def record_publication_dispatch(self, now_ms: int) -> None:
        """Count one change-driven entity publication dispatch."""
        self._record_churn(now_ms, "publication_dispatch", "", None)

    # ----- drift classification (shared by on_state_delta and tick) -----

    def _classify_drift(
        self,
        engine: Any,
        state: Any,
        context_tracker: ContextTracker,
        now_ms: int,
    ) -> list[ReconciliationEvent]:
        """Classify one observed state as intentional, staged, or promoted drift."""
        entity_id = state.entity_id
        if _state_is_unavailable(state):
            self._unavailable_targets.add(entity_id)
            clear_pending_state_drift(self._drift_candidates, entity_id)
            return []
        if entity_id in self._unavailable_targets:
            self._unavailable_targets.remove(entity_id)
            self._availability_recovery_until[entity_id] = max(
                self._availability_recovery_until.get(entity_id, 0),
                now_ms + self._availability_recovery_grace_ms,
            )
        recovery_until = self._availability_recovery_until.get(entity_id)
        if recovery_until is not None:
            if now_ms < recovery_until:
                clear_pending_state_drift(self._drift_candidates, entity_id)
                return []
            self._availability_recovery_until.pop(entity_id, None)
        policy = getattr(engine, "target_policy", lambda _target: None)(entity_id)
        if policy is not None and policy.ownership != "managed":
            clear_pending_state_drift(self._drift_candidates, entity_id)
            return []
        if context_tracker.owns_state(state):
            clear_pending_state_drift(self._drift_candidates, entity_id)
            return [ReconciliationEvent("context_ignored", entity_id, {"state": state})]
        promoted = classify_state_drift(
            engine,
            self._last_applied,
            state,
            ttl_ms=_detected_override_ttl_ms(
                engine, entity_id, self._drift_override_ttl_ms
            ),
            now_ms=now_ms,
            drift_suppressed_until=self._drift_suppressed_until,
            drift_candidates=self._drift_candidates,
            confirmation_ms=self._drift_confirmation_ms,
        )
        if promoted is not None:
            return [ReconciliationEvent("drift_promoted", entity_id, promoted)]
        return []

    # ----- entry point 1: the listener path -----

    def on_state_delta(
        self,
        engine: Any,
        new_state: Any,
        context_tracker: ContextTracker,
        now_ms: int,
    ) -> list[ReconciliationEvent]:
        """Classify a single observed state change for drift.

        Does not apply service plans; that is :meth:`tick`'s job.
        """
        events = self._classify_drift(engine, new_state, context_tracker, now_ms)
        self._record_events(events, now_ms)
        return events

    # ----- entry point 2: the periodic path -----

    async def tick(
        self,
        engine: Any,
        adapter: HAAdapter,
        context_tracker: ContextTracker,
        now_ms: int,
        *,
        revision_is_current: Callable[[], bool] | None = None,
        before_dispatch: Callable[[], Awaitable[None]] | None = None,
    ) -> list[ReconciliationEvent]:
        """Confirm pending drift, apply resolved targets, and withdraw stale ones."""
        self._last_reconciliation_now_ms = now_ms
        events: list[ReconciliationEvent] = []
        resolved_targets: dict[str, Any] = {}
        events.extend(self._confirm_pending_drift(engine, adapter, context_tracker, now_ms))
        promoted_targets = {event.target for event in events if event.kind == "drift_promoted"}
        events.extend(
            await self._apply_active_targets(
                engine,
                adapter,
                now_ms,
                excluded_targets=promoted_targets,
                revision_is_current=revision_is_current,
                before_dispatch=before_dispatch,
                resolved_targets=resolved_targets,
            )
        )
        if revision_is_current is not None and not revision_is_current():
            return events
        events.extend(
            await self._withdraw_stale_targets(
                engine, adapter, now_ms, revision_is_current=revision_is_current,
                before_dispatch=before_dispatch,
            )
        )
        events.extend(self._shadowing_transitions(engine, now_ms, resolved_targets))
        self._record_events(events, now_ms)
        return events

    async def apply(
        self,
        engine: Any,
        adapter: HAAdapter,
        now_ms: int,
    ) -> list[ReconciliationEvent]:
        """Apply resolved targets and withdraw stale ones (no drift confirmation)."""
        self._last_reconciliation_now_ms = now_ms
        events: list[ReconciliationEvent] = []
        resolved_targets: dict[str, Any] = {}
        events.extend(await self._apply_active_targets(
            engine, adapter, now_ms, resolved_targets=resolved_targets
        ))
        events.extend(await self._withdraw_stale_targets(engine, adapter, now_ms))
        events.extend(self._shadowing_transitions(engine, now_ms, resolved_targets))
        self._record_events(events, now_ms)
        return events

    def _confirm_pending_drift(
        self,
        engine: Any,
        adapter: HAAdapter,
        context_tracker: ContextTracker,
        now_ms: int,
    ) -> list[ReconciliationEvent]:
        events: list[ReconciliationEvent] = []
        for target in pending_drift_targets(self._drift_candidates):
            state = adapter.get_state(target)
            if state is None:
                clear_pending_state_drift(self._drift_candidates, target)
                continue
            events.extend(self._classify_drift(engine, state, context_tracker, now_ms))
        return events

    async def _apply_active_targets(
        self,
        engine: Any,
        adapter: HAAdapter,
        now_ms: int,
        *,
        excluded_targets: set[str] | None = None,
        revision_is_current: Callable[[], bool] | None = None,
        before_dispatch: Callable[[], Awaitable[None]] | None = None,
        resolved_targets: dict[str, Any] | None = None,
    ) -> list[ReconciliationEvent]:
        events: list[ReconciliationEvent] = []
        active_targets = set(engine.list_active_targets())
        self._prune_obsolete_retry_state(active_targets | set(self._last_resolved))
        for target in sorted(active_targets):
            if excluded_targets and target in excluded_targets:
                events.append(ReconciliationEvent("service_skipped_drift_promoted", target))
                continue
            resolved = engine.resolve(target)
            if resolved_targets is not None:
                resolved_targets[target] = resolved
            if resolved is None:
                self._last_resolved.pop(target, None)
                continue
            resolved_value = dict(resolved.value)
            winner = resolved.winning_intent
            winner_key = None if winner is None else (winner.rule_id, winner.created_at_ms)
            if target in self._target_winners and self._target_winners[target] != winner_key:
                self._record_churn(now_ms, "winner_change", target, winner.rule_id if winner else None)
            self._target_winners[target] = winner_key
            policy = getattr(engine, "target_policy", lambda _target: None)(target)
            ownership = policy.ownership if policy is not None else "managed"
            denial = target_policy_denial(engine, target, resolved_value, policy)
            if denial is not None:
                if self._policy_denials.get(target) != denial:
                    self._policy_denials[target] = denial
                    events.append(ReconciliationEvent("service_denied_target_policy", target, denial))
                continue
            previous = self._last_resolved.get(target)
            current_state = adapter.get_state(target)
            providers = getattr(resolved, "field_providers", {})
            if policy is not None and policy.dispatch == "shadow":
                self._last_resolved.pop(target, None)
                self._last_applied.pop(target, None)
                self._service_failure_backoff.pop(target, None)
                self._service_plan_progress.pop(target, None)
                clear_pending_state_drift(self._drift_candidates, target)
                shadow = {"would_apply": resolved_value, "would_withdraw": {}}
                if self._shadow_plans.get(target) != shadow:
                    self._shadow_plans[target] = shadow
                    events.append(ReconciliationEvent("service_would_apply", target, {"value": resolved_value, "withdraw": {}}))
                continue
            uncaptured_adoptions = [
                field_name
                for field_name, provider in providers.items()
                if provider.withdraw.get(field_name) == "adopt"
                and not (
                    previous is not None
                    and isinstance(previous.field_ownership.get(field_name), dict)
                    and previous.field_ownership[field_name].get("policy") == "adopt"
                )
            ]
            if uncaptured_adoptions and _state_is_unavailable(current_state):
                events.append(ReconciliationEvent("adoption_deferred_unavailable", target, {"fields": sorted(uncaptured_adoptions)}))
                continue
            transition_ms = _transition_ms_for_resolved_change(previous, resolved)
            orphaned = _orphaned_withdrawals(previous, set(providers))
            dispatch_value = dict(orphaned)
            dispatch_value.update(resolved_value)
            calls = service_calls_for_resolved_target(
                target,
                dispatch_value,
                transition_ms=transition_ms,
            )
            if not calls:
                previous_denial = self._policy_denials.pop(target, None)
                if previous_denial is not None:
                    events.append(
                        ReconciliationEvent(
                            "service_target_policy_recovered", target, previous_denial
                        )
                    )
                self._last_resolved[target] = _build_resolved_target_state(
                    resolved, calls, ownership=ownership, previous=previous, actual=current_state,
                    dispatch=policy.dispatch if policy is not None else "apply",
                )
                continue
            signature = service_plan_signature(calls)
            old_signature = self._desired_signatures.get(target)
            if old_signature is not None and old_signature != signature:
                self._record_churn(now_ms, "plan_signature_change", target, getattr(resolved.winning_intent, "rule_id", None))
            self._desired_signatures[target] = signature
            retry = self._service_failure_backoff.get(target)
            if (
                retry is not None
                and retry.signature != signature
                and _is_safety_reducing_plan(calls)
            ):
                self._service_failure_backoff.pop(target, None)
                self._service_plan_progress.pop(target, None)
                retry = None
                events.append(ReconciliationEvent("service_retry_superseded", target))
            if retry is not None and now_ms < retry.retry_at_ms:
                continue
            if retry is not None and retry.signature != signature:
                self._service_failure_backoff.pop(target, None)
                self._service_plan_progress.pop(target, None)
                retry = None
            has_pending_withdraw = _last_applied_is_withdraw_signature(
                target, previous, self._last_applied
            )
            self._shadow_plans.pop(target, None)
            if (
                (
                    policy is not None
                    and policy.unavailable == "skip"
                    or policy is None
                    and current_state is not None
                )
                and _state_is_unavailable(current_state)
            ):
                denial = {
                    "code": "target_unavailable",
                    "message": f"{target} is unavailable; policy requires dispatch to be skipped.",
                }
                if self._policy_denials.get(target) != denial:
                    self._policy_denials[target] = denial
                    events.append(ReconciliationEvent("service_denied_target_policy", target, denial))
                continue
            if (
                retry is not None
                and policy is not None
                and policy.max_retries is not None
                and retry.failures > policy.max_retries
            ):
                denial = {
                    "code": "max_retries_exhausted",
                    "max_retries": policy.max_retries,
                    "failures": retry.failures,
                    "message": f"{target} exhausted its retry ceiling.",
                }
                if self._policy_denials.get(target) != denial:
                    self._policy_denials[target] = denial
                    events.append(ReconciliationEvent("service_denied_target_policy", target, denial))
                continue
            previous_denial = self._policy_denials.pop(target, None)
            if previous_denial is not None:
                events.append(
                    ReconciliationEvent("service_target_policy_recovered", target, previous_denial)
                )
            suppress_until = self._drift_suppressed_until.get(target)
            if (
                previous is not None
                and previous.value == resolved_value
                and self._last_applied.get(target) is not None
                and not has_pending_withdraw
                and suppress_until is not None
                and now_ms < suppress_until
            ):
                self._last_resolved[target] = _build_resolved_target_state(
                    resolved, calls, ownership=ownership, previous=previous, actual=current_state
                )
                events.append(ReconciliationEvent("service_skipped_pending_transition", target))
                continue
            if self._last_applied.get(target) == signature:
                if (
                    current_state is None
                    or service_plan_match(signature, current_state) is not ServicePlanMatch.MISMATCH
                ):
                    self._last_resolved[target] = _build_resolved_target_state(
                        resolved, calls, ownership=ownership, previous=previous, actual=current_state
                    )
                    continue
                if target in pending_drift_targets(self._drift_candidates):
                    self._last_resolved[target] = _build_resolved_target_state(
                        resolved, calls, ownership=ownership, previous=previous, actual=current_state
                    )
                    continue
                if suppress_until is not None and now_ms < suppress_until:
                    self._last_resolved[target] = _build_resolved_target_state(
                        resolved, calls, ownership=ownership, previous=previous, actual=current_state
                    )
                    continue
            if (
                not has_pending_withdraw
                and current_state is not None
                and service_plan_matches_state(signature, current_state)
            ):
                self._last_applied[target] = signature
                self._last_resolved[target] = _build_resolved_target_state(
                    resolved, calls, ownership=ownership, previous=previous, actual=current_state
                )
                events.append(ReconciliationEvent("service_skipped_matching_state", target))
                continue
            applied = await self._invoke_service_plan(
                adapter,
                target,
                calls,
                signature,
                revision_is_current=revision_is_current,
                before_dispatch=before_dispatch,
            )
            events.extend(applied.events)
            if revision_is_current is not None and not revision_is_current():
                events.append(ReconciliationEvent("stale_result_discarded", target))
                return events
            if applied.failed:
                if applied.completed_calls:
                    self._last_resolved[target] = _build_partially_applied_target_state(
                        resolved,
                        applied.completed_calls,
                        ownership=ownership,
                        previous=previous,
                        actual=current_state,
                    )
                events.append(self._schedule_retry(target, signature, now_ms))
            else:
                self._policy_denials.pop(target, None)
                recovered = self._service_failure_backoff.pop(target, None)
                if recovered is not None:
                    events.append(
                        ReconciliationEvent(
                            "service_retry_recovered",
                            target,
                            {"failures": recovered.failures},
                        )
                    )
                self._last_applied[target] = signature
                clear_pending_state_drift(self._drift_candidates, target)
                self._suppress_drift_during_transition(target, transition_ms, now_ms)
                self._last_resolved[target] = _build_resolved_target_state(
                    resolved, calls, ownership=ownership, previous=previous, actual=current_state
                )
        return events

    def _shadowing_transitions(
        self, engine: Any, now_ms: int, resolved_targets: dict[str, Any] | None = None
    ) -> list[ReconciliationEvent]:
        visible: set[str] = set()
        active: set[str] = set()
        for target in engine.list_active_targets():
            resolved = (
                resolved_targets[target]
                if resolved_targets is not None and target in resolved_targets
                else engine.resolve(target)
            )
            if resolved is None:
                continue
            intents = engine.list_active_intents(target)
            active.update(intent.rule_id for intent in intents if intent.rule_id)
            visible.update(
                provider.rule_id for provider in getattr(resolved, "field_providers", {}).values()
                if provider.rule_id
            )
            for intent in intents:
                if intent.rule_id and any(
                    getattr(intent, operation, {}) for operation in ("cap", "floor", "offset", "multiply")
                ):
                    visible.add(intent.rule_id)
        events: list[ReconciliationEvent] = []
        for rule_id in sorted(active):
            is_visible = rule_id in visible
            previous = self._rule_visibility.get(rule_id)
            observed_at = self._rule_observed_at.get(rule_id, now_ms)
            if previous:
                self._rule_winning_ms[rule_id] = self._rule_winning_ms.get(rule_id, 0) + max(0, now_ms - observed_at)
            if previous is not None and previous != is_visible:
                since = self._rule_visibility_since.get(rule_id, now_ms)
                events.append(ReconciliationEvent(
                    "rule_visible" if is_visible else "rule_fully_shadowed",
                    details={"rule_id": rule_id, "previous_duration_ms": max(0, now_ms - since)},
                ))
                self._record_churn(now_ms, "winner_change", "", rule_id)
                self._rule_visibility_since[rule_id] = now_ms
            elif previous is None:
                self._rule_visibility_since[rule_id] = now_ms
                self._rule_active_since[rule_id] = now_ms
            self._rule_visibility[rule_id] = is_visible
            self._rule_observed_at[rule_id] = now_ms
        for rule_id in set(self._rule_visibility) - active:
            self._rule_visibility.pop(rule_id, None)
            self._rule_visibility_since.pop(rule_id, None)
            self._rule_active_since.pop(rule_id, None)
            self._rule_winning_ms.pop(rule_id, None)
            self._rule_observed_at.pop(rule_id, None)
        return events

    def _record_events(self, events: list[ReconciliationEvent], now_ms: int) -> None:
        dispositions = {
            "service_applied": "applied", "service_failed": "failed",
            "service_skipped_matching_state": "skipped", "service_skipped_pending_transition": "skipped",
            "service_skipped_drift_promoted": "skipped", "service_denied_target_policy": "denied",
            "withdraw_skipped_target_policy": "withdraw_skipped", "withdraw_cancelled_user_change": "withdraw_cancelled",
            "service_would_apply": "skipped", "service_would_withdraw": "withdraw_skipped",
            "stale_result_discarded": "stale",
        }
        for event in events:
            if event.kind == "drift_promoted":
                self._record_churn(now_ms, "drift", event.target, None)
            if event.kind in {"service_applied", "service_failed"}:
                self._record_churn(now_ms, "failure" if event.kind == "service_failed" else "call", event.target, None)
            elif event.kind.startswith("service_skipped") and event.kind != "service_skipped_matching_state":
                self._record_churn(now_ms, "skip", event.target, None)
            disposition = dispositions.get(event.kind)
            if disposition is None:
                continue
            details = event.details
            call = None
            if details.get("domain") and details.get("service"):
                call = {
                    "domain": details["domain"], "service": details["service"],
                    "data": _redact_call_data(details.get("service_data", {})),
                }
            signature = self._desired_signatures.get(event.target) or self._last_applied.get(event.target)
            correlation = sha256(repr(signature or (event.target, event.kind)).encode()).hexdigest()[:16]
            self._attempt_history.append({
                "time_ms": now_ms, "target": event.target, "plan_id": correlation,
                "disposition": disposition, "withdraw": bool(details.get("withdraw")),
                "call": call, "error": _truncate_utf8(details.get("error"), 200),
            })

    def _record_churn(self, now_ms: int, kind: str, target: str, rule_id: str | None) -> None:
        self._expire_churn(now_ms)
        if target:
            evicted = self._bounded_dimension(self._churn_targets, target)
            if evicted is not None:
                self._churn_events = deque(
                    event for event in self._churn_events if event[2] != evicted
                )
        if rule_id:
            evicted = self._bounded_dimension(self._churn_rules, rule_id)
            if evicted is not None:
                self._churn_events = deque(
                    event for event in self._churn_events if event[3] != evicted
                )
        self._churn_events.append((now_ms, kind, target, rule_id))

    @staticmethod
    def _bounded_dimension(values: OrderedDict[str, None], key: str) -> str | None:
        values.pop(key, None)
        values[key] = None
        evicted = None
        while len(values) > 256:
            evicted, _value = values.popitem(last=False)
        return evicted

    def _expire_churn(self, now_ms: int) -> None:
        cutoff = now_ms - 3_600_000
        while self._churn_events and self._churn_events[0][0] < cutoff:
            self._churn_events.popleft()

    def _summarize_churn(self, cutoff: int) -> dict[str, int]:
        result: dict[str, int] = {}
        for timestamp, kind, _target, _rule_id in self._churn_events:
            if timestamp >= cutoff:
                result[kind] = result.get(kind, 0) + 1
        return result

    async def _withdraw_stale_targets(
        self,
        engine: Any,
        adapter: HAAdapter,
        now_ms: int,
        *,
        revision_is_current: Callable[[], bool] | None = None,
        before_dispatch: Callable[[], Awaitable[None]] | None = None,
    ) -> list[ReconciliationEvent]:
        events: list[ReconciliationEvent] = []
        active_targets = set(engine.list_active_targets())
        for stale_target in sorted(set(self._last_resolved) - active_targets):
            previous = self._last_resolved[stale_target]
            policy = getattr(engine, "target_policy", lambda _target: None)(stale_target)
            explicit_withdraw = _owned_withdrawals(previous)
            if previous.dispatch == "shadow" or policy is not None and policy.dispatch == "shadow":
                self._last_resolved.pop(stale_target, None)
                self._last_applied.pop(stale_target, None)
                value = explicit_withdraw or _default_withdraw_value(stale_target, previous)
                shadow = {"would_apply": None, "would_withdraw": value}
                if self._shadow_plans.get(stale_target) != shadow:
                    self._shadow_plans[stale_target] = shadow
                    events.append(ReconciliationEvent("service_would_withdraw", stale_target, {"value": value}))
                continue
            if (
                previous.ownership != "managed"
                or policy is not None
                and policy.ownership != "managed"
            ):
                self._last_resolved.pop(stale_target, None)
                self._last_applied.pop(stale_target, None)
                ownership = policy.ownership if policy is not None else previous.ownership
                events.append(
                    ReconciliationEvent(
                        "withdraw_skipped_target_policy", stale_target, {"ownership": ownership}
                    )
                )
                continue
            withdraw_value = explicit_withdraw or (
                _default_withdraw_value(stale_target, previous)
                if not _has_explicit_withdrawal(previous)
                else None
            )
            if withdraw_value is None:
                self._last_resolved.pop(stale_target, None)
                continue
            transition_ms = (
                previous.transition_withdraw_ms
                if previous.transition_withdraw_ms is not None
                else previous.transition_ms
            )
            calls = service_calls_for_resolved_target(
                stale_target,
                withdraw_value,
                transition_ms=transition_ms,
            )
            if not calls:
                continue
            signature = service_plan_signature(calls)
            retry = self._service_failure_backoff.get(stale_target)
            if retry is not None and now_ms < retry.retry_at_ms:
                continue
            current_state = adapter.get_state(stale_target)
            if (
                (
                    policy is not None
                    and policy.unavailable == "skip"
                    or policy is None
                    and current_state is not None
                )
                and _state_is_unavailable(current_state)
            ):
                denial = {
                    "code": "target_unavailable",
                    "message": (
                        f"{stale_target} is unavailable; dispatch is skipped."
                    ),
                }
                if self._policy_denials.get(stale_target) != denial:
                    self._policy_denials[stale_target] = denial
                    events.append(
                        ReconciliationEvent(
                            "service_denied_target_policy", stale_target, denial
                        )
                    )
                continue
            if current_state is not None and service_plan_matches_state(signature, current_state):
                self._last_resolved.pop(stale_target, None)
                self._last_applied[stale_target] = signature
                events.append(ReconciliationEvent("service_skipped_matching_state", stale_target))
                continue
            if self._last_applied.get(stale_target) == signature:
                if current_state is not None and _state_has_user_context(current_state):
                    self._last_resolved.pop(stale_target, None)
                    self._last_applied.pop(stale_target, None)
                    clear_pending_state_drift(self._drift_candidates, stale_target)
                    events.append(
                        ReconciliationEvent("withdraw_cancelled_user_change", stale_target)
                    )
                    continue
                suppress_until = self._drift_suppressed_until.get(stale_target)
                if suppress_until is not None and now_ms < suppress_until:
                    continue
            applied = await self._invoke_service_plan(
                adapter,
                stale_target,
                calls,
                signature,
                withdraw=True,
                revision_is_current=revision_is_current,
                before_dispatch=before_dispatch,
            )
            events.extend(applied.events)
            if revision_is_current is not None and not revision_is_current():
                events.append(ReconciliationEvent("stale_result_discarded", stale_target))
                return events
            if applied.failed:
                events.append(self._schedule_retry(stale_target, signature, now_ms, withdraw=True))
            else:
                recovered = self._service_failure_backoff.pop(stale_target, None)
                if recovered is not None:
                    events.append(
                        ReconciliationEvent(
                            "service_retry_recovered",
                            stale_target,
                            {"failures": recovered.failures, "withdraw": True},
                        )
                    )
                self._last_applied[stale_target] = signature
                clear_pending_state_drift(self._drift_candidates, stale_target)
                self._suppress_drift_during_transition(stale_target, transition_ms, now_ms)
                if current_state is None:
                    self._last_resolved.pop(stale_target, None)
        self._prune_obsolete_retry_state(active_targets | set(self._last_resolved))
        return events

    def _schedule_retry(
        self, target: str, signature: ServicePlanSignature, now_ms: int, *, withdraw: bool = False
    ) -> ReconciliationEvent:
        previous = self._service_failure_backoff.get(target)
        failures = previous.failures + 1 if previous is not None else 1
        capped_exponent = min(
            failures - 1,
            max(0, self._service_failure_backoff_max_ms.bit_length()),
        )
        exponential_delay = min(
            self._service_failure_backoff_ms * (2**capped_exponent),
            self._service_failure_backoff_max_ms,
        )
        delay_ms = self._retry_jitter_fn(target, failures, exponential_delay)
        delay_ms = max(0, min(int(delay_ms), self._service_failure_backoff_max_ms))
        retry_at_ms = now_ms + delay_ms
        self._service_failure_backoff[target] = _RetryState(failures, retry_at_ms, signature)
        return ReconciliationEvent(
            "service_retry_scheduled",
            target,
            {
                "attempt": failures + 1,
                "failures": failures,
                "delay_ms": delay_ms,
                "retry_at_ms": retry_at_ms,
                "withdraw": withdraw,
            },
        )

    def _prune_obsolete_retry_state(self, retained_targets: set[str]) -> None:
        for target in set(self._service_failure_backoff) - retained_targets:
            self._service_failure_backoff.pop(target, None)
        for target in set(self._service_plan_progress) - retained_targets:
            self._service_plan_progress.pop(target, None)

    async def _invoke_service_plan(
        self,
        adapter: HAAdapter,
        target: str,
        calls: tuple[tuple[str, str, dict[str, Any]], ...],
        signature: ServicePlanSignature,
        *,
        withdraw: bool = False,
        revision_is_current: Callable[[], bool] | None = None,
        before_dispatch: Callable[[], Awaitable[None]] | None = None,
    ) -> _ServicePlanResult:
        """Call each service in the plan; stop on first failure."""
        fired: list[ReconciliationEvent] = []
        progress_signature, start_at = self._service_plan_progress.get(target, (signature, 0))
        if progress_signature != signature:
            start_at = 0
        # Include successful calls from earlier attempts so partial ownership is
        # cumulative when another later call fails.
        completed: list[tuple[str, str, dict[str, Any]]] = list(calls[:start_at])
        context = adapter.new_context()
        resumable = all(
            _service_call_is_idempotent(service) for _domain, service, _data in calls[:start_at]
        )
        for call_index, (domain, service, service_data) in enumerate(calls[start_at:], start_at):
            try:
                if before_dispatch is not None:
                    await before_dispatch()
                await adapter.async_call(domain, service, service_data, context=context)
            except Exception as err:  # noqa: BLE001 — record and back off
                fired.append(
                    ReconciliationEvent(
                        "service_failed",
                        target,
                        {
                            "domain": domain,
                            "service": service,
                            "service_data": service_data,
                            "error": str(err),
                            "retry_after_ms": self._service_failure_backoff_ms,
                            "withdraw": withdraw,
                        },
                    )
                )
                if revision_is_current is not None and not revision_is_current():
                    return _ServicePlanResult(fired, failed=True, completed_calls=tuple(completed))
                if call_index and resumable:
                    self._service_plan_progress[target] = (signature, call_index)
                else:
                    self._service_plan_progress.pop(target, None)
                return _ServicePlanResult(fired, failed=True, completed_calls=tuple(completed))
            if revision_is_current is not None and not revision_is_current():
                completed.append((domain, service, service_data))
                return _ServicePlanResult(fired, failed=False, completed_calls=tuple(completed))
            fired.append(
                ReconciliationEvent(
                    "service_applied",
                    target,
                    {
                        "domain": domain,
                        "service": service,
                        "service_data": service_data,
                        "withdraw": withdraw,
                    },
                )
            )
            completed.append((domain, service, service_data))
            resumable = resumable and _service_call_is_idempotent(service)
        self._service_plan_progress.pop(target, None)
        return _ServicePlanResult(fired, failed=False, completed_calls=tuple(completed))

    def _suppress_drift_during_transition(
        self, target: str, transition_ms: int, now_ms: int
    ) -> None:
        self._drift_suppressed_until[target] = (
            now_ms + max(0, transition_ms) + self._drift_transition_grace_ms
        )

    # ----- lifecycle / restart safety -----

    def export_pending_withdraws(self, engine: Any) -> list[dict[str, Any]]:
        """Return target ownership records needed to withdraw after reload/restart."""
        records: list[dict[str, Any]] = []
        for target, state in sorted(self._last_resolved.items()):
            records.append(
                {
                    "target": target,
                    "value": dict(state.value),
                    "winner_key": list(state.winner_key) if state.winner_key is not None else None,
                    "winner_confidence": state.winner_confidence,
                    "transition_ms": state.transition_ms,
                    "transition_withdraw_ms": state.transition_withdraw_ms,
                    "withdraw_value": dict(state.withdraw_value)
                    if state.withdraw_value is not None
                    else None,
                    "ownership": state.ownership,
                    "field_ownership": dict(state.field_ownership),
                    "dispatch": state.dispatch,
                }
            )
        return records

    def pending_withdraw_targets(self) -> tuple[str, ...]:
        """Return Targets retained for possible withdrawal."""
        return tuple(sorted(self._last_resolved))

    def export_runtime_state(self, engine: Any) -> dict[str, Any]:
        """Return bounded, versioned restart state for ownership and retries."""
        now_ms = self._last_reconciliation_now_ms or 0
        retries = []
        for target, retry in sorted(self._service_failure_backoff.items())[:_MAX_PERSISTED_TARGETS]:
            retries.append({
                "target": target,
                "failures": retry.failures,
                "remaining_ms": max(
                    0,
                    min(retry.retry_at_ms - now_ms, self._service_failure_backoff_max_ms),
                ),
                "signature": retry.signature,
            })
        progress = []
        for target, (signature, next_call_index) in sorted(self._service_plan_progress.items())[:_MAX_PERSISTED_TARGETS]:
            progress.append({
                "target": target,
                "signature": signature,
                "next_call_index": next_call_index,
            })
        return {
            "version": _RECONCILIATION_STATE_VERSION,
            "pending_withdraws": self.export_pending_withdraws(engine)[:_MAX_PERSISTED_TARGETS],
            "service_failure_backoff": retries,
            "service_plan_progress": progress,
        }

    def restore_pending_withdraws(
        self,
        records: dict[str, Any] | None,
        *,
        linger_rule_ids: set[str] | None = None,
        now_ms: int | None = None,
    ) -> None:
        if not isinstance(records, dict):
            return
        runtime = records.get("reconciliation", records)
        if not isinstance(runtime, dict):
            return
        pending_withdraws = runtime.get("pending_withdraws", [])
        if not isinstance(pending_withdraws, list):
            pending_withdraws = []
        for raw in pending_withdraws:
            if not isinstance(raw, dict):
                continue
            restored = _resolved_target_state_from_record(raw, value_key="value")
            if restored is not None:
                target, state = restored
                self._last_resolved[target] = state
        version = runtime.get("version")
        if version in {1, _RECONCILIATION_STATE_VERSION}:
            restore_now_ms = 0 if now_ms is None else now_ms
            for raw in runtime.get("service_failure_backoff", [])[:_MAX_PERSISTED_TARGETS]:
                try:
                    target = raw["target"]
                    failures = raw["failures"]
                    persisted_delay = (
                        raw["remaining_ms"]
                        if version == _RECONCILIATION_STATE_VERSION
                        else raw["retry_at_ms"]
                    )
                    signature = _freeze_signature_value(raw["signature"])
                    if not isinstance(target, str) or not target or not isinstance(failures, int) or isinstance(failures, bool) or failures < 1 or not isinstance(persisted_delay, int) or isinstance(persisted_delay, bool) or persisted_delay < 0:
                        continue
                    delay_ms = min(persisted_delay, self._service_failure_backoff_max_ms)
                    retry_at_ms = restore_now_ms + delay_ms
                    self._service_failure_backoff[target] = _RetryState(failures, retry_at_ms, signature)
                except (KeyError, TypeError, ValueError):
                    continue
            for raw in runtime.get("service_plan_progress", [])[:_MAX_PERSISTED_TARGETS]:
                try:
                    target = raw["target"]
                    next_call_index = raw["next_call_index"]
                    signature = _freeze_signature_value(raw["signature"])
                    if not isinstance(target, str) or not target or not isinstance(next_call_index, int) or isinstance(next_call_index, bool) or next_call_index < 1:
                        continue
                    self._service_plan_progress[target] = (signature, next_call_index)
                except (KeyError, TypeError, ValueError):
                    continue
        if linger_rule_ids is None or now_ms is None:
            return
        intents = records.get("intents", [])
        if not isinstance(intents, list):
            return
        for raw in intents:
            if not isinstance(raw, dict):
                continue
            rule_id = raw.get("rule_id")
            if (
                not isinstance(rule_id, str)
                or rule_id not in linger_rule_ids
                or raw.get("ignore_when") is not True
            ):
                continue
            ttl_ms = raw.get("ttl_ms")
            created_at_ms = raw.get("created_at_ms")
            if (
                not isinstance(ttl_ms, int)
                or isinstance(ttl_ms, bool)
                or ttl_ms < 0
                or not isinstance(created_at_ms, int)
                or isinstance(created_at_ms, bool)
                or created_at_ms < 0
            ):
                continue
            expires_at_ms = created_at_ms + ttl_ms
            if expires_at_ms > now_ms:
                continue
            restored = _resolved_target_state_from_record(raw, value_key="set")
            if restored is not None:
                target, state = restored
                self._last_resolved[target] = state

    def drop_inactive_applied(self, active_targets: set[str]) -> None:
        """Forget applied plans for targets no longer active (after pulse clearing)."""
        for stale_target in set(self._last_applied) - active_targets:
            self._last_applied.pop(stale_target, None)

    def retain_targets(self, targets: set[str]) -> None:
        """Reset transient bookkeeping for targets whose Rule fingerprint changed."""
        for mapping in (
            self._last_applied,
            self._drift_suppressed_until,
            self._drift_candidates,
            self._availability_recovery_until,
            self._service_failure_backoff,
            self._service_plan_progress,
            self._policy_denials,
            self._shadow_plans,
            self._desired_signatures,
            self._target_winners,
        ):
            for target in set(mapping) - targets:
                mapping.pop(target, None)
        self._unavailable_targets.intersection_update(targets)


def _state_is_unavailable(state: Any | None) -> bool:
    return state is None or str(getattr(state, "state", "")).lower() in {"unknown", "unavailable"}


def target_policy_denial(
    engine: Any, target: str, value: dict[str, Any], policy: Any | None = None
) -> dict[str, Any] | None:
    """Return the prospective dispatch denial for a resolved Target."""
    if policy is None:
        policy = getattr(engine, "target_policy", lambda _target: None)(target)
    if policy is None:
        return None
    if policy.ownership == "observe_only":
        return {
            "code": "observe_only",
            "message": f"{target} is observe_only and cannot receive a Service plan.",
        }
    fields = set(value)
    if policy.allowed_fields is not None and not fields <= policy.allowed_fields:
        denied = sorted(fields - policy.allowed_fields)
        return {
            "code": "field_not_allowed",
            "fields": denied,
            "message": f"{target} policy does not allow fields: {', '.join(denied)}.",
        }
    intents = engine.list_active_intents(target)
    state = str(value.get("state")) if "state" in value else None
    if state in policy.forbidden_automatic_states and not _field_has_user_authority(
        intents, "state"
    ):
        return {
            "code": "automatic_state_forbidden",
            "state": state,
            "message": f"{target} state {state!r} is forbidden without user Authority.",
        }
    required_fields = sorted(fields & policy.user_authority_fields)
    denied_fields = [
        field for field in required_fields if not _field_has_user_authority(intents, field)
    ]
    if denied_fields:
        return {
            "code": "user_authority_required",
            "fields": denied_fields,
            "message": f"{target} fields require user Authority: {', '.join(denied_fields)}.",
        }
    if state in policy.user_authority_states and not _field_has_user_authority(intents, "state"):
        return {
            "code": "user_authority_required",
            "state": state,
            "message": f"{target} state {state!r} requires user Authority.",
        }
    return None


def _field_has_user_authority(intents: list[Any], field: str) -> bool:
    providers = [intent for intent in intents if field in intent.set]
    return bool(
        providers and max(providers, key=lambda intent: intent.priority).authority.value == "user"
    )


@dataclass
class _ServicePlanResult:
    events: list[ReconciliationEvent]
    failed: bool
    completed_calls: tuple[tuple[str, str, dict[str, Any]], ...] = ()


# --------------------------------------------------------------------------- #
# API helpers (kept for the HTTP API; see ADR-0001 flagged ambiguity)
# --------------------------------------------------------------------------- #


def actual_conditions_for_desired_record(
    record: dict[str, Any],
    actual_state: Any | None,
) -> list[dict[str, str]]:
    """Return reconciliation conditions for one desired record and actual state."""
    if actual_state is None:
        return [{"type": "ActualObserved", "status": "false"}]
    target = record["target"]
    calls = service_calls_for_resolved_target(target, dict(record["desired"]))
    matches = (
        service_plan_matches_state(service_plan_signature(calls), actual_state) if calls else False
    )
    return [{"type": "ActualMatchesDesired", "status": "true" if matches else "false"}]


def actual_snapshot(actual_state: Any) -> dict[str, Any]:
    """Return the compact actual-state shape exposed to agents."""
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
    return {
        "state": actual_state.state,
        "attributes": {
            key: value for key, value in actual_state.attributes.items() if key in safe_fields
        },
    }
