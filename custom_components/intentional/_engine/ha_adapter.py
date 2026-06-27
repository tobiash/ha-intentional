"""Pure Home Assistant adapter helpers for the intent engine."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .adapter import FrozenValue as FrozenValue
from .adapter import SceneActivationPlan as SceneActivationPlan
from .adapter import ServiceCall as ServiceCall
from .adapter import ServicePlanSignature as ServicePlanSignature
from .adapter import ServiceSignature as ServiceSignature
from .adapter.extractor import (
    manual_set_from_state_object as manual_set_from_state_object,
)
from .adapter.matcher import service_plan_matches_state as service_plan_matches_state
from .adapter.signer import _freeze_signature_value as _freeze_signature_value
from .adapter.signer import service_plan_signature as service_plan_signature
from .adapter.signer import service_signature as service_signature
from .adapter.translator import MANUAL_SET_FIELDS as MANUAL_SET_FIELDS
from .adapter.translator import (
    manual_set_from_service_data as manual_set_from_service_data,
)
from .adapter.translator import scene_activation_plan as scene_activation_plan
from .adapter.translator import (
    service_call_for_resolved_target as service_call_for_resolved_target,
)
from .adapter.translator import (
    service_calls_for_resolved_target as service_calls_for_resolved_target,
)
from .engine import Engine
from .reconciliation import (
    classify_state_drift as classify_state_drift,
)
from .reconciliation import (
    clear_pending_state_drift as clear_pending_state_drift,
)
from .reconciliation import (
    emit_manual_override_for_state_drift as emit_manual_override_for_state_drift,
)
from .reconciliation import (
    invalidate_service_plan_for_state_change as invalidate_service_plan_for_state_change,
)
from .reconciliation import (
    pending_drift_targets as pending_drift_targets,
)
from .registry import ALARM_STATE_SERVICES as ALARM_STATE_SERVICES


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
