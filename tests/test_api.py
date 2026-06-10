"""Tests for the Intentional HTTP API.

We test the API view classes directly by mounting them in a
minimal aiohttp app — no real Home Assistant instance required.

What we test:

- The view classes construct without error
- The URL patterns match what the API documentation claims
- Helper functions (path lookups, error formatting) work
- View classes have the correct HA flags (requires_auth, etc.)

Why we don't test the full request lifecycle here
------------------------------------------------

Testing the full request lifecycle would require a HomeAssistant
instance with:
- a real aiohttp web server
- a real config entry
- the engine wired up
- the integration's __init__.py fully loaded

That's the job of the integration test suite (see
``test_integration.py`` in this directory). This module focuses on
the unit-level correctness of the view classes themselves.

If you want to test the full request flow without spinning up HA,
you can do it manually:

    curl -H "Authorization: Bearer <long-lived-token>" \\
         http://localhost:8123/api/intentional/health
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

# conftest.py handles sys.path setup (adds both src/ and custom_components/).
# This is here for documentation only — if you run this test in isolation
# (without conftest), the imports below will fail.
REPO_ROOT = Path(__file__).parent.parent
INTEGRATION_DIR = REPO_ROOT / "custom_components" / "intentional"

# The api module imports homeassistant, which is a heavy dep. Skip
# the API unit tests if it's not available; the integration tests
# in test_integration.py cover the same code paths with a real HA.
pytest.importorskip("homeassistant", reason="homeassistant not installed")
pytest.importorskip("aiohttp", reason="aiohttp not installed")


# ── View class structure ───────────────────────────────────────────


def test_health_view_has_correct_url() -> None:
    from custom_components.intentional.api import IntentionalHealthView

    assert IntentionalHealthView.url == "/api/intentional/health"
    assert IntentionalHealthView.requires_auth is True
    assert IntentionalHealthView.name == "api:intentional:health"


def test_rules_view_has_correct_url() -> None:
    from custom_components.intentional.api import IntentionalRulesView

    assert IntentionalRulesView.url == "/api/intentional/rules"
    assert IntentionalRulesView.requires_auth is True


def test_rule_view_supports_get_put_delete() -> None:
    """The /rules/{filename} endpoint should support GET, PUT, DELETE."""
    from custom_components.intentional.api import IntentionalRuleView

    assert hasattr(IntentionalRuleView, "get")
    assert hasattr(IntentionalRuleView, "put")
    assert hasattr(IntentionalRuleView, "delete")


def test_rule_history_views_have_correct_urls() -> None:
    from custom_components.intentional.api import (
        IntentionalRuleDocumentView,
        IntentionalRuleHistoryGenerationView,
        IntentionalRuleHistoryView,
        IntentionalRuleRollbackView,
    )

    assert IntentionalRuleDocumentView.url == "/api/intentional/rules/document"
    assert IntentionalRuleDocumentView.requires_auth is True
    assert IntentionalRuleHistoryView.url == "/api/intentional/rules/history"
    assert IntentionalRuleHistoryView.requires_auth is True
    assert "{generation" in IntentionalRuleHistoryGenerationView.url
    assert IntentionalRuleHistoryGenerationView.requires_auth is True
    assert IntentionalRuleRollbackView.url == "/api/intentional/rules/rollback"
    assert IntentionalRuleRollbackView.requires_auth is True


def test_reload_view_accepts_post() -> None:
    from custom_components.intentional.api import IntentionalReloadView

    assert IntentionalReloadView.url == "/api/intentional/reload"
    assert hasattr(IntentionalReloadView, "post")


def test_state_view_exposes_state() -> None:
    from custom_components.intentional.api import IntentionalStateView

    assert IntentionalStateView.url == "/api/intentional/state"
    assert hasattr(IntentionalStateView, "get")


def test_explain_view_supports_target_param() -> None:
    from custom_components.intentional.api import IntentionalExplainView

    # URL pattern with a parameter
    assert "{target" in IntentionalExplainView.url
    assert hasattr(IntentionalExplainView, "get")


async def test_explain_view_reports_rule_firing_status() -> None:
    from custom_components.intentional._engine import Engine
    from custom_components.intentional._engine.yaml_loader import Rule
    from custom_components.intentional.api import IntentionalExplainView
    from custom_components.intentional.const import CONF_RULE_DIR, DOMAIN

    engine = Engine(clock_fn=lambda: 1000)
    engine.load_rules([
        Rule(
            id="rule-on",
            when="input_boolean.test == 'on'",
            target="light.test",
            set={"state": "on"},
        )
    ])
    engine.update_state("input_boolean.test", "on")
    engine.evaluate_all()

    entry = SimpleNamespace(entry_id="entry-1", data={CONF_RULE_DIR: "/tmp/rules"})
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_entries=lambda domain: [entry]),
        data={DOMAIN: {"entry-1": engine}},
    )
    request = SimpleNamespace(app={"hass": hass})

    response = await IntentionalExplainView().get(request, "light.test")
    body = json.loads(response.body.decode())

    assert response.status == 200
    assert body["target"] == "light.test"
    assert body["rules_for_target"] == [
        {
            "rule_id": "rule-on",
            "firing": True,
            "condition_firing": True,
            "blocked_by": [],
            "for_remaining_ms": None,
        },
    ]


def test_all_views_require_auth() -> None:
    """All API views must require authentication."""
    from custom_components.intentional.api import (
        IntentionalExplainView,
        IntentionalHealthView,
        IntentionalReloadView,
        IntentionalRuleDocumentView,
        IntentionalRuleHistoryGenerationView,
        IntentionalRuleHistoryView,
        IntentionalRuleRollbackView,
        IntentionalRulesView,
        IntentionalRuleView,
        IntentionalStateView,
    )

    for view_cls in [
        IntentionalHealthView,
        IntentionalRulesView,
        IntentionalRuleDocumentView,
        IntentionalRuleHistoryView,
        IntentionalRuleHistoryGenerationView,
        IntentionalRuleRollbackView,
        IntentionalRuleView,
        IntentionalReloadView,
        IntentionalStateView,
        IntentionalExplainView,
    ]:
        assert view_cls.requires_auth is True, (
            f"{view_cls.__name__} must require auth"
        )


def test_rule_document_response_has_no_file_semantics() -> None:
    from custom_components.intentional.api import _rule_document_response

    store = SimpleNamespace(
        contents="- id: one\n  when: sensor.x == 'on'\n  emit:\n    target: light.x\n",
        generation="abc123",
        list_rules=lambda: [{"id": "one"}],
    )

    assert _rule_document_response(store) == {
        "contents": store.contents,
        "size": len(store.contents.encode("utf-8")),
        "generation": "abc123",
        "rule_count": 1,
        "source": "storage",
    }


# ── Error format consistency ───────────────────────────────────────


def test_error_returns_json_response() -> None:
    from aiohttp import web

    from custom_components.intentional.api import _error

    resp = _error("test message", "test_code", 400)
    assert isinstance(resp, web.Response)
    assert resp.status == 400
    # Body should be JSON
    assert resp.body is not None


def test_error_default_status_is_400() -> None:

    from custom_components.intentional.api import _error

    resp = _error("test", "test_code")
    assert resp.status == 400


def test_error_custom_status() -> None:

    from custom_components.intentional.api import _error

    for status in [400, 404, 500, 503]:
        resp = _error("test", "test_code", status)
        assert resp.status == status


# ── Register API ────────────────────────────────────────────────────


def test_register_api_is_callable() -> None:
    """register_api should be a callable that takes a hass instance."""
    from custom_components.intentional.api import register_api

    assert callable(register_api)


def test_register_api_registers_history_before_filename_route() -> None:
    from custom_components.intentional.api import register_api

    registered = []
    hass = SimpleNamespace(
        http=SimpleNamespace(register_view=lambda view: registered.append(view.url))
    )

    register_api(hass)

    assert registered.index("/api/intentional/rules/history") < registered.index(
        "/api/intentional/rules/{filename:.+}"
    )
    assert registered.index("/api/intentional/rules/document") < registered.index(
        "/api/intentional/rules/{filename:.+}"
    )


# ── URL pattern coverage ────────────────────────────────────────────


def test_url_patterns_are_unique() -> None:
    """No two views should register the same URL."""
    from custom_components.intentional.api import (
        IntentionalExplainView,
        IntentionalHealthView,
        IntentionalReloadView,
        IntentionalRuleDocumentView,
        IntentionalRuleHistoryGenerationView,
        IntentionalRuleHistoryView,
        IntentionalRuleRollbackView,
        IntentionalRulesView,
        IntentionalRuleView,
        IntentionalStateView,
    )

    urls = [
        IntentionalHealthView.url,
        IntentionalRulesView.url,
        IntentionalRuleDocumentView.url,
        IntentionalRuleHistoryView.url,
        IntentionalRuleHistoryGenerationView.url,
        IntentionalRuleRollbackView.url,
        IntentionalRuleView.url,
        IntentionalReloadView.url,
        IntentionalStateView.url,
        IntentionalExplainView.url,
    ]
    assert len(urls) == len(set(urls)), f"Duplicate URL patterns: {urls}"


def test_all_urls_under_api_intentional() -> None:
    """All API endpoints should be namespaced under /api/intentional/."""
    from custom_components.intentional.api import (
        IntentionalExplainView,
        IntentionalHealthView,
        IntentionalReloadView,
        IntentionalRuleDocumentView,
        IntentionalRuleHistoryGenerationView,
        IntentionalRuleHistoryView,
        IntentionalRuleRollbackView,
        IntentionalRulesView,
        IntentionalRuleView,
        IntentionalStateView,
    )

    for view_cls in [
        IntentionalHealthView,
        IntentionalRulesView,
        IntentionalRuleDocumentView,
        IntentionalRuleHistoryView,
        IntentionalRuleHistoryGenerationView,
        IntentionalRuleRollbackView,
        IntentionalRuleView,
        IntentionalReloadView,
        IntentionalStateView,
        IntentionalExplainView,
    ]:
        assert view_cls.url.startswith("/api/intentional/"), (
            f"{view_cls.__name__}.url = {view_cls.url!r} "
            f"is not under /api/intentional/"
        )
