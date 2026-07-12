"""Mutation-driven persistence for integration lifecycle state."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from homeassistant.helpers.storage import Store

from ._engine.runtime import monotonic_ms


def lifecycle_writer_key(entry_id: str) -> str:
    """Return the hass.data key for an entry's lifecycle writer."""
    return f"{entry_id}:lifecycle_writer"


class LifecycleWriter:
    """Coalesce lifecycle mutations while retaining the latest durable state."""

    def __init__(
        self,
        store: Store,
        snapshot: Callable[[], dict[str, Any]],
        *,
        durable_snapshot: dict[str, Any] | None,
        clock_fn: Callable[[], int] = monotonic_ms,
        min_save_interval_ms: int = 1_000,
        retry_base_ms: int = 1_000,
        retry_max_ms: int = 300_000,
    ) -> None:
        if min_save_interval_ms <= 0:
            raise ValueError("min_save_interval_ms must be positive")
        if retry_base_ms <= 0 or retry_max_ms < retry_base_ms:
            raise ValueError("invalid lifecycle persistence retry bounds")
        self._store = store
        self._snapshot = snapshot
        self._clock_fn = clock_fn
        self._min_save_interval_ms = min_save_interval_ms
        self._retry_base_ms = retry_base_ms
        self._retry_max_ms = retry_max_ms
        self.desired_snapshot = durable_snapshot
        self.durable_snapshot = durable_snapshot
        self.dirty_generation = 0
        self.durable_generation = 0
        self.last_failure_ms: int | None = None
        self.last_failure_error: str | None = None
        self.current_error: str | None = None
        self.failure_count = 0
        self.success_count = 0
        self.consecutive_failures = 0
        self.next_retry_ms: int | None = None
        self.next_save_ms: int | None = None
        self._save_task: asyncio.Task[None] | None = None

    def mutated(self) -> bool:
        """Capture current state and schedule a save when it changed."""
        snapshot = self._snapshot()
        if snapshot == self.desired_snapshot:
            return False
        self.desired_snapshot = snapshot
        self.dirty_generation += 1
        if self._save_task is None or self._save_task.done():
            self._save_task = asyncio.create_task(self._delayed_save())
        return True

    async def _delayed_save(self) -> None:
        await asyncio.sleep(0)
        if self._save_is_due():
            await self._save_desired()

    def _save_is_due(self) -> bool:
        now_ms = self._clock_fn()
        retry_due = self.next_retry_ms is None or now_ms >= self.next_retry_ms
        cadence_due = self.next_save_ms is None or now_ms >= self.next_save_ms
        return retry_due and cadence_due

    async def _save_desired(self) -> None:
        snapshot = self.desired_snapshot
        generation = self.dirty_generation
        if snapshot is None or generation == self.durable_generation:
            return
        try:
            await self._store.async_save(snapshot)
        except Exception as err:  # noqa: BLE001 - health retains storage failures
            now_ms = self._clock_fn()
            self.consecutive_failures += 1
            delay_ms = min(
                self._retry_max_ms,
                self._retry_base_ms * (2 ** min(self.consecutive_failures - 1, 20)),
            )
            self.last_failure_ms = now_ms
            self.last_failure_error = str(err)
            self.current_error = str(err)
            self.failure_count += 1
            self.next_retry_ms = now_ms + delay_ms
            return
        self.durable_snapshot = snapshot
        self.durable_generation = generation
        self.next_save_ms = self._clock_fn() + self._min_save_interval_ms
        self.current_error = None
        self.consecutive_failures = 0
        self.next_retry_ms = None
        self.success_count += 1

    async def async_flush(self, *, force: bool = False) -> None:
        """Save dirty work when due, or immediately at a durability boundary."""
        task = self._save_task
        if task is not None and task is not asyncio.current_task():
            await task
        if self.dirty_generation != self.durable_generation and (force or self._save_is_due()):
            await self._save_desired()

    def contains_durable_effect(self, activation_id: str, effect_index: int) -> bool:
        """Return whether an Effect obligation crossed the Store boundary."""
        records = self.durable_snapshot or {}
        return any(
            record.get("activation_id") == activation_id
            and record.get("effect_index") == effect_index
            and record.get("acknowledged_at_ms") is None
            for record in records.get("effect_outbox", [])
        )

    def health(self) -> dict[str, Any]:
        """Return persistence health independently from reconciliation health."""
        dirty = self.dirty_generation != self.durable_generation
        return {
            "status": "degraded" if self.current_error else "ok",
            "dirty": dirty,
            "dirty_generation": self.dirty_generation,
            "durable_generation": self.durable_generation,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "consecutive_failures": self.consecutive_failures,
            "next_retry_ms": self.next_retry_ms,
            "next_save_ms": self.next_save_ms,
            "retry_remaining_ms": None
            if self.next_retry_ms is None
            else max(0, self.next_retry_ms - self._clock_fn()),
            "current_error": self.current_error,
            "last_failure_error": self.last_failure_error,
        }


def mark_lifecycle_mutated(hass: Any, entry_id: str) -> bool:
    """Capture a lifecycle mutation when the entry has a writer."""
    writer = hass.data.get("intentional", {}).get(lifecycle_writer_key(entry_id))
    return isinstance(writer, LifecycleWriter) and writer.mutated()
