"""Registration-aware lifecycle for dynamically published entities."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from homeassistant.helpers.entity import Entity


class RegistrationAwareEntity(Entity):
    """Defer state operations until Home Assistant has registered the entity."""

    def __init__(self) -> None:
        super().__init__()
        self._lifecycle_desired = True
        self._lifecycle_added = False
        self._lifecycle_removing = False
        self._lifecycle_removed: Callable[[], None] | None = None

    def set_removal_callback(self, callback: Callable[[], None]) -> None:
        """Set the callback used after a requested removal completes."""
        self._lifecycle_removed = callback

    def mark_desired(self) -> None:
        """Cancel a pending removal."""
        self._lifecycle_desired = True

    async def async_mark_removed(self) -> None:
        """Remove now when registered, or after registration otherwise."""
        self._lifecycle_desired = False
        if self._lifecycle_added:
            await self._async_remove_requested()
            return

        # Give a queued add one loop turn to complete. If HA aborts it (for
        # example, a disabled registry entry), release the desired-set slot
        # without operating on an entity that never entered the platform.
        await asyncio.sleep(0)
        if not self._lifecycle_desired and not self._lifecycle_added:
            self._notify_removed()

    def async_write_if_registered(self) -> None:
        """Write only after Home Assistant has successfully added the entity."""
        if self._lifecycle_added:
            self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Finish a removal requested while registration was pending."""
        await super().async_added_to_hass()
        self._lifecycle_added = True
        if not self._lifecycle_desired:
            self.hass.async_create_task(self._async_remove_requested())

    async def async_will_remove_from_hass(self) -> None:
        """Reset registration before the entity leaves the platform."""
        self._lifecycle_added = False
        self._lifecycle_removing = False
        await super().async_will_remove_from_hass()

    async def _async_remove_requested(self) -> None:
        if (
            self._lifecycle_desired
            or not self._lifecycle_added
            or self._lifecycle_removing
        ):
            return
        self._lifecycle_removing = True
        try:
            await self.async_remove()
            self._notify_removed()
        finally:
            self._lifecycle_removing = False

    def _notify_removed(self) -> None:
        callback = self._lifecycle_removed
        self._lifecycle_removed = None
        if callback is not None:
            callback()
