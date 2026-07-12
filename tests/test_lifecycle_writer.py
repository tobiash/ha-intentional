"""Tests for mutation-driven lifecycle persistence."""

from __future__ import annotations

import asyncio

from tests.dependencies import require_test_dependency

require_test_dependency("homeassistant", reason="homeassistant not installed")

from custom_components.intentional.lifecycle_writer import LifecycleWriter  # noqa: E402


class FakeStore:
    def __init__(self) -> None:
        self.saved: list[dict] = []
        self.fail = False

    async def async_save(self, snapshot: dict) -> None:
        if self.fail:
            raise RuntimeError("disk unavailable")
        self.saved.append(snapshot)


async def test_mutations_coalesce_and_restart_from_durable_boundary() -> None:
    state = {"intents": []}
    store = FakeStore()
    writer = LifecycleWriter(store, lambda: dict(state), durable_snapshot={"intents": []})

    state["intents"] = ["first"]
    assert writer.mutated()
    state["intents"] = ["latest"]
    assert writer.mutated()
    await writer.async_flush()

    assert store.saved == [{"intents": ["latest"]}]
    assert writer.durable_snapshot == {"intents": ["latest"]}
    restarted = LifecycleWriter(
        store, lambda: dict(state), durable_snapshot=writer.durable_snapshot
    )
    assert not restarted.mutated()


async def test_failed_save_stays_dirty_and_final_flush_retries() -> None:
    state = {"enabled": True}
    store = FakeStore()
    writer = LifecycleWriter(store, lambda: dict(state), durable_snapshot={"enabled": True})
    store.fail = True
    state["enabled"] = False
    writer.mutated()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert writer.health()["status"] == "degraded"
    assert writer.health()["dirty"] is True
    store.fail = False
    await writer.async_flush(force=True)

    assert store.saved == [{"enabled": False}]
    assert writer.health()["status"] == "ok"
    assert writer.health()["dirty"] is False


async def test_failed_save_retries_with_bounded_exponential_backoff() -> None:
    now = [10_000]
    state = {"enabled": True}
    store = FakeStore()
    writer = LifecycleWriter(
        store,
        lambda: dict(state),
        durable_snapshot={"enabled": True},
        clock_fn=lambda: now[0],
        retry_base_ms=1_000,
        retry_max_ms=2_000,
    )
    attempts = 0

    async def fail_counted(_snapshot: dict) -> None:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("disk unavailable")

    store.async_save = fail_counted
    state["enabled"] = False
    writer.mutated()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert attempts == 1
    assert writer.health()["next_retry_ms"] == 11_000

    for _ in range(20):
        await writer.async_flush()
    assert attempts == 1

    now[0] = 11_000
    await writer.async_flush()
    assert attempts == 2
    assert writer.health()["next_retry_ms"] == 13_000

    now[0] = 12_999
    await writer.async_flush()
    assert attempts == 2
    now[0] = 13_000
    await writer.async_flush()
    assert attempts == 3
    assert writer.health()["next_retry_ms"] == 15_000


async def test_generator_hour_has_bounded_writes_and_latest_recoverable_state() -> None:
    now = [0]
    state = {"generated_fields": []}
    store = FakeStore()
    writer = LifecycleWriter(
        store,
        lambda: {"generated_fields": list(state["generated_fields"])},
        durable_snapshot={"generated_fields": []},
        clock_fn=lambda: now[0],
    )

    for now[0] in range(0, 3_600_000, 100):
        state["generated_fields"] = [now[0]]
        writer.mutated()
        await writer.async_flush()

    assert len(store.saved) <= 3_600
    assert writer.durable_snapshot == store.saved[-1]
    assert writer.desired_snapshot == {"generated_fields": [3_599_900]}
    await writer.async_flush(force=True)
    assert store.saved[-1] == writer.desired_snapshot
