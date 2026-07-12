"""HTTP API for the Intentional integration.

Exposes a small JSON-over-HTTP API on HA's existing web server
(port 8123) so external agents (and humans via curl) can observe
and modify the engine without going through the config flow UI.

Auth uses HA's existing bearer-token pattern — same as the rest of
the HA REST API. No new auth code is needed: if a request can hit
HA's API, it can hit these endpoints.

Endpoints
---------

- ``GET    /api/intentional/health``
    Health check. Returns integration status, engine state, rule count.

- ``GET    /api/intentional/rules``
    List all rule files in the configured rule directory.

- ``GET    /api/intentional/rules/document``
    Read the storage-backed authored rule document.

- ``PUT    /api/intentional/rules/document``
    Replace the storage-backed authored rule document.

- ``DELETE /api/intentional/rules/document``
    Clear the storage-backed authored rule document.

- ``GET    /api/intentional/rules/<filename>``
    Read a rule file's contents.

- ``PUT    /api/intentional/rules/<filename>``
    Write a rule file. Validates YAML first.

- ``DELETE /api/intentional/rules/<filename>``
    Delete a rule file.

- ``POST   /api/intentional/reload``
    Trigger the ``intentional.reload`` service. Useful for tests and for agents
    that have just written a new rule.

- ``GET    /api/intentional/state``
    Snapshot of the engine's current state: all active intents,
    grouped by target, with the winning rule highlighted.

- ``GET    /api/intentional/explain/<target>``
    Detailed explanation of why a target is in its current state:
    winning rule, competing intents, modifiers, authority chain.
    Useful for debugging conflicts.

- ``POST   /api/intentional/simulate``
    Evaluate proposed YAML over a timeline of state changes without applying
    anything to Home Assistant.

Error format
------------

All errors return JSON in the form:
    ``{"error": "<message>", "code": "<error_code>"}``

with the appropriate HTTP status code (400 for bad input, 404 for
missing resources, 500 for internal errors).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import datetime
from functools import partial
from typing import Any

from aiohttp import web
from homeassistant.components.http import HomeAssistantView, require_admin
from homeassistant.core import HomeAssistant

from ._engine import __version__  # noqa: TID252
from ._engine.engine import Engine  # noqa: TID252
from ._engine.projection import (  # noqa: TID252
    dashboard_cards,
    explain_card,
    preview_targets,
    redact_sensitive,
    simulation_step,
    target_projection,
)
from ._engine.reconciliation import (  # noqa: TID252
    actual_conditions_for_desired_record,
    actual_snapshot,
    reconciliation_key,
)
from ._engine.runtime import TickRuntime, runtime_key  # noqa: TID252
from ._engine.schema import dsl_schema  # noqa: TID252
from ._engine.simulation import (  # noqa: TID252
    _simulation_selector_resolver,
    simulate_timeline,
    validate_simulation_input,
)
from ._engine.yaml_loader import RuleLoadError  # noqa: TID252
from .const import CONF_RULE_DIR, DEFAULT_RULE_DIR, DOMAIN  # noqa: TID252
from .diagnostics import list_diagnostics  # noqa: TID252
from .document_validation import (  # noqa: TID252
    load_and_preflight_document,
    validate_document,
)
from .lifecycle_writer import LifecycleWriter, lifecycle_writer_key  # noqa: TID252
from .room_controls import area_for_target, room_controls_for_engine  # noqa: TID252
from .rule_files import (  # noqa: TID252
    _delete_rule_file,
    _is_safe_filename,
    _list_rule_files,
    _patch_rule_by_id,
    _read_rule_file,
    _write_rule_file,
)
from .rule_mutation import (  # noqa: TID252
    RuleMutationCoordinator,
    mutate_and_reload,
    mutation_coordinator_key,
)
from .rule_store import RULE_STORE_FILENAME, StorageRuleStore, rule_store_key  # noqa: TID252
from .validation import validation_warnings as _validation_warnings  # noqa: TID252

_LOGGER = logging.getLogger(__name__)
_API_REGISTERED = f"{DOMAIN}_api_registered"


def _entry_for_view(hass: HomeAssistant) -> Any:
    """Return the first config entry for the integration, or None.

    The integration supports multiple entries in theory, but in
    practice most users have one. We pick the first one for the
    API; if you have multiple and need per-entry routing, that
    can be added later.
    """
    entries = hass.config_entries.async_entries(DOMAIN)
    return entries[0] if entries else None


def _engine_for(hass: HomeAssistant) -> Any | None:
    """Return the engine instance for the first config entry, or None."""
    entry = _entry_for_view(hass)
    if entry is None:
        return None
    return hass.data.get(DOMAIN, {}).get(entry.entry_id)


def _reconciliation_for(hass: HomeAssistant) -> Any | None:
    entry = _entry_for_view(hass)
    if entry is None:
        return None
    return hass.data.get(DOMAIN, {}).get(reconciliation_key(entry.entry_id))


def _rule_dir_for(hass: HomeAssistant) -> str:
    """Return the rule directory for the first config entry."""
    entry = _entry_for_view(hass)
    if entry is None:
        return DEFAULT_RULE_DIR
    return entry.data.get(CONF_RULE_DIR, DEFAULT_RULE_DIR)


def _rule_store_for(hass: HomeAssistant) -> StorageRuleStore | None:
    """Return storage-backed rule store for the first config entry, if loaded."""
    entry = _entry_for_view(hass)
    if entry is None:
        return None
    store = hass.data.get(DOMAIN, {}).get(rule_store_key(entry.entry_id))
    return store if isinstance(store, StorageRuleStore) else None


def _mutation_coordinator_for(hass: HomeAssistant) -> RuleMutationCoordinator | None:
    entry = _entry_for_view(hass)
    if entry is None:
        return None
    coordinator = hass.data.get(DOMAIN, {}).get(mutation_coordinator_key(entry.entry_id))
    return coordinator if isinstance(coordinator, RuleMutationCoordinator) else None


def _expected_generation(data: dict[str, Any]) -> tuple[str | None, web.Response | None]:
    generation = data.get("expected_generation")
    if not isinstance(generation, str):
        return None, _error("Request body must include string `expected_generation`", "precondition_required", 428)
    return generation, None


def _runtime_for(hass: HomeAssistant) -> TickRuntime | None:
    """Return tick runtime state for the first config entry, if loaded."""
    entry = _entry_for_view(hass)
    if entry is None:
        return None
    runtime = hass.data.get(DOMAIN, {}).get(runtime_key(entry.entry_id))
    return runtime if isinstance(runtime, TickRuntime) else None


def _runtime_health(hass: HomeAssistant) -> dict[str, Any]:
    runtime = _runtime_for(hass)
    if runtime is None:
        return {"status": "degraded", "error": "runtime_not_loaded"}
    return runtime.health()


def _persistence_health(hass: HomeAssistant) -> dict[str, Any]:
    entry = _entry_for_view(hass)
    writer = (
        None
        if entry is None
        else hass.data.get(DOMAIN, {}).get(lifecycle_writer_key(entry.entry_id))
    )
    if not isinstance(writer, LifecycleWriter):
        return {"status": "degraded", "error": "persistence_not_loaded"}
    return writer.health()


def _overall_status(runtime_health: dict[str, Any]) -> str:
    status = runtime_health.get("status")
    return "ok" if status in ("ok", "starting") else "degraded"


def _error(message: str, code: str, status: int = 400) -> web.Response:
    """Return a JSON error response."""
    return web.json_response(
        {"error": message, "code": code},
        status=status,
    )


async def _json_object(request: web.Request) -> tuple[dict[str, Any] | None, web.Response | None]:
    """Decode a request body and require a JSON object."""
    try:
        data = await request.json()
    except (ValueError, json.JSONDecodeError) as err:
        return None, _error(f"Invalid JSON: {err}", "bad_request", 400)
    if not isinstance(data, dict):
        return None, _error("Request body must be a JSON object", "bad_request", 400)
    return data, None


async def _rule_file_job(
    hass: HomeAssistant,
    func: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Run blocking rule-file work in HA's executor."""
    job = partial(func, **kwargs) if kwargs else func
    return await hass.async_add_executor_job(job, *args)


# ── Health ─────────────────────────────────────────────────────────


class IntentionalHealthView(HomeAssistantView):
    """GET /api/intentional/health

    Returns the integration's status. Useful as a readiness probe
    for monitoring or for agents checking that the integration
    is alive before issuing commands.
    """

    url = "/api/intentional/health"
    name = "api:intentional:health"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        engine = _engine_for(hass)
        entry = _entry_for_view(hass)
        if engine is None or entry is None:
            return _error("Integration not configured", "not_configured", 503)
        runtime_health = _runtime_health(hass)
        persistence_health = _persistence_health(hass)
        return web.json_response(
            {
                "status": "ok"
                if _overall_status(runtime_health) == "ok" and persistence_health["status"] == "ok"
                else "degraded",
                "version": __version__,
                "rule_dir": entry.data.get(CONF_RULE_DIR, DEFAULT_RULE_DIR),
                "rule_count": engine.rule_count(),
                "active_intent_count": engine.active_intent_count(),
                "runtime": runtime_health,
                "persistence": persistence_health,
            }
        )


# ── Rules list / read / write / delete ─────────────────────────────


class IntentionalRulesView(HomeAssistantView):
    """GET /api/intentional/rules — list rule files."""

    url = "/api/intentional/rules"
    name = "api:intentional:rules"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        store = _rule_store_for(hass)
        if store is not None:
            return web.json_response(
                {
                    "rule_dir": "homeassistant_storage",
                    "count": 1,
                    "files": store.list_files(),
                    "source": "storage",
                }
            )
        rule_dir = _rule_dir_for(hass)
        files = await _rule_file_job(hass, _list_rule_files, rule_dir)
        return web.json_response(
            {
                "rule_dir": rule_dir,
                "count": len(files),
                "files": files,
            }
        )


class IntentionalRuleDocumentView(HomeAssistantView):
    """GET / PUT / DELETE the storage-backed authored rule document."""

    url = "/api/intentional/rules/document"
    name = "api:intentional:rule_document"
    requires_auth = True

    @require_admin
    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        store = _rule_store_for(hass)
        if store is None:
            return _error(
                "Rule document is only available for storage-backed rules", "not_available", 404
            )
        return web.json_response(_rule_document_response(store))

    @require_admin
    async def put(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        store = _rule_store_for(hass)
        if store is None:
            return _error(
                "Rule document is only available for storage-backed rules", "not_available", 404
            )
        data, error = await _json_object(request)
        if error is not None:
            return error
        assert data is not None
        contents = data.get("contents")
        if not isinstance(contents, str):
            return _error("Request body must include string `contents`", "bad_request", 400)
        expected_generation = data.get("expected_generation")
        if not isinstance(expected_generation, str):
            return _error("Request body must include string `expected_generation`", "precondition_required", 428)
        coordinator = _mutation_coordinator_for(hass)
        if coordinator is None:
            return _error("Rule mutation coordinator is not loaded", "not_available", 503)
        result, reload_error = await mutate_and_reload(
            coordinator,
            lambda: store.async_write(RULE_STORE_FILENAME, contents),
            expected_generation=expected_generation,
        )
        if isinstance(result, dict) and result.get("error") == "generation_mismatch":
            return web.json_response(result, status=409)
        err = result if isinstance(result, str) else None
        if err:
            return _error(err, "validation_failed", 400)
        if reload_error is not None:
            return _error(f"Reload failed: {reload_error}", "reload_failed", 500)
        return web.json_response({"status": "saved", **_rule_document_response(store)}, status=200)

    @require_admin
    async def delete(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        store = _rule_store_for(hass)
        if store is None:
            return _error(
                "Rule document is only available for storage-backed rules", "not_available", 404
            )
        try:
            data = await request.json()
        except (ValueError, json.JSONDecodeError):
            data = {}
        if not isinstance(data, dict):
            return _error("Request body must be a JSON object", "bad_request", 400)
        expected_generation, error = _expected_generation(data)
        if error is not None:
            return error
        coordinator = _mutation_coordinator_for(hass)
        if coordinator is None:
            return _error("Rule mutation coordinator is not loaded", "not_available", 503)
        result, reload_error = await mutate_and_reload(
            coordinator,
            lambda: store.async_delete(RULE_STORE_FILENAME),
            expected_generation=expected_generation,
        )
        if isinstance(result, dict) and result.get("error") == "generation_mismatch":
            return web.json_response(result, status=409)
        err = result if isinstance(result, str) else None
        if err:
            return _error(err, "delete_failed", 500)
        if reload_error is not None:
            return _error(f"Reload failed: {reload_error}", "reload_failed", 500)
        return web.json_response({"status": "deleted", **_rule_document_response(store)})


class IntentionalRuleView(HomeAssistantView):
    """GET / PUT / DELETE a single rule file.

    URL: /api/intentional/rules/<filename>

    The filename comes from a URL parameter, so it's already
    URL-decoded by aiohttp. We re-validate it via
    ``_is_safe_filename`` to reject any path-traversal attempts
    that might have been URL-encoded.
    """

    url = r"/api/intentional/rules/{filename:.+}"
    name = "api:intentional:rule"
    requires_auth = True

    @require_admin
    async def get(self, request: web.Request, filename: str) -> web.Response:
        hass = request.app["hass"]
        if not _is_safe_filename(filename):
            return _error(f"Invalid filename: {filename!r}", "invalid_filename", 400)
        store = _rule_store_for(hass)
        if store is not None:
            contents = store.read(filename)
            if contents is None:
                return _error(f"Rule file not found: {filename}", "not_found", 404)
            return web.json_response(
                {
                    "filename": filename,
                    "contents": contents,
                    "size": len(contents),
                    "generation": store.generation,
                    "source": "storage",
                }
            )
        rule_dir = _rule_dir_for(hass)
        contents = await _rule_file_job(hass, _read_rule_file, rule_dir, filename)
        if contents is None:
            return _error(f"Rule file not found: {filename}", "not_found", 404)
        return web.json_response(
            {
                "filename": filename,
                "contents": contents,
                "size": len(contents),
            }
        )

    @require_admin
    async def put(self, request: web.Request, filename: str) -> web.Response:
        hass = request.app["hass"]
        if not _is_safe_filename(filename):
            return _error(f"Invalid filename: {filename!r}", "invalid_filename", 400)
        if not filename.endswith((".yaml", ".yml")):
            return _error("Filename must end in .yaml or .yml", "invalid_filename", 400)

        # Read the JSON body
        data, error = await _json_object(request)
        if error is not None:
            return error
        assert data is not None
        contents = data.get("contents")
        if not isinstance(contents, str):
            return _error('Request body must be {"contents": "<yaml>"}', "bad_request", 400)

        store = _rule_store_for(hass)
        if store is not None:
            filename = RULE_STORE_FILENAME
            expected_generation, error = _expected_generation(data)
            if error is not None:
                return error
            coordinator = _mutation_coordinator_for(hass)
            if coordinator is None:
                return _error("Rule mutation coordinator is not loaded", "not_available", 503)
            result, reload_error = await mutate_and_reload(
                coordinator,
                lambda: store.async_write(filename, contents),
                expected_generation=expected_generation,
            )
            if isinstance(result, dict) and result.get("error") == "generation_mismatch":
                return web.json_response(result, status=409)
            err = result if isinstance(result, str) else None
            if err:
                return _error(err, "validation_failed", 400)
            if reload_error is not None:
                return _error(f"Reload failed: {reload_error}", "reload_failed", 500)
            return web.json_response(
                {
                    "filename": filename,
                    "status": "saved",
                    "size": len(contents),
                    "generation": store.generation,
                    "source": "storage",
                },
                status=200,
            )

        rule_dir = _rule_dir_for(hass)
        err = await _rule_file_job(hass, _write_rule_file, rule_dir, filename, contents)
        if err:
            return _error(err, "validation_failed", 400)
        # Auto-reload so the new rule takes effect
        await hass.services.async_call(DOMAIN, "reload", blocking=True)
        return web.json_response(
            {
                "filename": filename,
                "status": "saved",
                "size": len(contents),
            },
            status=200,
        )

    @require_admin
    async def delete(self, request: web.Request, filename: str) -> web.Response:
        hass = request.app["hass"]
        if not _is_safe_filename(filename):
            return _error(f"Invalid filename: {filename!r}", "invalid_filename", 400)
        store = _rule_store_for(hass)
        if store is not None:
            try:
                data = await request.json()
            except (ValueError, json.JSONDecodeError):
                data = {}
            if not isinstance(data, dict):
                return _error("Request body must be a JSON object", "bad_request", 400)
            expected_generation, error = _expected_generation(data)
            if error is not None:
                return error
            coordinator = _mutation_coordinator_for(hass)
            if coordinator is None:
                return _error("Rule mutation coordinator is not loaded", "not_available", 503)
            result, reload_error = await mutate_and_reload(
                coordinator,
                lambda: store.async_delete(filename),
                expected_generation=expected_generation,
            )
            if isinstance(result, dict) and result.get("error") == "generation_mismatch":
                return web.json_response(result, status=409)
            err = result if isinstance(result, str) else None
            if err:
                return _error(err, "delete_failed", 500)
            if reload_error is not None:
                return _error(f"Reload failed: {reload_error}", "reload_failed", 500)
            return web.json_response(
                {"filename": filename, "status": "deleted", "source": "storage"}
            )
        rule_dir = _rule_dir_for(hass)
        err = await _rule_file_job(hass, _delete_rule_file, rule_dir, filename)
        if err:
            return _error(err, "delete_failed", 500)
        await hass.services.async_call(DOMAIN, "reload", blocking=True)
        return web.json_response({"filename": filename, "status": "deleted"})


class IntentionalRuleByIDView(HomeAssistantView):
    """PATCH /api/intentional/rules/id/<rule_id> — generation-guarded rule update."""

    url = r"/api/intentional/rules/id/{rule_id:.+}"
    name = "api:intentional:rule_by_id"
    requires_auth = True

    @require_admin
    async def patch(self, request: web.Request, rule_id: str) -> web.Response:
        hass = request.app["hass"]
        data, error = await _json_object(request)
        if error is not None:
            return error
        assert data is not None
        contents = data.get("contents")
        expected_generation = data.get("expected_generation")
        if not isinstance(contents, str) or not isinstance(expected_generation, str):
            return _error(
                "Request body must include string `contents` and `expected_generation`",
                "bad_request",
                400,
            )
        store = _rule_store_for(hass)
        if store is not None:
            coordinator = _mutation_coordinator_for(hass)
            if coordinator is None:
                return _error("Rule mutation coordinator is not loaded", "not_available", 503)
            result, reload_error = await mutate_and_reload(
                coordinator,
                lambda: store.async_patch_rule_by_id(
                    rule_id,
                    contents,
                    expected_generation=expected_generation,
                ),
                expected_generation=expected_generation,
            )
        else:
            reload_error = None
            result = await _rule_file_job(
                hass,
                _patch_rule_by_id,
                _rule_dir_for(hass),
                rule_id,
                contents,
                expected_generation=expected_generation,
            )
        if "error" in result:
            status = 409 if result["error"] == "generation_mismatch" else 400
            return web.json_response(result, status=status)
        if store is None:
            await hass.services.async_call(DOMAIN, "reload", blocking=True)
        elif reload_error is not None:
            return _error(f"Reload failed: {reload_error}", "reload_failed", 500)
        return web.json_response({"status": "saved", **result})


class IntentionalRuleHistoryView(HomeAssistantView):
    """GET /api/intentional/rules/history — list stored rule snapshots."""

    url = "/api/intentional/rules/history"
    name = "api:intentional:rule_history"
    requires_auth = True

    @require_admin
    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        store = _rule_store_for(hass)
        if store is None:
            return _error(
                "Rule history is only available for storage-backed rules", "not_available", 404
            )
        history = store.list_history()
        return web.json_response(
            {
                "current_generation": store.generation,
                "count": len(history),
                "history": history,
            }
        )


class IntentionalRuleHistoryGenerationView(HomeAssistantView):
    """GET /api/intentional/rules/history/<generation> — read one snapshot."""

    url = r"/api/intentional/rules/history/{generation:.+}"
    name = "api:intentional:rule_history_generation"
    requires_auth = True

    @require_admin
    async def get(self, request: web.Request, generation: str) -> web.Response:
        hass = request.app["hass"]
        store = _rule_store_for(hass)
        if store is None:
            return _error(
                "Rule history is only available for storage-backed rules", "not_available", 404
            )
        record = store.read_history(generation)
        if record is None:
            return _error(f"Rule history generation not found: {generation}", "not_found", 404)
        return web.json_response(record)


class IntentionalRuleRollbackView(HomeAssistantView):
    """POST /api/intentional/rules/rollback — restore a history snapshot."""

    url = "/api/intentional/rules/rollback"
    name = "api:intentional:rule_rollback"
    requires_auth = True

    @require_admin
    async def post(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        store = _rule_store_for(hass)
        if store is None:
            return _error(
                "Rule rollback is only available for storage-backed rules", "not_available", 404
            )
        data, error = await _json_object(request)
        if error is not None:
            return error
        assert data is not None
        generation = data.get("generation")
        expected_generation = data.get("expected_generation")
        if not isinstance(generation, str) or not isinstance(expected_generation, str):
            return _error(
                "Request body must include string `generation` and `expected_generation`",
                "bad_request",
                400,
            )
        coordinator = _mutation_coordinator_for(hass)
        if coordinator is None:
            return _error("Rule mutation coordinator is not loaded", "not_available", 503)
        result, reload_error = await mutate_and_reload(
            coordinator,
            lambda: store.async_rollback(
                generation,
                expected_generation=expected_generation,
            ),
            expected_generation=expected_generation,
        )
        if "error" in result:
            status = 409 if result["error"] == "generation_mismatch" else 400
            if result["error"] == "history_not_found":
                status = 404
            return web.json_response(result, status=status)
        if reload_error is not None:
            return _error(f"Reload failed: {reload_error}", "reload_failed", 500)
        return web.json_response({"status": "restored", **result})


# ── Reload ─────────────────────────────────────────────────────────


class IntentionalReloadView(HomeAssistantView):
    """POST /api/intentional/reload

    Reloads all rules from disk. Equivalent to calling the
    ``intentional.reload`` service. Returns the new rule count.
    """

    url = "/api/intentional/reload"
    name = "api:intentional:reload"
    requires_auth = True

    @require_admin
    async def post(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        try:
            await hass.services.async_call(DOMAIN, "reload", blocking=True)
        except Exception as err:  # noqa: BLE001
            return _error(f"Reload failed: {err}", "reload_failed", 500)
        engine = _engine_for(hass)
        return web.json_response(
            {
                "status": "reloaded",
                "rule_count": engine.rule_count() if engine else 0,
            }
        )


# ── State inspection ──────────────────────────────────────────────


class IntentionalStateView(HomeAssistantView):
    """GET /api/intentional/state

    Returns the engine's current state: all active intents grouped
    by target, with the winning intent highlighted. This is the
    primary endpoint for agents that need to observe what the
    engine is currently doing.
    """

    url = "/api/intentional/state"
    name = "api:intentional:state"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        engine = _engine_for(hass)
        if engine is None:
            return _error("Integration not configured", "not_configured", 503)

        by_target: dict[str, list[dict[str, Any]]] = {}
        for target in engine.list_active_targets():
            explanation = engine.explain_target(target)
            by_target[target] = explanation["active_intents"]

        # Compute resolved values per target
        resolved: dict[str, dict[str, Any]] = {}
        for target in by_target:
            try:
                res = engine.resolve(target)
                if res is not None:
                    resolved[target] = {
                        "value": dict(res.value),
                        "ttl_remaining_ms": res.ttl_remaining_ms,
                    }
            except Exception:  # noqa: BLE001
                # Some targets may not have a resolvable state — skip
                pass

        return web.json_response(
            redact_sensitive(
                {
                    "rule_count": engine.rule_count(),
                    "active_intent_count": engine.active_intent_count(),
                    "by_target": by_target,
                    "resolved": resolved,
                }
            )
        )


class IntentionalExplainView(HomeAssistantView):
    """GET /api/intentional/explain/<target>

    Detailed explanation of why a target is in its current state.

    Returns:
    - The resolved value (what the entity is being told to be)
    - All active intents, sorted by priority
    - The winning intent and the rule that produced it
    - Per-field breakdown of how each modifier contributed
    """

    url = r"/api/intentional/explain/{target:.+}"
    name = "api:intentional:explain"
    requires_auth = True

    async def get(self, request: web.Request, target: str) -> web.Response:
        hass = request.app["hass"]
        engine = _engine_for(hass)
        if engine is None:
            return _error("Integration not configured", "not_configured", 503)
        explanation = engine.explain_target(target)
        explanation["projection"] = target_projection(
            engine,
            target,
            actual_state=hass.states.get(target),
            reconciliation=_reconciliation_for(hass),
        )
        return web.json_response(redact_sensitive(explanation))


# ── Agent-optimized VNext endpoints ─────────────────────────────────


class IntentionalSchemaView(HomeAssistantView):
    """GET /api/intentional/schema — machine-readable VNext capabilities."""

    url = "/api/intentional/schema"
    name = "api:intentional:schema"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        return web.json_response(dsl_schema())


class IntentionalValidateView(HomeAssistantView):
    """POST /api/intentional/validate — validate proposed YAML."""

    url = "/api/intentional/validate"
    name = "api:intentional:validate"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        data, error = await _json_object(request)
        if error is not None:
            return error
        assert data is not None
        contents = data.get("contents")
        if not isinstance(contents, str):
            return _error("Request body must include string `contents`", "bad_request", 400)
        rules, findings = validate_document(contents)
        hass = request.app["hass"]
        return web.json_response(
            redact_sensitive(
                {
                    "valid": not findings["errors"],
                    "rule_count": len(rules),
                    "normalized": [_rule_to_api_dict(rule) for rule in rules],
                    "errors": findings["errors"],
                    "warnings": [*findings["warnings"], *_validation_warnings(hass, rules)],
                }
            ),
            status=(
                400
                if any(
                    error["code"] in {"rule_load_error", "rule_validation_error"}
                    for error in findings["errors"]
                )
                else 200
            ),
        )


class IntentionalDryRunView(HomeAssistantView):
    """POST /api/intentional/dry-run — evaluate proposed YAML without applying."""

    url = "/api/intentional/dry-run"
    name = "api:intentional:dry_run"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        data, error = await _json_object(request)
        if error is not None:
            return error
        assert data is not None
        contents = data.get("contents")
        if not isinstance(contents, str):
            return _error("Request body must include string `contents`", "bad_request", 400)
        try:
            rules, _findings = load_and_preflight_document(contents)
        except RuleLoadError as err:
            return web.json_response({"valid": False, "errors": [str(err)]}, status=400)

        engine = Engine(selector_resolver=lambda _selector: [])
        engine.load_rules(rules)
        state_overrides = data.get("state_overrides", {})
        if not isinstance(state_overrides, dict):
            return _error("`state_overrides` must be a mapping", "bad_request", 400)
        for key, value in state_overrides.items():
            if not isinstance(key, str) or "." not in key:
                continue
            entity_id, _sep, field = key.rpartition(".")
            engine.update_state(entity_id, value, field=field)
        engine.evaluate_all()

        resolved = []
        for target in engine.list_active_targets():
            result = engine.resolve(target)
            if result is not None:
                resolved.append({"target": target, "value": dict(result.value)})
        effects = [
            {
                "rule_id": effect.rule_id,
                "domain": effect.domain,
                "service": effect.service,
                "target": effect.target,
                "data": effect.data,
            }
            for effect in engine.due_effects()
        ]
        return web.json_response(
            redact_sensitive(
                {
                    "valid": True,
                    "active_targets": list(engine.list_active_targets()),
                    "resolved_targets": resolved,
                    "preview": preview_targets(engine),
                    "effects": effects,
                    "errors": [],
                }
            )
        )


class IntentionalPreviewView(HomeAssistantView):
    """POST /api/intentional/preview — show desired-vs-actual target diffs."""

    url = "/api/intentional/preview"
    name = "api:intentional:preview"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        data, error = await _json_object(request)
        if error is not None:
            return error
        assert data is not None

        engine, error = _preview_engine(hass, data)
        if error is not None:
            return error
        assert engine is not None
        return web.json_response(
            {
                "valid": True,
                "preview": preview_targets(
                    engine, actual_for_target=lambda target: _actual_for_target(hass, target)
                ),
                "errors": [],
            }
        )


class IntentionalCardView(HomeAssistantView):
    """GET /api/intentional/card — Lovelace-friendly explain data."""

    url = "/api/intentional/card"
    name = "api:intentional:card"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        engine = _engine_for(hass)
        if engine is None:
            return _error("Integration not configured", "not_configured", 503)
        target = request.query.get("target")
        return web.json_response(explain_card(engine, target=target))


class IntentionalDashboardView(HomeAssistantView):
    """GET /api/intentional/dashboard — suggested Lovelace room cards."""

    url = "/api/intentional/dashboard"
    name = "api:intentional:dashboard"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        engine = _engine_for(hass)
        if engine is None:
            return _error("Integration not configured", "not_configured", 503)
        rooms = room_controls_for_engine(engine, lambda target: area_for_target(hass, target))
        return web.json_response(dashboard_cards(rooms))


class IntentionalSimulateView(HomeAssistantView):
    """POST /api/intentional/simulate — evaluate YAML across a state timeline."""

    url = "/api/intentional/simulate"
    name = "api:intentional:simulate"
    requires_auth = True

    @require_admin
    async def post(self, request: web.Request) -> web.Response:
        data, error = await _json_object(request)
        if error is not None:
            return error
        assert data is not None
        contents = data.get("contents")
        if not isinstance(contents, str):
            return _error("Request body must include string `contents`", "bad_request", 400)
        if len(contents.encode("utf-8")) > 1_000_000:
            return _error("`contents` may not exceed 1000000 bytes", "request_too_large", 400)
        timeline = data.get("timeline")
        if not isinstance(timeline, list):
            return _error("Request body must include list `timeline`", "bad_request", 400)
        try:
            rules, _findings = load_and_preflight_document(contents)
        except RuleLoadError as err:
            return web.json_response({"valid": False, "errors": [str(err)]}, status=400)

        engine = Engine(clock_fn=lambda: 0, selector_resolver=lambda _selector: [])
        engine.load_rules(rules)
        options = data.get("reconciliation", {})
        selectors = data.get("selectors")
        semantic_metadata = data.get("semantic_metadata")
        try:
            validate_simulation_input(
                timeline,
                options,
                projected_rule_targets=len({rule.target for rule in rules if rule.target}),
                selector_memberships=selectors,
                semantic_metadata=semantic_metadata,
            )
            steps = await simulate_timeline(
                engine,
                timeline,
                reconciliation_options=options,
                selector_memberships=selectors,
                semantic_metadata=semantic_metadata,
            )
        except (TypeError, ValueError) as err:
            return _error(str(err), "bad_request", 400)
        return web.json_response({"valid": True, "steps": steps, "errors": []})


class IntentionalReplayView(HomeAssistantView):
    """POST /api/intentional/replay — simulate rules over HA history-shaped data."""

    url = "/api/intentional/replay"
    name = "api:intentional:replay"
    requires_auth = True

    @require_admin
    async def post(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        data, error = await _json_object(request)
        if error is not None:
            return error
        assert data is not None
        contents = data.get("contents")
        if not isinstance(contents, str):
            store = _rule_store_for(hass)
            contents = store.contents if store is not None else None
        if not isinstance(contents, str):
            return _error(
                "Replay requires `contents` when storage-backed rules are unavailable",
                "bad_request",
                400,
            )
        if len(contents.encode("utf-8")) > 1_000_000:
            return _error("`contents` may not exceed 1000000 bytes", "request_too_large", 400)
        timeline = data.get("timeline")
        if timeline is None:
            timeline = _timeline_from_history(data.get("history"))
        if not isinstance(timeline, list):
            return _error(
                "Request body must include list `timeline` or HA history `history`",
                "bad_request",
                400,
            )
        try:
            rules, _findings = load_and_preflight_document(contents)
        except RuleLoadError as err:
            return web.json_response({"valid": False, "errors": [str(err)]}, status=400)
        try:
            selectors = data.get("selectors")
            semantic_metadata = data.get("semantic_metadata")
            validate_simulation_input(
                timeline,
                {},
                projected_rule_targets=len({rule.target for rule in rules if rule.target}),
                selector_memberships=selectors,
                semantic_metadata=semantic_metadata,
            )
        except (TypeError, ValueError) as err:
            return _error(str(err), "bad_request", 400)

        engine = Engine(clock_fn=lambda: 0, selector_resolver=lambda _selector: [])
        engine.load_rules(rules)
        try:
            engine.set_selector_resolver(
                _simulation_selector_resolver(engine, selectors, semantic_metadata)
            )
        except (TypeError, ValueError) as err:
            return _error(str(err), "bad_request", 400)
        steps = []
        for index, step in enumerate(timeline):
            pulses: set[str] = set()
            if not isinstance(step, dict):
                return _error(f"timeline[{index}] must be a mapping", "bad_request", 400)
            advance_ms = step.get("advance_ms", 0)
            if isinstance(advance_ms, int) and advance_ms > 0:
                engine.advance_clock(advance_ms)
            states = step.get("states", {})
            if not isinstance(states, dict):
                return _error(f"timeline[{index}].states must be a mapping", "bad_request", 400)
            for key, value in states.items():
                if not isinstance(key, str) or "." not in key:
                    continue
                entity_id, _sep, field = key.rpartition(".")
                old_key = f"{entity_id}.{field}"
                if field == "state" and old_key in engine.state and engine.state[old_key] != value:
                    engine.update_state(entity_id, True, field="changed")
                    pulses.add(entity_id)
                engine.update_state(entity_id, value, field=field)
            engine.evaluate_all()
            steps.append(simulation_step(engine, index=index))
            for entity_id in pulses:
                engine.update_state(entity_id, False, field="changed")
            if index % 10 == 9:
                await asyncio.sleep(0)
        return web.json_response({"valid": True, "steps": steps, "errors": []})


class IntentionalWorldView(HomeAssistantView):
    """GET /api/intentional/world — compact agent world model."""

    url = "/api/intentional/world"
    name = "api:intentional:world"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        engine = _engine_for(hass)
        if engine is None:
            return _error("Integration not configured", "not_configured", 503)

        world = engine.world_model()
        entities: dict[str, dict[str, Any]] = {}
        for record in world["desired_records"]:
            target = record["target"]
            state = hass.states.get(target)
            if state is not None:
                entities[target] = actual_snapshot(state)
                record["actual"] = entities[target]
                record["conditions"].extend(actual_conditions_for_desired_record(record, state))
            else:
                record["conditions"].extend(actual_conditions_for_desired_record(record, None))

        runtime_health = _runtime_health(hass)
        persistence_health = _persistence_health(hass)
        world["health"] = {
            "status": "ok"
            if _overall_status(runtime_health) == "ok" and persistence_health["status"] == "ok"
            else "degraded",
            "rule_count": engine.rule_count(),
            "active_intent_count": engine.active_intent_count(),
            "runtime": runtime_health,
            "persistence": persistence_health,
        }
        world["entities"] = entities
        reconciler = _reconciliation_for(hass)
        world["targets"] = [
            target_projection(
                engine,
                target,
                actual_state=hass.states.get(target),
                reconciliation=reconciler,
            )
            for target in sorted(
                set(engine.list_active_targets())
                | (set(reconciler.pending_withdraw_targets()) if reconciler is not None else set())
            )
        ]
        return web.json_response(redact_sensitive(world))


class IntentionalDiagnosticsView(HomeAssistantView):
    """GET /api/intentional/diagnostics — recent runtime diagnostic events."""

    url = "/api/intentional/diagnostics"
    name = "api:intentional:diagnostics"
    requires_auth = True

    @require_admin
    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        limit_value = request.query.get("limit")
        limit = None
        if limit_value is not None:
            try:
                limit = max(0, min(500, int(limit_value)))
            except ValueError:
                return _error("`limit` must be an integer", "bad_request", 400)
        events = list_diagnostics(hass, limit=limit)
        return web.json_response({"count": len(events), "events": events})


def _rule_to_api_dict(rule: Any) -> dict[str, Any]:
    return {
        "id": rule.id,
        "enabled": rule.enabled,
        "labels": list(rule.labels),
        "notes": rule.notes,
        "when": rule.when,
        "target": rule.target,
        "set": dict(rule.set),
        "cap": dict(rule.cap),
        "floor": dict(rule.floor),
        "offset": dict(rule.offset),
        "multiply": dict(rule.multiply),
        "effects": [
            {
                "domain": effect.domain,
                "service": effect.service,
                "target": effect.target,
                "data": effect.data,
            }
            for effect in rule.effects
        ],
    }


def _rule_document_response(store: StorageRuleStore) -> dict[str, Any]:
    return {
        "contents": store.contents,
        "size": len(store.contents.encode("utf-8")),
        "generation": store.generation,
        "rule_count": len(store.list_rules()),
        "source": "storage",
    }


def _preview_engine(
    hass: HomeAssistant, data: dict[str, Any]
) -> tuple[Any | None, web.Response | None]:
    contents = data.get("contents")
    state_overrides = data.get("state_overrides", {})
    if not isinstance(state_overrides, dict):
        return None, _error("`state_overrides` must be a mapping", "bad_request", 400)
    if isinstance(contents, str):
        try:
            rules, _findings = load_and_preflight_document(contents)
        except RuleLoadError as err:
            return None, web.json_response({"valid": False, "errors": [str(err)]}, status=400)
        source = _engine_for(hass)
        engine = Engine(selector_resolver=lambda _selector: [])
        if source is not None:
            for key, value in source.state.items():
                if isinstance(key, str) and "." in key:
                    entity_id, _sep, field = key.rpartition(".")
                    engine.update_state(entity_id, value, field=field)
        engine.load_rules(rules)
        for key, value in state_overrides.items():
            if not isinstance(key, str) or "." not in key:
                continue
            entity_id, _sep, field = key.rpartition(".")
            engine.update_state(entity_id, value, field=field)
        engine.evaluate_all()
    else:
        if state_overrides:
            return None, _error("`state_overrides` require preview `contents`", "bad_request", 400)
        engine = _engine_for(hass)
        if engine is None:
            return None, _error("Integration not configured", "not_configured", 503)
    return engine, None


def _actual_for_target(hass: HomeAssistant, target: str) -> dict[str, Any] | None:
    state = hass.states.get(target)
    if state is None:
        return None
    return actual_snapshot(state)


def _timeline_from_history(history: Any) -> list[dict[str, Any]] | None:
    if not isinstance(history, list):
        return None
    events = []
    for series in history:
        if not isinstance(series, list):
            continue
        for item in series:
            if not isinstance(item, dict):
                continue
            entity_id = item.get("entity_id")
            if not isinstance(entity_id, str):
                continue
            timestamp = _parse_history_time(item.get("last_changed") or item.get("last_updated"))
            states = {f"{entity_id}.state": item.get("state")}
            attributes = item.get("attributes")
            if isinstance(attributes, dict):
                for key, value in attributes.items():
                    if isinstance(key, str):
                        states[f"{entity_id}.{key}"] = value
            events.append((timestamp, states))
    events.sort(key=lambda event: event[0] or datetime.min)
    previous: datetime | None = None
    timeline = []
    for timestamp, states in events:
        advance_ms = 0
        if timestamp is not None and previous is not None:
            advance_ms = max(0, int((timestamp - previous).total_seconds() * 1000))
        if timestamp is not None:
            previous = timestamp
        timeline.append({"advance_ms": advance_ms, "states": states})
    return timeline


def _parse_history_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


# ── Registration ───────────────────────────────────────────────────


def register_api(hass: HomeAssistant) -> None:
    """Register all API views with HA's HTTP server.

    Called from ``async_setup_entry`` after the entry is set up.
    Idempotent: re-registering is a no-op.
    """
    if hass.data.get(_API_REGISTERED):
        return

    views = [
        IntentionalHealthView,
        IntentionalRulesView,
        IntentionalRuleDocumentView,
        IntentionalRuleHistoryView,
        IntentionalRuleHistoryGenerationView,
        IntentionalRuleRollbackView,
        IntentionalRuleView,
        IntentionalRuleByIDView,
        IntentionalReloadView,
        IntentionalStateView,
        IntentionalExplainView,
        IntentionalSchemaView,
        IntentionalValidateView,
        IntentionalDryRunView,
        IntentionalPreviewView,
        IntentionalCardView,
        IntentionalDashboardView,
        IntentionalSimulateView,
        IntentionalReplayView,
        IntentionalWorldView,
        IntentionalDiagnosticsView,
    ]
    for view_cls in views:
        hass.http.register_view(view_cls())
    hass.data[_API_REGISTERED] = True
    _LOGGER.info("Registered %d Intentional API views", len(views))
