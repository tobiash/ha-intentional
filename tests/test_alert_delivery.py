from __future__ import annotations

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
