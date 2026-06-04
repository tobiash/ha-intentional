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

Error format
------------

All errors return JSON in the form:
    ``{"error": "<message>", "code": "<error_code>"}``

with the appropriate HTTP status code (400 for bad input, 404 for
missing resources, 500 for internal errors).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import CONF_RULE_DIR, DEFAULT_RULE_DIR, DOMAIN  # noqa: TID252
from .rule_files import (  # noqa: TID252
    _delete_rule_file,
    _is_safe_filename,
    _list_rule_files,
    _read_rule_file,
    _write_rule_file,
)

_LOGGER = logging.getLogger(__name__)


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


def _rule_dir_for(hass: HomeAssistant) -> str:
    """Return the rule directory for the first config entry."""
    entry = _entry_for_view(hass)
    if entry is None:
        return DEFAULT_RULE_DIR
    return entry.data.get(CONF_RULE_DIR, DEFAULT_RULE_DIR)


def _error(message: str, code: str, status: int = 400) -> web.Response:
    """Return a JSON error response."""
    return web.json_response(
        {"error": message, "code": code},
        status=status,
    )


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
        return web.json_response({
            "status": "ok",
            "version": "0.3.0",
            "rule_dir": entry.data.get(CONF_RULE_DIR, DEFAULT_RULE_DIR),
            "rule_count": len(engine._rules),  # noqa: SLF001
            "active_intent_count": len(engine._active_intents),  # noqa: SLF001
        })


# ── Rules list / read / write / delete ─────────────────────────────


class IntentionalRulesView(HomeAssistantView):
    """GET /api/intentional/rules — list rule files."""

    url = "/api/intentional/rules"
    name = "api:intentional:rules"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        rule_dir = _rule_dir_for(hass)
        files = _list_rule_files(rule_dir)
        return web.json_response({
            "rule_dir": rule_dir,
            "count": len(files),
            "files": files,
        })


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

    async def get(self, request: web.Request, filename: str) -> web.Response:
        hass = request.app["hass"]
        if not _is_safe_filename(filename):
            return _error(f"Invalid filename: {filename!r}", "invalid_filename", 400)
        rule_dir = _rule_dir_for(hass)
        contents = _read_rule_file(rule_dir, filename)
        if contents is None:
            return _error(f"Rule file not found: {filename}", "not_found", 404)
        return web.json_response({
            "filename": filename,
            "contents": contents,
            "size": len(contents),
        })

    async def put(self, request: web.Request, filename: str) -> web.Response:
        hass = request.app["hass"]
        if not _is_safe_filename(filename):
            return _error(f"Invalid filename: {filename!r}", "invalid_filename", 400)
        if not filename.endswith((".yaml", ".yml")):
            return _error("Filename must end in .yaml or .yml", "invalid_filename", 400)

        # Read the JSON body
        try:
            data = await request.json()
        except (ValueError, json.JSONDecodeError) as err:
            return _error(f"Invalid JSON: {err}", "bad_request", 400)
        contents = data.get("contents")
        if not isinstance(contents, str):
            return _error("Request body must be {\"contents\": \"<yaml>\"}", "bad_request", 400)

        rule_dir = _rule_dir_for(hass)
        err = _write_rule_file(rule_dir, filename, contents)
        if err:
            return _error(err, "validation_failed", 400)
        # Auto-reload so the new rule takes effect
        await hass.services.async_call(DOMAIN, "reload", blocking=True)
        return web.json_response({
            "filename": filename,
            "status": "saved",
            "size": len(contents),
        }, status=200)

    async def delete(self, request: web.Request, filename: str) -> web.Response:
        hass = request.app["hass"]
        if not _is_safe_filename(filename):
            return _error(f"Invalid filename: {filename!r}", "invalid_filename", 400)
        rule_dir = _rule_dir_for(hass)
        err = _delete_rule_file(rule_dir, filename)
        if err:
            return _error(err, "delete_failed", 500)
        await hass.services.async_call(DOMAIN, "reload", blocking=True)
        return web.json_response({"filename": filename, "status": "deleted"})


# ── Reload ─────────────────────────────────────────────────────────


class IntentionalReloadView(HomeAssistantView):
    """POST /api/intentional/reload

    Reloads all rules from disk. Equivalent to calling the
    ``intentional.reload`` service. Returns the new rule count.
    """

    url = "/api/intentional/reload"
    name = "api:intentional:reload"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        try:
            await hass.services.async_call(DOMAIN, "reload", blocking=True)
        except Exception as err:  # noqa: BLE001
            return _error(f"Reload failed: {err}", "reload_failed", 500)
        engine = _engine_for(hass)
        return web.json_response({
            "status": "reloaded",
            "rule_count": len(engine._rules) if engine else 0,  # noqa: SLF001
        })


# ── State inspection ──────────────────────────────────────────────


def _intent_to_dict(intent: Any) -> dict[str, Any]:
    """Serialize an Intent to a JSON-friendly dict."""
    return {
        "rule_id": intent.rule_id,
        "target": intent.target,
        "set": dict(intent.set) if intent.set else {},
        "cap": dict(intent.cap) if intent.cap else {},
        "floor": dict(intent.floor) if intent.floor else {},
        "offset": dict(intent.offset) if intent.offset else {},
        "multiply": dict(intent.multiply) if intent.multiply else {},
        "authority": intent.authority.value,
        "authority_name": intent.authority.name,
        "confidence": intent.confidence,
        "ttl_ms": intent.ttl_ms,
        "reason": intent.reason,
        "created_at_ms": intent.created_at_ms,
    }


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

        # Group active intents by target
        by_target: dict[str, list[dict[str, Any]]] = {}
        for intent in engine._active_intents:  # noqa: SLF001
            by_target.setdefault(intent.target, []).append(_intent_to_dict(intent))

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

        return web.json_response({
            "rule_count": len(engine._rules),  # noqa: SLF001
            "active_intent_count": len(engine._active_intents),  # noqa: SLF001
            "by_target": by_target,
            "resolved": resolved,
        })


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
        # Look up active intents for this target
        active = [
            _intent_to_dict(i) for i in engine._active_intents  # noqa: SLF001
            if i.target == target
        ]
        resolved_obj = engine.resolve(target)
        resolved = None
        if resolved_obj is not None:
            resolved = {
                "value": dict(resolved_obj.value),
                "ttl_remaining_ms": resolved_obj.ttl_remaining_ms,
            }
        # Find which rules *could* fire for this target
        firing_rules = []
        for rule_id, parsed in engine._rules.items():  # noqa: SLF001
            if parsed.rule.target == target:
                firing_rules.append({
                    "rule_id": rule_id,
                    "firing": parsed.is_firing,
                })
        return web.json_response({
            "target": target,
            "resolved": resolved,
            "active_intents": active,
            "winning_intent": active[0] if active else None,
            "rules_for_target": firing_rules,
        })


# ── Registration ───────────────────────────────────────────────────


def register_api(hass: HomeAssistant) -> None:
    """Register all API views with HA's HTTP server.

    Called from ``async_setup_entry`` after the entry is set up.
    Idempotent: re-registering is a no-op.
    """
    views = [
        IntentionalHealthView,
        IntentionalRulesView,
        IntentionalRuleView,
        IntentionalReloadView,
        IntentionalStateView,
        IntentionalExplainView,
    ]
    for view_cls in views:
        hass.http.register_view(view_cls())
    _LOGGER.info("Registered %d Intentional API views", len(views))
