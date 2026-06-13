"""Runtime context attribution helpers for Intentional-owned HA actions."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from homeassistant.core import Context

DEFAULT_CONTEXT_TTL_MS = 10 * 60 * 1000
DEFAULT_CONTEXT_MAX_ENTRIES = 512


class IntentionalContextTracker:
    """Track recent Home Assistant contexts created by Intentional."""

    def __init__(
        self,
        *,
        ttl_ms: int = DEFAULT_CONTEXT_TTL_MS,
        max_entries: int = DEFAULT_CONTEXT_MAX_ENTRIES,
        clock_fn: Callable[[], int] | None = None,
    ) -> None:
        self._ttl_ms = ttl_ms
        self._max_entries = max_entries
        self._clock_fn = clock_fn or (lambda: int(time.monotonic() * 1000))
        self._expires_by_context_id: dict[str, int] = {}

    def new_context(self) -> Context:
        """Create and remember a Home Assistant context owned by Intentional."""
        context = Context()
        self.remember(context)
        return context

    def remember(self, context: Context) -> None:
        """Remember an externally-created context as Intentional-owned."""
        now_ms = self._clock_fn()
        self._prune(now_ms)
        context_id = getattr(context, "id", None)
        if not context_id:
            return
        self._expires_by_context_id[context_id] = now_ms + self._ttl_ms
        self._trim()

    def owns_state(self, state: Any) -> bool:
        """Return whether an HA state carries an Intentional-owned context."""
        self._prune(self._clock_fn())
        context = getattr(state, "context", None)
        context_id = getattr(context, "id", None)
        parent_id = getattr(context, "parent_id", None)
        return any(
            context_ref in self._expires_by_context_id
            for context_ref in (context_id, parent_id)
            if context_ref
        )

    def __len__(self) -> int:
        self._prune(self._clock_fn())
        return len(self._expires_by_context_id)

    def ids(self) -> tuple[str, ...]:
        """Return tracked context ids, mainly for tests."""
        self._prune(self._clock_fn())
        return tuple(self._expires_by_context_id)

    def _prune(self, now_ms: int) -> None:
        for context_id, expires_at_ms in list(self._expires_by_context_id.items()):
            if expires_at_ms <= now_ms:
                self._expires_by_context_id.pop(context_id, None)

    def _trim(self) -> None:
        excess = len(self._expires_by_context_id) - self._max_entries
        if excess <= 0:
            return
        for context_id, _expires_at_ms in sorted(
            self._expires_by_context_id.items(),
            key=lambda item: item[1],
        )[:excess]:
            self._expires_by_context_id.pop(context_id, None)


def new_intentional_context(tracker: IntentionalContextTracker | None = None) -> Context:
    """Create a Home Assistant context, tracking it when a tracker is provided."""
    if tracker is None:
        return Context()
    return tracker.new_context()
