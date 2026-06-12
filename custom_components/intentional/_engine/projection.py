"""Projection records for agent, UI, and simulation views."""

from __future__ import annotations

from typing import Any, Protocol


class SimulationEngine(Protocol):
    """Small Interface needed to project a simulated engine step."""

    def now_ms(self) -> int: ...

    def list_active_targets(self) -> tuple[str, ...]: ...

    def resolve(self, target: str) -> Any: ...

    def list_authored_rule_statuses(self) -> dict[str, dict[str, Any]]: ...


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


def active_rule_statuses(engine: SimulationEngine) -> list[dict[str, Any]]:
    """Return authored rule statuses that are active or still hold runtime intents."""
    return [
        status
        for status in engine.list_authored_rule_statuses().values()
        if status["active"] or status["active_intent_count"]
    ]
