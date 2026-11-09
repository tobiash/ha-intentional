"""Read-only scoped dashboard facts; evaluation and resolution stay in Engine."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .compositor import resolve_intents
from .projection import redact_sensitive, target_projection


def dashboard_presentation(
    engine: Any,
    *,
    area: str | None = None,
    domain: str | None = None,
    group: str | None = None,
    area_for_target: Callable[[str], str | None] = lambda _: None,
    actual_for_target: Callable[[str], Any] = lambda _: None,
    reconciliation: Any = None,
) -> dict[str, Any]:
    """Intersect filters, retaining full Rule impact alongside scoped facts."""
    statuses = engine.list_rule_statuses()
    requests = engine.rule_target_requests()
    active_targets = engine.list_active_targets()
    intents = {target: engine.list_active_intents(target) for target in active_targets}
    grouped: dict[str, list[dict[str, Any]]] = {}
    for rule_id, status in statuses.items():
        authored_id = status.get("authored_rule_id", rule_id)
        grouped.setdefault(authored_id, []).append(status)

    def matches(target: str) -> bool:
        return (area is None or area_for_target(target) == area) and (
            domain is None or target.split(".", 1)[0] == domain
        )

    rows = []
    selected_targets: set[str] = set()
    for authored_id, variants in sorted(grouped.items()):
        if group is not None and not any(v.get("group") == group for v in variants):
            continue
        ids = {v["rule_id"] for v in variants}
        targets = {r["target"] for rule_id in ids for r in requests[rule_id]}
        targets.update(t for t, items in intents.items() if any(i.rule_id in ids for i in items))
        local = sorted(t for t in targets if matches(t))
        if not local and (area is not None or domain is not None):
            continue
        selected_targets.update(local)
        outside = sorted(targets - set(local))
        rows.append({
            "rule_id": authored_id,
            "reason": variants[0].get("reason", ""),
            "notes": variants[0].get("notes", ""),
            "group": variants[0].get("group"),
            "scope": {
                "targets": local, "outside_targets": outside,
                "coverage": "current_membership",
                "shared": bool(outside),
                "non_target_effects": any(v["non_target_effects"] for v in variants),
            },
            "variants": [{
                **v, "requests": requests[v["rule_id"]],
                "presentation_state": _lifecycle_state(v, engine.is_enabled()),
            } for v in variants],
            "scoped_active_intent_count": sum(
                i.rule_id in ids for t in local for i in intents.get(t, [])
            ),
        })
    if group is None:
        selected_targets.update(t for t in active_targets if matches(t))
        if reconciliation is not None:
            selected_targets.update(t for t in reconciliation.pending_withdraw_targets() if matches(t))
    targets = []
    for target in sorted(selected_targets):
        projection = target_projection(
            engine, target, actual_state=actual_for_target(target),
            reconciliation=reconciliation,
            rule_statuses=statuses,
        )
        actual = projection["actual"]
        projection["availability"] = (
            "missing" if actual is None else
            actual["state"] if actual["state"] in {"unknown", "unavailable"} else "available"
        )
        # Resolver provenance identifies baseline contributions, not application.
        projection["baseline_rule_ids"] = sorted({
            field["provider"]["rule_id"] for field in projection["fields"]
            if field["provider"] and field["provider"]["rule_id"]
        })
        targets.append(projection)
    by_target = {t["target"]: t for t in targets}
    for row in rows:
        ids = {v["rule_id"] for v in row["variants"]}
        row["contributions"] = [
            _contribution(engine, target, ids, intents.get(target, []), by_target[target])
            for target in row["scope"]["targets"]
        ]
        phases = {v["presentation_state"] for v in row["variants"]}
        row["presentation_state"] = next(iter(phases)) if len(phases) == 1 else "mixed"
    return redact_sensitive({
        "schema_version": 1,
        "scope": {"area": area, "domain": domain, "group": group},
        "global_enabled": engine.is_enabled(),
        "rules": rows,
        "targets": targets,
        "capabilities": {"resume_targets": [t["target"] for t in targets if t["manual_override"]]},
    })


def _lifecycle_state(status: dict[str, Any], enabled: bool) -> str:
    """Presentation precedence only; preserve orthogonal engine facts alongside."""
    if not enabled:
        return "global_off"
    if not status["enabled"]:
        return "disabled"
    if status["paused"]:
        return "paused"
    if status["blocked_by"]:
        return "blocked"
    return status["phase"]


def _contribution(engine, target, rule_ids, intents, projection) -> dict[str, Any]:
    own = [i for i in intents if i.rule_id in rule_ids]
    baseline = [
        f["field"] for f in projection["fields"]
        if f["provider"] and f["provider"]["rule_id"] in rule_ids
    ]
    modifiers = sorted({
        f["field"] for f in projection["fields"]
        if any(m["rule_id"] in rule_ids for m in f["modifiers"])
    })
    manual_fields = [
        f["field"] for f in projection["fields"]
        if f["provider"] and f["provider"]["authority"] == "user"
        and not f["provider"]["rule_id"] and any(f["field"] in i.set for i in own)
    ]
    impact_known = not any(i.animation is not None for i in intents)
    changed = [] if impact_known else None
    if own and impact_known:
        without = resolve_intents(
            target, [i for i in intents if i.rule_id not in rule_ids],
            into_the_future_ms=engine.now_ms(),
        )
        alternate = without.value if without else {}
        desired = projection["desired"] or {}
        changed = sorted(k for k in desired.keys() | alternate.keys() if desired.get(k) != alternate.get(k))
    return {
        "target": target,
        "active_intent_count": len(own),
        "baseline_fields": baseline,
        "modifier_fields": modifiers,
        "changed_fields_without_rule": changed,
        "impact_known": impact_known,
        "manual_precedence_fields": manual_fields,
        "state": (
            "inactive" if not own else "contributing" if baseline or changed else
            ("modifier_no_change" if impact_known else "modifier_unknown") if modifiers else "competing"
        ),
        "active_requests": [{
            op: dict(getattr(i, op)) for op in ("set", "cap", "floor", "offset", "multiply")
            if getattr(i, op)
        } for i in own],
    }
