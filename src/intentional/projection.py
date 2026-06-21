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
        previews.append({
            "target": target,
            "desired": desired,
            "actual": actual,
            "changes": _changes(desired, actual),
            "winning_rule_id": getattr(result.winning_intent, "rule_id", None),
            "reason": getattr(result.winning_intent, "reason", "") if result.winning_intent is not None else "",
        })
    return previews


def explain_card(engine: SimulationEngine, *, target: str | None = None) -> dict[str, Any]:
    """Return Lovelace-card-friendly explain data."""
    targets = [target] if target else list(engine.list_active_targets())
    cards = []
    for current_target in targets:
        explain = engine.explain_target(current_target)  # type: ignore[attr-defined]
        winner = explain.get("winning_intent") or {}
        resolved = explain.get("resolved") or {}
        cards.append({
            "target": current_target,
            "state": "active" if resolved else "idle",
            "desired": resolved.get("value"),
            "winning_rule_id": winner.get("rule_id"),
            "reason": winner.get("reason", ""),
            "active_intent_count": len(explain.get("active_intents") or []),
            "rules": explain.get("rules_for_target") or [],
            "intents": explain.get("active_intents") or [],
        })
    return {"targets": cards, "count": len(cards)}


def dashboard_cards(rooms: Any) -> dict[str, Any]:
    """Return a simple Lovelace entities-card layout for room controls."""
    cards = []
    for room in rooms.values():
        slug = "".join(ch if ch.isalnum() else "_" for ch in room.area_id).strip("_") or "area"
        cards.append({
            "type": "entities",
            "title": f"Intentional: {room.name}",
            "entities": [
                f"sensor.intentional_{slug}_status",
                f"switch.intentional_pause_{slug}_rules",
                f"button.intentional_clear_{slug}_manual_overrides",
            ],
        })
    return {"title": "Intentional Rooms", "cards": cards}


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
        changes.append({
            "field": field,
            "from": current,
            "to": value,
            "changed": current != value,
        })
    return changes
