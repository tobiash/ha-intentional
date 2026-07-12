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
import contextlib
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, State, callback
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
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
from ._engine.reconciliation import Reconciliation, ReconciliationEvent, reconciliation_key
from ._engine.runtime import StateChangePulseQueue, TickRuntime, monotonic_ms, runtime_key
from ._engine.when_parser import referenced_entities
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
from .document_validation import load_and_preflight_document
from .lifecycle_writer import LifecycleWriter, lifecycle_writer_key
from .publication import EntityPublication, publication_key
from .rule_mutation import RuleMutationCoordinator, mutation_coordinator_key
from .rule_store import StorageRuleStore, rule_store_key
from .runtime_context import IntentionalContextTracker, new_intentional_context
from .selector_ingest import MembershipChange, SelectorMembershipPlanner
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
        await self._hass.services.async_call(domain, service, data, blocking=False, context=context)

    def new_context(self) -> Any:
        return new_intentional_context(self._context_tracker)


def _apply_reconciliation_events(
    hass: HomeAssistant,
    engine: Engine,
    events: list[ReconciliationEvent],
    *,
    now_ms: int,
    rate_limiter: DiagnosticRateLimiter | None = None,
) -> None:
    """Apply reconciliation events (overrides) and record diagnostics."""
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
            engine.emit_user_intent(
                target=event.details["target"],
                set=event.details["set"],
                ttl_ms=event.details["ttl_ms"],
                reason=event.details["reason"],
            )
            record_diagnostic(
                hass,
                "drift_promoted",
                target=event.target,
                reason=event.details.get("reason", ""),
            )
        elif event.kind == "service_applied":
            record_diagnostic(hass, "service_applied", target=event.target, **event.details)
        elif event.kind == "service_skipped_matching_state":
            record_diagnostic(hass, "service_skipped_matching_state", target=event.target)
        elif event.kind == "service_skipped_pending_transition":
            record_diagnostic(hass, "service_skipped_pending_transition", target=event.target)
        elif event.kind == "service_failed":
            record_diagnostic(hass, "service_failed", target=event.target, **event.details)
        elif event.kind in {
            "service_retry_scheduled",
            "service_retry_recovered",
            "service_denied_target_policy",
            "service_target_policy_recovered",
        }:
            diagnostic_key = (
                event.kind,
                event.target,
                str(event.details.get("code", "")),
            )
            if rate_limiter is None or rate_limiter.allow(diagnostic_key, now_ms=now_ms):
                record_diagnostic(hass, event.kind, target=event.target, **event.details)
        elif event.kind == "withdraw_cancelled_user_change":
            record_diagnostic(hass, "withdraw_cancelled_user_change", target=event.target)


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
        vol.Optional("rgb_color"): vol.All(
            [vol.All(int, vol.Range(min=0, max=255))], vol.Length(min=3, max=3)
        ),
        vol.Optional("rgbw_color"): vol.All(
            [vol.All(int, vol.Range(min=0, max=255))], vol.Length(min=4, max=4)
        ),
        vol.Optional("rgbww_color"): vol.All(
            [vol.All(int, vol.Range(min=0, max=255))], vol.Length(min=5, max=5)
        ),
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
    domain_data = hass.data.setdefault(DOMAIN, {})
    if any(
        loaded_entry.entry_id != entry.entry_id and loaded_entry.entry_id in domain_data
        for loaded_entry in hass.config_entries.async_entries(DOMAIN)
    ):
        raise ConfigEntryError("Intentional supports only one config entry")

    rule_dir = entry.data.get(CONF_RULE_DIR, DEFAULT_RULE_DIR)

    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)

    def _entry_area(entry: Any) -> str | None:
        if entry.area_id is not None:
            return entry.area_id
        if entry.device_id is None:
            return None
        device = device_registry.async_get(entry.device_id)
        return device.area_id if device is not None else None

    selector_planner = SelectorMembershipPlanner(
        lambda: entity_registry.entities.values(),
        area_for_entry=_entry_area,
        state_entity_ids=lambda: (state.entity_id for state in hass.states.async_all()),
        state_metadata=lambda entity_id: (
            dict(state.attributes) if (state := hass.states.get(entity_id)) is not None else None
        ),
    )
    engine = Engine(selector_resolver=selector_planner.resolve)
    domain_data[entry.entry_id] = engine
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

    store: Store = Store(hass, STORAGE_VERSION, f"{STORAGE_KEY}_{entry.entry_id}")
    lifecycle_records = await store.async_load()
    if lifecycle_records is None:
        legacy_store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        lifecycle_records = await legacy_store.async_load()
        if isinstance(lifecycle_records, dict):
            # Claim the legacy snapshot in the entry-specific store. A later
            # empty/corrupt entry store must never repeatedly resurrect it.
            await store.async_save(lifecycle_records)
        else:
            # An empty entry-specific record is also a migration marker.
            lifecycle_records = {}
            await store.async_save(lifecycle_records)

    engine.load_rules(initial_rules)
    if isinstance(lifecycle_records, dict):
        engine.import_lifecycle_records(lifecycle_records)
    publication = EntityPublication(hass, entry.entry_id, engine, rule_store)
    hass.data[DOMAIN][publication_key(entry.entry_id)] = publication
    publication.publish_if_changed()
    _LOGGER.info("Loaded %d stored rule(s)", len(initial_rules))

    # Set up platforms (sensors)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Set up tick loop — runs every 100ms, drives animations + TTL expiry
    tick_interval_ms = 100
    # Runtime owns tick-local state such as scene activations, rule activity,
    # state-change pulses, and reconciliation-loop liveness.
    _intentional_contexts = IntentionalContextTracker()
    _diagnostic_rate_limiter = DiagnosticRateLimiter()
    _ha_adapter = _HAAdapter(hass, _intentional_contexts)
    _reconciler = Reconciliation(
        drift_override_ttl_ms=DRIFT_OVERRIDE_TTL_SECONDS * 1000,
        drift_confirmation_ms=DRIFT_CONFIRMATION_MS,
        service_failure_backoff_ms=SERVICE_FAILURE_BACKOFF_MS,
    )
    hass.data[DOMAIN][reconciliation_key(entry.entry_id)] = _reconciler
    _reconciler.restore_pending_withdraws(
        lifecycle_records if isinstance(lifecycle_records, dict) else None,
        linger_rule_ids={rule.id for rule in initial_rules if rule.linger_ms},
        now_ms=engine.now_ms(),
    )
    selector_planner.configure(initial_rules, referenced_entities(initial_rules))
    selector_planner.update_owned(
        set(engine.list_active_targets()) | set(_reconciler.pending_withdraw_targets())
    )
    _sync_state_into_engine(hass, engine, entity_ids=set(selector_planner.relevant))
    _runtime = TickRuntime(tick_interval_ms=tick_interval_ms)
    hass.data[DOMAIN][runtime_key(entry.entry_id)] = _runtime
    # Event used to signal the tick loop to stop. We use an event rather
    # than ``while True:`` + ``task.cancel()`` because cancellation only
    # takes effect at the next ``await``; if a slow tick operation is in
    # progress, cancellation can be delayed indefinitely and
    # ``hass.async_block_till_done()`` would hang waiting for it. An
    # event lets the loop exit cleanly on the next tick.
    stop_event = _runtime.stop_event

    def _lifecycle_snapshot() -> dict[str, Any]:
        snapshot = engine.export_lifecycle_records()
        snapshot["pending_withdraws"] = _reconciler.export_pending_withdraws(engine)
        return snapshot

    lifecycle_writer = LifecycleWriter(
        store,
        _lifecycle_snapshot,
        durable_snapshot=lifecycle_records if isinstance(lifecycle_records, dict) else None,
    )
    hass.data[DOMAIN][lifecycle_writer_key(entry.entry_id)] = lifecycle_writer

    def _prepare_rules(contents: str) -> tuple[list[Rule], set[str]]:
        try:
            new_rules, _findings = load_and_preflight_document(contents)
            engine.validate_rules(new_rules)
        except Exception as err:
            raise HomeAssistantError(f"Rule reload failed: {err}") from err
        return new_rules, referenced_entities(new_rules)

    async def _commit_rules(prepared: tuple[list[Rule], set[str]]) -> None:
        new_rules, new_referenced = prepared
        async with _runtime.mutation_lock:
            old_fingerprints = engine.rule_fingerprints()
            new_fingerprints = {rule.id: engine.rule_fingerprint(rule) for rule in new_rules}
            unchanged_ids = {
                rule_id for rule_id, fingerprint in new_fingerprints.items()
                if old_fingerprints.get(rule_id) == fingerprint
            }
            engine.load_rules(new_rules)
            _apply_membership_change(hass, engine, selector_planner.configure(new_rules, new_referenced))
            sync_time_context_into_engine(engine)
            engine.evaluate_all()
            _runtime.active_rule_ids.intersection_update(unchanged_ids)
            _runtime.active_scenes.intersection_update({
                rule.scene for rule in new_rules
                if rule.id in unchanged_ids and rule.scene is not None
            })
            retained_targets = {
                rule.target for rule in new_rules if rule.id in unchanged_ids and rule.target
            }
            retained_targets.update(
                target for rule in new_rules if rule.id in unchanged_ids
                for selector in rule.intent_selectors for target in selector_planner.resolve(selector)
            )
            _reconciler.retain_targets(retained_targets)
            _runtime.advance_revision()
            lifecycle_writer.mutated()
        publication.publish_if_changed()

    mutation_coordinator = RuleMutationCoordinator(rule_store, _prepare_rules, _commit_rules)
    hass.data[DOMAIN][mutation_coordinator_key(entry.entry_id)] = mutation_coordinator

    async def _tick_loop() -> None:
        try:
            while not stop_event.is_set():
                # Sleep with a timeout so we can react to stop_event promptly.
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=tick_interval_ms / 1000)
                    # If wait() returned normally, stop was requested.
                    break
                except TimeoutError:
                    pass  # normal tick interval
                if _runtime.unloading:
                    continue
                async with _runtime.mutation_lock:
                    if stop_event.is_set():
                        _runtime.tick_idle.set()
                        break
                    if _runtime.unloading:
                        _runtime.tick_idle.set()
                        continue
                    _runtime.tick_idle.clear()
                    pulse_drain = _runtime.pulses.begin_drain()
                    try:
                        # Drive the engine's evaluation cycle
                        sync_time_context_into_engine(engine)
                        _apply_membership_change(
                            hass,
                            engine,
                            selector_planner.update_owned(
                                set(engine.list_active_targets())
                                | set(_reconciler.pending_withdraw_targets())
                            ),
                        )
                        engine.evaluate_all()
                        now_ms = _monotonic_ms()
                        _runtime.active_rule_ids = _record_rule_activity_changes(
                            hass,
                            engine,
                            _runtime.active_rule_ids,
                        )
                        engine.tick(tick_interval_ms)
                        tick_revision = _runtime.advance_revision()
                        lifecycle_writer.mutated()
                    except Exception as err:  # noqa: BLE001
                        now_ms = _monotonic_ms()
                        _runtime.mark_failure(err, now_ms=now_ms)
                        if _runtime.should_report_failure(now_ms=now_ms):
                            _LOGGER.exception("Intentional tick failed; continuing")
                            record_diagnostic(hass, "tick_failed", error=str(err))
                        _runtime.tick_idle.set()
                        continue
                # Effect activation is not dispatchable until its outbox
                # snapshot has crossed the lifecycle storage boundary.
                await lifecycle_writer.async_flush()
                if lifecycle_writer.current_error is not None:
                    _runtime.tick_idle.set()
                    continue
                try:
                    events = await _reconciler.tick(
                        engine,
                        _ha_adapter,
                        _intentional_contexts,
                        now_ms,
                        revision_is_current=lambda revision=tick_revision: _runtime.is_revision(
                            revision
                        ),
                    )
                    async with _runtime.mutation_lock:
                        if not _runtime.is_revision(tick_revision):
                            _runtime.tick_idle.set()
                            continue
                        _apply_reconciliation_events(
                            hass,
                            engine,
                            events,
                            now_ms=now_ms,
                            rate_limiter=_diagnostic_rate_limiter,
                        )
                        # Activate any newly-firing scene rules
                        previous_scenes = set(_runtime.active_scenes)
                    active_scenes = await _activate_scenes_and_dispatch_effects(
                        hass,
                        engine,
                        _intentional_contexts,
                        _runtime,
                        lifecycle_writer,
                        previous_scenes,
                        tick_revision,
                    )
                    if active_scenes is None:
                        _runtime.tick_idle.set()
                        continue
                    async with _runtime.mutation_lock:
                        if not _runtime.is_revision(tick_revision):
                            _runtime.tick_idle.set()
                            continue
                        _runtime.active_scenes = active_scenes
                        pulses_to_clear = _runtime.pulses.current_entity_ids(pulse_drain)
                        if pulses_to_clear:
                            clear_state_change_pulses(engine, set(pulses_to_clear))
                            _runtime.pulses.finish_drain(pulse_drain)
                            engine.evaluate_all()
                            _reconciler.drop_inactive_applied(set(engine.list_active_targets()))
                        _runtime.mark_success(now_ms=_monotonic_ms())
                        lifecycle_writer.mutated()
                    publication.publish_if_changed()
                    _runtime.tick_idle.set()
                except Exception as err:  # noqa: BLE001
                    now_ms = _monotonic_ms()
                    _runtime.mark_failure(err, now_ms=now_ms)
                    if _runtime.should_report_failure(now_ms=now_ms):
                        _LOGGER.exception("Intentional tick failed; continuing")
                        record_diagnostic(hass, "tick_failed", error=str(err))
                    _runtime.tick_idle.set()
        finally:
            _runtime.tick_idle.set()
            try:
                lifecycle_writer.mutated()
                await lifecycle_writer.async_flush(force=True)
                if lifecycle_writer.current_error is not None:
                    record_diagnostic(
                        hass,
                        "lifecycle_save_failed",
                        error=lifecycle_writer.current_error,
                    )
            except Exception as err:  # noqa: BLE001
                _LOGGER.exception("Could not save final Intentional lifecycle state")
                record_diagnostic(hass, "lifecycle_save_failed", error=str(err))

    if hasattr(hass, "async_create_background_task"):
        _runtime.tick_task = hass.async_create_background_task(_tick_loop(), name=f"{DOMAIN}_tick")
    else:
        _runtime.tick_task = hass.async_create_task(_tick_loop(), name=f"{DOMAIN}_tick")

    # Set up services
    await _register_services(
        hass,
        engine,
        rule_store,
        entry,
        _runtime,
        _reconciler,
        lifecycle_writer,
        selector_planner,
        mutation_coordinator,
    )

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
                _runtime.pulses,
                _runtime,
                lifecycle_writer,
                selector_planner,
            ),
        )
    )

    async def _apply_registry_changes() -> None:
        try:
            while _runtime.registry_pending and not _runtime.unloading:
                # Registry updates commonly arrive in entity/device/area bursts.
                await asyncio.sleep(0.05)
                _runtime.registry_pending = False
                async with _runtime.mutation_lock:
                    _apply_membership_change(
                        hass, engine, selector_planner.registry_changed()
                    )
                    sync_time_context_into_engine(engine)
                    engine.evaluate_all()
                    _runtime.advance_revision()
                    lifecycle_writer.mutated()
                publication.publish_if_changed()
        finally:
            _runtime.registry_task = None

    @callback
    def _registry_changed(_event) -> None:
        if _runtime.unloading:
            return
        _runtime.registry_pending = True
        if _runtime.registry_task is None or _runtime.registry_task.done():
            _runtime.registry_task = hass.async_create_task(
                _apply_registry_changes(), name=f"{DOMAIN}_registry"
            )

    for registry_event in (
        "entity_registry_updated",
        "device_registry_updated",
        "area_registry_updated",
        "label_registry_updated",
    ):
        entry.async_on_unload(hass.bus.async_listen(registry_event, _registry_changed))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload an Intentional config entry."""
    runtime = hass.data.get(DOMAIN, {}).get(runtime_key(entry.entry_id))
    if not isinstance(runtime, TickRuntime):
        return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    publication = hass.data[DOMAIN].get(publication_key(entry.entry_id))
    async with runtime.mutation_lock:
        runtime.unloading = True
        runtime.advance_revision()
        if isinstance(publication, EntityPublication):
            publication.pause()
    await runtime.tick_idle.wait()
    if runtime.registry_task is not None:
        runtime.registry_pending = False
        runtime.registry_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await runtime.registry_task

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unload_ok:
        async with runtime.mutation_lock:
            runtime.unloading = False
            if isinstance(publication, EntityPublication):
                publication.resume()
        return False

    async with runtime.mutation_lock:
        runtime.stop_event.set()
        runtime.advance_revision()
    if runtime.tick_task is not None:
        try:
            await runtime.tick_task
        except asyncio.CancelledError:
            current_task = asyncio.current_task()
            if current_task is not None and current_task.cancelling():
                raise
            _LOGGER.warning("Intentional tick task was cancelled during unload")
            record_diagnostic(hass, "tick_task_cancelled")
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("Intentional tick task failed during unload")
            record_diagnostic(hass, "tick_task_failed", error=str(err))
    hass.data[DOMAIN].pop(entry.entry_id, None)
    hass.data[DOMAIN].pop(rule_store_key(entry.entry_id), None)
    hass.data[DOMAIN].pop(runtime_key(entry.entry_id), None)
    hass.data[DOMAIN].pop(lifecycle_writer_key(entry.entry_id), None)
    hass.data[DOMAIN].pop(publication_key(entry.entry_id), None)
    hass.data[DOMAIN].pop(reconciliation_key(entry.entry_id), None)
    hass.data[DOMAIN].pop(mutation_coordinator_key(entry.entry_id), None)
    return True


async def _register_frontend_panel(hass: HomeAssistant) -> None:
    """Serve and register the Intentional rule editor panel."""
    from homeassistant.components import frontend, panel_custom

    if not frontend.async_panel_exists(hass, "profile"):
        _LOGGER.debug("Skipping Intentional panel registration; frontend is not loaded")
        return

    domain_data = hass.data.setdefault(DOMAIN, {})
    if not domain_data.get(FRONTEND_STATIC_REGISTERED):
        frontend_path = Path(__file__).parent / "frontend"
        await hass.http.async_register_static_paths(
            [
                StaticPathConfig(FRONTEND_URL_PATH, str(frontend_path), True),
            ]
        )
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
                copied,
                rule_dir,
            )
        return copied
    except OSError as err:
        _LOGGER.warning("Could not install starter rules: %s", err)
        return 0


def _sync_state_into_engine(
    hass: HomeAssistant,
    engine: Engine,
    entity_ids: set[str],
) -> None:
    """Pull selected HA state into the engine."""
    states = hass.states
    for entity_id in entity_ids:
        state = states.get(entity_id)
        if state is not None:
            sync_state_object_into_engine(engine, state)


def _apply_membership_change(hass: HomeAssistant, engine: Engine, change: MembershipChange) -> None:
    """Eagerly ingest arrivals and discard state with no remaining owner."""
    if change.added:
        _sync_state_into_engine(hass, engine, entity_ids=set(change.added))
    for entity_id in change.removed:
        engine.remove_state(entity_id)


def _on_ha_state_change_factory(
    hass: HomeAssistant,
    engine: Engine,
    reconciler: Reconciliation,
    context_tracker: IntentionalContextTracker | None = None,
    diagnostic_rate_limiter: DiagnosticRateLimiter | None = None,
    state_change_pulses: StateChangePulseQueue | None = None,
    runtime: TickRuntime | None = None,
    lifecycle_writer: LifecycleWriter | None = None,
    selector_planner: SelectorMembershipPlanner | None = None,
):
    """Return a state_changed listener that pushes updates into the engine."""

    async def _listener(event) -> None:
        if runtime is not None and runtime.unloading:
            return
        lock = runtime.mutation_lock if runtime is not None else asyncio.Lock()
        async with lock:
            if runtime is not None and runtime.unloading:
                return
            old_state: State | None = event.data.get("old_state")
            new_state: State | None = event.data.get("new_state")
            entity_id = (
                new_state.entity_id
                if new_state is not None
                else (old_state.entity_id if old_state is not None else None)
            )
            if selector_planner is not None and entity_id is not None:
                old_device_class = (
                    old_state.attributes.get("device_class")
                    if old_state is not None
                    else None
                )
                new_device_class = (
                    new_state.attributes.get("device_class")
                    if new_state is not None
                    else None
                )
                _apply_membership_change(
                    hass,
                    engine,
                    selector_planner.state_changed(
                        entity_id,
                        exists=new_state is not None,
                        device_class_changed=old_device_class != new_device_class,
                    ),
                )
            if selector_planner is not None and entity_id not in selector_planner.relevant:
                return
            if new_state is None:
                if old_state is not None:
                    engine.remove_state(old_state.entity_id)
                    sync_time_context_into_engine(engine)
                    engine.evaluate_all()
                if runtime is not None:
                    runtime.advance_revision()
                if lifecycle_writer is not None:
                    lifecycle_writer.mutated()
                return
            lifecycle_before = engine.export_lifecycle_records()
            owned_result = (
                context_tracker is not None and context_tracker.owns_state(new_state)
            )
            sync_state_object_into_engine(engine, new_state)
            if state_change_pulses is not None and pulse_state_change(engine, old_state, new_state):
                state_change_pulses.add(new_state.entity_id)
            now_ms = _monotonic_ms()
            events = reconciler.on_state_delta(engine, new_state, context_tracker, now_ms)
            if events:
                _apply_reconciliation_events(
                    hass,
                    engine,
                    events,
                    now_ms=now_ms,
                    rate_limiter=diagnostic_rate_limiter,
                )
            # Re-evaluate immediately for snappy response
            sync_time_context_into_engine(engine)
            engine.evaluate_all()
            own_feedback_only = owned_result and bool(events) and all(
                event.kind == "context_ignored" for event in events
            )
            lifecycle_changed = engine.export_lifecycle_records() != lifecycle_before
            if runtime is not None and (not own_feedback_only or lifecycle_changed):
                runtime.advance_revision()
            if lifecycle_writer is not None:
                lifecycle_writer.mutated()

    return _listener


def _monotonic_ms() -> int:
    """Compatibility helper for tests that patch integration monotonic time."""
    return monotonic_ms()


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
            scene_id,
            service_data.get("transition"),
        )
        try:
            await hass.services.async_call(
                domain,
                service,
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


async def _dispatch_effect_outbox(
    hass: HomeAssistant,
    engine: Engine,
    intentional_contexts: IntentionalContextTracker | None = None,
    runtime: TickRuntime | None = None,
    lifecycle_writer: LifecycleWriter | None = None,
) -> None:
    """Deliver durable outbox records and force their acknowledgement."""
    lock = runtime.mutation_lock if runtime is not None else asyncio.Lock()
    for due in engine.due_effects():
        # The queue record, rather than its attempt counter, is the at-least-once
        # boundary. A fresh obligation must be durable before HA can accept it.
        if lifecycle_writer is not None and not lifecycle_writer.contains_durable_effect(
            due.activation_id, due.effect_index
        ):
            lifecycle_writer.mutated()
            await lifecycle_writer.async_flush(force=True)
            if lifecycle_writer.current_error is not None:
                return
        async with lock:
            effect = engine.begin_effect_attempt(due.activation_id, due.effect_index)
            if effect is None:
                continue
            if lifecycle_writer is not None:
                lifecycle_writer.mutated()
        service_data = {**effect.target, **effect.data}
        context = new_intentional_context(intentional_contexts)
        try:
            await hass.services.async_call(
                effect.domain,
                effect.service,
                service_data,
                # Successful return is our acceptance boundary. HA may have
                # completed the handler or accepted delegated work; either is
                # sufficient for acknowledgement, but not exactly-once proof.
                blocking=True,
                context=context,
            )
            async with lock:
                engine.acknowledge_effect(effect.activation_id, effect.effect_index)
                if lifecycle_writer is not None:
                    lifecycle_writer.mutated()
            if lifecycle_writer is not None:
                await lifecycle_writer.async_flush(force=True)
            record_diagnostic(
                hass,
                "effect_applied",
                activation_id=effect.activation_id,
                rule_id=effect.rule_id,
                effect_index=effect.effect_index,
                attempts=effect.attempts,
                domain=effect.domain,
                service=effect.service,
                service_data=service_data,
            )
        except Exception as err:  # noqa: BLE001
            async with lock:
                terminal = engine.fail_effect(effect.activation_id, effect.effect_index, str(err))
                if lifecycle_writer is not None:
                    lifecycle_writer.mutated()
            if lifecycle_writer is not None:
                await lifecycle_writer.async_flush(force=True)
            record_diagnostic(
                hass,
                "effect_failed",
                activation_id=effect.activation_id,
                rule_id=effect.rule_id,
                effect_index=effect.effect_index,
                attempts=effect.attempts,
                next_retry_ms=effect.next_retry_ms,
                terminal=terminal,
                domain=effect.domain,
                service=effect.service,
                service_data=service_data,
                error=str(err),
            )
            _LOGGER.warning(
                "Failed to apply effect from rule %s via %s.%s: %s",
                effect.rule_id,
                effect.domain,
                effect.service,
                err,
            )


async def _activate_scenes_and_dispatch_effects(
    hass: HomeAssistant,
    engine: Engine,
    intentional_contexts: IntentionalContextTracker,
    runtime: TickRuntime,
    lifecycle_writer: LifecycleWriter,
    previous_scenes: set[str],
    tick_revision: int,
) -> set[str] | None:
    """Dispatch Effects only if scene calls did not make the tick stale."""
    active_scenes = await _activate_scene_rules(hass, engine, previous_scenes, intentional_contexts)
    async with runtime.mutation_lock:
        if not runtime.is_revision(tick_revision):
            return None
    await _dispatch_effect_outbox(hass, engine, intentional_contexts, runtime, lifecycle_writer)
    return active_scenes


async def _register_services(
    hass: HomeAssistant,
    engine: Engine,
    rule_store: StorageRuleStore,
    entry: ConfigEntry,
    runtime: TickRuntime,
    reconciler: Reconciliation,
    lifecycle_writer: LifecycleWriter,
    selector_planner: SelectorMembershipPlanner,
    mutation_coordinator: RuleMutationCoordinator,
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

        async with runtime.mutation_lock:
            _apply_membership_change(
                hass,
                engine,
                selector_planner.update_owned(
                    set(engine.list_active_targets())
                    | set(reconciler.pending_withdraw_targets())
                    | {target}
                ),
            )
            engine.emit_user_intent(
                target=target,
                set=set_dict,
                ttl_ms=ttl_ms,
                reason=f"Manual fire service at {call.service}",
            )
            # Force a re-evaluation cycle
            engine.evaluate_all()
            runtime.advance_revision()
            lifecycle_writer.mutated()
        hass.data[DOMAIN][publication_key(entry.entry_id)].publish_if_changed()

    async def _activate_scene_service(call: ServiceCall) -> None:
        """Handle the `intentional.activate_scene` service call.

        Looks up a scene rule by ID and forces it to fire, regardless of
        its `when` condition. Honors the rule's transition and TTL.
        """
        rule_id = call.data["rule_id"]
        ttl_override = call.data.get("ttl", 0)

        async with runtime.mutation_lock:
            intent = engine.activate_scene_rule(
                rule_id, ttl_ms=ttl_override * 1000 if ttl_override else None
            )
            if intent is None:
                _LOGGER.error("No scene rule found with id %r", rule_id)
                return
            # Re-evaluate so the activation path picks it up
            engine.evaluate_all()
            runtime.advance_revision()
            lifecycle_writer.mutated()
        hass.data[DOMAIN][publication_key(entry.entry_id)].publish_if_changed()
        _LOGGER.info(
            "Scene rule %r activated manually (ttl=%sms)",
            rule_id,
            intent.ttl_ms,
        )

    async def _clear_service(call: ServiceCall) -> None:
        """Handle the `intentional.clear` service call."""
        target = call.data.get(ATTR_TARGET)
        async with runtime.mutation_lock:
            removed = engine.clear_user_intents(target=target)
            engine.evaluate_all()
            runtime.advance_revision()
            lifecycle_writer.mutated()
        hass.data[DOMAIN][publication_key(entry.entry_id)].publish_if_changed()
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
        await mutation_coordinator.async_reload()

    hass.services.async_register(DOMAIN, SERVICE_FIRE, _fire_service, schema=FIRE_SERVICE_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_CLEAR, _clear_service, schema=CLEAR_SERVICE_SCHEMA)
    hass.services.async_register(
        DOMAIN, SERVICE_ACTIVATE_SCENE, _activate_scene_service, schema=ACTIVATE_SCENE_SCHEMA
    )
    hass.services.async_register(DOMAIN, SERVICE_RELOAD, _reload_service)
