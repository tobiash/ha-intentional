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
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, State
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from ._engine import Engine, RuleLoadError, __version__
from ._engine.ha_adapter import (
    clear_state_change_pulses,
    manual_set_from_service_data,
    pulse_state_change,
    scene_activation_plan,
    sync_state_object_into_engine,
    sync_time_context_into_engine,
)
from ._engine.reconciliation import Reconciliation, ReconciliationEvent
from ._engine.yaml_loader import Rule
from .const import (
    ATTR_TARGET,
    ATTR_TICK_INTERVAL_MS,
    CONF_RULE_DIR,
    DEFAULT_RULE_DIR,
    DOMAIN,
    SERVICE_ACTIVATE_SCENE,
    SERVICE_CLEAR,
    SERVICE_FIRE,
    SERVICE_RELOAD,
    STORAGE_KEY,
    STORAGE_VERSION,
)
from .diagnostics import (
    DiagnosticRateLimiter,
    record_diagnostic,
    record_intentional_context_ignored_for_drift,
)
from .rule_store import StorageRuleStore, rule_store_key
from .runtime_context import IntentionalContextTracker, new_intentional_context
from .sensor import IntentionalSummarySensor, IntentionalTargetSensor

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.SWITCH, Platform.BUTTON]
MANUAL_OVERRIDE_TTL_SECONDS = 7200
DRIFT_OVERRIDE_TTL_SECONDS = 300
DRIFT_CONFIRMATION_MS = 1_500
SERVICE_FAILURE_BACKOFF_MS = 30_000


class _HAAdapter:
    """Concrete HA adapter for the Reconciliation module."""

    def __init__(self, hass: HomeAssistant, context_tracker: IntentionalContextTracker) -> None:
        self._hass = hass
        self._context_tracker = context_tracker

    def get_state(self, entity_id: str) -> State | None:
        states = getattr(self._hass, "states", None)
        return states.get(entity_id) if states is not None else None

    async def async_call(
        self, domain: str, service: str, data: dict[str, Any], *, context: Any
    ) -> None:
        await self._hass.services.async_call(
            domain, service, data, blocking=False, context=context
        )

    def new_context(self) -> Any:
        return new_intentional_context(self._context_tracker)


def _record_reconciliation_events(
    hass: HomeAssistant,
    events: list[ReconciliationEvent],
    *,
    now_ms: int,
    rate_limiter: DiagnosticRateLimiter | None = None,
) -> None:
    """Translate reconciliation events into the in-memory diagnostic ring."""
    for event in events:
        if event.kind == "context_ignored":
            record_intentional_context_ignored_for_drift(
                hass,
                target=event.target,
                state=event.details.get("state"),
                now_ms=now_ms,
                rate_limiter=rate_limiter,
            )
        elif event.kind == "drift_promoted":
            record_diagnostic(
                hass, "drift_promoted", target=event.target,
                reason=event.details.get("reason", ""),
            )
        elif event.kind == "service_applied":
            record_diagnostic(hass, "service_applied", target=event.target, **event.details)
        elif event.kind == "service_skipped_matching_state":
            record_diagnostic(hass, "service_skipped_matching_state", target=event.target)
        elif event.kind == "service_skipped_pending_transition":
            record_diagnostic(
                hass, "service_skipped_pending_transition", target=event.target
            )
        elif event.kind == "service_failed":
            record_diagnostic(hass, "service_failed", target=event.target, **event.details)
        elif event.kind == "withdraw_cancelled_user_change":
            record_diagnostic(
                hass, "withdraw_cancelled_user_change", target=event.target
            )

# Declaring http as a dependency tells HA to ensure API helpers are available.
# The frontend panel is registered opportunistically when the frontend is loaded;
# it must not be a hard dependency because headless HA test installs often lack
# the separate hass_frontend package.
DEPENDENCIES = ["http"]
FRONTEND_URL_PATH = "/api/intentional/frontend"
PANEL_URL_PATH = "intentional"
FRONTEND_STATIC_REGISTERED = "frontend_static_registered"

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
        vol.Optional("name"): cv.string,
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
        vol.Optional("reverse"): cv.boolean,
        vol.Optional("update_action"): cv.string,
        vol.Optional("version"): cv.string,
        vol.Optional("backup"): cv.boolean,
        vol.Optional("mac"): cv.string,
        vol.Optional("dev_id"): cv.string,
        vol.Optional("host_name"): cv.string,
        vol.Optional("location_name"): cv.string,
        vol.Optional("gps"): vol.All([vol.Coerce(float)], vol.Length(min=2, max=2)),
        vol.Optional("gps_accuracy"): vol.All(vol.Coerce(float), vol.Range(min=0)),
        vol.Optional("battery"): vol.All(vol.Coerce(float), vol.Range(min=0, max=100)),
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

CLEAR_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_TARGET): cv.entity_id,
    }
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the integration from YAML (no-op; we use config entries)."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Intentional from a config entry."""
    rule_dir = entry.data.get(CONF_RULE_DIR, DEFAULT_RULE_DIR)

    engine = Engine(selector_resolver=lambda selector: _resolve_intent_selector(hass, selector))
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = engine
    rule_store = StorageRuleStore(hass, entry.entry_id)
    hass.data[DOMAIN][rule_store_key(entry.entry_id)] = rule_store

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

    # Initial rule load. Storage is the source of truth; existing YAML files are
    # imported once when the storage document does not exist yet.
    try:
        initial_rules = await rule_store.async_load_or_import(rule_dir)
    except RuleLoadError as err:
        _LOGGER.error("Could not load stored rules: %s", err)
        # Don't fail the whole integration — let the user fix the file
        # and call `intentional.reload` to retry. We log the error.
        initial_rules = []
    except Exception as err:
        raise ConfigEntryNotReady(f"Failed to load rules: {err}") from err

    store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    lifecycle_records = await store.async_load()

    engine.load_rules(initial_rules)
    if isinstance(lifecycle_records, dict):
        engine.import_lifecycle_records(lifecycle_records)
    _LOGGER.info("Loaded %d stored rule(s)", len(initial_rules))

    # Set up platforms (sensors)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Set up tick loop — runs every 100ms, drives animations + TTL expiry
    tick_interval_ms = 100
    # Track which scenes we've already activated in this session, to avoid
    # firing scene.turn_on on every tick. Cleared when a scene rule stops
    # firing, so the next activation re-fires.
    _active_scenes: set[str] = set()
    _intentional_contexts = IntentionalContextTracker()
    _diagnostic_rate_limiter = DiagnosticRateLimiter()
    _ha_adapter = _HAAdapter(hass, _intentional_contexts)
    _reconciler = Reconciliation(
        drift_override_ttl_ms=DRIFT_OVERRIDE_TTL_SECONDS * 1000,
        drift_confirmation_ms=DRIFT_CONFIRMATION_MS,
        service_failure_backoff_ms=SERVICE_FAILURE_BACKOFF_MS,
    )
    _reconciler.restore_pending_withdraws(
        lifecycle_records if isinstance(lifecycle_records, dict) else None,
        linger_rule_ids={rule.id for rule in initial_rules if rule.linger_ms},
        now_ms=engine.now_ms(),
    )
    _active_rule_ids: set[str] = set()
    # Track state-change pulses that should stay true through one apply cycle.
    _state_change_pulses: set[str] = set()
    # Event used to signal the tick loop to stop. We use an event rather
    # than ``while True:`` + ``task.cancel()`` because cancellation only
    # takes effect at the next ``await``; if a slow ``_refresh_entities``
    # is in progress, cancellation can be delayed indefinitely and
    # ``hass.async_block_till_done()`` would hang waiting for it. An
    # event lets the loop exit cleanly on the next tick.
    stop_event = asyncio.Event()

    async def _tick_loop() -> None:
        nonlocal _active_scenes, _active_rule_ids
        while not stop_event.is_set():
            # Sleep with a timeout so we can react to stop_event promptly.
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=tick_interval_ms / 1000)
                # If wait() returned normally, stop was requested.
                break
            except TimeoutError:
                pass  # normal tick interval
            # Drive the engine's evaluation cycle
            sync_time_context_into_engine(engine)
            _sync_state_into_engine(hass, engine)
            engine.evaluate_all()
            now_ms = _monotonic_ms()
            _active_rule_ids = _record_rule_activity_changes(
                hass,
                engine,
                _active_rule_ids,
            )
            engine.tick(tick_interval_ms)
            events = await _reconciler.tick(
                engine, _ha_adapter, _intentional_contexts, now_ms
            )
            _record_reconciliation_events(
                hass, events, now_ms=now_ms, rate_limiter=_diagnostic_rate_limiter
            )
            # Activate any newly-firing scene rules
            _active_scenes = await _activate_scene_rules(
                hass, engine, _active_scenes, _intentional_contexts
            )
            await _apply_pending_effects(hass, engine, _intentional_contexts)
            if _state_change_pulses:
                clear_state_change_pulses(engine, _state_change_pulses)
                _state_change_pulses.clear()
                engine.evaluate_all()
                _reconciler.drop_inactive_applied(set(engine.list_active_targets()))
            # Push resolved values to entities
            lifecycle_snapshot = engine.export_lifecycle_records()
            lifecycle_snapshot["pending_withdraws"] = _reconciler.export_pending_withdraws(
                engine
            )
            await store.async_save(lifecycle_snapshot)
            await _refresh_entities(hass, entry)

    if hasattr(hass, "async_create_background_task"):
        hass.async_create_background_task(_tick_loop(), name=f"{DOMAIN}_tick")
    else:
        hass.async_create_task(_tick_loop(), name=f"{DOMAIN}_tick")

    def _stop_tick_loop() -> None:
        """Signal the tick loop to stop on entry unload."""
        stop_event.set()

    entry.async_on_unload(_stop_tick_loop)

    # Set up services
    await _register_services(hass, engine, rule_store, entry)

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
        await _register_frontend_panel(hass)
        entry.async_on_unload(lambda: _remove_frontend_panel(hass))

    # Subscribe to state changes to keep engine in sync
    entry.async_on_unload(
        hass.bus.async_listen(
            "state_changed",
            _on_ha_state_change_factory(
                hass,
                engine,
                _reconciler,
                _intentional_contexts,
                _diagnostic_rate_limiter,
                _state_change_pulses,
            ),
        )
    )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload an Intentional config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        hass.data[DOMAIN].pop(rule_store_key(entry.entry_id), None)
    return unload_ok


async def _register_frontend_panel(hass: HomeAssistant) -> None:
    """Serve and register the Intentional rule editor panel."""
    from homeassistant.components import frontend, panel_custom

    if not frontend.async_panel_exists(hass, "profile"):
        _LOGGER.debug("Skipping Intentional panel registration; frontend is not loaded")
        return

    domain_data = hass.data.setdefault(DOMAIN, {})
    if not domain_data.get(FRONTEND_STATIC_REGISTERED):
        frontend_path = Path(__file__).parent / "frontend"
        await hass.http.async_register_static_paths([
            StaticPathConfig(FRONTEND_URL_PATH, str(frontend_path), True),
        ])
        domain_data[FRONTEND_STATIC_REGISTERED] = True

    if frontend.async_panel_exists(hass, PANEL_URL_PATH):
        frontend.async_remove_panel(hass, PANEL_URL_PATH, warn_if_unknown=False)

    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=PANEL_URL_PATH,
        webcomponent_name="intentional-panel",
        sidebar_title="Intentional",
        sidebar_icon="mdi:target",
        module_url=f"{FRONTEND_URL_PATH}/intentional-panel.js?v={__version__}",
        config={"domain": DOMAIN},
        require_admin=True,
        config_panel_domain=DOMAIN,
    )


def _remove_frontend_panel(hass: HomeAssistant) -> None:
    """Remove the Intentional rule editor panel on unload."""
    from homeassistant.components import frontend

    frontend.async_remove_panel(hass, PANEL_URL_PATH, warn_if_unknown=False)


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


def _resolve_intent_selector(hass: HomeAssistant, selector: Any) -> list[str]:
    """Resolve a VNext intent selector against current HA state/registry metadata."""
    registry = er.async_get(hass)
    matches: list[str] = []
    excluded = set(getattr(selector, "exclude", ()))
    for state in hass.states.async_all():
        entity_id = state.entity_id
        if entity_id in excluded:
            continue
        domain, _sep, _object_id = entity_id.partition(".")
        if selector.domain and domain != selector.domain:
            continue
        entry = registry.async_get(entity_id)
        if selector.area and (entry is None or entry.area_id != selector.area):
            continue
        labels = getattr(entry, "labels", set()) if entry is not None else set()
        if selector.label and selector.label not in labels:
            continue
        matches.append(entity_id)
    return matches


def _on_ha_state_change_factory(
    hass: HomeAssistant,
    engine: Engine,
    reconciler: Reconciliation,
    context_tracker: IntentionalContextTracker | None = None,
    diagnostic_rate_limiter: DiagnosticRateLimiter | None = None,
    state_change_pulses: set[str] | None = None,
):
    """Return a state_changed listener that pushes updates into the engine."""

    def _listener(event) -> None:
        old_state: State | None = event.data.get("old_state")
        new_state: State | None = event.data.get("new_state")
        if new_state is None:
            return
        sync_state_object_into_engine(engine, new_state)
        if state_change_pulses is not None and pulse_state_change(
            engine, old_state, new_state
        ):
            state_change_pulses.add(new_state.entity_id)
        now_ms = _monotonic_ms()
        events = reconciler.on_state_delta(engine, new_state, context_tracker, now_ms)
        if events:
            _record_reconciliation_events(
                hass, events, now_ms=now_ms, rate_limiter=diagnostic_rate_limiter
            )
        # Re-evaluate immediately for snappy response
        sync_time_context_into_engine(engine)
        engine.evaluate_all()

    return _listener


def _record_rule_activity_changes(
    hass: HomeAssistant,
    engine: Engine,
    previous_active: set[str],
) -> set[str]:
    """Record authored rule fire/withdraw transitions."""
    statuses = engine.list_authored_rule_statuses()
    active = {
        rule_id
        for rule_id, status in statuses.items()
        if status.get("active") or status.get("active_intent_count")
    }
    for rule_id in sorted(active - previous_active):
        record_diagnostic(
            hass,
            "rule_fired",
            rule_id=rule_id,
            targets=statuses.get(rule_id, {}).get("targets", []),
        )
    for rule_id in sorted(previous_active - active):
        record_diagnostic(hass, "rule_withdrawn", rule_id=rule_id)
    return active


async def _activate_scene_rules(
    hass: HomeAssistant,
    engine: Engine,
    already_activated: set[str],
    intentional_contexts: IntentionalContextTracker | None = None,
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
        context = new_intentional_context(intentional_contexts)
        _LOGGER.info(
            "Activating scene %s (transition=%ss)",
            scene_id, service_data.get("transition"),
        )
        try:
            await hass.services.async_call(
                domain, service,
                service_data,
                blocking=False,
                context=context,
            )
        except Exception as err:  # noqa: BLE001 — log and continue
            _LOGGER.warning("Failed to activate scene %s: %s", scene_id, err)

    if no_longer_active:
        _LOGGER.info(
            "Scene rules deactivated: %s (will re-activate on next trigger)",
            sorted(no_longer_active),
        )

    return active


async def _apply_pending_effects(
    hass: HomeAssistant,
    engine: Engine,
    intentional_contexts: IntentionalContextTracker | None = None,
) -> None:
    """Apply effect service calls that became active this cycle."""
    for rule_id, effect in engine.drain_pending_effects():
        service_data = {**effect.target, **effect.data}
        context = new_intentional_context(intentional_contexts)
        try:
            await hass.services.async_call(
                effect.domain,
                effect.service,
                service_data,
                blocking=False,
                context=context,
            )
            record_diagnostic(
                hass,
                "effect_applied",
                rule_id=rule_id,
                domain=effect.domain,
                service=effect.service,
                service_data=service_data,
            )
        except Exception as err:  # noqa: BLE001
            record_diagnostic(
                hass,
                "effect_failed",
                rule_id=rule_id,
                domain=effect.domain,
                service=effect.service,
                service_data=service_data,
                error=str(err),
            )
            _LOGGER.warning(
                "Failed to apply effect from rule %s via %s.%s: %s",
                rule_id,
                effect.domain,
                effect.service,
                err,
            )


def _monotonic_ms() -> int:
    return int(time.monotonic() * 1000)


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
    rule_store: StorageRuleStore,
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

    async def _clear_service(call: ServiceCall) -> None:
        """Handle the `intentional.clear` service call."""
        target = call.data.get(ATTR_TARGET)
        removed = engine.clear_user_intents(target=target)
        engine.evaluate_all()
        await _refresh_entities(hass, entry)
        _LOGGER.info(
            "Cleared %s manual intent(s)%s",
            removed,
            f" for {target}" if target else "",
        )

    async def _reload_service(_call: ServiceCall) -> None:
        """Handle the `intentional.reload` service call.

        Re-reads stored rules. Same effect as saving through the YAML editor
        or HTTP API.
        """
        try:
            new_rules = await rule_store.async_rules()
        except RuleLoadError as err:
            _LOGGER.error("Rule reload failed: %s", err)
            return
        engine.load_rules(new_rules)
        sync_time_context_into_engine(engine)
        engine.evaluate_all()
        await _refresh_entities(hass, entry)

    hass.services.async_register(DOMAIN, SERVICE_FIRE, _fire_service, schema=FIRE_SERVICE_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_CLEAR, _clear_service, schema=CLEAR_SERVICE_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_ACTIVATE_SCENE, _activate_scene_service, schema=ACTIVATE_SCENE_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_RELOAD, _reload_service)
