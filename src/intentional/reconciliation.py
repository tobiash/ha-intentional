"""Actual-vs-desired reconciliation status helpers."""

from __future__ import annotations

from typing import Any

from intentional.ha_adapter import (
    service_calls_for_resolved_target,
    service_plan_matches_state,
    service_plan_signature,
)


def actual_conditions_for_desired_record(
    record: dict[str, Any],
    actual_state: Any | None,
) -> list[dict[str, str]]:
    """Return reconciliation conditions for one desired record and actual state."""
    if actual_state is None:
        return [{"type": "ActualObserved", "status": "false"}]
    target = record["target"]
    calls = service_calls_for_resolved_target(target, dict(record["desired"]))
    matches = service_plan_matches_state(service_plan_signature(calls), actual_state) if calls else False
    return [{"type": "ActualMatchesDesired", "status": "true" if matches else "false"}]


def actual_snapshot(actual_state: Any) -> dict[str, Any]:
    """Return the compact actual-state shape exposed to agents."""
    return {
        "state": actual_state.state,
        "attributes": dict(actual_state.attributes),
    }
