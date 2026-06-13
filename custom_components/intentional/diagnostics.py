"""Small in-memory diagnostics ring for Intentional runtime events."""

from __future__ import annotations

from collections import deque
from time import time
from typing import Any

from homeassistant.core import HomeAssistant

from .const import DOMAIN

DIAGNOSTICS_KEY = "diagnostics"
DIAGNOSTICS_LIMIT = 200
DEFAULT_DIAGNOSTIC_COOLDOWN_MS = 60_000


class DiagnosticRateLimiter:
    """Rate-limit repeated diagnostic records with identical keys."""

    def __init__(self, *, cooldown_ms: int = DEFAULT_DIAGNOSTIC_COOLDOWN_MS) -> None:
        self._cooldown_ms = cooldown_ms
        self._suppressed_until: dict[tuple[str, ...], int] = {}

    def allow(self, key: tuple[str, ...], *, now_ms: int) -> bool:
        """Return true once per key per cooldown window."""
        for stored_key, suppress_until_ms in list(self._suppressed_until.items()):
            if suppress_until_ms <= now_ms:
                self._suppressed_until.pop(stored_key, None)
        suppress_until = self._suppressed_until.get(key)
        if suppress_until is not None and now_ms < suppress_until:
            return False
        self._suppressed_until[key] = now_ms + self._cooldown_ms
        return True


def diagnostics_ring(hass: HomeAssistant) -> deque[dict[str, Any]]:
    """Return the shared diagnostics ring for this HA instance."""
    if not hasattr(hass, "data"):
        hass.data = {}
    domain_data = hass.data.setdefault(DOMAIN, {})
    ring = domain_data.get(DIAGNOSTICS_KEY)
    if not isinstance(ring, deque):
        ring = deque(maxlen=DIAGNOSTICS_LIMIT)
        domain_data[DIAGNOSTICS_KEY] = ring
    return ring


def record_diagnostic(hass: HomeAssistant, event_type: str, **data: Any) -> None:
    """Append one compact runtime diagnostic event."""
    diagnostics_ring(hass).append({
        "time": time(),
        "type": event_type,
        **data,
    })


def record_intentional_context_ignored_for_drift(
    hass: HomeAssistant,
    *,
    target: str,
    state: Any,
    now_ms: int,
    rate_limiter: DiagnosticRateLimiter | None = None,
) -> None:
    """Record that an Intentional-owned context was ignored for drift."""
    context = getattr(state, "context", None)
    context_id = str(getattr(context, "id", None) or "")
    parent_id = str(getattr(context, "parent_id", None) or "")
    if rate_limiter is not None and not rate_limiter.allow(
        ("intentional_context_ignored_for_drift", target, context_id, parent_id),
        now_ms=now_ms,
    ):
        return
    record_diagnostic(
        hass,
        "intentional_context_ignored_for_drift",
        target=target,
        context_id=context_id or None,
        parent_id=parent_id or None,
    )


def list_diagnostics(hass: HomeAssistant, *, limit: int | None = None) -> list[dict[str, Any]]:
    """Return newest diagnostics in chronological order."""
    events = list(diagnostics_ring(hass))
    if limit is not None:
        events = events[-max(0, limit):]
    return events
