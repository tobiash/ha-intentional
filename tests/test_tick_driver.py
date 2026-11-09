"""Cycle invariants through the driver, with real Engine/Reconciliation/storage logic."""

# HA-dependent imports must follow the optional dependency gate.
# ruff: noqa: E402

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from tests.dependencies import require_test_dependency

require_test_dependency("homeassistant", reason="homeassistant not installed")

from custom_components.intentional._engine.engine import Engine
from custom_components.intentional._engine.ha_adapter import pulse_state_change
from custom_components.intentional._engine.reconciliation import Reconciliation
from custom_components.intentional._engine.runtime import TickRuntime
from custom_components.intentional._engine.yaml_loader import Rule
from custom_components.intentional.diagnostics import list_diagnostics
from custom_components.intentional.lifecycle_writer import LifecycleWriter
from custom_components.intentional.tick_driver import TickDriver
from tests.test_lifecycle_writer import FakeStore
from tests.test_reconciliation import _FakeAdapter, _NoContextTracker


@pytest.fixture
async def cycle():
    now = [10_000]
    engine = Engine(clock_fn=lambda: now[0])
    engine.load_rules([Rule(id="desk", when="true", target="light.desk", set={"state": "on"})])
    runtime = TickRuntime(tick_interval_ms=100)
    store = FakeStore()
    writer = LifecycleWriter(
        store, engine.export_lifecycle_records,
        durable_snapshot=engine.export_lifecycle_records(), clock_fn=lambda: now[0],
    )
    tasks = []

    def spawn(coro):
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    driver = TickDriver(
        hass=SimpleNamespace(data={}, async_create_task=spawn), engine=engine,
        runtime=runtime, reconciler=Reconciliation(
            drift_override_ttl_ms=300_000, drift_confirmation_ms=1_500,
            service_failure_backoff_ms=30_000,
        ),
        ha_adapter=_FakeAdapter(), contexts=_NoContextTracker(), writer=writer,
        alerting=SimpleNamespace(available=True, async_observe=AsyncMock()),
        alert_deadlines=SimpleNamespace(wake=Mock()),
        rollback=SimpleNamespace(
            async_failure=AsyncMock(), async_success=AsyncMock(), async_disqualify=AsyncMock(),
        ),
        rules=SimpleNamespace(generation="generation"),
        publication=SimpleNamespace(publish_if_changed=Mock(return_value=True)),
        prepare_inputs=Mock(), record_activity=Mock(return_value={"desk"}),
        commit_events=Mock(), dispatch_effects=AsyncMock(return_value=set()),
        clock=lambda: now[0],
    )
    yield SimpleNamespace(driver=driver, now=now, store=store, tasks=tasks)
    if tasks:
        await asyncio.gather(*tasks)
    await writer.async_close()


def add_pulse(driver):
    pulse_state_change(
        driver.engine, SimpleNamespace(state="off"),
        SimpleNamespace(entity_id="binary_sensor.motion", state="on"),
    )
    driver.runtime.pulses.add("binary_sensor.motion", source_timestamp_ms=1234)


async def test_success_persists_before_dispatch_and_acknowledges_observed_pulses(cycle):
    driver = cycle.driver
    add_pulse(driver)
    original_call = driver.ha_adapter.async_call

    async def dispatch(*args, **kwargs):
        assert cycle.store.saved
        assert not driver.runtime.mutation_lock.locked()
        assert not driver.runtime.tick_idle.is_set()
        await original_call(*args, **kwargs)

    driver.ha_adapter.async_call = dispatch
    await driver.async_step()
    assert driver.ha_adapter.calls == [("light", "turn_on", {"entity_id": "light.desk"})]
    assert not driver.runtime.pulses
    assert driver.runtime.tick_idle.is_set()
    assert driver.runtime.health(now_ms=cycle.now[0])["status"] == "ok"
    assert driver.runtime.success_count == 1
    driver.rollback.async_success.assert_awaited_once_with(
        generation="generation", revision=0, next_revision=1,
    )
    assert driver.engine.state["binary_sensor.motion.changed"] is False


async def test_new_same_entity_pulse_during_dispatch_survives_completion(cycle):
    driver = cycle.driver
    add_pulse(driver)

    async def arrive(previous, revision, before_dispatch):
        add_pulse(driver)
        return set()

    driver.dispatch_effects.side_effect = arrive
    await driver.async_step()
    assert driver.runtime.success_count == 1
    assert driver.runtime.pulses.entity_ids() == {"binary_sensor.motion"}
    assert driver.engine.state["binary_sensor.motion.changed"] is True
    driver.dispatch_effects.side_effect = None
    await driver.async_step()
    assert not driver.runtime.pulses


@pytest.mark.parametrize("stage", ["reconciliation", "effects"])
async def test_revision_change_during_dispatch_cannot_commit_or_clear_pulses(cycle, stage):
    driver = cycle.driver
    add_pulse(driver)

    async def change_during_reconciliation(*args, **kwargs):
        driver.runtime.advance_revision()

    async def change_during_effects(*args):
        driver.runtime.advance_revision()
        return {"scene.stale"}

    if stage == "reconciliation":
        driver.ha_adapter.async_call = change_during_reconciliation
    else:
        driver.dispatch_effects.side_effect = change_during_effects
    await driver.async_step()
    assert driver.runtime.tick_idle.is_set()
    assert driver.runtime.success_count == 0
    assert driver.runtime.active_scenes == set()
    assert driver.runtime.pulses.entity_ids() == {"binary_sensor.motion"}
    driver.rollback.async_success.assert_not_awaited()
    driver.publication.publish_if_changed.assert_not_called()
    if stage == "reconciliation":
        driver.commit_events.assert_not_called()
        driver.dispatch_effects.assert_not_awaited()


async def test_failed_persistence_blocks_dispatch_and_preserves_pulse_for_retry(cycle):
    driver = cycle.driver
    add_pulse(driver)
    cycle.store.fail = True
    await driver.async_step()
    assert driver.writer.current_error is not None
    assert driver.ha_adapter.calls == []
    driver.dispatch_effects.assert_not_awaited()
    assert driver.runtime.pulses
    assert driver.runtime.tick_idle.is_set()

    cycle.store.fail = False
    cycle.now[0] += 2000
    await driver.async_step()
    assert driver.writer.current_error is None
    assert driver.ha_adapter.calls
    assert not driver.runtime.pulses
    assert driver.runtime.success_count == 1


async def test_evaluation_failure_reports_rollback_outside_lock_then_recovers(cycle):
    driver = cycle.driver
    add_pulse(driver)
    driver.prepare_inputs.side_effect = RuntimeError("invalid observation")

    async def rollback(*args, **kwargs):
        assert not driver.runtime.mutation_lock.locked()
        async with driver.runtime.mutation_lock:
            pass

    driver.rollback.async_failure.side_effect = rollback
    await driver.async_step()
    await asyncio.gather(*cycle.tasks)
    assert driver.runtime.failure_count == 1
    assert driver.runtime.tick_idle.is_set()
    assert driver.runtime.pulses
    assert driver.ha_adapter.calls == []
    driver.rollback.async_failure.assert_awaited_once()
    assert driver.rollback.async_failure.call_args.kwargs == {
        "stage": "evaluation", "generation": "generation", "revision": 0,
    }
    assert list_diagnostics(driver.hass)[-1]["type"] == "tick_failed"
    driver.prepare_inputs.side_effect = None
    await driver.async_step()
    assert driver.runtime.success_count == 1
    assert driver.runtime.current_error is None


async def test_dispatch_failure_is_not_eligible_for_automatic_rollback(cycle):
    driver = cycle.driver
    add_pulse(driver)
    driver.dispatch_effects.side_effect = RuntimeError("dispatch interrupted")
    await driver.async_step()
    assert driver.runtime.failure_count == 1
    assert driver.runtime.tick_idle.is_set()
    assert driver.runtime.pulses
    driver.rollback.async_failure.assert_not_awaited()
    driver.dispatch_effects.side_effect = None
    await driver.async_step()
    assert driver.runtime.success_count == 1


async def test_cancellation_restores_idle_without_acknowledging_pulses(cycle):
    driver = cycle.driver
    add_pulse(driver)
    entered = asyncio.Event()

    async def blocked(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    driver.alerting.async_observe.side_effect = blocked
    task = asyncio.create_task(driver.async_step())
    await asyncio.wait_for(entered.wait(), timeout=1)
    assert not driver.runtime.tick_idle.is_set()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert driver.runtime.tick_idle.is_set()
    assert driver.runtime.pulses
    assert driver.runtime.failure_count == 0


async def test_stop_before_lock_acquisition_prevents_evaluation(cycle):
    driver = cycle.driver
    await driver.runtime.mutation_lock.acquire()
    task = asyncio.create_task(driver.async_step())
    await asyncio.sleep(0)
    driver.runtime.stop_event.set()
    driver.runtime.mutation_lock.release()
    await task
    driver.prepare_inputs.assert_not_called()
    assert driver.runtime.tick_idle.is_set()


async def test_stopped_driver_forces_final_checkpoint_without_running_a_cycle(cycle):
    driver = cycle.driver
    driver.engine.emit_manual_intent("light.desk", {"state": "off"})
    driver.runtime.stop_event.set()
    await driver.async_run()
    assert cycle.store.saved[-1] == driver.engine.export_lifecycle_records()
    driver.prepare_inputs.assert_not_called()
    assert driver.runtime.tick_idle.is_set()


async def test_stable_stages_are_throttled_but_pulses_force_alert_observation(cycle):
    driver = cycle.driver
    await driver.async_step()
    await driver.async_step()
    assert driver.alerting.async_observe.await_count == 1
    assert driver.publication.publish_if_changed.call_count == 1
    add_pulse(driver)
    await driver.async_step()
    assert driver.alerting.async_observe.await_count == 2
    assert driver.publication.publish_if_changed.call_count == 1
    cycle.now[0] += 1000
    await driver.async_step()
    assert driver.alerting.async_observe.await_count == 3
    assert driver.publication.publish_if_changed.call_count == 2
