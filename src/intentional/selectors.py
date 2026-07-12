"""Selector matching and provenance for observation selectors."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from intentional.records import ObservationGroup, ObserveSelector
from intentional.yaml_loader import Rule

SelectorResolver = Callable[[ObserveSelector], list[str]]


def observe_selectors_fire(
    rule: Rule,
    state: dict[str, Any],
    resolver: SelectorResolver,
) -> bool:
    """Return whether a rule's selector-backed observation is satisfied."""
    semantic_fires = observation_groups_fire(rule.observation_groups, state, resolver)
    if not semantic_fires or not rule.observe_selectors:
        return semantic_fires
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


def observation_groups_fire(
    groups: tuple[ObservationGroup, ...], state: dict[str, Any], resolver: SelectorResolver
) -> bool:
    """Evaluate independent semantic clauses; clauses compose with AND."""
    return all(observation_group_fires(group, state, resolver) for group in groups)


def observation_group_fires(
    group: ObservationGroup, state: dict[str, Any], resolver: SelectorResolver
) -> bool:
    matches = [
        observe_selector_target_matches(state, target, group.selector)
        for target in resolver(group.selector)
        if target not in group.selector.exclude
    ]
    if group.behavior == "all":
        return bool(matches) and all(matches)
    if group.behavior == "none":
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
        entries = [
            (selector, rule.observe_selector_mode, "while")
            for selector in rule.observe_selectors
        ]
        entries.extend((group.selector, group.behavior, "while") for group in rule.observation_groups)
        entries.extend((group.selector, group.behavior, "hold.while") for group in rule.hold_observation_groups)
        entries.extend((group.selector, group.behavior, "hold.until") for group in rule.hold_until_observation_groups)
        for selector, mode, phase in entries:
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
            selector_record = {
                "domain": selector.domain,
                "area": selector.area,
                "label": selector.label,
                "exclude": list(selector.exclude),
            }
            if selector.device is not None:
                selector_record["device"] = selector.device
            if selector.entity is not None:
                selector_record["entity"] = selector.entity
            if selector.purpose is not None:
                selector_record["purpose"] = selector.purpose
            diagnostic = {
                "rule_id": rule_id,
                "phase": phase,
                "mode": mode,
                "selector": selector_record,
                "matches": matches,
            }
            if selector.edge:
                diagnostic["edge"] = True
            diagnostics.append(diagnostic)
    return diagnostics


def observe_selector_target_matches(
    state: dict[str, Any],
    target: str,
    selector: ObserveSelector,
) -> bool:
    """Return whether one selected target satisfies its observation comparison."""
    actual = state.get(f"{target}.{selector.field}")
    expected = selector.value
    if selector.edge and not state.get(f"{target}.changed", False):
        return False
    if selector.purpose is not None and actual in {"unknown", "unavailable"}:
        return False
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
