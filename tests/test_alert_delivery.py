from __future__ import annotations

import json
from datetime import UTC, datetime

from intentional.alerting import AlertCoordinator, AlertObservation
from intentional.alerting.delivery import NotificationRuntime

POLICY = """
route:
  id: root
  receiver: household
  group_by: [alertname, area]
  group_wait: 30s
  group_interval: 1m
  repeat_interval: 4h
receivers:
  - name: household
    destinations:
      - {type: notify_entity, entity_id: notify.family}
      - {type: persistent_notification}
"""


def firing(instance_id: str = "instance-1") -> dict[str, object]:
    return {
        "instance_id": instance_id,
        "state": "firing",
        "severity": "critical",
        "summary": "Freezer is too warm",
        "annotations": {"summary": "Freezer is too warm"},
        "labels": {"alertname": "FreezerHigh", "area": "kitchen"},
    }


def test_group_wait_plans_one_immutable_obligation_per_destination() -> None:
    ids = iter(["obligation-1", "obligation-2"])
    runtime = NotificationRuntime(id_factory=ids.__next__)

    runtime.reconcile([firing()], POLICY, now_ms=0)

    assert runtime.advance(now_ms=29_999) == []
    due = runtime.advance(now_ms=30_000)
    assert [item["obligation_id"] for item in due] == [
        "obligation-1",
        "obligation-2",
    ]
    assert all(item["status"] == "planned" for item in due)
    assert all(item["payload"]["message"] == "Freezer is too warm" for item in due)

    restored = NotificationRuntime()
    restored.import_state(runtime.export_state())
    assert restored.list_obligations() == runtime.list_obligations()


def test_resolving_during_group_wait_sends_nothing() -> None:
    runtime = NotificationRuntime()
    runtime.reconcile([firing()], POLICY, now_ms=0)

    runtime.reconcile([], POLICY, now_ms=10_000)

    assert runtime.advance(now_ms=30_000) == []
    assert runtime.list_obligations() == []


def test_uncertain_failure_retries_same_obligation_and_payload_then_dead_letters() -> None:
    runtime = NotificationRuntime(id_factory=lambda: "obligation-1", jitter=lambda: 0)
    single_destination = POLICY.replace(
        "      - {type: persistent_notification}\n", ""
    ).replace("group_wait: 30s", "group_wait: 0s")
    runtime.reconcile([firing()], single_destination, now_ms=0)
    obligation = runtime.advance(now_ms=0)[0]
    original_payload = obligation["payload"]

    for attempt in range(1, 9):
        runtime.mark_in_flight("obligation-1", now_ms=attempt * 1_000)
        runtime.reject("obligation-1", now_ms=attempt * 1_000, error_class="timeout")
        current = runtime.list_obligations()[0]
        assert current["obligation_id"] == "obligation-1"
        assert current["payload"] == original_payload

    assert runtime.list_obligations()[0]["status"] == "dead_lettered"


def test_policy_reconciliation_uses_explicit_simulation_instant() -> None:
    runtime = NotificationRuntime()
    runtime.reconcile(
        [firing()],
        POLICY,
        now_ms=0,
        at=datetime(2026, 7, 13, 12, tzinfo=UTC),
    )
    assert runtime.next_deadline_ms() == 30_000


def test_suppression_does_not_emit_false_resolution_and_in_flight_retries_after_restart() -> None:
    runtime = NotificationRuntime(id_factory=lambda: "obligation-1")
    policy = POLICY.replace("group_wait: 30s", "group_wait: 0s").replace(
        "      - {type: persistent_notification}\n", ""
    )
    runtime.reconcile([firing()], policy, now_ms=0)
    obligation = runtime.advance(now_ms=0)[0]
    runtime.mark_in_flight(obligation["obligation_id"], now_ms=0)

    restored = NotificationRuntime()
    restored.import_state(runtime.export_state())
    assert restored.advance(now_ms=0)[0]["status"] == "planned"
    restored.mark_in_flight(obligation["obligation_id"], now_ms=1)
    restored.accept(obligation["obligation_id"], now_ms=1)
    suppressed = {**firing(), "notification_suppressed": True}
    restored.reconcile([suppressed], policy, now_ms=2)
    restored.advance(now_ms=60_001)

    assert all(
        item["message_kind"] != "resolved" for item in restored.list_obligations()
    )


def test_resolution_during_in_flight_persistent_delivery_schedules_cleanup_after_acceptance() -> None:
    ids = iter(["initial", "cleanup"])
    runtime = NotificationRuntime(id_factory=ids.__next__)
    policy = POLICY.replace("group_wait: 30s", "group_wait: 0s").replace(
        "      - {type: notify_entity, entity_id: notify.family}\n", ""
    )
    runtime.reconcile([firing()], policy, now_ms=0)
    initial = runtime.advance(now_ms=0)[0]
    runtime.mark_in_flight(initial["obligation_id"], now_ms=0)

    runtime.reconcile([], policy, now_ms=1)
    runtime.accept(initial["obligation_id"], now_ms=2)

    assert runtime.advance(now_ms=60_001) == []
    cleanup = runtime.advance(now_ms=60_002)
    assert len(cleanup) == 1
    assert cleanup[0]["message_kind"] == "resolved"


def test_policy_reroute_during_in_flight_delivery_schedules_replaceable_cleanup() -> None:
    ids = iter(["initial", "new-initial", "cleanup"])
    runtime = NotificationRuntime(id_factory=ids.__next__)
    policy = POLICY.replace("group_wait: 30s", "group_wait: 0s").replace(
        "      - {type: notify_entity, entity_id: notify.family}\n", ""
    ).replace("repeat_interval: 4h", "repeat_interval: 4h\n  send_resolved: false")
    runtime.reconcile([firing()], policy, now_ms=0)
    initial = runtime.advance(now_ms=0)[0]
    runtime.mark_in_flight(initial["obligation_id"], now_ms=0)

    edited = policy.replace("id: root", "id: rerouted")
    runtime.reconcile([firing()], edited, now_ms=1)
    runtime.accept(initial["obligation_id"], now_ms=2)
    due = runtime.advance(now_ms=60_002)

    assert any(item["message_kind"] == "cleanup" for item in due)


def test_suppression_release_is_debounced_before_first_acceptance() -> None:
    runtime = NotificationRuntime()
    policy = POLICY.replace("group_wait: 30s", "group_wait: 0s")
    runtime.reconcile(
        [{**firing(), "notification_suppressed": True}], policy, now_ms=0
    )
    runtime.reconcile([firing()], policy, now_ms=1_000)

    assert runtime.advance(now_ms=5_999) == []
    assert len(runtime.advance(now_ms=6_000)) == 2


def test_resolved_group_retires_after_all_destinations_are_terminal() -> None:
    runtime = NotificationRuntime(id_factory=iter(["initial", "resolved"]).__next__)
    policy = POLICY.replace("group_wait: 30s", "group_wait: 0s").replace(
        "      - {type: persistent_notification}\n", ""
    )
    runtime.reconcile([firing()], policy, now_ms=0)
    initial = runtime.advance(now_ms=0)[0]
    runtime.mark_in_flight(initial["obligation_id"], now_ms=0)
    runtime.accept(initial["obligation_id"], now_ms=0)
    runtime.reconcile([], policy, now_ms=1)
    resolved = runtime.advance(now_ms=60_001)[0]
    runtime.mark_in_flight(resolved["obligation_id"], now_ms=60_001)
    runtime.accept(resolved["obligation_id"], now_ms=60_001)

    assert runtime.export_state()["groups"] == {}


def test_receiver_revision_change_only_plans_replaceable_cleanup() -> None:
    ids = iter(
        [
            "notify-initial",
            "persistent-initial",
            "cleanup",
            "notify-edited",
            "persistent-edited",
        ]
    )
    runtime = NotificationRuntime(id_factory=ids.__next__)
    policy = POLICY.replace("group_wait: 30s", "group_wait: 0s")
    runtime.reconcile([firing()], policy, now_ms=0)
    for obligation in runtime.advance(now_ms=0):
        runtime.mark_in_flight(obligation["obligation_id"], now_ms=0)
        runtime.accept(obligation["obligation_id"], now_ms=0)

    edited = policy.replace("notify.family", "notify.household")
    runtime.reconcile([firing()], edited, now_ms=1)
    cleanup = [
        item
        for item in runtime.advance(now_ms=60_001)
        if item["message_kind"] == "cleanup"
    ]

    assert [item["message_kind"] for item in cleanup] == ["cleanup"]
    assert cleanup[0]["destination"]["type"] == "persistent_notification"


def test_large_group_rendering_is_bounded_with_explicit_omission_count() -> None:
    runtime = NotificationRuntime()
    policy = POLICY.replace("group_wait: 30s", "group_wait: 0s")
    alerts = [
        {
            **firing(f"instance-{index}"),
            "summary": f"{index}:" + "x" * 4_000,
        }
        for index in range(10)
    ]
    runtime.reconcile(alerts, policy, now_ms=0)

    due = runtime.advance(now_ms=0)

    assert "more (10 total)" in due[0]["payload"]["message"]
    assert len(json.dumps(due[0]["payload"]).encode()) <= 16_384


def test_terminal_notification_details_expire_but_dead_letter_totals_survive() -> None:
    runtime = NotificationRuntime()
    runtime.import_state(
        {
            "groups": {},
            "obligations": [
                {
                    "obligation_id": "old",
                    "status": "dead_lettered",
                    "planned_at_ms": 0,
                }
            ],
            "attempts": [],
            "dead_letter_totals": {"notify_entity:timeout": 3},
        }
    )

    runtime.reconcile([], POLICY, now_ms=31 * 24 * 60 * 60 * 1_000)

    assert runtime.list_obligations() == []
    assert runtime.export_state()["dead_letter_totals"] == {
        "notify_entity:timeout": 3
    }


async def test_coordinator_persists_obligation_before_returning_it_for_dispatch() -> None:
    class Store:
        def __init__(self) -> None:
            self.data = None

        async def async_load(self):
            return self.data

        async def async_save(self, data) -> None:
            self.data = data

    store = Store()
    coordinator = AlertCoordinator(store, id_factory=lambda: "instance-1")
    await coordinator.async_load()
    await coordinator.async_set_policy(POLICY.replace("group_wait: 30s", "group_wait: 0s"), now_ms=0)
    await coordinator.async_observe(
        [
            AlertObservation(
                "freezer",
                "FreezerHigh",
                "critical",
                "Freezer is too warm",
                True,
                labels={"alertname": "FreezerHigh", "area": "kitchen"},
            )
        ],
        now_ms=0,
    )

    due = await coordinator.async_notification_advance(now_ms=0)

    assert len(due) == 2
    assert store.data["notifications"]["obligations"] == due
