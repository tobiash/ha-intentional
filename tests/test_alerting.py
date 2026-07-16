from __future__ import annotations

from copy import deepcopy

import pytest

from intentional.alerting import AlertCoordinator, AlertStateUnavailableError
from intentional.engine import Engine
from intentional.yaml_loader import load_rules_from_string


class MemoryAlertStore:
    def __init__(self) -> None:
        self.data: dict | None = None

    async def async_load(self) -> dict | None:
        return deepcopy(self.data)

    async def async_save(self, data: dict) -> None:
        self.data = deepcopy(data)


class FlakyAlertStore(MemoryAlertStore):
    def __init__(self) -> None:
        super().__init__()
        self.fail = True

    async def async_save(self, data: dict) -> None:
        if self.fail:
            raise OSError("storage unavailable")
        await super().async_save(data)


class UnreadableAlertStore(MemoryAlertStore):
    async def async_load(self) -> dict | None:
        raise OSError("storage unreadable")


@pytest.mark.asyncio
async def test_firing_alert_instance_is_durable_across_coordinator_restart() -> None:
    engine = Engine(clock_fn=lambda: 1_000)
    engine.load_rules(load_rules_from_string("""
- id: freezer-too-warm
  while: {sensor.freezer_temperature: {gt: -10}}
  alert:
    name: FreezerTemperatureHigh
    severity: critical
    annotations: {summary: Freezer is too warm}
"""))
    engine.update_state("sensor.freezer_temperature", -5)
    engine.evaluate_all()
    store = MemoryAlertStore()
    coordinator = AlertCoordinator(
        store,
        id_factory=lambda: "00000000-0000-4000-8000-000000000001",
    )

    await coordinator.async_load()
    await coordinator.async_observe(engine.alert_observations(), now_ms=engine.now_ms())
    firing = coordinator.list_alerts()[0]

    restored = AlertCoordinator(store)
    await restored.async_load()

    assert firing == {
        "rule_id": "freezer-too-warm",
        "name": "FreezerTemperatureHigh",
        "severity": "critical",
        "summary": "Freezer is too warm",
        "state": "firing",
        "instance_id": "00000000-0000-4000-8000-000000000001",
        "evaluation_status": "current",
    }
    assert restored.list_alerts() == [firing]


@pytest.mark.asyncio
async def test_inactive_observation_durably_resolves_alert() -> None:
    store = MemoryAlertStore()
    coordinator = AlertCoordinator(
        store,
        id_factory=lambda: "00000000-0000-4000-8000-000000000001",
    )
    await coordinator.async_load()
    active = engine_observation(active=True)
    await coordinator.async_observe([active], now_ms=1_000)

    transitions = await coordinator.async_observe(
        [engine_observation(active=False)], now_ms=2_000
    )

    assert transitions == [{
        "rule_id": "freezer-too-warm",
        "name": "FreezerTemperatureHigh",
        "instance_id": "00000000-0000-4000-8000-000000000001",
        "to": "resolved",
        "at_ms": 2_000,
        "reason": "condition_inactive",
    }]
    assert coordinator.list_alerts()[0]["state"] == "inactive"
    assert store.data["alerts"][0]["state"] == "inactive"


@pytest.mark.asyncio
async def test_missing_definition_resolves_active_alert() -> None:
    store = MemoryAlertStore()
    coordinator = AlertCoordinator(
        store,
        id_factory=lambda: "00000000-0000-4000-8000-000000000001",
    )
    await coordinator.async_load()
    await coordinator.async_observe([engine_observation(active=True)], now_ms=1_000)

    transitions = await coordinator.async_observe([], now_ms=2_000)

    assert transitions[0]["to"] == "resolved"
    assert transitions[0]["reason"] == "definition_removed"
    assert coordinator.list_alerts()[0]["state"] == "inactive"


@pytest.mark.asyncio
async def test_alert_truth_remains_visible_while_persistence_retries() -> None:
    store = FlakyAlertStore()
    coordinator = AlertCoordinator(
        store,
        id_factory=lambda: "00000000-0000-4000-8000-000000000001",
    )
    await coordinator.async_load()

    await coordinator.async_observe([engine_observation(active=True)], now_ms=1_000)

    firing = coordinator.list_alerts()[0]
    assert firing["state"] == "firing"
    assert coordinator.health() == {
        "status": "degraded",
        "dirty": True,
        "current_error": "storage unavailable",
    }
    assert store.data is None

    store.fail = False
    await coordinator.async_observe([engine_observation(active=True)], now_ms=2_000)

    assert store.data["alerts"][0]["instance_id"] == firing["instance_id"]
    assert coordinator.health() == {
        "status": "ok",
        "dirty": False,
        "current_error": None,
    }


@pytest.mark.asyncio
async def test_corrupt_alert_store_fails_state_closed() -> None:
    store = MemoryAlertStore()
    store.data = {"version": 99, "lifecycle": "invalid", "alerts": "invalid"}
    coordinator = AlertCoordinator(store)

    await coordinator.async_load()

    assert coordinator.available is False
    assert coordinator.list_alerts() == []
    assert coordinator.health() == {
        "status": "unhealthy",
        "dirty": False,
        "current_error": "corrupt_alert_store",
    }
    with pytest.raises(AlertStateUnavailableError):
        await coordinator.async_observe([engine_observation(active=True)], now_ms=1_000)


@pytest.mark.asyncio
async def test_alert_store_load_failure_does_not_escape_coordinator() -> None:
    coordinator = AlertCoordinator(UnreadableAlertStore())

    await coordinator.async_load()

    assert coordinator.available is False
    assert coordinator.health() == {
        "status": "unhealthy",
        "dirty": False,
        "current_error": "storage unreadable",
    }


@pytest.mark.asyncio
async def test_semantically_invalid_alert_store_fails_closed() -> None:
    from intentional.alerting import alert_definition_key

    store = MemoryAlertStore()
    store.data = {
        "version": 1,
        "generation": 1,
        "lifecycle": {
            "active": {
                alert_definition_key("freezer", "FreezerHigh"): {
                    "instance_id": "instance",
                    "active_at_ms": 0,
                    "state": "nonsense",
                    "resolve_at_ms": None,
                    "for_ms": 0,
                    "last_pulse_id": None,
                    "duration_revision": "",
                }
            },
            "unknown_since": {},
        },
        "alerts": [{
            "rule_id": "freezer",
            "name": "FreezerHigh",
            "severity": "critical",
            "summary": "Freezer high",
            "state": "firing",
            "instance_id": "instance",
            "evaluation_status": "bogus",
        }],
    }
    coordinator = AlertCoordinator(store)

    await coordinator.async_load()

    assert coordinator.available is False
    assert coordinator.health()["current_error"] == "corrupt_alert_store"


@pytest.mark.asyncio
async def test_contradictory_persisted_alert_states_fail_closed() -> None:
    from intentional.alerting import alert_definition_key

    key = alert_definition_key("freezer", "FreezerHigh")
    store = MemoryAlertStore()
    store.data = {
        "version": 1,
        "generation": 1,
        "lifecycle": {
            "active": {key: {
                "instance_id": "instance",
                "active_at_ms": 0,
                "state": "pending",
                "resolve_at_ms": None,
                "for_ms": 10_000,
                "last_pulse_id": None,
                "duration_revision": "for:10000",
            }},
            "unknown_since": {},
            "consumed_pulses": {},
        },
        "alerts": [{
            "rule_id": "freezer",
            "name": "FreezerHigh",
            "severity": "critical",
            "summary": "Freezer high",
            "state": "firing",
            "instance_id": "instance",
            "evaluation_status": "current",
        }],
    }
    coordinator = AlertCoordinator(store)

    await coordinator.async_load()

    assert coordinator.available is False
    assert coordinator.health()["current_error"] == "corrupt_alert_store"


@pytest.mark.asyncio
async def test_repeated_pulse_observation_does_not_extend_alert_deadline() -> None:
    store = MemoryAlertStore()
    coordinator = AlertCoordinator(
        store,
        id_factory=lambda: "00000000-0000-4000-8000-000000000001",
    )
    await coordinator.async_load()
    pulse = engine_observation(
        active=True,
        resolve_after_ms=5_000,
        pulse_id="event.doorbell:7",
    )

    await coordinator.async_observe([pulse], now_ms=0)
    await coordinator.async_observe([pulse], now_ms=4_000)
    transitions = await coordinator.async_observe(
        [engine_observation(active=False, resolve_after_ms=5_000)],
        now_ms=5_000,
    )

    assert transitions[0]["to"] == "resolved"
    assert coordinator.list_alerts()[0]["state"] == "inactive"

    replay = await coordinator.async_observe([pulse], now_ms=6_000)

    assert replay == []
    assert coordinator.list_alerts()[0]["state"] == "inactive"


@pytest.mark.asyncio
async def test_operational_disable_immediately_closes_pulse_alert() -> None:
    coordinator = AlertCoordinator(
        MemoryAlertStore(),
        id_factory=lambda: "00000000-0000-4000-8000-000000000001",
    )
    await coordinator.async_load()
    await coordinator.async_observe(
        [
            engine_observation(
                active=True,
                resolve_after_ms=5_000,
                pulse_id="event.doorbell:7",
            )
        ],
        now_ms=0,
    )

    transitions = await coordinator.async_observe(
        [
            engine_observation(
                active=False,
                resolve_after_ms=5_000,
                inactive_reason="evaluation_disabled",
            )
        ],
        now_ms=1,
    )

    assert transitions[0]["to"] == "resolved"
    assert transitions[0]["reason"] == "evaluation_disabled"


@pytest.mark.asyncio
async def test_pending_alert_applies_edited_duration_from_original_activation() -> None:
    coordinator = AlertCoordinator(
        MemoryAlertStore(),
        id_factory=lambda: "00000000-0000-4000-8000-000000000001",
    )
    await coordinator.async_load()
    await coordinator.async_observe(
        [engine_observation(active=True, for_ms=10_000)], now_ms=0
    )

    await coordinator.async_observe(
        [engine_observation(active=True, for_ms=6_000)], now_ms=5_000
    )
    transitions = await coordinator.async_observe(
        [engine_observation(active=True, for_ms=6_000)], now_ms=6_000
    )

    assert transitions[0]["to"] == "firing"
    assert coordinator.list_alerts()[0]["state"] == "firing"


@pytest.mark.asyncio
async def test_alert_identity_cannot_collide_on_colon_boundaries() -> None:
    from intentional.alerting import AlertObservation

    coordinator = AlertCoordinator(
        MemoryAlertStore(),
        id_factory=iter(["instance-one", "instance-two"]).__next__,
    )
    await coordinator.async_load()

    await coordinator.async_observe(
        [
            AlertObservation("a:b", "c", "info", "First", True),
            AlertObservation("a", "b:c", "info", "Second", True),
        ],
        now_ms=0,
    )

    assert {alert["instance_id"] for alert in coordinator.list_alerts()} == {
        "instance-one",
        "instance-two",
    }


def engine_observation(
    *,
    active: bool,
    resolve_after_ms: int | None = None,
    pulse_id: str | None = None,
    for_ms: int = 0,
    inactive_reason: str = "condition_inactive",
):
    from intentional.alerting import AlertObservation

    return AlertObservation(
        rule_id="freezer-too-warm",
        name="FreezerTemperatureHigh",
        severity="critical",
        summary="Freezer is too warm",
        active=active,
        resolve_after_ms=resolve_after_ms,
        pulse_id=pulse_id,
        for_ms=for_ms,
        duration_revision=f"for:{for_ms}",
        inactive_reason=inactive_reason,
    )
