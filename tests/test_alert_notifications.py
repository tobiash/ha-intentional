"""Validate the HA notification boundary against the real notify component."""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.components.notify import DATA_COMPONENT, NotifyEntity, NotifyEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.intentional.alerting import AlertDeadlineDriver, AlertNotificationDispatcher


@pytest.fixture
async def notify_messages(hass: HomeAssistant):
    messages = []

    class TestNotifyEntity(NotifyEntity):
        _attr_name = "Family"
        _attr_supported_features = NotifyEntityFeature.TITLE

        async def async_send_message(self, message: str, title: str | None = None) -> None:
            messages.append((message, title))

    assert await async_setup_component(hass, "notify", {})
    await hass.data[DATA_COMPONENT].async_add_entities([TestNotifyEntity()])
    return messages


@pytest.mark.parametrize("instance_ids", [[], ["instance-1"], ["instance-1", "instance-2"]])
async def test_notify_entity_dispatch(hass: HomeAssistant, notify_messages, instance_ids):
    """Even obligations carrying mobile capabilities use the generic entity API."""
    dispatcher = AlertNotificationDispatcher(hass, Mock())
    await dispatcher._async_call(
        {
            "destination": {"type": "notify_entity", "entity_id": "notify.family"},
            "payload": {"message": "Check the freezer", "title": "Freezer is warm"},
            "capabilities": [
                {
                    "operation": operation,
                    "record_id": f"record-{instance_id}-{operation}",
                    "token": "secret-token",
                    "instance_id": instance_id,
                }
                for instance_id in instance_ids
                for operation in ("acknowledge", "silence")
            ],
        }
    )

    assert notify_messages == [("Check the freezer", "Freezer is warm")]


async def test_notify_entity_test_destination(hass: HomeAssistant, notify_messages):
    dispatcher = AlertNotificationDispatcher(hass, Mock())
    await dispatcher.async_test_destination(
        {"type": "notify_entity", "entity_id": "notify.family"}, receiver="household"
    )

    assert notify_messages == [
        ("Receiver household is configured correctly.", "Intentional Receiver test")
    ]


async def test_legacy_destination_keeps_plain_payload(hass: HomeAssistant):
    calls = []

    async def send(call):
        calls.append(dict(call.data))

    hass.services.async_register("notify", "mobile_app_phone", send)
    dispatcher = AlertNotificationDispatcher(hass, Mock())
    await dispatcher._async_call(
        {
            "destination": {"type": "legacy_action", "action": "notify.mobile_app_phone"},
            "payload": {"message": "Check the freezer", "title": "Freezer is warm"},
        }
    )

    assert calls == [{"message": "Check the freezer", "title": "Freezer is warm"}]


@pytest.mark.parametrize("stalled_rejection", [False, True])
async def test_deadline_stop_cancels_stalled_notification_without_accepting(hass, monkeypatch, stalled_rejection):
    from custom_components.intentional import alerting

    monkeypatch.setattr(alerting, "STOP_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(alerting, "REJECTION_TIMEOUT_SECONDS", 0.01)
    started = asyncio.Event()
    finished = asyncio.Event()

    async def stalled(_call):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    hass.services.async_register("notify", "stalled", stalled)
    obligation = {
        "obligation_id": "test",
        "destination": {"type": "legacy_action", "action": "notify.stalled"},
        "payload": {"message": "test", "title": "test"},
    }
    coordinator = Mock(
        next_deadline_ms=Mock(return_value=0),
        async_advance=AsyncMock(),
        async_notification_advance=AsyncMock(return_value=[obligation]),
        async_begin_notification_dispatch=AsyncMock(return_value=obligation),
        async_accept_notification=AsyncMock(),
        async_reject_notification=AsyncMock(),
    )
    if stalled_rejection:
        async def reject(*args, **kwargs):
            await asyncio.Event().wait()
        coordinator.async_reject_notification.side_effect = reject
    driver = AlertDeadlineDriver(hass, coordinator, AlertNotificationDispatcher(hass, coordinator))
    driver.start()
    await asyncio.wait_for(started.wait(), timeout=1)
    await asyncio.wait_for(driver.async_stop(), timeout=1)
    assert driver._task.done()
    assert finished.is_set()
    coordinator.async_accept_notification.assert_not_awaited()
    coordinator.async_reject_notification.assert_awaited_once()
