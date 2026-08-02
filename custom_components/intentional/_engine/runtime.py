"""Runtime state helpers for the Home Assistant integration loop."""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


def monotonic_ms() -> int:
    """Return process-monotonic milliseconds for runtime liveness checks."""
    return int(time.monotonic() * 1000)


def runtime_key(entry_id: str) -> str:
    """Return the hass.data key for one config entry's runtime state."""
    return f"{entry_id}:runtime"


def _set_event() -> asyncio.Event:
    event = asyncio.Event()
    event.set()
    return event


@dataclass(frozen=True)
class PulseDrain:
    """Snapshot of pulses that are eligible to clear after one tick."""

    tokens: dict[str, str]
    source_timestamps_ms: dict[str, int]

    def __bool__(self) -> bool:
        return bool(self.tokens)


class StateChangePulseQueue:
    """Drainable queue for one-cycle state-change pulses.

    Pulses may arrive while a tick is awaiting Home Assistant service calls. A
    plain set cannot distinguish "already existed at tick start" from "newly
    added during this tick". Tokens let the tick clear only the pulse generation
    it observed, leaving newer same-entity pulses for the next cycle.
    """

    def __init__(self, *, epoch: str | None = None) -> None:
        self._tokens: dict[str, str] = {}
        self._source_timestamps_ms: dict[str, int] = {}
        self._next_token = 0
        self._epoch = epoch or str(uuid.uuid4())

    def add(self, entity_id: str, *, source_timestamp_ms: int | None = None) -> None:
        self._next_token += 1
        self._tokens[entity_id] = f"{self._epoch}:{self._next_token}"
        self._source_timestamps_ms[entity_id] = (
            source_timestamp_ms
            if source_timestamp_ms is not None
            else int(time.time() * 1_000)
        )

    def begin_drain(self) -> PulseDrain:
        return PulseDrain(dict(self._tokens), dict(self._source_timestamps_ms))

    def current_entity_ids(self, drain: PulseDrain) -> frozenset[str]:
        return frozenset(
            entity_id
            for entity_id, token in drain.tokens.items()
            if self._tokens.get(entity_id) == token
        )

    def finish_drain(self, drain: PulseDrain) -> None:
        for entity_id, token in drain.tokens.items():
            if self._tokens.get(entity_id) == token:
                self._tokens.pop(entity_id, None)
                self._source_timestamps_ms.pop(entity_id, None)

    def entity_ids(self) -> frozenset[str]:
        return frozenset(self._tokens)

    def __bool__(self) -> bool:
        return bool(self._tokens)

    def __len__(self) -> int:
        return len(self._tokens)


@dataclass
class TickRuntime:
    """Mutable runtime state for Intentional's periodic reconciliation loop."""

    tick_interval_ms: int
    stale_after_ms: int = 10_000
    active_scenes: set[str] = field(default_factory=set)
    active_rule_ids: set[str] = field(default_factory=set)
    pulses: StateChangePulseQueue = field(default_factory=StateChangePulseQueue)
    last_success_ms: int | None = None
    last_failure_ms: int | None = None
    last_failure_error: str | None = None
    current_error: str | None = None
    consecutive_failures: int = 0
    success_count: int = 0
    failure_count: int = 0
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    mutation_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    revision: int = 0
    tick_task: asyncio.Task[None] | None = None
    registry_task: asyncio.Task[None] | None = None
    registry_pending: bool = False
    unloading: bool = False
    tick_idle: asyncio.Event = field(default_factory=_set_event)
    lifecycle_snapshot: dict[str, Any] | None = None
    _last_failure_report_ms: int | None = None
    _timing_last_ms: dict[str, int] = field(default_factory=dict)
    _timing_total_ms: dict[str, int] = field(default_factory=dict)
    _timing_max_ms: dict[str, int] = field(default_factory=dict)
    _timing_samples: dict[str, int] = field(default_factory=dict)
    _stage_last_run_ms: dict[str, int] = field(default_factory=dict)

    def advance_revision(self) -> int:
        """Record one atomic mutation of engine or reconciliation state."""
        self.revision += 1
        return self.revision

    def is_revision(self, revision: int) -> bool:
        """Return whether no newer mutation has occurred."""
        return self.revision == revision

    def mark_success(self, *, now_ms: int | None = None) -> None:
        self.last_success_ms = monotonic_ms() if now_ms is None else now_ms
        self.consecutive_failures = 0
        self.current_error = None
        self.success_count += 1

    def mark_failure(self, error: BaseException, *, now_ms: int | None = None) -> None:
        self.last_failure_ms = monotonic_ms() if now_ms is None else now_ms
        self.last_failure_error = str(error)
        self.current_error = str(error)
        self.consecutive_failures += 1
        self.failure_count += 1

    def should_report_failure(
        self,
        *,
        now_ms: int | None = None,
        cooldown_ms: int = 60_000,
    ) -> bool:
        now = monotonic_ms() if now_ms is None else now_ms
        if self._last_failure_report_ms is None:
            self._last_failure_report_ms = now
            return True
        if now - self._last_failure_report_ms < cooldown_ms:
            return False
        self._last_failure_report_ms = now
        return True

    def record_timing(self, stage: str, duration_ms: int) -> None:
        """Record one nonnegative Tick runtime stage duration."""
        duration_ms = max(0, duration_ms)
        self._timing_last_ms[stage] = duration_ms
        self._timing_total_ms[stage] = self._timing_total_ms.get(stage, 0) + duration_ms
        self._timing_max_ms[stage] = max(self._timing_max_ms.get(stage, 0), duration_ms)
        self._timing_samples[stage] = self._timing_samples.get(stage, 0) + 1

    def stage_due(
        self, stage: str, *, now_ms: int, interval_ms: int, force: bool = False
    ) -> bool:
        """Return whether a periodic stage is due and advance its cadence."""
        previous = self._stage_last_run_ms.get(stage)
        if not force and previous is not None and now_ms - previous < interval_ms:
            return False
        self._stage_last_run_ms[stage] = now_ms
        return True

    def health(self, *, now_ms: int | None = None) -> dict[str, Any]:
        now = monotonic_ms() if now_ms is None else now_ms
        last_success_age_ms = _age(now, self.last_success_ms)
        last_failure_age_ms = _age(now, self.last_failure_ms)
        status = "ok"
        if self.consecutive_failures:
            status = "degraded"
        elif last_success_age_ms is None:
            status = "starting"
        elif last_success_age_ms > self.stale_after_ms:
            status = "degraded"
        return {
            "status": status,
            "tick_interval_ms": self.tick_interval_ms,
            "stale_after_ms": self.stale_after_ms,
            "last_success_age_ms": last_success_age_ms,
            "last_failure_age_ms": last_failure_age_ms,
            "consecutive_failures": self.consecutive_failures,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "current_error": self.current_error,
            "last_failure_error": self.last_failure_error,
            "pending_pulse_count": len(self.pulses),
            "timings_ms": {
                stage: {
                    "last": self._timing_last_ms[stage],
                    "average": round(
                        self._timing_total_ms[stage] / self._timing_samples[stage], 1
                    ),
                    "max": self._timing_max_ms[stage],
                    "samples": self._timing_samples[stage],
                }
                for stage in sorted(self._timing_samples)
            },
        }


def _age(now_ms: int, then_ms: int | None) -> int | None:
    if then_ms is None:
        return None
    return max(0, now_ms - then_ms)
