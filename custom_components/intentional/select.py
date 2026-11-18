"""Opt-in duration selectors for explicit lighting holds."""

from .hold_controls import HoldDuration, setup_hold_entities


async def async_setup_entry(hass, entry, async_add_entities):
    """Publish one duration selector per configured control target."""
    await setup_hold_entities(hass, entry, async_add_entities, lambda target: [HoldDuration(hass, entry, target)])
