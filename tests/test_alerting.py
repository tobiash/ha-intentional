from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace

import pytest

from intentional.alerting import (
    AlertCoordinator,
    AlertObservation,
    AlertStateUnavailableError,
)
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

    assert {key: firing[key] for key in (
        "rule_id",
        "name",
        "severity",
        "summary",
        "state",
        "instance_id",
        "evaluation_status",
    )} == {
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
async def test_v1_alert_store_is_rewritten_as_current_schema_on_load() -> None:
    store = MemoryAlertStore()
    coordinator = AlertCoordinator(store)
    await coordinator.async_load()
    await coordinator.async_observe([engine_observation(active=True)], now_ms=0)
    store.data["version"] = 1

    restored = AlertCoordinator(store)
    await restored.async_load()

    assert restored.available is True
    assert store.data["version"] == 2


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
        "current_error": "OSError",
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
        "current_error": "OSError",
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
async def test_malformed_stored_silence_fails_closed_before_runtime_ingest() -> None:
    store = MemoryAlertStore()
    coordinator = AlertCoordinator(store)
    await coordinator.async_load()
    await coordinator.async_observe([engine_observation(active=True)], now_ms=0)
    store.data["silences"] = [{}]

    restored = AlertCoordinator(store)
    await restored.async_load()

    assert restored.available is False
    assert restored.health()["current_error"] == "corrupt_alert_store"


@pytest.mark.asyncio
async def test_invalid_stored_silence_matcher_fails_closed_before_matching() -> None:
    store = MemoryAlertStore()
    coordinator = AlertCoordinator(store)
    await coordinator.async_load()
    await coordinator.async_observe([engine_observation(active=True)], now_ms=0)
    store.data["silences"] = [
        {
            "silence_id": "silence-1",
            "matchers": ["not valid matcher syntax"],
            "match_all": False,
            "actor": "admin",
            "reason": "maintenance",
            "created_at_ms": 0,
            "expires_at_ms": 1_000,
        }
    ]

    restored = AlertCoordinator(store)
    await restored.async_load()

    assert restored.available is False
    assert restored.health()["current_error"] == "corrupt_alert_store"


@pytest.mark.asyncio
async def test_malformed_optional_capability_state_does_not_disable_alert_lifecycle() -> None:
    store = MemoryAlertStore()
    coordinator = AlertCoordinator(store)
    coordinator.configure_capabilities(b"x" * 32, entry_id="entry")
    await coordinator.async_load()
    await coordinator.async_observe([engine_observation(active=True)], now_ms=0)
    store.data["capabilities"] = {"records": "malformed"}

    restored = AlertCoordinator(store)
    restored.configure_capabilities(b"x" * 32, entry_id="entry")
    await restored.async_load()

    assert restored.available is True
    assert restored.list_alerts()[0]["state"] == "firing"
    assert restored.health()["current_error"] == "capability_state_invalid"


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
async def test_pulse_watermark_rejects_replay_after_more_than_64_later_pulses() -> None:
    store = MemoryAlertStore()
    coordinator = AlertCoordinator(store, id_factory=lambda: "instance-1")
    await coordinator.async_load()
    for sequence in range(1, 70):
        await coordinator.async_observe(
            [
                engine_observation(
                    active=True,
                    resolve_after_ms=5_000,
                    pulse_id=f"event.doorbell:epoch:{sequence}",
                )
            ],
            now_ms=sequence,
        )
    await coordinator.async_advance(now_ms=6_000)

    restored = AlertCoordinator(store, id_factory=lambda: "replayed")
    await restored.async_load()
    transitions = await restored.async_observe(
        [
            engine_observation(
                active=True,
                resolve_after_ms=5_000,
                pulse_id="event.doorbell:epoch:1",
            )
        ],
        now_ms=7_000,
    )

    assert transitions == []
    assert restored.list_alerts()[0]["state"] == "inactive"


@pytest.mark.asyncio
async def test_pulse_watermarks_remain_loadable_after_more_than_64_runtime_epochs() -> None:
    store = MemoryAlertStore()
    coordinator = AlertCoordinator(store, id_factory=lambda: "instance")
    await coordinator.async_load()
    for epoch in range(65):
        await coordinator.async_observe(
            [
                engine_observation(
                    active=True,
                    resolve_after_ms=5_000,
                    pulse_id=f"event.doorbell:epoch-{epoch}:1",
                )
            ],
            now_ms=epoch,
        )

    restored = AlertCoordinator(store)
    await restored.async_load()

    assert restored.available is True


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


def test_engine_alert_observation_contains_routing_and_definition_context() -> None:
    from intentional.engine import Engine
    from intentional.yaml_loader import load_rules_from_string

    engine = Engine(clock_fn=lambda: 12_345)
    engine.load_rules(load_rules_from_string("""
- id: freezer
  while: {sensor.freezer_temperature: {gt: -10}}
  alert:
    name: FreezerTemperatureHigh
    severity: warning
    stale_after: 5m
    labels: {category: appliance}
    annotations:
      summary: Freezer is too warm
      description: Check the freezer door
"""))
    engine.update_state("sensor.freezer_temperature", -5)
    engine.evaluate_all()

    observation = engine.alert_observations()[0]

    assert observation.observed_at_ms == 12_345
    assert observation.labels == {
        "alertname": "FreezerTemperatureHigh",
        "rule_id": "freezer",
        "severity": "warning",
        "integration": "intentional",
        "category": "appliance",
    }
    assert observation.annotations["description"] == "Check the freezer door"
    assert observation.stale_after_ms == 300_000
    assert len(observation.definition_revision) == 64


def test_alert_annotation_rendering_degrades_then_recovers_without_lifecycle_loss() -> None:
    engine = Engine(clock_fn=lambda: 1_000)
    engine.load_rules(load_rules_from_string("""
- id: calculation
  while: {binary_sensor.problem: {is: "on"}}
  alert:
    name: CalculationProblem
    severity: warning
    annotations:
      summary: "Value {{ 10 / (states('sensor.divisor') | int) }}"
"""))
    engine.update_state("binary_sensor.problem", "on")
    engine.evaluate_all()

    degraded = engine.alert_observations()[0]
    engine.update_state("sensor.divisor", "2")
    recovered = engine.alert_observations()[0]

    assert degraded.presentation_degraded is True
    assert degraded.summary.startswith("Value {{")
    assert recovered.presentation_degraded is False
    assert recovered.summary == "Value 5.0"


@pytest.mark.asyncio
async def test_coordinator_retains_resolved_instance_and_transition_audit() -> None:
    store = MemoryAlertStore()
    coordinator = AlertCoordinator(store, id_factory=lambda: "instance-1")
    await coordinator.async_load()
    firing = AlertObservation(
        "freezer",
        "FreezerHigh",
        "warning",
        "Freezer is too warm",
        True,
        observed_at_ms=1_000,
        labels={"alertname": "FreezerHigh", "area": "kitchen"},
        annotations={"summary": "Freezer is too warm"},
        definition_revision="revision-1",
    )

    await coordinator.async_observe([firing], now_ms=1_000)
    projected = coordinator.list_alerts()[0]
    assert projected["active_at_ms"] == 1_000
    assert projected["firing_at_ms"] == 1_000
    assert projected["observed_at_ms"] == 1_000
    assert projected["labels"]["area"] == "kitchen"
    assert projected["definition_revision"] == "revision-1"

    await coordinator.async_observe(
        [replace(firing, active=False, observed_at_ms=2_000)], now_ms=2_000
    )

    assert coordinator.list_instances()[0] == {
        "instance_id": "instance-1",
        "rule_id": "freezer",
        "name": "FreezerHigh",
        "state": "resolved",
        "active_at_ms": 1_000,
        "firing_at_ms": 1_000,
        "resolved_at_ms": 2_000,
        "reason": "condition_inactive",
    }
    assert [event["to"] for event in coordinator.list_audit()] == [
        "firing",
        "resolved",
    ]

    restored = AlertCoordinator(store)
    await restored.async_load()
    assert restored.list_instances() == coordinator.list_instances()
    assert restored.list_audit() == coordinator.list_audit()


@pytest.mark.asyncio
async def test_coordinator_advances_durable_deadlines_without_new_observation() -> None:
    store = MemoryAlertStore()
    coordinator = AlertCoordinator(store, id_factory=lambda: "instance-1")
    await coordinator.async_load()
    observation = AlertObservation(
        "freezer",
        "FreezerHigh",
        "warning",
        "Freezer is too warm",
        True,
        observed_at_ms=0,
        for_ms=1_000,
    )
    await coordinator.async_observe([observation], now_ms=0)

    assert coordinator.next_deadline_ms() == 1_000
    transitions = await coordinator.async_advance(now_ms=1_000)

    assert transitions[0]["to"] == "firing"
    assert coordinator.list_alerts()[0]["state"] == "firing"
    restored = AlertCoordinator(store)
    await restored.async_load()
    assert restored.list_alerts()[0]["state"] == "firing"


@pytest.mark.asyncio
async def test_startup_barrier_blocks_delivery_until_known_post_sync_evidence() -> None:
    store = MemoryAlertStore()
    original = AlertCoordinator(store, id_factory=lambda: "instance-1")
    await original.async_load()
    await original.async_observe([engine_observation(active=True)], now_ms=0)

    restored = AlertCoordinator(store)
    await restored.async_load()
    restored.begin_startup_barrier()
    await restored.async_set_policy(
        """
route: {id: root, receiver: household, group_wait: 0s}
receivers:
  - {name: household, destinations: [{type: persistent_notification}]}
""",
        now_ms=0,
    )

    assert await restored.async_notification_advance(now_ms=0) == []
    await restored.async_observe(
        [engine_observation(active=True, quality="unknown")], now_ms=1
    )
    assert await restored.async_notification_advance(now_ms=5_000) == []
    await restored.async_observe([engine_observation(active=True)], now_ms=5_001)
    assert await restored.async_notification_advance(now_ms=10_000) == []
    assert len(await restored.async_notification_advance(now_ms=10_001)) == 1


@pytest.mark.asyncio
async def test_operational_mutations_serialize_store_commits() -> None:
    class SerialStore(MemoryAlertStore):
        concurrent = 0
        maximum = 0

        async def async_save(self, data: dict) -> None:
            self.concurrent += 1
            self.maximum = max(self.maximum, self.concurrent)
            await asyncio.sleep(0)
            await super().async_save(data)
            self.concurrent -= 1

    store = SerialStore()
    coordinator = AlertCoordinator(store, id_factory=lambda: "instance-1")
    await coordinator.async_load()
    await coordinator.async_observe([engine_observation(active=True)], now_ms=0)

    await asyncio.gather(
        coordinator.async_acknowledge(
            "instance-1", actor="user-1", comment=None, now_ms=1
        ),
        coordinator.async_create_instance_silence(
            "instance-1",
            actor="user-2",
            reason="maintenance",
            now_ms=1,
            duration_ms=60_000,
        ),
    )

    assert store.maximum == 1
    assert coordinator.list_alerts()[0]["acknowledgment"] is not None
    assert len(coordinator.list_silences()) == 1


@pytest.mark.asyncio
async def test_stable_observation_timestamp_does_not_churn_store_generation() -> None:
    class CountingStore(MemoryAlertStore):
        saves = 0

        async def async_save(self, data: dict) -> None:
            self.saves += 1
            await super().async_save(data)

    store = CountingStore()
    coordinator = AlertCoordinator(store, id_factory=lambda: "instance-1")
    await coordinator.async_load()
    await coordinator.async_observe(
        [replace(engine_observation(active=True), observed_at_ms=1)], now_ms=1
    )
    generation = store.data["generation"]

    for now_ms in (100, 200, 300):
        await coordinator.async_observe(
            [replace(engine_observation(active=True), observed_at_ms=now_ms)],
            now_ms=now_ms,
        )

    assert store.saves == 1
    assert store.data["generation"] == generation


@pytest.mark.asyncio
async def test_notification_capacity_degradation_is_visible_in_health() -> None:
    store = MemoryAlertStore()
    coordinator = AlertCoordinator(store)
    await coordinator.async_load()
    await coordinator.async_observe([engine_observation(active=True)], now_ms=0)
    store.data["notifications"]["degraded"] = True

    restored = AlertCoordinator(store)
    await restored.async_load()

    assert restored.health() == {
        "status": "degraded",
        "dirty": False,
        "current_error": "notification_capacity_exhausted",
    }


def engine_observation(
    *,
    active: bool,
    resolve_after_ms: int | None = None,
    pulse_id: str | None = None,
    for_ms: int = 0,
    inactive_reason: str = "condition_inactive",
    quality: str = "known",
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
        quality=quality,
    )
