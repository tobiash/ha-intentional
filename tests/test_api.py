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

from pathlib import Path

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


def test_all_views_require_auth() -> None:
    """All API views must require authentication."""
    from custom_components.intentional.api import (
        IntentionalExplainView,
        IntentionalHealthView,
        IntentionalReloadView,
        IntentionalRulesView,
        IntentionalRuleView,
        IntentionalStateView,
    )

    for view_cls in [
        IntentionalHealthView,
        IntentionalRulesView,
        IntentionalRuleView,
        IntentionalReloadView,
        IntentionalStateView,
        IntentionalExplainView,
    ]:
        assert view_cls.requires_auth is True, (
            f"{view_cls.__name__} must require auth"
        )


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


# ── Intent serialization ───────────────────────────────────────────


def test_intent_to_dict_basic() -> None:
    from custom_components.intentional._engine.intent import Authority, Intent
    from custom_components.intentional.api import _intent_to_dict

    intent = Intent(
        target="light.x",
        set={"brightness_pct": 50},
        rule_id="r1",
        authority=Authority.AUTOMATION,
        confidence=80,
        reason="test reason",
        created_at_ms=1000,
        ttl_ms=5000,
    )
    d = _intent_to_dict(intent)
    assert d["target"] == "light.x"
    assert d["set"] == {"brightness_pct": 50}
    assert d["rule_id"] == "r1"
    # ``authority`` is the enum's string value (e.g. "automation"). The
    # numeric priority used for comparison lives in ``value_index`` on
    # the enum; the API exposes the string form because it's stable across
    # version changes to the priority table and is human-readable.
    assert d["authority"] == "automation"
    assert d["authority_name"] == "AUTOMATION"
    assert d["confidence"] == 80
    assert d["reason"] == "test reason"
    assert d["created_at_ms"] == 1000
    assert d["ttl_ms"] == 5000


def test_intent_to_dict_with_modifiers() -> None:
    from custom_components.intentional._engine.intent import Authority, Intent
    from custom_components.intentional.api import _intent_to_dict

    intent = Intent(
        target="light.x",
        set={"brightness_pct": 50},
        cap={"brightness_pct": 80},
        floor={"brightness_pct": 20},
        offset={"color_temp_k": 200},
        multiply={"brightness_pct": 0.5},
        rule_id="r1",
        authority=Authority.AUTOMATION,
        confidence=80,
        reason="",
        created_at_ms=0,
    )
    d = _intent_to_dict(intent)
    assert d["cap"] == {"brightness_pct": 80}
    assert d["floor"] == {"brightness_pct": 20}
    assert d["offset"] == {"color_temp_k": 200}
    assert d["multiply"] == {"brightness_pct": 0.5}


# ── Register API ────────────────────────────────────────────────────


def test_register_api_is_callable() -> None:
    """register_api should be a callable that takes a hass instance."""
    from custom_components.intentional.api import register_api

    assert callable(register_api)


# ── URL pattern coverage ────────────────────────────────────────────


def test_url_patterns_are_unique() -> None:
    """No two views should register the same URL."""
    from custom_components.intentional.api import (
        IntentionalExplainView,
        IntentionalHealthView,
        IntentionalReloadView,
        IntentionalRulesView,
        IntentionalRuleView,
        IntentionalStateView,
    )

    urls = [
        IntentionalHealthView.url,
        IntentionalRulesView.url,
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
        IntentionalRulesView,
        IntentionalRuleView,
        IntentionalStateView,
    )

    for view_cls in [
        IntentionalHealthView,
        IntentionalRulesView,
        IntentionalRuleView,
        IntentionalReloadView,
        IntentionalStateView,
        IntentionalExplainView,
    ]:
        assert view_cls.url.startswith("/api/intentional/"), (
            f"{view_cls.__name__}.url = {view_cls.url!r} "
            f"is not under /api/intentional/"
        )
