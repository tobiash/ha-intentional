"""Selector matching and provenance for observation selectors."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from intentional.records import ObserveSelector
from intentional.yaml_loader import Rule

SelectorResolver = Callable[[ObserveSelector], list[str]]


def observe_selectors_fire(
    rule: Rule,
    state: dict[str, Any],
    resolver: SelectorResolver,
) -> bool:
    """Return whether a rule's selector-backed observation is satisfied."""
    if not rule.observe_selectors:
        return True
    matches: list[bool] = []
    for selector in rule.observe_selectors:
        for target in resolver(selector):
            if target in selector.exclude:
                continue
            matches.append(observe_selector_target_matches(state, target, selector))
    if rule.observe_selector_mode == "all":
        return bool(matches) and all(matches)
    if rule.observe_selector_mode == "none":
        return not any(matches)
    return any(matches)


def selector_diagnostics(
    rules: dict[str, Rule],
    state: dict[str, Any],
    resolver: SelectorResolver,
) -> list[dict[str, Any]]:
    """Return selector expansion and match provenance for diagnostics."""
    diagnostics: list[dict[str, Any]] = []
    for rule_id, rule in rules.items():
        for selector in rule.observe_selectors:
            matches = []
            for target in resolver(selector):
                if target in selector.exclude:
                    continue
                matches.append({
                    "target": target,
                    "matched": observe_selector_target_matches(state, target, selector),
                    "actual": state.get(f"{target}.{selector.field}"),
                    "expected": selector.value,
                })
            diagnostics.append({
                "rule_id": rule_id,
                "mode": rule.observe_selector_mode,
                "selector": {
                    "domain": selector.domain,
                    "area": selector.area,
                    "label": selector.label,
                    "exclude": list(selector.exclude),
                },
                "matches": matches,
            })
    return diagnostics


def observe_selector_target_matches(
    state: dict[str, Any],
    target: str,
    selector: ObserveSelector,
) -> bool:
    """Return whether one selected target satisfies its observation comparison."""
    actual = state.get(f"{target}.{selector.field}")
    expected = selector.value
    if selector.operator == "is":
        return actual == expected
    if selector.operator == "is_not":
        return actual != expected
    try:
        actual_number = float(actual)
        expected_number = float(expected)
    except (TypeError, ValueError):
        return False
    if selector.operator == "lt":
        return actual_number < expected_number
    if selector.operator == "lte":
        return actual_number <= expected_number
    if selector.operator == "gt":
        return actual_number > expected_number
    if selector.operator == "gte":
        return actual_number >= expected_number
    return False
