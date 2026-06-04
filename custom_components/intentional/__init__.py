"""The Intentional integration.

Sets up the intent engine, loads rules from the configured directory,
wires Home Assistant state changes into the engine, and exposes services
for manual control.

Architecture:
- IntentionalEngine: wraps the pure-Python Engine, bridges HA ↔ intents
- IntentionalEntity: sensor entities (one per target, plus summary)
- Service handlers: fire, reload
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, State, SupportsResponse
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.storage import Store
from homeassistant.helpers.directory_watcher import async_watch_directory
from homeassistant.helpers.typing import ConfigType
import voluptuous as vol

from intentional import Engine, RuleLoadError, load_rules
from intentional.yaml_loader import Rule

from .const import (
    ATTR_TARGET,
    ATTR_TICK_INTERVAL_MS,
    CONF_RULE_DIR,
    DEFAULT_RULE_DIR,
    DOMAIN,
    SERVICE_FIRE,
    SERVICE_RELOAD,
    STORAGE_KEY,
    STORAGE_VERSION,
)
from .entity import IntentionalTargetSensor, IntentionalSummarySensor

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]

# Service schemas
FIRE_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_TARGET): cv.entity_id,
        vol.Optional("brightness_pct"): vol.All(int, vol.Range(min=0, max=100)),
        vol.Optional("ttl"): vol.All(int, vol.Range(min=0, max=86400)),
    }
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the integration from YAML (no-op; we use config entries)."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Intentional from a config entry."""
    rule_dir = entry.data.get(CONF_RULE_DIR, DEFAULT_RULE_DIR)

    engine = Engine()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = engine

    # Initial rule load
    try:
        initial_rules = await hass.async_add_executor_job(load_rules, rule_dir)
    except RuleLoadError as err:
        _LOGGER.error("Could not load rules from %s: %s", rule_dir, err)
        # Don't fail the whole integration — let the user fix the file
        # and the directory watcher will retry. We log the error.
        initial_rules = []
    except Exception as err:
        raise ConfigEntryNotReady(f"Failed to load rules: {err}") from err

    engine.load_rules(initial_rules)
    _LOGGER.info("Loaded %d rules from %s", len(initial_rules), rule_dir)

    # Watch the rule directory for changes (hot reload)
    async def _on_rule_dir_change(_change_type: str, _path: str) -> None:
        _LOGGER.info("Rule directory changed, reloading rules from %s", rule_dir)
        try:
            new_rules = await hass.async_add_executor_job(load_rules, rule_dir)
        except RuleLoadError as err:
            _LOGGER.error("Rule reload failed: %s", err)
            return
        engine.load_rules(new_rules)
        # Re-evaluate to pick up the new rules immediately
        engine.evaluate_all()
        # Notify entities to refresh
        await hass.data[DOMAIN]["refresh_callbacks"].__contains__.__class__  # noop
        # Trigger a manual state update so entities refresh
        await _refresh_entities(hass, entry)

    # Make sure the directory exists before watching
    rule_path = Path(rule_dir)
    if not rule_path.exists():
        try:
            rule_path.mkdir(parents=True, exist_ok=True)
        except OSError as err:
            _LOGGER.warning("Could not create rule directory %s: %s", rule_dir, err)

    if rule_path.exists():
        entry.async_on_unload(
            await async_watch_directory(
                hass, str(rule_path), _on_rule_dir_change
            )
        )

    # Set up platforms (sensors)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Set up tick loop — runs every 100ms, drives animations + TTL expiry
    tick_interval_ms = 100

    async def _tick_loop() -> None:
        while True:
            await asyncio.sleep(tick_interval_ms / 1000)
            # Drive the engine's evaluation cycle
            _sync_state_into_engine(hass, engine)
            engine.evaluate_all()
            engine.tick(tick_interval_ms)
            # Push resolved values to entities
            await _refresh_entities(hass, entry)

    tick_task = hass.async_create_task(_tick_loop(), name=f"{DOMAIN}_tick")
    entry.async_on_unload(tick_task.cancel)

    # Set up services
    await _register_services(hass, engine, rule_dir, entry)

    # Subscribe to state changes to keep engine in sync
    entry.async_on_unload(
        hass.bus.async_listen("state_changed", _on_ha_state_change_factory(hass, engine))
    )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload an Intentional config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok


def _sync_state_into_engine(hass: HomeAssistant, engine: Engine) -> None:
    """Pull a snapshot of HA state into the engine.

    The engine only tracks entities that rules reference; this is a
    best-effort sync of the entities we know about.
    """
    # In a real implementation, we'd track the entity_ids referenced
    # by rules. For now, we sync ALL light.* and sensor.* states —
    # cheap, since most homes have a few hundred at most.
    for state in hass.states.async_all():
        entity_id = state.entity_id
        engine.update_state(entity_id, state.state)


def _on_ha_state_change_factory(hass: HomeAssistant, engine: Engine):
    """Return a state_changed listener that pushes updates into the engine."""

    def _listener(event) -> None:
        new_state: State | None = event.data.get("new_state")
        if new_state is None:
            return
        engine.update_state(new_state.entity_id, new_state.state)
        # Re-evaluate immediately for snappy response
        engine.evaluate_all()

    return _listener


async def _refresh_entities(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Trigger a state update on all Intentional entities."""
    # Force the sensor platform to refresh — they pull from the engine
    # on each state read, so we just need to nudge HA to poll them.
    for entity_id in hass.states.async_entity_ids(DOMAIN):
        hass.states.async_set(entity_id, hass.states.get(entity_id).state if hass.states.get(entity_id) else "0")


async def _register_services(
    hass: HomeAssistant,
    engine: Engine,
    rule_dir: str,
    entry: ConfigEntry,
) -> None:
    """Register the fire and reload services."""

    async def _fire_service(call: ServiceCall) -> None:
        """Handle the `intentional.fire` service call.

        Creates a USER-authority intent for the target entity with the
        given set/ttl. This is the recommended way to inject manual
        control from automations or scripts.
        """
        target = call.data[ATTR_TARGET]
        set_dict: dict[str, Any] = {}
        if "brightness_pct" in call.data:
            set_dict["brightness_pct"] = call.data["brightness_pct"]
        ttl_seconds = call.data.get("ttl", 7200)
        ttl_ms = ttl_seconds * 1000

        engine.emit_user_intent(
            target=target,
            set=set_dict,
            ttl_ms=ttl_ms,
            reason=f"Manual fire service at {call.service}",
        )
        # Force a re-evaluation cycle
        engine.evaluate_all()
        await _refresh_entities(hass, entry)

    async def _reload_service(_call: ServiceCall) -> None:
        """Handle the `intentional.reload` service call.

        Re-reads rule files from disk. Same effect as saving a file in
        the rule directory (which the directory watcher picks up).
        """
        try:
            new_rules = await hass.async_add_executor_job(load_rules, rule_dir)
        except RuleLoadError as err:
            _LOGGER.error("Rule reload failed: %s", err)
            return
        engine.load_rules(new_rules)
        engine.evaluate_all()
        await _refresh_entities(hass, entry)

    hass.services.async_register(DOMAIN, SERVICE_FIRE, _fire_service, schema=FIRE_SERVICE_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_RELOAD, _reload_service)
