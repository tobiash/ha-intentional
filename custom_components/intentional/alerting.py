"""Home Assistant integration boundary for durable Alert state."""

from __future__ import annotations

import hashlib
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send

from ._engine.alerting import AlertCoordinator, alert_definition_key
from ._engine.alerting.policy import AlertingPolicyRepository

ALERTING_STORAGE_VERSION = 1
ALERTING_POLICY_STORAGE_VERSION = 1


def alerting_coordinator_key(entry_id: str) -> str:
    """Return the hass.data key for one entry's Alert coordinator."""
    return f"{entry_id}:alerting_coordinator"


def alerting_storage_key(entry_id: str) -> str:
    """Return the Store key for one entry's Alert runtime state."""
    return f"intentional_alerting_{entry_id}_v1"


def alerting_policy_storage_key(entry_id: str) -> str:
    """Return the Store key for one entry's Alert routing policy."""
    return f"intentional_alerting_policy_{entry_id}_v1"


def alerting_policy_repository_key(entry_id: str) -> str:
    """Return the hass.data key for one entry's Alert routing policy."""
    return f"{entry_id}:alerting_policy"


def alerting_signal(entry_id: str) -> str:
    """Return the dispatcher signal for Alert projection changes."""
    return f"intentional_alerting_{entry_id}"


def alert_sensor_unique_id(entry_id: str, rule_id: str, name: str) -> str:
    """Return a collision-safe stable HA entity unique ID."""
    identity = alert_definition_key(rule_id, name).encode()
    return f"{entry_id}_alert_{hashlib.sha256(identity).hexdigest()[:32]}"


def publish_alerts(hass: HomeAssistant, entry_id: str) -> None:
    """Notify HA entities that the durable Alert projection changed."""
    async_dispatcher_send(hass, alerting_signal(entry_id))


def alerting_coordinator_for(hass: Any, entry_id: str) -> AlertCoordinator | None:
    """Return a loaded Alert coordinator for an entry."""
    coordinator = hass.data.get("intentional", {}).get(alerting_coordinator_key(entry_id))
    return coordinator if isinstance(coordinator, AlertCoordinator) else None


def alerting_policy_for(
    hass: Any, entry_id: str
) -> AlertingPolicyRepository | None:
    """Return a loaded Alert routing policy repository for an entry."""
    repository = hass.data.get("intentional", {}).get(
        alerting_policy_repository_key(entry_id)
    )
    return repository if isinstance(repository, AlertingPolicyRepository) else None
