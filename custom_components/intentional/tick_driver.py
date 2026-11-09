"""Own the periodic cycle's ordering, fencing, persistence and liveness.

The integration supplies HA-specific input/dispatch callbacks. Callers start
async_run; tests can drive async_step without a timer or a Home Assistant boot.
Rule evaluation and Reconciliation policy remain in their existing modules.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ._engine.ha_adapter import clear_state_change_pulses
from ._engine.runtime import TickRuntime, monotonic_ms

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from ._engine import Engine
    from ._engine.alerting import AlertCoordinator
    from ._engine.reconciliation import (
        ContextTracker,
        HAAdapter,
        Reconciliation,
        ReconciliationEvent,
    )
    from .alerting import AlertDeadlineDriver
    from .automatic_rollback import AutomaticRollback
    from .lifecycle_writer import LifecycleWriter
    from .publication import EntityPublication
    from .rule_store import StorageRuleStore

from .diagnostics import record_diagnostic

_LOGGER = logging.getLogger(__name__)
STABLE_STAGE_INTERVAL_MS = 1_000
ALERT_OBSERVATION_TIMEOUT_SECONDS = 30


@dataclass(kw_only=True)
class TickDriver:
    """Run serial cycles; mutations share runtime.mutation_lock.

    prepare_inputs, record_activity and commit_events are synchronous and run
    under the mutation lock. dispatch_effects may await HA and must honor the
    supplied revision. No caller has to acknowledge pulses or mark tick_idle.
    """

    hass: HomeAssistant
    engine: Engine
    runtime: TickRuntime
    reconciler: Reconciliation
    ha_adapter: HAAdapter
    contexts: ContextTracker
    writer: LifecycleWriter
    alerting: AlertCoordinator
    alert_deadlines: AlertDeadlineDriver
    rollback: AutomaticRollback
    rules: StorageRuleStore
    publication: EntityPublication
    prepare_inputs: Callable[[], None]
    record_activity: Callable[[set[str]], set[str]]
    commit_events: Callable[[list[ReconciliationEvent], int], None]
    dispatch_effects: Callable[[set[str], int, Callable[[], Awaitable[None]]], Awaitable[set[str] | None]]
    clock: Callable[[], int] = monotonic_ms

    async def async_run(self) -> None:
        """Run until stopped/cancelled, then attempt a final durable checkpoint."""
        try:
            while not self.runtime.stop_event.is_set():
                try:
                    await asyncio.wait_for(
                        self.runtime.stop_event.wait(),
                        timeout=self.runtime.tick_interval_ms / 1000,
                    )
                    break
                except TimeoutError:
                    pass
                await self.async_step()
        finally:
            self.runtime.tick_idle.set()
            try:
                self.writer.mutated()
                await self.writer.async_flush(force=True)
                if self.writer.current_error is not None:
                    record_diagnostic(
                        self.hass, "lifecycle_save_failed", error=self.writer.current_error
                    )
            except Exception as err:  # noqa: BLE001
                _LOGGER.exception("Could not save final Intentional lifecycle state")
                record_diagnostic(self.hass, "lifecycle_save_failed", error=str(err))

    def _timing(self, stage: str, started: int) -> None:
        self.runtime.record_timing(stage, self.clock() - started)

    def _failure(self, error: Exception) -> None:
        now = self.clock()
        self.runtime.mark_failure(error, now_ms=now)
        if self.runtime.should_report_failure(now_ms=now):
            _LOGGER.exception("Intentional tick failed; continuing")
            record_diagnostic(self.hass, "tick_failed", error=str(error))

    async def async_step(self) -> None:
        """Attempt one cycle, leaving interrupted pulses pending for the next.

        Only one driver may call this at a time. Cancellation propagates, but
        every exit restores the idle signal so unload cannot wait on a dead tick.
        """
        runtime = self.runtime
        if runtime.unloading or runtime.stop_event.is_set():
            return
        cycle_revision = runtime.revision
        cycle_started = self.clock()
        evaluation = True
        try:
            async with runtime.mutation_lock:
                self._timing("mutation_lock_wait", cycle_started)
                if runtime.unloading or runtime.stop_event.is_set():
                    return
                runtime.tick_idle.clear()
                drain = runtime.pulses.begin_drain()
                started = self.clock()
                self.prepare_inputs()
                self.engine.evaluate_all()
                observations = self.engine.alert_observations(
                    drain.tokens, drain.source_timestamps_ms
                )
                now_ms = self.clock()
                runtime.active_rule_ids = self.record_activity(runtime.active_rule_ids)
                self.engine.tick(runtime.tick_interval_ms)
                revision = runtime.advance_revision()
                self.writer.mutated()
                self._timing("evaluation", started)
            evaluation = False

            started = self.clock()
            if self.alerting.available and runtime.stage_due(
                "alert_observation", now_ms=started,
                interval_ms=STABLE_STAGE_INTERVAL_MS, force=bool(drain),
            ):
                async with asyncio.timeout(ALERT_OBSERVATION_TIMEOUT_SECONDS):
                    await self.alerting.async_observe(observations, now_ms=self.engine.now_ms())
                self.alert_deadlines.wake()
            self._timing("alerting", started)

            started = self.clock()
            await self.writer.async_flush()
            self._timing("persistence", started)
            if self.writer.current_error is not None:
                return

            started = self.clock()
            events = await self.reconciler.tick(
                self.engine, self.ha_adapter, self.contexts, now_ms,
                revision_is_current=lambda: runtime.is_revision(revision),
                before_dispatch=lambda: self.rollback.async_disqualify("service_call_attempted"),
            )
            self._timing("reconciliation", started)
            dispatch_started = self.clock()
            started = self.clock()
            async with runtime.mutation_lock:
                if not runtime.is_revision(revision):
                    return
                self.commit_events(events, now_ms)
                previous_scenes = set(runtime.active_scenes)
            self._timing("reconciliation_commit", started)

            started = self.clock()
            scenes = await self.dispatch_effects(
                previous_scenes, revision,
                lambda: self.rollback.async_disqualify("effect_or_scene_call_attempted"),
            )
            self._timing("effect_dispatch", started)
            if scenes is None:
                return

            started = self.clock()
            async with runtime.mutation_lock:
                if not runtime.is_revision(revision):
                    return
                runtime.active_scenes = scenes
                pulses_to_clear = runtime.pulses.current_entity_ids(drain)
                if pulses_to_clear:
                    clear_state_change_pulses(self.engine, set(pulses_to_clear))
                    runtime.pulses.finish_drain(drain)
                    self.engine.evaluate_all()
                    self.reconciler.drop_inactive_applied(set(self.engine.list_active_targets()))
                runtime.mark_success(now_ms=self.clock())
                await self.rollback.async_success(
                    generation=self.rules.generation, revision=cycle_revision,
                    next_revision=runtime.revision,
                )
                self.writer.mutated()
            self._timing("finalization", started)

            started = self.clock()
            if runtime.stage_due(
                "publication", now_ms=started, interval_ms=STABLE_STAGE_INTERVAL_MS,
            ) and self.publication.publish_if_changed():
                self.reconciler.record_publication_dispatch(self.engine.now_ms())
            self._timing("publication", started)
            self._timing("dispatch", dispatch_started)
            self._timing("cycle", cycle_started)
        except Exception as err:  # noqa: BLE001
            self._failure(err)
            if evaluation:
                # Rollback acquires mutation_lock. Schedule only after leaving
                # the failed locked phase, preserving the generation fence.
                self.hass.async_create_task(self.rollback.async_failure(
                    err, stage="evaluation", generation=self.rules.generation,
                    revision=cycle_revision,
                ))
        finally:
            runtime.tick_idle.set()
