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

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, State
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from ._engine import Engine, RuleLoadError, load_rules
from ._engine.intent import Authority, Intent
from ._engine.yaml_loader import Rule

from .const import (
    ATTR_TARGET,
    ATTR_TICK_INTERVAL_MS,
    CONF_RULE_DIR,
    DEFAULT_RULE_DIR,
    DOMAIN,
    SERVICE_ACTIVATE_SCENE,
    SERVICE_FIRE,
    SERVICE_RELOAD,
    STORAGE_KEY,
    STORAGE_VERSION,
)
from .entity import IntentionalSummarySensor, IntentionalTargetSensor

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

ACTIVATE_SCENE_SCHEMA = vol.Schema(
    {
        vol.Required("rule_id"): cv.string,
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
        # and call `intentional.reload` to retry. We log the error.
        initial_rules = []
    except Exception as err:
        raise ConfigEntryNotReady(f"Failed to load rules: {err}") from err

    engine.load_rules(initial_rules)
    _LOGGER.info("Loaded %d rules from %s", len(initial_rules), rule_dir)

    # Make sure the rule directory exists. (We don't watch it for
    # changes — that's a v0.2 polish. The `intentional.reload` service
    # is the supported way to pick up rule edits for now.)
    rule_path = Path(rule_dir)
    if not rule_path.exists():
        try:
            rule_path.mkdir(parents=True, exist_ok=True)
        except OSError as err:
            _LOGGER.warning("Could not create rule directory %s: %s", rule_dir, err)

    # Set up platforms (sensors)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Set up tick loop — runs every 100ms, drives animations + TTL expiry
    tick_interval_ms = 100
    # Track which scenes we've already activated in this session, to avoid
    # firing scene.turn_on on every tick. Cleared when a scene rule stops
    # firing, so the next activation re-fires.
    _active_scenes: set[str] = set()

    async def _tick_loop() -> None:
        nonlocal _active_scenes
        while True:
            await asyncio.sleep(tick_interval_ms / 1000)
            # Drive the engine's evaluation cycle
            _sync_state_into_engine(hass, engine)
            engine.evaluate_all()
            engine.tick(tick_interval_ms)
            # Activate any newly-firing scene rules
            _active_scenes = await _activate_scene_rules(
                hass, engine, _active_scenes
            )
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


async def _activate_scene_rules(
    hass: HomeAssistant,
    engine: Engine,
    already_activated: set[str],
) -> set[str]:
    """Fire scene.turn_on for any newly-active scene rules.

    Returns the updated set of activated scenes. Scenes that have stopped
    firing (e.g. the trigger condition is no longer met) are removed
    from the set so they can be re-activated next time the rule fires.

    We track already-activated scenes to avoid firing scene.turn_on on
    every 100ms tick. A scene is idempotent in HA, but this is cleaner
    and shows up nicely in the log when the user is debugging.

    Transition from the rule is passed through to scene.turn_on as
    `transition`, which HA's scene service accepts.
    """
    active = set(engine.list_active_scene_intents())
    new_or_changed = active - already_activated
    no_longer_active = already_activated - active

    if not new_or_changed and not no_longer_active:
        # No change — return the same set (no-op for the caller)
        return already_activated

    # Build a lookup of (scene_id → intent) so we can pull transition/easing
    scene_intent_map = {
        scene: intent
        for intent, scene in engine.list_active_scene_intents(return_intents=True)
    }

    for scene_id in new_or_changed:
        intent = scene_intent_map.get(scene_id)
        if intent is None:
            continue
        # Transition is in ms; HA's scene.turn_on expects seconds
        transition_s = intent.transition_ms / 1000.0 if intent.transition_ms else None
        service_data: dict[str, Any] = {"entity_id": scene_id}
        if transition_s:
            service_data["transition"] = transition_s
        _LOGGER.info(
            "Activating scene %s (rule=%s, transition=%ss)",
            scene_id, intent.rule_id, transition_s,
        )
        try:
            await hass.services.async_call(
                "scene", "turn_on",
                service_data,
                blocking=False,
            )
        except Exception as err:  # noqa: BLE001 — log and continue
            _LOGGER.warning("Failed to activate scene %s: %s", scene_id, err)

    if no_longer_active:
        _LOGGER.info(
            "Scene rules deactivated: %s (will re-activate on next trigger)",
            sorted(no_longer_active),
        )

    return active


async def _refresh_entities(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Force the sensor platform to refresh by dispatching a custom event.

    The sensor entities listen for this event and call async_write_ha_state.
    This is the standard HA pattern for forcing entity refreshes from
    non-entity code (see the entity documentation on coordinator pattern).
    """
    hass.bus.async_fire(f"{DOMAIN}_refresh", {"entry_id": entry.entry_id})


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

    async def _activate_scene_service(call: ServiceCall) -> None:
        """Handle the `intentional.activate_scene` service call.

        Looks up a scene rule by ID and forces it to fire, regardless of
        its `when` condition. Honors the rule's transition and TTL.
        """
        rule_id = call.data["rule_id"]
        ttl_override = call.data.get("ttl", 0)

        parsed = engine._rules.get(rule_id)  # noqa: SLF001 (intentional access)
        if parsed is None:
            _LOGGER.error("No rule found with id %r", rule_id)
            return
        if parsed.rule.scene is None:
            _LOGGER.error(
                "Rule %r is not a scene rule (no `scene:` in emit)",
                rule_id,
            )
            return

        # Emit a user intent for this scene with the configured TTL
        ttl_ms = ttl_override * 1000 if ttl_override else parsed.rule.ttl_ms
        # Build an intent and append it directly (bypasses when evaluation)
        intent = Intent(
            target="",  # marks this as a scene intent
            ttl_ms=ttl_ms,
            authority=Authority.USER,
            rule_id=rule_id,
            reason="Manual activate_scene service",
            created_at_ms=engine.now_ms(),
        )
        engine._active_intents.append(intent)  # noqa: SLF001 (intentional access)
        # Re-evaluate so the activation path picks it up
        engine.evaluate_all()
        await _refresh_entities(hass, entry)
        _LOGGER.info(
            "Scene rule %r activated manually (scene=%s, ttl=%sms)",
            rule_id, parsed.rule.scene, ttl_ms,
        )

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
    hass.services.async_register(DOMAIN, SERVICE_ACTIVATE_SCENE, _activate_scene_service, schema=ACTIVATE_SCENE_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_RELOAD, _reload_service)
