"""Small in-memory diagnostics ring for Intentional runtime events."""

from __future__ import annotations

from collections import deque
from time import time
from typing import Any

from homeassistant.core import HomeAssistant

from .const import DOMAIN

DIAGNOSTICS_KEY = "diagnostics"
DIAGNOSTICS_LIMIT = 200


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


def list_diagnostics(hass: HomeAssistant, *, limit: int | None = None) -> list[dict[str, Any]]:
    """Return newest diagnostics in chronological order."""
    events = list(diagnostics_ring(hass))
    if limit is not None:
        events = events[-max(0, limit):]
    return events
