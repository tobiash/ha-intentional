"""Document-wide policy preflight for loaded Intentional rules."""

from __future__ import annotations

from collections import defaultdict
from numbers import Real
from typing import Any

from ._engine.capabilities import EFFECT_ONLY_DOMAINS
from ._engine.engine import Engine
from ._engine.yaml_loader import RuleLoadError, load_rules_from_string

DANGEROUS_TARGET_DOMAINS = frozenset({"lock", "alarm_control_panel", "cover", "valve", "climate"})


def document_policy_findings(rules: list[Any]) -> dict[str, list[dict[str, Any]]]:
    """Return policy errors and warnings without consulting runtime state."""
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    by_target: dict[str, list[Any]] = defaultdict(list)
    authored_ids: set[str] = set()
    graph: dict[str, set[str]] = defaultdict(set)

    for rule in rules:
        authored_id = _authored_id(rule)
        authored_ids.add(authored_id)
        authored_ids.add(rule.id)
        graph.setdefault(authored_id, set())
        graph[authored_id].update(getattr(rule, "blocks", ()) or ())
        target = getattr(rule, "target", "")
        if target:
            by_target[target].append(rule)

    for source, blocked_ids in sorted(graph.items()):
        for blocked_id in sorted(blocked_ids - authored_ids):
            errors.append(
                {
                    "code": "missing_suppression_rule",
                    "rule_id": source,
                    "suppressed_rule_id": blocked_id,
                    "message": f"Rule {source!r} suppresses unknown Rule ID {blocked_id!r}.",
                }
            )

    for cycle in _suppression_cycles(graph, authored_ids):
        errors.append(
            {
                "code": "suppression_cycle",
                "rule_ids": cycle,
                "message": f"Suppression cycle detected: {' -> '.join([*cycle, cycle[0]])}.",
            }
        )

    for target, target_rules in sorted(by_target.items()):
        domain = target.partition(".")[0]
        policy = next(
            (
                getattr(rule, "target_policy", None)
                for rule in target_rules
                if getattr(rule, "target_policy", None) is not None
            ),
            None,
        )
        if domain in DANGEROUS_TARGET_DOMAINS and policy is None:
            warnings.append(
                {
                    "code": "dangerous_target_without_policy",
                    "target": target,
                    "rule_ids": sorted({_authored_id(rule) for rule in target_rules}),
                    "message": f"{target} uses a safety-sensitive domain without an explicit document Target policy; legacy behavior is preserved.",
                }
            )
        if policy is not None:
            for rule in target_rules:
                fields = set().union(
                    *(
                        set(getattr(rule, operation, {}) or {})
                        for operation in ("set", "cap", "floor", "offset", "multiply")
                    )
                )
                if policy.allowed_fields is not None and not fields <= policy.allowed_fields:
                    denied = sorted(fields - policy.allowed_fields)
                    errors.append(
                        {
                            "code": "target_policy_field_denied",
                            "target": target,
                            "rule_id": _authored_id(rule),
                            "fields": denied,
                            "message": f"{target} policy does not allow fields: {', '.join(denied)}.",
                        }
                    )
                state = (getattr(rule, "set", {}) or {}).get("state")
                if (
                    state is not None
                    and str(state) in policy.forbidden_automatic_states
                    and getattr(rule.authority, "value", rule.authority) != "user"
                ):
                    errors.append(
                        {
                            "code": "target_policy_automatic_state_denied",
                            "target": target,
                            "rule_id": _authored_id(rule),
                            "state": str(state),
                            "message": f"{target} state {state!r} is forbidden without user Authority.",
                        }
                    )
        if domain in EFFECT_ONLY_DOMAINS:
            rule_ids = sorted({_authored_id(rule) for rule in target_rules})
            errors.append(
                {
                    "code": "effect_only_durable_target",
                    "target": target,
                    "rule_ids": rule_ids,
                    "message": f"{target} is effect-only and cannot be used as a durable Target; use `effect` instead.",
                }
            )

        fields = set().union(*(set(getattr(rule, "floor", {}) or {}) for rule in target_rules))
        fields.update(*(set(getattr(rule, "cap", {}) or {}) for rule in target_rules))
        for field in sorted(fields):
            floors = [
                (rule, rule.floor[field])
                for rule in target_rules
                if field in (getattr(rule, "floor", {}) or {}) and _is_number(rule.floor[field])
            ]
            caps = [
                (rule, rule.cap[field])
                for rule in target_rules
                if field in (getattr(rule, "cap", {}) or {}) and _is_number(rule.cap[field])
            ]
            if floors and caps:
                floor_rule, floor_value = max(floors, key=lambda item: item[1])
                cap_rule, cap_value = min(caps, key=lambda item: item[1])
                if floor_value > cap_value:
                    warnings.append(
                        {
                            "code": "contradictory_floor_cap",
                            "target": target,
                            "field": field,
                            "floor": floor_value,
                            "cap": cap_value,
                            "rule_ids": sorted({_authored_id(floor_rule), _authored_id(cap_rule)}),
                            "message": f"{target}.{field} has floor {floor_value!r} above cap {cap_value!r}.",
                        }
                    )

        baseline_fields = set().union(
            *(set(getattr(rule, "set", {}) or {}) for rule in target_rules)
        )
        modifier_rules: dict[str, set[str]] = defaultdict(set)
        for rule in target_rules:
            for modifier in ("offset", "multiply"):
                for field in getattr(rule, modifier, {}) or {}:
                    if field not in baseline_fields:
                        modifier_rules[field].add(_authored_id(rule))
        for field, rule_ids in sorted(modifier_rules.items()):
            warnings.append(
                {
                    "code": "modifier_without_document_baseline",
                    "target": target,
                    "field": field,
                    "rule_ids": sorted(rule_ids),
                    "message": f"{target}.{field} is only modified by `offset`/`multiply`; no Rule in this document provides a `set` baseline.",
                }
            )

    return {"errors": errors, "warnings": warnings}


def load_and_preflight_document(contents: str) -> tuple[list[Any], dict[str, list[dict[str, Any]]]]:
    """Completely validate a document and reject document-wide policy errors."""
    rules, findings = validate_document(contents)
    if findings["errors"]:
        messages = "; ".join(finding["message"] for finding in findings["errors"])
        raise RuleLoadError(f"Document preflight failed: {messages}")
    return rules, findings


def validate_document(contents: str) -> tuple[list[Any], dict[str, list[dict[str, Any]]]]:
    """Run the complete document validation pipeline and return structured findings."""
    try:
        rules = load_rules_from_string(contents)
    except RuleLoadError as err:
        return [], {
            "errors": [
                {
                    "code": "rule_load_error",
                    "message": f"Rule loading failed: {err}",
                }
            ],
            "warnings": [],
        }
    try:
        Engine.validate_rules(rules)
    except Exception as err:
        return rules, {
            "errors": [
                {
                    "code": "rule_validation_error",
                    "message": f"Rule validation failed: {err}",
                }
            ],
            "warnings": [],
        }
    findings = document_policy_findings(rules)
    if findings["errors"]:
        for finding in findings["errors"]:
            finding["message"] = f"Document preflight failed: {finding['message']}"
    return rules, findings


def _authored_id(rule: Any) -> str:
    return getattr(rule, "authored_rule_id", "") or rule.id


def _is_number(value: Any) -> bool:
    return isinstance(value, Real) and not isinstance(value, bool)


def _suppression_cycles(graph: dict[str, set[str]], known_ids: set[str]) -> list[list[str]]:
    cycles: dict[frozenset[str], list[str]] = {}

    def visit(node: str, path: list[str]) -> None:
        if node in path:
            start = path.index(node)
            cycle = path[start:]
            key = frozenset(cycle)
            if key not in cycles:
                first = min(range(len(cycle)), key=lambda index: cycle[index])
                cycles[key] = cycle[first:] + cycle[:first]
            return
        for blocked_id in sorted(graph.get(node, set())):
            if blocked_id in known_ids:
                visit(blocked_id, [*path, node])

    for rule_id in sorted(graph):
        visit(rule_id, [])
    return sorted(cycles.values())
