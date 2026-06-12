"""Tests for the bundled Intentional rule editor panel."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
PANEL_PATH = REPO_ROOT / "custom_components" / "intentional" / "frontend" / "intentional-panel.js"
INTEGRATION_INIT = REPO_ROOT / "custom_components" / "intentional" / "__init__.py"


def test_frontend_panel_asset_is_bundled() -> None:
    source = PANEL_PATH.read_text()

    assert 'customElements.define("intentional-panel"' in source
    assert '"rules/document"' in source
    assert '"validate"' in source
    assert '"dry-run"' in source
    assert '"simulate"' in source
    assert '"rules/history"' in source
    assert '"rules/rollback"' in source


def test_frontend_panel_has_visual_rule_editor() -> None:
    source = PANEL_PATH.read_text()

    assert '_editorMode = "visual"' in source
    assert 'Visual rule editor' in source
    assert 'Rule editor' in source
    assert 'while → intent' in source
    assert 'Build a durable' in source
    assert 'Add Condition' in source
    assert 'Add Target' in source
    assert 'Add Effect' in source


def test_frontend_panel_keeps_yaml_escape_hatches() -> None:
    source = PANEL_PATH.read_text()

    assert 'Rule YAML' in source
    assert 'Document YAML' in source
    assert 'extractRuleBlock' in source
    assert 'replaceRuleBlock' in source
    assert '_uniqueRules()' in source


def test_frontend_panel_has_no_install_validation_loop() -> None:
    source = PANEL_PATH.read_text()

    assert '_queueValidate()' in source
    assert '_validateLocally()' in source
    assert 'Fix the highlighted fields before saving.' in source
    assert 'Dry-run evaluates desired targets without applying services.' in source
    assert 'Use simulation for after/hold timing before installing on the live instance.' in source
    assert 'this._api("POST", "validate"' in source
    assert 'this._api("POST", "dry-run"' in source
    assert 'this._api("POST", "simulate"' in source


def test_integration_registers_frontend_panel_asset() -> None:
    source = INTEGRATION_INIT.read_text()

    assert 'FRONTEND_URL_PATH = "/api/intentional/frontend"' in source
    assert 'PANEL_URL_PATH = "intentional"' in source
    assert 'DEPENDENCIES = ["http"]' in source
    assert "async_register_static_paths" in source
    assert "panel_custom.async_register_panel" in source
    assert 'webcomponent_name="intentional-panel"' in source
    assert 'module_url=f"{FRONTEND_URL_PATH}/intentional-panel.js?v={__version__}"' in source
