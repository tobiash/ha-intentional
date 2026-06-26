"""Pure Home Assistant adapter helpers for the intent engine."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from intentional.adapter import FrozenValue as FrozenValue
from intentional.adapter import SceneActivationPlan as SceneActivationPlan
from intentional.adapter import ServiceCall as ServiceCall
from intentional.adapter import ServicePlanSignature as ServicePlanSignature
from intentional.adapter import ServiceSignature as ServiceSignature
from intentional.adapter.extractor import (
    manual_set_from_state_object as manual_set_from_state_object,
)
from intentional.adapter.matcher import service_plan_matches_state as service_plan_matches_state
from intentional.adapter.signer import _freeze_signature_value as _freeze_signature_value
from intentional.adapter.signer import service_plan_signature as service_plan_signature
from intentional.adapter.signer import service_signature as service_signature
from intentional.adapter.translator import MANUAL_SET_FIELDS as MANUAL_SET_FIELDS
from intentional.adapter.translator import (
    manual_set_from_service_data as manual_set_from_service_data,
)
from intentional.adapter.translator import scene_activation_plan as scene_activation_plan
from intentional.adapter.translator import (
    service_call_for_resolved_target as service_call_for_resolved_target,
)
from intentional.adapter.translator import (
    service_calls_for_resolved_target as service_calls_for_resolved_target,
)
from intentional.engine import Engine
from intentional.registry import ALARM_STATE_SERVICES as ALARM_STATE_SERVICES


def time_of_day_bucket(hour: int) -> str:
    """Return the Intentional time bucket for a local hour."""
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 17:
        return "afternoon"
    if 17 <= hour < 22:
        return "evening"
    return "night"


def sync_time_context_into_engine(engine: Engine, now: datetime | None = None) -> None:
    """Sync the local time helper used by `time_of_day` rule expressions."""
    current = now or datetime.now().astimezone()
    engine.set_time_of_day(
        time_of_day_bucket(current.hour),
        clock=f"{current.hour:02d}:{current.minute:02d}",
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
    if service_plan_matches_state(plan, state):
        return False
    last_applied.pop(entity_id, None)
    return True


def classify_state_drift(
    engine: Engine,
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
            if now_ms < suppress_until:
                if drift_candidates is not None:
                    drift_candidates.pop(entity_id, None)
                return None
            drift_suppressed_until.pop(entity_id, None)
    plan = last_applied.get(entity_id)
    if plan is None:
        if drift_candidates is not None:
            drift_candidates.pop(entity_id, None)
        return None
    if service_plan_matches_state(plan, state):
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


def emit_manual_override_for_state_drift(
    engine: Engine,
    last_applied: dict[str, ServicePlanSignature],
    state: Any,
    *,
    ttl_ms: int,
    now_ms: int | None = None,
    drift_suppressed_until: dict[str, int] | None = None,
    drift_candidates: dict[str, tuple[int, FrozenValue]] | None = None,
    confirmation_ms: int = 0,
    reason: str = "Manual HA state change",
) -> bool:
    """Emit a USER intent when a managed target drifts from the applied plan.

    Legacy wrapper around classify_state_drift that applies the override
    directly to the engine. Prefer classify_state_drift for new callers.
    """
    result = classify_state_drift(
        engine,
        last_applied,
        state,
        ttl_ms=ttl_ms,
        now_ms=now_ms,
        drift_suppressed_until=drift_suppressed_until,
        drift_candidates=drift_candidates,
        confirmation_ms=confirmation_ms,
        reason=reason,
    )
    if result is None:
        return False
    engine.emit_user_intent(
        target=result["target"],
        set=result["set"],
        ttl_ms=result["ttl_ms"],
        reason=result["reason"],
    )
    return True


def _state_change_looks_like_ignored_activation(
    plan: ServicePlanSignature,
    state: Any,
) -> bool:
    """True when HA still reports off after an Intentional turn_on call.

    Some light integrations accept ``light.turn_on`` but the device never reaches
    ``on``. Without this guard that stale off state is promoted to a manual
    override, blocking retries for the drift TTL.
    """
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


def sync_state_object_into_engine(engine: Engine, state: Any) -> None:
    """Sync one HA-style State object into the engine, including attributes."""
    entity_id = state.entity_id
    engine.update_state(entity_id, state.state)

    current_fields = {"state"}
    for synthetic_field in ("changed", "triggered"):
        if f"{entity_id}.{synthetic_field}" in engine.state:
            current_fields.add(synthetic_field)
    for field, value in state.attributes.items():
        current_fields.add(field)
        engine.update_state(entity_id, value, field=field)

    prefix = f"{entity_id}."
    for key in list(engine.state):
        if not key.startswith(prefix):
            continue
        field = key[len(prefix):]
        if field not in current_fields:
            del engine.state[key]


def pulse_state_change(
    engine: Engine,
    old_state: Any | None,
    new_state: Any,
) -> bool:
    """Expose a real HA entity state change as one-cycle trigger pulses.

    Rules often need edge semantics instead of level semantics: "this value
    just changed." Home Assistant event entities also keep their latest event
    as state and attributes, so they retain the older `triggered` pulse as a
    more domain-specific alias. The integration clears pulses after one apply
    cycle.
    """
    entity_id = new_state.entity_id
    if old_state is None:
        return False
    old_attributes = getattr(old_state, "attributes", {})
    new_attributes = getattr(new_state, "attributes", {})
    event_type_changed = old_attributes.get("event_type") != new_attributes.get(
        "event_type"
    )
    if old_state.state == new_state.state and not event_type_changed:
        return False
    engine.update_state(entity_id, True, field="changed")
    if entity_id.startswith("event."):
        engine.update_state(entity_id, True, field="triggered")
    return True


def pulse_event_state_change(
    engine: Engine,
    old_state: Any | None,
    new_state: Any,
) -> bool:
    """Backward-compatible alias for event/entity state-change pulses."""
    return pulse_state_change(engine, old_state, new_state)


def clear_state_change_pulses(engine: Engine, entity_ids: set[str]) -> None:
    """Clear one-cycle state-change pulses after the integration applies them."""
    for entity_id in entity_ids:
        engine.update_state(entity_id, False, field="changed")
        if entity_id.startswith("event."):
            engine.update_state(entity_id, False, field="triggered")


def clear_event_trigger_pulses(engine: Engine, entity_ids: set[str]) -> None:
    """Backward-compatible alias for clearing state-change pulses."""
    clear_state_change_pulses(engine, entity_ids)
