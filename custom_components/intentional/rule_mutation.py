"""Transactional coordination of durable Rule mutations and runtime reload."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import Any

from .rule_store import StorageRuleStore

PreparedRules = Any
PrepareRules = Callable[[str], PreparedRules]
CommitRules = Callable[[PreparedRules], Awaitable[None]]


def mutation_coordinator_key(entry_id: str) -> str:
    """Return the hass.data key for an entry's Rule mutation coordinator."""
    return f"{entry_id}:rule_mutation"


class RuleMutationCoordinator:
    """Serialize document preparation, durable mutation, and runtime commit."""

    def __init__(
        self,
        store: StorageRuleStore,
        prepare: PrepareRules,
        commit: CommitRules,
    ) -> None:
        self._store = store
        self._prepare = prepare
        self._commit = commit
        self._lock = asyncio.Lock()

    async def async_reload(self) -> None:
        """Prepare and commit one stable durable document generation."""
        async with self._lock:
            prepared = self._prepare(self._store.contents)
            await self._commit(prepared)

    async def async_mutate_and_reload(
        self,
        mutation: Callable[[], Awaitable[dict[str, Any] | str | None]],
        *,
        expected_generation: str,
    ) -> tuple[dict[str, Any] | str | None, Exception | None]:
        """Persist and apply one mutation, restoring durable state if apply fails."""
        async with self._lock:
            if self._store.generation != expected_generation:
                return {"error": "generation_mismatch"}, None
            snapshot = self._store.snapshot()
            try:
                result = await mutation()
            except Exception as err:
                try:
                    await self._store.async_restore(snapshot)
                except Exception as restore_err:
                    err.add_note(f"Rule store recovery failed: {restore_err!r}")
                raise
            if isinstance(result, str) or (isinstance(result, dict) and "error" in result):
                return result, None
            try:
                prepared = self._prepare(self._store.contents)
                await self._commit(prepared)
            except Exception as err:  # Durable and applied Rule sets must agree.
                await self._store.async_restore(snapshot)
                with contextlib.suppress(Exception):
                    prepared = self._prepare(self._store.contents)
                    await self._commit(prepared)
                return result, err
            return result, None


async def mutate_and_reload(
    coordinator: RuleMutationCoordinator,
    mutation: Callable[[], Awaitable[dict[str, Any] | str | None]],
    *,
    expected_generation: str,
) -> tuple[dict[str, Any] | str | None, Exception | None]:
    """Compatibility wrapper around the shared transaction coordinator."""
    return await coordinator.async_mutate_and_reload(
        mutation, expected_generation=expected_generation
    )
