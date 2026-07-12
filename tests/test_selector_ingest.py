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


def test_purpose_uses_effective_then_original_then_state_device_class() -> None:
    entries = [
        SimpleNamespace(entity_id="binary_sensor.override", area_id="office", device_id=None, labels=set(), device_class="motion", original_device_class="door"),
        SimpleNamespace(entity_id="binary_sensor.original", area_id="office", device_id=None, labels=set(), device_class=None, original_device_class="motion"),
        SimpleNamespace(entity_id="binary_sensor.wrong", area_id="office", device_id=None, labels=set(), device_class="door", original_device_class="motion"),
    ]
    metadata = {"binary_sensor.state_only": {"device_class": "motion"}}
    planner = SelectorMembershipPlanner(
        lambda: entries,
        state_entity_ids=lambda: metadata,
        state_metadata=metadata.get,
    )

    selected = selector(domain="binary_sensor", area=None, purpose="motion")
    assert planner.resolve(selected) == [
        "binary_sensor.original", "binary_sensor.override", "binary_sensor.state_only"
    ]


def test_existing_state_only_device_class_change_recomputes_semantic_membership() -> None:
    metadata = {"binary_sensor.dynamic": {"device_class": "door"}}
    planner = SelectorMembershipPlanner(
        lambda: (), state_entity_ids=lambda: metadata, state_metadata=metadata.get
    )
    planner.configure([rule(selector(domain="binary_sensor", purpose="motion"))], ())
    assert planner.relevant == set()

    metadata["binary_sensor.dynamic"]["device_class"] = "motion"
    assert planner.state_changed("binary_sensor.dynamic", exists=True).added == {
        "binary_sensor.dynamic"
    }


def test_registered_live_device_class_fallback_changes_membership_and_cache() -> None:
    calls = 0
    entry = SimpleNamespace(
        entity_id="binary_sensor.dynamic", area_id=None, device_id=None,
        labels=set(), device_class=None, original_device_class=None,
    )
    metadata = {"binary_sensor.dynamic": {"device_class": "door"}}

    def get_entries():
        nonlocal calls
        calls += 1
        return [entry]

    planner = SelectorMembershipPlanner(
        get_entries,
        state_entity_ids=lambda: metadata,
        state_metadata=metadata.get,
    )
    planner.configure([rule(selector(domain="binary_sensor", purpose="motion"))], ())
    scans = calls
    assert planner.relevant == set()

    assert planner.state_changed("binary_sensor.dynamic", exists=True).added == frozenset()
    assert calls == scans

    metadata["binary_sensor.dynamic"]["device_class"] = "motion"
    assert planner.state_changed("binary_sensor.dynamic", exists=True).added == {
        "binary_sensor.dynamic"
    }
    assert calls == scans + 1

    metadata["binary_sensor.dynamic"].pop("device_class")
    assert planner.state_changed("binary_sensor.dynamic", exists=True).removed == {
        "binary_sensor.dynamic"
    }
    assert calls == scans + 2


def test_registered_live_device_class_fallback_invalidates_on_removal_and_recreation() -> None:
    entry = SimpleNamespace(
        entity_id="binary_sensor.dynamic", area_id=None, device_id=None,
        labels=set(), device_class=None, original_device_class=None,
    )
    metadata = {"binary_sensor.dynamic": {"device_class": "motion"}}
    planner = SelectorMembershipPlanner(
        lambda: [entry],
        state_entity_ids=lambda: metadata,
        state_metadata=metadata.get,
    )
    planner.configure([rule(selector(domain="binary_sensor", purpose="motion"))], ())
    assert planner.relevant == {"binary_sensor.dynamic"}

    metadata.clear()
    assert planner.state_changed("binary_sensor.dynamic", exists=False).removed == {
        "binary_sensor.dynamic"
    }

    metadata["binary_sensor.dynamic"] = {"device_class": "motion"}
    assert planner.state_changed("binary_sensor.dynamic", exists=True).added == {
        "binary_sensor.dynamic"
    }


def test_ordinary_state_changes_do_not_invalidate_semantic_membership_cache() -> None:
    calls = 0
    entries = [SimpleNamespace(
        entity_id="binary_sensor.registered", area_id=None, device_id=None,
        labels=set(), device_class="motion", original_device_class=None,
    )]
    metadata = {
        "binary_sensor.state_only": {"device_class": "door"},
        "sensor.unrelated": {"device_class": "temperature"},
    }

    def get_entries():
        nonlocal calls
        calls += 1
        return entries

    planner = SelectorMembershipPlanner(
        get_entries,
        state_entity_ids=lambda: {*metadata, "binary_sensor.registered"},
        state_metadata=metadata.get,
    )
    planner.configure([rule(selector(domain="binary_sensor", purpose="motion"))], ())
    generation = planner.generation
    scans = calls

    planner.state_changed("binary_sensor.registered", exists=True)
    planner.state_changed("sensor.unrelated", exists=True)
    planner.state_changed("binary_sensor.state_only", exists=True)
    planner.state_changed("switch.unrelated_arrival", exists=True)
    planner.state_changed("switch.unrelated_arrival", exists=False)

    assert planner.generation == generation
    assert calls == scans

    metadata["binary_sensor.state_only"]["device_class"] = "motion"
    assert planner.state_changed("binary_sensor.state_only", exists=True).added == {
        "binary_sensor.state_only"
    }
    assert planner.generation == generation + 1
    assert calls == scans + 1
