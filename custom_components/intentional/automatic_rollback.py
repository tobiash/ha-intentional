"""Conservative automatic rollback for newly authored Rule generations."""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

from homeassistant.helpers.storage import Store

JOURNAL_VERSION = 1
FAILURE_THRESHOLD = 3
SUCCESS_TICK_LIMIT = 10
STABLE_SECONDS = 300
COOLDOWN_SECONDS = 300
ELIGIBLE_STAGES = frozenset({"evaluation"})


def automatic_rollback_key(entry_id: str) -> str:
    return f"{entry_id}:automatic_rollback"


def _utc_now() -> datetime:
    return datetime.now(UTC)


class AutomaticRollback:
    """Persist and enforce the one-shot generation rollback fence."""

    def __init__(
        self, hass: Any, entry_id: str, rule_store: Any, diagnostic: Any = None
    ) -> None:
        self._store: Store = Store(
            hass, JOURNAL_VERSION, f"intentional_automatic_rollback_{entry_id}_v1"
        )
        self._rules = rule_store
        self._coordinator: Any = None
        self._diagnostic = diagnostic
        self._journal: dict[str, Any] = self._empty()

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {
            "version": JOURNAL_VERSION,
            "state": "disarmed",
            "rolled_back_generations": [],
        }

    async def async_load(self) -> None:
        raw = await self._store.async_load()
        if not isinstance(raw, dict) or raw.get("version") != JOURNAL_VERSION:
            return
        rolled_back = raw.get("rolled_back_generations")
        if not isinstance(rolled_back, list) or not all(isinstance(item, str) for item in rolled_back):
            return
        self._journal = {**raw, "rolled_back_generations": rolled_back[-25:]}

    def set_coordinator(self, coordinator: Any) -> None:
        self._coordinator = coordinator

    async def async_arm(self, current: str, previous: str, revision: int) -> None:
        if current == previous or current in self._journal["rolled_back_generations"]:
            return
        history = self._rules.list_history()
        if not history or history[0].get("generation") != previous:
            return
        last_rollback = self._parse_time(self._journal.get("last_rollback_at"))
        if last_rollback and (_utc_now() - last_rollback).total_seconds() < COOLDOWN_SECONDS:
            return
        self._journal.update({
            "state": "armed",
            "current_generation": current,
            "previous_generation": previous,
            "revision": revision,
            "armed_at": _utc_now().isoformat(),
            "success_ticks": 0,
            "fingerprint": None,
            "consecutive_failures": 0,
            "last_error": None,
        })
        await self._save()
        self._record("automatic_rollback_armed", generation=current)

    async def async_success(
        self, *, generation: str, revision: int, next_revision: int
    ) -> None:
        if not self._fence_matches(generation, revision):
            if (
                self._journal.get("state") == "armed"
                and self._journal.get("current_generation") == generation
                and self._journal.get("revision") != revision
            ):
                await self.async_disqualify("revision_changed")
            return
        self._journal["success_ticks"] = int(self._journal.get("success_ticks", 0)) + 1
        self._journal["revision"] = next_revision
        armed_at = self._parse_time(self._journal.get("armed_at"))
        stable_seconds = (_utc_now() - armed_at).total_seconds() if armed_at else 0
        if self._journal["success_ticks"] >= SUCCESS_TICK_LIMIT or stable_seconds >= STABLE_SECONDS:
            self._journal.update({"state": "disarmed", "reason": "stable"})
            self._record("automatic_rollback_disarmed", reason="stable")
        await self._save()

    async def async_failure(
        self,
        error: BaseException,
        *,
        stage: str,
        generation: str,
        revision: int,
        dispatch_attempted: bool = False,
    ) -> bool:
        """Record an eligible pre-dispatch failure and perform at most one rollback."""
        if dispatch_attempted or stage not in ELIGIBLE_STAGES or not self._fence_matches(generation, revision):
            return False
        fingerprint = self.fingerprint(stage, error)
        if self._journal.get("fingerprint") == fingerprint:
            self._journal["consecutive_failures"] = int(
                self._journal.get("consecutive_failures", 0)
            ) + 1
        else:
            self._journal["fingerprint"] = fingerprint
            self._journal["consecutive_failures"] = 1
        self._journal["last_error"] = str(error)[:240]
        self._record(
            "automatic_rollback_failure_observed",
            stage=stage,
            consecutive_failures=self._journal["consecutive_failures"],
            fingerprint=fingerprint[:12],
        )
        await self._save()
        if self._journal["consecutive_failures"] < FAILURE_THRESHOLD:
            return False
        await self._rollback(fingerprint)
        return self._journal["state"] == "rolled_back"

    async def async_disqualify(self, reason: str) -> None:
        if self._journal.get("state") != "armed":
            return
        self._journal.update({"state": "disarmed", "reason": reason})
        await self._save()
        self._record("automatic_rollback_disarmed", reason=reason)

    async def _rollback(self, fingerprint: str) -> None:
        failed = self._journal["current_generation"]
        previous = self._journal["previous_generation"]
        rolled_back = self._journal["rolled_back_generations"]
        if failed in rolled_back or self._coordinator is None:
            return
        # Mark attempted before I/O: restart or storage failure must not create a retry loop.
        rolled_back.append(failed)
        self._journal.update({
            "state": "rollback_pending",
            "last_rollback_at": _utc_now().isoformat(),
        })
        await self._save()
        try:
            result, reload_error = await self._coordinator.async_mutate_and_reload(
                lambda: self._rules.async_rollback(
                    previous,
                    expected_generation=failed,
                    reason=f"auto_rollback:{failed[:12]}:{fingerprint[:12]}",
                ),
                expected_generation=failed,
                arm_rollback=False,
            )
            if reload_error is not None or not isinstance(result, dict) or "error" in result:
                raise RuntimeError(str(reload_error or result))
        except Exception as err:  # noqa: BLE001 - terminal state retains rollback failure
            self._journal.update({
                "state": "manual_intervention_required",
                "last_error": str(err)[:240],
            })
            self._record("automatic_rollback_failed", error=str(err)[:240])
        else:
            self._journal.update({
                "state": "rolled_back",
                "reason": "deterministic_internal_failure",
                "rollback_generation": self._rules.generation,
            })
            self._record(
                "automatic_rollback_succeeded",
                failed_generation=failed,
                restored_generation=self._rules.generation,
            )
        await self._save()

    def _fence_matches(self, generation: str, revision: int) -> bool:
        return (
            self._journal.get("state") == "armed"
            and self._journal.get("current_generation") == generation
            and self._journal.get("revision") == revision
            and generation == self._rules.generation
        )

    @staticmethod
    def fingerprint(stage: str, error: BaseException) -> str:
        normalized = " ".join(str(error).split())[:500]
        value = f"{stage}:{type(error).__module__}.{type(error).__qualname__}:{normalized}"
        return sha256(value.encode()).hexdigest()

    @staticmethod
    def _parse_time(value: Any) -> datetime | None:
        try:
            parsed = datetime.fromisoformat(value) if isinstance(value, str) else None
            return parsed if parsed is not None and parsed.tzinfo is not None else None
        except ValueError:
            return None

    async def _save(self) -> None:
        await self._store.async_save(self._journal)

    def _record(self, event_type: str, **data: Any) -> None:
        if self._diagnostic is not None:
            self._diagnostic(event_type, **data)

    def health(self) -> dict[str, Any]:
        """Return compact non-secret rollback health."""
        return {
            key: self._journal.get(key)
            for key in (
                "state", "current_generation", "previous_generation", "success_ticks",
                "consecutive_failures", "last_error", "reason", "last_rollback_at",
            )
            if key in self._journal
        }
