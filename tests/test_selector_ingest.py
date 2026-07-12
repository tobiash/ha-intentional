"""Pure tests for selector-aware targeted state-ingest planning."""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

_MODULE_PATH = (
    Path(__file__).parent.parent
    / "custom_components"
    / "intentional"
    / "selector_ingest.py"
)
_SPEC = importlib.util.spec_from_file_location("intentional_selector_ingest", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
SelectorMembershipPlanner = _MODULE.SelectorMembershipPlanner


@dataclass
class Entry:
    entity_id: str
    area_id: str | None = None
    device_id: str | None = None
    labels: set[str] = field(default_factory=set)
    disabled_by: str | None = None
    hidden_by: str | None = None


def selector(**values):
    return SimpleNamespace(**{
        "domain": None,
        "area": None,
        "label": None,
        "exclude": (),
        **values,
    })


def rule(*selectors):
    return SimpleNamespace(intent_selectors=selectors, observe_selectors=())


def test_membership_add_remove_and_entity_rename() -> None:
    entries = [Entry("light.desk", area_id="office")]
    planner = SelectorMembershipPlanner(lambda: entries)
    assert planner.configure([rule(selector(area="office"))], ()).added == {"light.desk"}

    entries.append(Entry("light.ceiling", area_id="office"))
    assert planner.registry_changed().added == {"light.ceiling"}
    entries[0].entity_id = "light.work_desk"
    change = planner.registry_changed()
    assert change.added == {"light.work_desk"}
    assert change.removed == {"light.desk"}

    entries[1].area_id = "kitchen"
    assert planner.registry_changed().removed == {"light.ceiling"}


def test_state_creation_order_is_covered_by_registry_membership() -> None:
    entries: list[Entry] = []
    planner = SelectorMembershipPlanner(lambda: entries)
    planner.configure([rule(selector(label="task"))], ())
    entries.append(Entry("switch.late_state", labels={"task"}))

    assert planner.registry_changed().added == {"switch.late_state"}
    assert "switch.late_state" in planner.relevant


def test_overlapping_selectors_remove_only_after_last_reference_departs() -> None:
    entries = [Entry("light.desk", area_id="office", labels={"task"})]
    planner = SelectorMembershipPlanner(lambda: entries)
    planner.configure(
        [rule(selector(area="office"), selector(label="task"))],
        {"light.explicit"},
    )
    planner.update_owned({"light.desk"})

    entries[0].area_id = "kitchen"
    entries[0].labels.clear()
    assert planner.registry_changed().removed == frozenset()
    assert planner.update_owned(()).removed == {"light.desk"}


def test_reload_replaces_selectors_and_preserves_owned_targets() -> None:
    entries = [
        Entry("light.office", area_id="office"),
        Entry("light.kitchen", area_id="kitchen"),
    ]
    planner = SelectorMembershipPlanner(lambda: entries)
    planner.configure([rule(selector(area="office"))], ())
    planner.update_owned({"light.office"})

    change = planner.configure([rule(selector(area="kitchen"))], ())
    assert change.added == {"light.kitchen"}
    assert change.removed == frozenset()
    assert planner.update_owned(()).removed == {"light.office"}


def test_disabled_hidden_excluded_but_unavailable_membership_is_not_state_based() -> None:
    entries = [
        Entry("light.available", area_id="office"),
        Entry("light.unavailable", area_id="office"),
        Entry("light.disabled", area_id="office", disabled_by="user"),
        Entry("light.hidden", area_id="office", hidden_by="user"),
    ]
    planner = SelectorMembershipPlanner(lambda: entries)

    planner.configure([rule(selector(area="office"))], ())
    assert planner.relevant == {
        "light.available",
        "light.unavailable",
        "light.disabled",
        "light.hidden",
    }


def test_domain_selector_includes_state_only_entities() -> None:
    entries = [Entry("light.registered")]
    state_entities = {"light.registered", "light.state_only", "switch.other"}
    planner = SelectorMembershipPlanner(
        lambda: entries, state_entity_ids=lambda: state_entities
    )

    assert planner.configure([rule(selector(domain="light"))], ()).added == {
        "light.registered",
        "light.state_only",
    }

    change = planner.state_changed("light.late", exists=True)
    assert change.added == {"light.late"}
    change = planner.state_changed("light.state_only", exists=False)
    assert change.removed == {"light.state_only"}


def test_state_only_entity_cannot_satisfy_registry_metadata_selector() -> None:
    planner = SelectorMembershipPlanner(
        lambda: (), state_entity_ids=lambda: {"light.state_only"}
    )

    planner.configure([rule(selector(area="office"), selector(label="task"))], ())

    assert planner.relevant == set()


def test_selector_cache_is_keyed_by_registry_generation() -> None:
    calls = 0
    entries = [Entry("light.desk", area_id="office")]

    def get_entries():
        nonlocal calls
        calls += 1
        return entries

    planner = SelectorMembershipPlanner(get_entries)
    selected = selector(area="office")
    assert planner.resolve(selected) == ["light.desk"]
    assert planner.resolve(selected) == ["light.desk"]
    assert calls == 1
    planner.registry_changed()
    assert planner.resolve(selected) == ["light.desk"]
    assert calls == 2
