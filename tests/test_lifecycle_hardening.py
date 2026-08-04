from __future__ import annotations

from dataclasses import replace

import pytest

from intentional.engine import Engine
from intentional.intent import Intent
from intentional.lifecycle import intent_from_lifecycle_record, intent_to_lifecycle_record
from intentional.reconciliation import Reconciliation
from intentional.yaml_loader import load_rules_from_string


@pytest.mark.parametrize(
    ("field", "malformed"),
    [
        ("target", None),
        ("set", None),
        ("merge", 1),
        ("cap", []),
        ("floor", "bad"),
        ("offset", 1),
        ("multiply", False),
        ("transition_ms", "1"),
        ("transition_assert_ms", "1"),
        ("transition_change_ms", -1),
        ("transition_withdraw_ms", True),
        ("easing", None),
        ("authority", 1),
        ("confidence", "1"),
        ("ttl_ms", "1000"),
        ("manual_override_ttl_ms", "1000"),
        ("reason", None),
        ("rule_id", 1),
        ("ignore_when", 0),
        ("selector_generated", "false"),
        ("created_at_ms", "1000"),
        ("animation", []),
        ("generators", []),
    ],
)
def test_malformed_intent_fields_fail_closed(field: str, malformed: object) -> None:
    record = intent_to_lifecycle_record(
        Intent(target="light.test", set={"state": "on"}, created_at_ms=1_000)
    )
    record[field] = malformed

    assert intent_from_lifecycle_record(record) is None


@pytest.mark.parametrize(
    "collection",
    [
        "intents",
        "active_effect_rule_ids",
        "generated_fields",
        "effect_outbox",
        "paused_labels",
        "paused_rule_ids",
        "rule_activations",
    ],
)
@pytest.mark.parametrize("malformed", [None, 1, True, {}, "records"])
def test_malformed_top_level_collections_never_crash(
    collection: str, malformed: object
) -> None:
    engine = Engine(clock_fn=lambda: 1_000)
    engine.import_lifecycle_records({"version": 2, collection: malformed})

    assert engine.list_active_intents("light.test") == []


@pytest.mark.parametrize("collection", ["pending_withdraws", "intents"])
@pytest.mark.parametrize("malformed", [None, 1, True, {}, "records"])
def test_malformed_reconciliation_lifecycle_collections_never_crash(
    collection: str, malformed: object
) -> None:
    reconciliation = Reconciliation(
        drift_override_ttl_ms=1,
        drift_confirmation_ms=0,
        service_failure_backoff_ms=1,
    )

    reconciliation.restore_pending_withdraws(
        {collection: malformed}, linger_rule_ids={"rule"}, now_ms=1_000
    )

    assert reconciliation.pending_withdraw_targets() == ()


def test_invalid_record_does_not_prevent_valid_sibling_restore() -> None:
    valid = intent_to_lifecycle_record(
        Intent(target="light.valid", set={"state": "on"}, ttl_ms=5_000, created_at_ms=1_000)
    )
    engine = Engine(clock_fn=lambda: 2_000)

    engine.import_lifecycle_records({"version": 1, "intents": [{"set": []}, valid]})

    assert engine.resolve("light.valid").value == {"state": "on"}


def test_ttl_restore_rebases_future_creation_after_wall_clock_rollback() -> None:
    record = intent_to_lifecycle_record(
        Intent(target="light.test", set={"state": "on"}, ttl_ms=5_000, created_at_ms=10_000)
    )
    engine = Engine(clock_fn=lambda: 1_000)

    engine.import_lifecycle_records({"version": 1, "intents": [record]})

    restored = engine.list_active_intents("light.test")[0]
    assert restored.created_at_ms == 1_000
    assert restored.ttl_ms == 5_000
    engine.advance_clock(4_999)
    assert engine.resolve("light.test") is not None
    engine.advance_clock(1)
    assert engine.resolve("light.test") is None


def test_ttl_restore_preserves_normal_absolute_expiry() -> None:
    record = intent_to_lifecycle_record(
        Intent(target="light.test", set={"state": "on"}, ttl_ms=5_000, created_at_ms=1_000)
    )
    engine = Engine(clock_fn=lambda: 3_000)

    engine.import_lifecycle_records({"version": 1, "intents": [record]})

    restored = engine.list_active_intents("light.test")[0]
    assert restored.created_at_ms == 1_000
    assert restored.expires_at_ms() == 6_000


def test_rule_fingerprint_is_shared_and_stable_under_dict_insertion_order() -> None:
    rule = load_rules_from_string("""
- id: stable
  observe: {binary_sensor.room: on}
  intent:
    light.room: {state: on, brightness_pct: 40}
""")[0]
    reordered = replace(rule, set={"brightness_pct": 40, "state": "on"})

    assert Engine.rule_fingerprint(rule) == Engine.rule_fingerprint(reordered)

    source = Engine(clock_fn=lambda: 1_000)
    source.load_rules([rule])
    source.update_state("binary_sensor.room", "on")
    source.evaluate_all()
    records = source.export_lifecycle_records()
    assert records["intents"][0]["rule_fingerprint"] == Engine.rule_fingerprint(rule)

    restored = Engine(clock_fn=lambda: 2_000)
    restored.load_rules([reordered])
    restored.import_lifecycle_records(records)
    assert restored.resolve("light.room").value == {"state": "on", "brightness_pct": 40}


def test_alert_declaration_does_not_invalidate_intent_lifecycle() -> None:
    without_alert = load_rules_from_string("""
- id: stable
  while: {binary_sensor.room: "on"}
  intent: {light.room: {state: "on"}}
""")[0]
    with_alert = load_rules_from_string("""
- id: stable
  while: {binary_sensor.room: "on"}
  intent: {light.room: {state: "on"}}
  alert:
    name: RoomOccupied
    severity: info
    annotations: {summary: Room is occupied}
""")[0]

    assert Engine.rule_fingerprint(with_alert) == Engine.rule_fingerprint(without_alert)


def test_v1_restores_rule_intent_without_fingerprint_but_v2_requires_it() -> None:
    rules = load_rules_from_string("""
- id: legacy
  observe: {binary_sensor.room: on}
  intent: {light.room: {state: on}}
""")
    record = intent_to_lifecycle_record(
        Intent(
            target="light.room",
            set={"state": "on"},
            rule_id="legacy",
            ttl_ms=5_000,
            created_at_ms=1_000,
        )
    )

    legacy = Engine(clock_fn=lambda: 2_000)
    legacy.load_rules(rules)
    legacy.import_lifecycle_records({"version": 1, "intents": [record]})
    assert legacy.resolve("light.room") is not None

    current = Engine(clock_fn=lambda: 2_000)
    current.load_rules(rules)
    current.import_lifecycle_records({"version": 2, "intents": [record]})
    assert current.resolve("light.room") is None
