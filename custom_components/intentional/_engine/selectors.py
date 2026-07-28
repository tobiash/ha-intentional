"""Selector matching and provenance for observation selectors."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .records import ObservationGroup, ObserveSelector
from .yaml_loader import Rule

SelectorResolver = Callable[[ObserveSelector], list[str]]


@dataclass(frozen=True)
class SelectorEvidence:
    """Selector result plus whether available evidence proves it."""

    value: bool
    quality: str


def observe_selectors_evidence(
    rule: Rule,
    state: dict[str, Any],
    resolver: SelectorResolver,
) -> SelectorEvidence:
    """Evaluate selector observations without collapsing unavailable evidence."""
    semantic = {True}
    for group in rule.observation_groups:
        semantic = {
            left and right
            for left in semantic
            for right in _selector_fold_possibilities(
                group.selector, group.behavior, state, resolver
            )
        }
    combined = semantic
    if rule.observe_selectors:
        legacy_matches = [
            _selector_target_possibilities(state, target, selector)
            for selector in rule.observe_selectors
            for target in resolver(selector)
            if target not in selector.exclude
        ]
        legacy = _fold_possibilities(legacy_matches, rule.observe_selector_mode)
        combined = {
            left and right for left in semantic for right in legacy
        }
    return SelectorEvidence(
        value=observe_selectors_fire(rule, state, resolver),
        quality="known" if len(combined) == 1 else "unknown",
    )


def _selector_fold_possibilities(
    selector: ObserveSelector,
    mode: str,
    state: dict[str, Any],
    resolver: SelectorResolver,
) -> set[bool]:
    matches = [
        _selector_target_possibilities(state, target, selector)
        for target in resolver(selector)
        if target not in selector.exclude
    ]
    return _fold_possibilities(matches, mode)


def _selector_target_possibilities(
    state: dict[str, Any], target: str, selector: ObserveSelector
) -> set[bool]:
    actual = state.get(f"{target}.{selector.field}")
    explicit_unavailable = (
        selector.operator in {"is", "is_not"}
        and selector.value in {"unknown", "unavailable"}
    )
    availability = state.get(f"{target}.availability")
    if (
        availability in {"unknown", "unavailable"}
        or actual is None
        or actual in {"unknown", "unavailable"}
    ) and not explicit_unavailable:
        return {False, True}
    return {observe_selector_target_matches(state, target, selector)}


def _fold_possibilities(matches: list[set[bool]], mode: str) -> set[bool]:
    if not matches:
        return {mode in {"all", "none"}}
    if mode == "any":
        if any(values == {True} for values in matches):
            return {True}
        if all(values == {False} for values in matches):
            return {False}
        return {False, True}
    if mode == "all":
        if any(values == {False} for values in matches):
            return {False}
        if all(values == {True} for values in matches):
            return {True}
        return {False, True}
    if mode == "none":
        any_possibilities = _fold_possibilities(matches, "any")
        return {not value for value in any_possibilities}
    return {False}


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
    availability = state.get(f"{target}.availability")
    if (
        selector.operator in {"is", "is_not"}
        and expected in {"unknown", "unavailable"}
        and availability in {"unknown", "unavailable"}
    ):
        actual = availability
    if selector.edge and not state.get(f"{target}.changed", False):
        return False
    if selector.purpose is not None and (
        availability in {"unknown", "unavailable"}
        or actual in {"unknown", "unavailable"}
    ):
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
