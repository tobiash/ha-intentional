from __future__ import annotations

import pytest

from intentional.alerting import AlertCoordinator, AlertObservation
from intentional.alerting.capabilities import CapabilityRuntime


def test_capability_is_bound_single_use_and_exports_no_raw_token() -> None:
    runtime = CapabilityRuntime(b"s" * 32, id_factory=lambda: "record-1")
    issued = runtime.issue(
        entry_id="entry-1",
        instance_id="instance-1",
        operation="acknowledge",
        destination_id="destination-1",
        now_ms=0,
        expires_at_ms=10_000,
    )

    exported = runtime.export_state()
    assert issued["token"] not in json_text(exported)
    with pytest.raises(ValueError, match="binding mismatch"):
        runtime.consume(
            issued["record_id"],
            issued["token"],
            actor="user-1",
            now_ms=1_000,
            entry_id="entry-1",
            instance_id="other",
            operation="acknowledge",
        )
    consumed = runtime.consume(
        issued["record_id"],
        issued["token"],
        actor="user-1",
        now_ms=1_000,
        entry_id="entry-1",
        instance_id="instance-1",
        operation="acknowledge",
    )
    assert consumed["consumed"] is True
    with pytest.raises(ValueError, match="unavailable"):
        runtime.consume(
            issued["record_id"],
            issued["token"],
            actor="user-1",
            now_ms=1_001,
            entry_id="entry-1",
            instance_id="instance-1",
            operation="acknowledge",
        )


def json_text(value) -> str:
    import json

    return json.dumps(value, sort_keys=True)


def test_export_never_prunes_active_capabilities() -> None:
    sequence = iter(str(index) for index in range(2_048))
    runtime = CapabilityRuntime(b"x" * 32, id_factory=sequence.__next__)
    for index in range(2_048):
        runtime.issue(
            entry_id="entry",
            instance_id=f"instance-{index}",
            operation="acknowledge",
            destination_id="destination",
            now_ms=0,
            expires_at_ms=1_000,
        )
    assert len(runtime.export_state()["records"]) == 2_048
    with pytest.raises(ValueError, match="too many active"):
        runtime.issue(
            entry_id="entry",
            instance_id="overflow",
            operation="acknowledge",
            destination_id="destination",
            now_ms=0,
            expires_at_ms=1_000,
        )


def test_capability_expiry_actor_operation_and_restart_boundaries() -> None:
    original = CapabilityRuntime(b"z" * 32, id_factory=lambda: "record-1")
    issued = original.issue(
        entry_id="entry-1",
        instance_id="instance-1",
        operation="acknowledge",
        destination_id="destination-1",
        now_ms=0,
        expires_at_ms=10_000,
    )
    restored = CapabilityRuntime(b"z" * 32)
    restored.import_state(original.export_state())
    assert restored.token("record-1") == issued["token"]

    for overrides, message in [
        ({"actor": None}, "authenticated actor"),
        ({"operation": "silence_1h"}, "binding mismatch"),
        ({"entry_id": "other"}, "binding mismatch"),
        ({"now_ms": 10_000}, "expired"),
    ]:
        arguments = {
            "actor": "user-1",
            "now_ms": 1,
            "entry_id": "entry-1",
            "instance_id": "instance-1",
            "operation": "acknowledge",
            **overrides,
        }
        with pytest.raises(ValueError, match=message):
            restored.consume("record-1", issued["token"], **arguments)

async def test_mobile_capability_consumption_is_atomic_with_acknowledgment() -> None:
    class Store:
        def __init__(self):
            self.data = None

        async def async_load(self):
            return self.data

        async def async_save(self, data):
            self.data = data

    store = Store()
    coordinator = AlertCoordinator(store, id_factory=lambda: "instance-1")
    coordinator.configure_capabilities(b"s" * 32, entry_id="entry-1")
    await coordinator.async_load()
    await coordinator.async_set_policy(
        """
route: {id: root, receiver: mobile, group_wait: 0s}
receivers:
  - {name: mobile, destinations: [{type: notify_entity, entity_id: notify.phone}]}
""",
        now_ms=0,
    )
    await coordinator.async_observe(
        [
            AlertObservation(
                "freezer",
                "FreezerHigh",
                "critical",
                "Freezer is too warm",
                True,
                labels={"alertname": "FreezerHigh"},
            )
        ],
        now_ms=0,
    )
    obligation = (await coordinator.async_notification_advance(now_ms=0))[0]
    dispatch = await coordinator.async_begin_notification_dispatch(
        obligation["obligation_id"], now_ms=0
    )
    capability = next(
        item for item in dispatch["capabilities"] if item["operation"] == "acknowledge"
    )

    await coordinator.async_consume_mobile_action(
        record_id=capability["record_id"],
        token=capability["token"],
        operation="acknowledge",
        instance_id="instance-1",
        actor="user-1",
        now_ms=1_000,
    )

    assert coordinator.list_alerts()[0]["acknowledgment"]["actor"] == "user-1"
    assert capability["token"] not in json_text(store.data)
