from __future__ import annotations

from intentional.alerting import AlertCoordinator, AlertObservation


class Store:
    def __init__(self) -> None:
        self.data = None

    async def async_load(self):
        return self.data

    async def async_save(self, data) -> None:
        self.data = data


def observation() -> AlertObservation:
    return AlertObservation(
        "freezer",
        "FreezerHigh",
        "critical",
        "Freezer is too warm",
        True,
        labels={"alertname": "FreezerHigh", "area": "kitchen"},
    )


async def test_acknowledgment_is_durable_idempotent_and_does_not_resolve_alert() -> None:
    store = Store()
    coordinator = AlertCoordinator(store, id_factory=lambda: "instance-1")
    await coordinator.async_load()
    await coordinator.async_observe([observation()], now_ms=0)

    first = await coordinator.async_acknowledge(
        "instance-1", actor="user-1", now_ms=1_000, comment="Investigating"
    )
    second = await coordinator.async_acknowledge(
        "instance-1", actor="user-2", now_ms=2_000
    )

    assert first == second
    assert coordinator.list_alerts()[0]["state"] == "firing"
    assert coordinator.list_alerts()[0]["acknowledgment"]["actor"] == "user-1"
    restored = AlertCoordinator(store)
    await restored.async_load()
    assert restored.list_alerts()[0]["acknowledgment"] == first

    assert await restored.async_revoke_acknowledgment(
        "instance-1", actor="user-1", now_ms=3_000
    )
    assert restored.list_alerts()[0]["acknowledgment"] is None


async def test_instance_silence_expires_without_changing_alert_lifecycle() -> None:
    store = Store()
    coordinator = AlertCoordinator(store, id_factory=lambda: "instance-1")
    await coordinator.async_load()
    await coordinator.async_observe([observation()], now_ms=0)

    silence = await coordinator.async_create_instance_silence(
        "instance-1",
        actor="user-1",
        reason="Working on it",
        now_ms=1_000,
        duration_ms=3_600_000,
    )

    assert silence["expires_at_ms"] == 3_601_000
    assert coordinator.list_alerts()[0]["suppression"] == ["silence"]
    await coordinator.async_advance(now_ms=3_601_000)
    assert coordinator.list_alerts()[0]["state"] == "firing"
    assert coordinator.list_alerts()[0]["suppression"] == []


async def test_timed_severity_escalation_preserves_instance_and_supersedes_ack() -> None:
    store = Store()
    coordinator = AlertCoordinator(store, id_factory=lambda: "instance-1")
    await coordinator.async_load()
    alert = AlertObservation(
        "freezer",
        "FreezerHigh",
        "info",
        "Freezer is too warm",
        True,
        labels={"alertname": "FreezerHigh", "severity": "info"},
        escalations=((1_000, "warning"), (2_000, "critical")),
    )
    await coordinator.async_observe([alert], now_ms=0)
    await coordinator.async_acknowledge("instance-1", actor="user-1", now_ms=100)

    transitions = await coordinator.async_advance(now_ms=1_000)

    projected = coordinator.list_alerts()[0]
    assert projected["instance_id"] == "instance-1"
    assert projected["severity"] == "warning"
    assert projected["acknowledgment"] is None
    assert transitions[-1]["reason"] == "severity_escalation"
