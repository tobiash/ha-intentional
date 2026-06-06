"""The Intentional integration.

Sets up the intent engine, loads rules from the configured directory,
wires Home Assistant state changes into the engine, and exposes services
for manual control.

Architecture:
- IntentionalEngine: wraps the pure-Python Engine, bridges HA ↔ intents
- IntentionalSummarySensor / IntentionalTargetSensor (sensor.py)
- Service handlers: fire, reload
"""

from __future__ import annotations

import asyncio
import logging
import shutil
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
from ._engine.ha_adapter import (
    ServicePlanSignature,
    clear_event_trigger_pulses,
    emit_manual_override_for_state_drift,
    manual_set_from_service_data,
    pulse_event_state_change,
    scene_activation_plan,
    service_calls_for_resolved_target,
    service_plan_signature,
    sync_state_object_into_engine,
    sync_time_context_into_engine,
)
from ._engine.yaml_loader import Rule, RuleDirFingerprint, rule_dir_fingerprint
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
from .sensor import IntentionalSummarySensor, IntentionalTargetSensor

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]
MANUAL_OVERRIDE_TTL_SECONDS = 7200
RULE_RELOAD_POLL_INTERVAL_MS = 2_000

# Declaring http as a dependency tells HA to ensure hass.http is
# available before this integration's setup runs. This is the
# official HA pattern for integrations that register HTTP views.
# The register_api call is additionally guarded so the integration
# still works in headless setups where http might not be loaded.
DEPENDENCIES = ["http"]

# Service schemas
FIRE_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_TARGET): cv.entity_id,
        vol.Optional("state"): cv.string,
        vol.Optional("brightness_pct"): vol.All(int, vol.Range(min=0, max=100)),
        vol.Optional("brightness"): vol.All(int, vol.Range(min=0, max=255)),
        vol.Optional("color_temp_k"): vol.All(int, vol.Range(min=1000, max=10000)),
        vol.Optional("color_temp_mired"): vol.All(int, vol.Range(min=50, max=500)),
        vol.Optional("rgb_color"): vol.All([vol.All(int, vol.Range(min=0, max=255))], vol.Length(min=3, max=3)),
        vol.Optional("rgbw_color"): vol.All([vol.All(int, vol.Range(min=0, max=255))], vol.Length(min=4, max=4)),
        vol.Optional("rgbww_color"): vol.All([vol.All(int, vol.Range(min=0, max=255))], vol.Length(min=5, max=5)),
        vol.Optional("hs_color"): vol.All([vol.Coerce(float)], vol.Length(min=2, max=2)),
        vol.Optional("xy_color"): vol.All([vol.Coerce(float)], vol.Length(min=2, max=2)),
        vol.Optional("effect"): cv.string,
        vol.Optional("flash"): cv.string,
        vol.Optional("volume_level"): vol.All(vol.Coerce(float), vol.Range(min=0, max=1)),
        vol.Optional("is_volume_muted"): cv.boolean,
        vol.Optional("tone"): cv.string,
        vol.Optional("source"): cv.string,
        vol.Optional("sound_mode"): cv.string,
        vol.Optional("media_action"): cv.string,
        vol.Optional("media_content_id"): cv.string,
        vol.Optional("media_content_type"): cv.string,
        vol.Optional("enqueue"): cv.string,
        vol.Optional("announce"): cv.boolean,
        vol.Optional("extra"): dict,
        vol.Optional("shuffle"): cv.boolean,
        vol.Optional("repeat"): cv.string,
        vol.Optional("seek_position"): vol.Coerce(float),
        vol.Optional("group_members"): vol.Any(cv.entity_ids, [cv.entity_id]),
        vol.Optional("position"): vol.All(int, vol.Range(min=0, max=100)),
        vol.Optional("tilt_position"): vol.All(int, vol.Range(min=0, max=100)),
        vol.Optional("percentage"): vol.All(int, vol.Range(min=0, max=100)),
        vol.Optional("hvac_mode"): cv.string,
        vol.Optional("temperature"): vol.Coerce(float),
        vol.Optional("target_temp_low"): vol.Coerce(float),
        vol.Optional("target_temp_high"): vol.Coerce(float),
        vol.Optional("preset_mode"): cv.string,
        vol.Optional("fan_mode"): cv.string,
        vol.Optional("direction"): cv.string,
        vol.Optional("oscillating"): cv.boolean,
        vol.Optional("humidity"): vol.All(int, vol.Range(min=0, max=100)),
        vol.Optional("swing_mode"): cv.string,
        vol.Optional("swing_horizontal_mode"): cv.string,
        vol.Optional("aux_heat"): cv.boolean,
        vol.Optional("mode"): cv.string,
        vol.Optional("operation_mode"): cv.string,
        vol.Optional("away_mode"): cv.boolean,
        vol.Optional("fan_speed"): cv.string,
        vol.Optional("camera_action"): cv.string,
        vol.Optional("filename"): cv.string,
        vol.Optional("media_player"): cv.entity_id,
        vol.Optional("format"): cv.string,
        vol.Optional("lookback"): vol.All(int, vol.Range(min=0)),
        vol.Optional("command"): vol.Any(cv.string, [cv.string]),
        vol.Optional("params"): dict,
        vol.Optional("cleaning_area_id"): vol.Any(cv.string, [cv.string]),
        vol.Optional("activity"): cv.string,
        vol.Optional("device"): cv.string,
        vol.Optional("num_repeats"): vol.All(int, vol.Range(min=1)),
        vol.Optional("delay_secs"): vol.All(vol.Coerce(float), vol.Range(min=0)),
        vol.Optional("hold_secs"): vol.All(vol.Coerce(float), vol.Range(min=0)),
        vol.Optional("value"): vol.Any(vol.Coerce(float), cv.string),
        vol.Optional("option"): cv.string,
        vol.Optional("cycle"): cv.boolean,
        vol.Optional("code"): cv.string,
        vol.Optional("message"): cv.string,
        vol.Optional("title"): cv.string,
        vol.Optional("data"): dict,
        vol.Optional("service"): cv.string,
        vol.Optional("service_data"): dict,
        vol.Optional("media_player_entity_id"): cv.entity_id,
        vol.Optional("cache"): cv.boolean,
        vol.Optional("language"): cv.string,
        vol.Optional("options"): dict,
        vol.Optional("browser_id"): vol.Any(cv.string, [cv.string]),
        vol.Optional("user_id"): vol.Any(cv.string, [cv.string]),
        vol.Optional("path"): cv.string,
        vol.Optional("action_text"): cv.string,
        vol.Optional("action"): dict,
        vol.Optional("parse_mode"): cv.string,
        vol.Optional("disable_notification"): cv.boolean,
        vol.Optional("disable_web_page_preview"): cv.boolean,
        vol.Optional("keyboard"): vol.Any(cv.string, [cv.string]),
        vol.Optional("inline_keyboard"): vol.Any(cv.string, list),
        vol.Optional("message_tag"): cv.string,
        vol.Optional("chat_id"): vol.Any(cv.string, [cv.string]),
        vol.Optional("todo_action"): cv.string,
        vol.Optional("item"): cv.string,
        vol.Optional("rename"): cv.string,
        vol.Optional("status"): vol.Any(cv.string, [cv.string]),
        vol.Optional("due_date"): cv.string,
        vol.Optional("due_datetime"): cv.string,
        vol.Optional("description"): cv.string,
        vol.Optional("variables"): dict,
        vol.Optional("skip_condition"): cv.boolean,
        vol.Optional("datetime"): cv.string,
        vol.Optional("date"): cv.string,
        vol.Optional("time"): cv.string,
        vol.Optional("timestamp"): vol.Coerce(float),
        vol.Optional("duration"): cv.string,
        vol.Optional("update_action"): cv.string,
        vol.Optional("version"): cv.string,
        vol.Optional("backup"): cv.boolean,
        vol.Optional("update_entity"): cv.boolean,
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

    # Make sure the rule directory exists before we try to load from it.
    # On first install, /config/intentional/rules doesn't exist yet —
    # we'd rather create it and start with zero rules than log an error
    # every restart.
    rule_path = Path(rule_dir)
    if not rule_path.exists():
        try:
            rule_path.mkdir(parents=True, exist_ok=True)
            _LOGGER.info("Created rule directory %s", rule_dir)
        except OSError as err:
            _LOGGER.warning("Could not create rule directory %s: %s", rule_dir, err)

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

    # Copy the starter rule pack on first install (zero rules loaded).
    # This makes the integration useful out of the box — the user sees
    # the summary sensor flip to >0 intents the moment setup completes.
    if not initial_rules:
        copied = await _maybe_install_starter_rules(hass, rule_dir)
        if copied:
            try:
                initial_rules = await hass.async_add_executor_job(load_rules, rule_dir)
            except RuleLoadError as err:
                _LOGGER.error("Could not load starter rules from %s: %s", rule_dir, err)

    engine.load_rules(initial_rules)
    _LOGGER.info("Loaded %d rules from %s", len(initial_rules), rule_dir)

    rule_fingerprint = await hass.async_add_executor_job(
        rule_dir_fingerprint,
        rule_dir,
    )
    next_rule_check_ms = engine.now_ms() + RULE_RELOAD_POLL_INTERVAL_MS

    # Set up platforms (sensors)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Set up tick loop — runs every 100ms, drives animations + TTL expiry
    tick_interval_ms = 100
    # Track which scenes we've already activated in this session, to avoid
    # firing scene.turn_on on every tick. Cleared when a scene rule stops
    # firing, so the next activation re-fires.
    _active_scenes: set[str] = set()
    # Track resolved target payloads we've already applied. The tick loop runs
    # frequently, so identical resolved values should not produce repeated HA
    # service calls.
    _last_applied_targets: dict[str, ServicePlanSignature] = {}
    # Track event entity pulses that should stay true through one apply cycle.
    _event_pulses: set[str] = set()
    # Event used to signal the tick loop to stop. We use an event rather
    # than ``while True:`` + ``task.cancel()`` because cancellation only
    # takes effect at the next ``await``; if a slow ``_refresh_entities``
    # is in progress, cancellation can be delayed indefinitely and
    # ``hass.async_block_till_done()`` would hang waiting for it. An
    # event lets the loop exit cleanly on the next tick.
    stop_event = asyncio.Event()

    async def _tick_loop() -> None:
        nonlocal _active_scenes, next_rule_check_ms, rule_fingerprint
        while not stop_event.is_set():
            # Sleep with a timeout so we can react to stop_event promptly.
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=tick_interval_ms / 1000)
                # If wait() returned normally, stop was requested.
                break
            except TimeoutError:
                pass  # normal tick interval
            # Drive the engine's evaluation cycle
            if engine.now_ms() >= next_rule_check_ms:
                rule_fingerprint = await _maybe_reload_rules_for_changed_files(
                    hass,
                    engine,
                    rule_dir,
                    rule_fingerprint,
                )
                next_rule_check_ms = engine.now_ms() + RULE_RELOAD_POLL_INTERVAL_MS
            sync_time_context_into_engine(engine)
            _sync_state_into_engine(hass, engine)
            engine.evaluate_all()
            engine.tick(tick_interval_ms)
            # Activate any newly-firing scene rules
            _active_scenes = await _activate_scene_rules(
                hass, engine, _active_scenes
            )
            # Apply resolved target intents to HA entities.
            await _apply_resolved_targets(hass, engine, _last_applied_targets)
            if _event_pulses:
                clear_event_trigger_pulses(engine, _event_pulses)
                _event_pulses.clear()
                engine.evaluate_all()
                for stale_target in set(_last_applied_targets) - set(
                    engine.list_active_targets()
                ):
                    _last_applied_targets.pop(stale_target, None)
            # Push resolved values to entities
            await _refresh_entities(hass, entry)

    hass.async_create_task(_tick_loop(), name=f"{DOMAIN}_tick")

    def _stop_tick_loop() -> None:
        """Signal the tick loop to stop on entry unload."""
        stop_event.set()

    entry.async_on_unload(_stop_tick_loop)

    # Set up services
    async def _set_rule_fingerprint(value: RuleDirFingerprint) -> None:
        nonlocal rule_fingerprint
        rule_fingerprint = value

    await _register_services(hass, engine, rule_dir, entry, _set_rule_fingerprint)

    # Register HTTP API views. Guarded for test environments where the
    # http component may not be loaded (e.g. tests that don't exercise
    # the HTTP surface). In production, HA loads the http component
    # automatically for any install with a web UI, so ``hass.http`` is
    # always available there. We deliberately do not declare a hard
    # dependency on http (see comment near PLATFORMS) so the integration
    # still installs and works in headless setups.
    if getattr(hass, "http", None) is not None:
        from .api import register_api
        register_api(hass)

    # Subscribe to state changes to keep engine in sync
    entry.async_on_unload(
        hass.bus.async_listen(
            "state_changed",
            _on_ha_state_change_factory(
                hass,
                engine,
                _last_applied_targets,
                _event_pulses,
            ),
        )
    )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload an Intentional config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok


async def _maybe_install_starter_rules(hass: HomeAssistant, rule_dir: str) -> int:
    """Copy the starter rule pack to ``rule_dir`` on first install.

    Looks for ``starter_rules/welcome.yaml`` packaged alongside this
    integration and copies it to the user's rule directory. This makes
    the integration useful out of the box — without this, a user who
    just installed via HACS would see an empty engine until they wrote
    a rule file by hand.

    The starter rules are intentionally conservative:
    - They don't override manual actions (authority: automation, low confidence)
    - They target entities that exist on most HA installs (lights)
    - They include detailed comments explaining the schema

    Only runs on first install (when the rule directory is empty).
    On subsequent runs, we don't touch the user's rules.
    """
    # Locate the bundled starter pack
    integration_dir = Path(__file__).parent
    starter_source = integration_dir / "starter_rules"
    if not starter_source.exists():
        _LOGGER.debug("No starter rule pack found at %s", starter_source)
        return 0

    rule_path = Path(rule_dir)
    try:
        # Glob + copy is filesystem I/O — must run in the executor,
        # not in the event loop. Earlier versions only wrapped the
        # shutil.copy2() in the executor but called the synchronous
        # .glob() inline, which blocks the event loop on every entry
        # load. HA logged:
        #   Detected blocking call to scandir with args
        #   ('/config/custom_components/intentional/starter_rules/',)
        # and the subsequent bootstrap timed out waiting on the
        # intentional_tick task. See CHANGELOG v0.3.3.
        def _copy_starter_pack() -> int:
            """Sync helper: glob the starter pack, copy any that don't exist."""
            count = 0
            for starter_file in starter_source.glob("*.yaml"):
                dest = rule_path / starter_file.name
                if dest.exists():
                    # Don't clobber — user may have edited it
                    continue
                shutil.copy2(starter_file, dest)
                count += 1
                _LOGGER.info("Installed starter rule: %s", dest)
            return count

        copied = await hass.async_add_executor_job(_copy_starter_pack)
        if copied:
            _LOGGER.info(
                "Installed %d starter rule(s) to %s. "
                "Edit them to match your home, or call `intentional.reload` "
                "to see them in action.",
                copied, rule_dir,
            )
        return copied
    except OSError as err:
        _LOGGER.warning("Could not install starter rules: %s", err)
        return 0


async def _maybe_reload_rules_for_changed_files(
    hass: HomeAssistant,
    engine: Engine,
    rule_dir: str,
    previous_fingerprint: RuleDirFingerprint,
) -> RuleDirFingerprint:
    """Reload rules when the rule directory fingerprint changes."""
    current_fingerprint = await hass.async_add_executor_job(
        rule_dir_fingerprint,
        rule_dir,
    )
    if current_fingerprint == previous_fingerprint:
        return previous_fingerprint

    try:
        new_rules = await hass.async_add_executor_job(load_rules, rule_dir)
    except RuleLoadError as err:
        _LOGGER.error("Rule auto-reload failed: %s", err)
        return previous_fingerprint

    engine.load_rules(new_rules)
    sync_time_context_into_engine(engine)
    engine.evaluate_all()
    _LOGGER.info("Auto-reloaded %d rules from %s", len(new_rules), rule_dir)
    return current_fingerprint


def _sync_state_into_engine(hass: HomeAssistant, engine: Engine) -> None:
    """Pull a snapshot of HA state into the engine.

    The engine only tracks entities that rules reference; this is a
    best-effort sync of the entities we know about.
    """
    # In a real implementation, we'd track the entity_ids referenced
    # by rules. For now, we sync ALL light.* and sensor.* states —
    # cheap, since most homes have a few hundred at most.
    for state in hass.states.async_all():
        sync_state_object_into_engine(engine, state)


def _on_ha_state_change_factory(
    hass: HomeAssistant,
    engine: Engine,
    last_applied: dict[str, ServicePlanSignature] | None = None,
    event_pulses: set[str] | None = None,
):
    """Return a state_changed listener that pushes updates into the engine."""

    def _listener(event) -> None:
        old_state: State | None = event.data.get("old_state")
        new_state: State | None = event.data.get("new_state")
        if new_state is None:
            return
        sync_state_object_into_engine(engine, new_state)
        if event_pulses is not None and pulse_event_state_change(
            engine, old_state, new_state
        ):
            event_pulses.add(new_state.entity_id)
        if last_applied is not None:
            emit_manual_override_for_state_drift(
                engine,
                last_applied,
                new_state,
                ttl_ms=MANUAL_OVERRIDE_TTL_SECONDS * 1000,
            )
        # Re-evaluate immediately for snappy response
        sync_time_context_into_engine(engine)
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
    calls, active, no_longer_active = scene_activation_plan(engine, already_activated)

    if not calls and not no_longer_active:
        return already_activated

    for domain, service, service_data in calls:
        scene_id = service_data["entity_id"]
        _LOGGER.info(
            "Activating scene %s (transition=%ss)",
            scene_id, service_data.get("transition"),
        )
        try:
            await hass.services.async_call(
                domain, service,
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


async def _apply_resolved_targets(
    hass: HomeAssistant,
    engine: Engine,
    last_applied: dict[str, ServicePlanSignature],
) -> None:
    """Apply resolved target intents to HA entities via service calls."""
    active_targets = set(engine.list_active_targets())
    for target in sorted(active_targets):
        resolved = engine.resolve(target)
        if resolved is None:
            last_applied.pop(target, None)
            continue
        calls = service_calls_for_resolved_target(
            target,
            dict(resolved.value),
            transition_ms=resolved.transition_ms,
        )
        if not calls:
            continue
        signature = service_plan_signature(calls)
        if last_applied.get(target) == signature:
            continue
        for domain, service, service_data in calls:
            try:
                await hass.services.async_call(
                    domain,
                    service,
                    service_data,
                    blocking=False,
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning(
                    "Failed to apply resolved intent to %s via %s.%s: %s",
                    target, domain, service, err,
                )
                break
        else:
            last_applied[target] = signature

    for stale_target in set(last_applied) - active_targets:
        last_applied.pop(stale_target, None)


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
    set_rule_fingerprint,
) -> None:
    """Register the fire and reload services."""

    async def _fire_service(call: ServiceCall) -> None:
        """Handle the `intentional.fire` service call.

        Creates a USER-authority intent for the target entity with the
        given set/ttl. This is the recommended way to inject manual
        control from automations or scripts.
        """
        target = call.data[ATTR_TARGET]
        set_dict = manual_set_from_service_data(dict(call.data))
        ttl_seconds = call.data.get("ttl", MANUAL_OVERRIDE_TTL_SECONDS)
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

        intent = engine.activate_scene_rule(
            rule_id,
            ttl_ms=ttl_override * 1000 if ttl_override else None,
        )
        if intent is None:
            _LOGGER.error("No scene rule found with id %r", rule_id)
            return
        # Re-evaluate so the activation path picks it up
        engine.evaluate_all()
        await _refresh_entities(hass, entry)
        _LOGGER.info(
            "Scene rule %r activated manually (ttl=%sms)",
            rule_id, intent.ttl_ms,
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
        sync_time_context_into_engine(engine)
        engine.evaluate_all()
        await set_rule_fingerprint(
            await hass.async_add_executor_job(rule_dir_fingerprint, rule_dir)
        )
        await _refresh_entities(hass, entry)

    hass.services.async_register(DOMAIN, SERVICE_FIRE, _fire_service, schema=FIRE_SERVICE_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_ACTIVATE_SCENE, _activate_scene_service, schema=ACTIVATE_SCENE_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_RELOAD, _reload_service)
