"""Tests for runtime pulse-drain and tick-liveness state."""

from __future__ import annotations


def test_pulse_queue_preserves_pulses_added_after_drain_begins() -> None:
    from intentional.runtime import StateChangePulseQueue

    pulses = StateChangePulseQueue()
    pulses.add("binary_sensor.front_door")

    drain = pulses.begin_drain()
    pulses.add("binary_sensor.back_door")

    assert pulses.current_entity_ids(drain) == frozenset({"binary_sensor.front_door"})

    pulses.finish_drain(drain)

    assert pulses.entity_ids() == frozenset({"binary_sensor.back_door"})


def test_pulse_queue_preserves_same_entity_readded_after_drain_begins() -> None:
    from intentional.runtime import StateChangePulseQueue

    pulses = StateChangePulseQueue()
    pulses.add("binary_sensor.front_door")

    drain = pulses.begin_drain()
    pulses.add("binary_sensor.front_door")
    pulses.finish_drain(drain)

    assert pulses.entity_ids() == frozenset({"binary_sensor.front_door"})


def test_pulse_tokens_do_not_repeat_after_runtime_restart() -> None:
    from intentional.runtime import StateChangePulseQueue

    before = StateChangePulseQueue(epoch="before")
    after = StateChangePulseQueue(epoch="after")
    before.add("event.doorbell")
    after.add("event.doorbell")

    assert before.begin_drain().tokens != after.begin_drain().tokens


def test_tick_runtime_reports_stale_liveness_as_degraded() -> None:
    from intentional.runtime import TickRuntime

    runtime = TickRuntime(tick_interval_ms=100, stale_after_ms=1_000)

    assert runtime.unloading is False
    assert runtime.tick_idle.is_set()
    runtime.mark_success(now_ms=1_000)

    assert runtime.health(now_ms=1_500)["status"] == "ok"
    stale = runtime.health(now_ms=2_500)

    assert stale["status"] == "degraded"
    assert stale["last_success_age_ms"] == 1_500


def test_tick_runtime_failure_reporting_is_rate_limited() -> None:
    from intentional.runtime import TickRuntime

    runtime = TickRuntime(tick_interval_ms=100)
    runtime.mark_failure(RuntimeError("boom"), now_ms=1_000)

    assert runtime.should_report_failure(now_ms=1_000, cooldown_ms=60_000)
    assert not runtime.should_report_failure(now_ms=2_000, cooldown_ms=60_000)
    assert runtime.should_report_failure(now_ms=61_000, cooldown_ms=60_000)

    health = runtime.health(now_ms=61_000)
    assert health["status"] == "degraded"
    assert health["failure_count"] == 1
    assert health["current_error"] == "boom"
