"""Home Assistant integration boundary for durable Alert state."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import secrets
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send

from ._engine.alerting import AlertCoordinator, alert_definition_key
from ._engine.alerting.policy import AlertingPolicyRepository

ALERTING_STORAGE_VERSION = 1
ALERTING_POLICY_STORAGE_VERSION = 1
ALERTING_SECRET_STORAGE_VERSION = 1
DISPATCH_TIMEOUT_SECONDS = 30
STOP_TIMEOUT_SECONDS = 5
REJECTION_TIMEOUT_SECONDS = 10

_LOGGER = logging.getLogger(__name__)


def alerting_coordinator_key(entry_id: str) -> str:
    """Return the hass.data key for one entry's Alert coordinator."""
    return f"{entry_id}:alerting_coordinator"


def alerting_storage_key(entry_id: str) -> str:
    """Return the Store key for one entry's Alert runtime state."""
    return f"intentional_alerting_{entry_id}_v1"


def alerting_policy_storage_key(entry_id: str) -> str:
    """Return the Store key for one entry's Alert routing policy."""
    return f"intentional_alerting_policy_{entry_id}_v1"


def alerting_secret_storage_key(entry_id: str) -> str:
    return f"intentional_alerting_secret_{entry_id}_v1"


def alerting_policy_repository_key(entry_id: str) -> str:
    """Return the hass.data key for one entry's Alert routing policy."""
    return f"{entry_id}:alerting_policy"


def alerting_dispatcher_key(entry_id: str) -> str:
    """Return the hass.data key for one entry's Notification dispatcher."""
    return f"{entry_id}:alerting_dispatcher"


def alerting_deadline_driver_key(entry_id: str) -> str:
    return f"{entry_id}:alerting_deadline_driver"


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


class AlertNotificationDispatcher:
    """Dispatch already-durable Notification obligations through HA services."""

    def __init__(self, hass: HomeAssistant, coordinator: AlertCoordinator) -> None:
        self._hass = hass
        self._coordinator = coordinator

    async def async_dispatch_due(self, *, now_ms: int | None = None) -> int:
        now_ms = now_ms if now_ms is not None else int(time.time() * 1_000)
        due = await self._coordinator.async_notification_advance(now_ms=now_ms)
        accepted = 0
        for candidate in due:
            obligation_id = str(candidate["obligation_id"])
            obligation = await self._coordinator.async_begin_notification_dispatch(
                obligation_id, now_ms=now_ms
            )
            if obligation is None:
                continue
            try:
                async with asyncio.timeout(DISPATCH_TIMEOUT_SECONDS):
                    await self._async_call(obligation)
            except asyncio.CancelledError:
                # Leave an unfinished obligation retryable without allowing a
                # stalled Store to hold shutdown indefinitely.
                try:
                    async with asyncio.timeout(REJECTION_TIMEOUT_SECONDS):
                        await self._coordinator.async_reject_notification(
                            obligation_id,
                            now_ms=int(time.time() * 1_000),
                            error_class="CancelledError",
                        )
                except TimeoutError:
                    _LOGGER.warning("Timed out persisting cancelled Notification")
                raise
            except Exception as err:  # noqa: BLE001 - durable retry stores error class
                await self._coordinator.async_reject_notification(
                    obligation_id,
                    now_ms=int(time.time() * 1_000),
                    error_class=type(err).__name__,
                )
            else:
                await self._coordinator.async_accept_notification(
                    obligation_id, now_ms=int(time.time() * 1_000)
                )
                accepted += 1
        return accepted

    async def _async_call(self, obligation: dict[str, Any]) -> None:
        destination = obligation["destination"]
        payload = obligation["payload"]
        destination_type = destination.get("type")
        if destination_type == "notify_entity":
            if not self._hass.services.has_service("notify", "send_message"):
                raise ValueError("notify.send_message is unavailable")
            # Generic notify entities accept message/title, not mobile-app action data.
            await self._hass.services.async_call(
                "notify",
                "send_message",
                {
                    "message": payload["message"],
                    "title": payload["title"],
                },
                target={"entity_id": destination["entity_id"]},
                blocking=True,
            )
            return
        if destination_type == "legacy_action":
            domain, service = destination["action"].split(".", 1)
            if not self._hass.services.has_service(domain, service):
                raise ValueError("configured legacy notify action is unavailable")
            await self._hass.services.async_call(
                domain,
                service,
                {"message": payload["message"], "title": payload["title"]},
                blocking=True,
            )
            return
        if destination_type == "persistent_notification":
            notification_id = "intentional_" + hashlib.sha256(
                f'{obligation["group_identity"]}:{obligation["destination_id"]}'.encode()
            ).hexdigest()[:32]
            if obligation["message_kind"] in {"resolved", "cleanup"}:
                if not self._hass.services.has_service(
                    "persistent_notification", "dismiss"
                ):
                    raise ValueError("persistent_notification.dismiss is unavailable")
                await self._hass.services.async_call(
                    "persistent_notification",
                    "dismiss",
                    {"notification_id": notification_id},
                    blocking=True,
                )
            else:
                if not self._hass.services.has_service(
                    "persistent_notification", "create"
                ):
                    raise ValueError("persistent_notification.create is unavailable")
                await self._hass.services.async_call(
                    "persistent_notification",
                    "create",
                    {
                        "notification_id": notification_id,
                        "message": payload["message"],
                        "title": payload["title"],
                    },
                    blocking=True,
                )
            return
        raise ValueError("unsupported Receiver destination")

    async def async_test_destination(
        self, destination: dict[str, Any], *, receiver: str
    ) -> None:
        """Send one explicit administrator-requested Receiver test."""
        identity = hashlib.sha256(repr(sorted(destination.items())).encode()).hexdigest()
        await self._async_call(
            {
                "group_identity": f"receiver-test:{receiver}",
                "destination_id": identity,
                "destination": destination,
                "message_kind": "test",
                "payload": {
                    "title": "Intentional Receiver test",
                    "message": f"Receiver {receiver} is configured correctly.",
                },
            }
        )


class AlertDeadlineDriver:
    """Wake the Alert coordinator at durable deadlines independently of ticks."""

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: AlertCoordinator,
        dispatcher: AlertNotificationDispatcher,
    ) -> None:
        self._hass = hass
        self._coordinator = coordinator
        self._dispatcher = dispatcher
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    def start(self, entry: ConfigEntry | None = None) -> None:
        if self._task is None:
            if entry is not None:
                self._task = entry.async_create_background_task(
                    self._hass, self._run(), name="intentional_alert_deadlines"
                )
            else:
                self._task = self._hass.async_create_background_task(
                    self._run(), name="intentional_alert_deadlines"
                )

    def wake(self) -> None:
        self._wake.set()

    async def async_stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=STOP_TIMEOUT_SECONDS)
            except (TimeoutError, asyncio.CancelledError):
                if asyncio.current_task().cancelling():
                    raise

    async def _run(self) -> None:
        while not self._stop.is_set():
            now_ms = int(time.time() * 1_000)
            deadline = self._coordinator.next_deadline_ms()
            if deadline is not None and deadline <= now_ms:
                try:
                    await self._coordinator.async_advance(now_ms=now_ms)
                    await self._dispatcher.async_dispatch_due(now_ms=now_ms)
                except Exception as err:  # noqa: BLE001 - keep the sole deadline driver alive
                    self._coordinator.record_runtime_error(type(err).__name__)
                    _LOGGER.exception("Alert deadline processing failed")
                    await asyncio.sleep(1)
                if self._coordinator.health()["status"] == "degraded":
                    await asyncio.sleep(1)
                continue
            self._wake.clear()
            timeout = (
                None if deadline is None else max(0, (deadline - now_ms) / 1_000)
            )
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=timeout)
