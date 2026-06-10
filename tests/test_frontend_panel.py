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
    assert '`rules/id/${encodeURIComponent(selectedRuleId)}`' in source
    assert '"validate"' in source
    assert '"dry-run"' in source
    assert '"rules/history"' in source
    assert '"rules/rollback"' in source


def test_frontend_panel_has_focused_rule_editor() -> None:
    source = PANEL_PATH.read_text()

    assert '_editorMode = "rule"' in source
    assert 'Save Rule' in source
    assert 'Document YAML' in source
    assert 'extractRuleBlock' in source
    assert 'replaceRuleBlock' in source
    assert '_uniqueRules()' in source


def test_integration_registers_frontend_panel_asset() -> None:
    source = INTEGRATION_INIT.read_text()

    assert 'FRONTEND_URL_PATH = "/api/intentional/frontend"' in source
    assert 'PANEL_URL_PATH = "intentional"' in source
    assert 'DEPENDENCIES = ["http"]' in source
    assert "async_register_static_paths" in source
    assert "panel_custom.async_register_panel" in source
    assert 'webcomponent_name="intentional-panel"' in source
    assert 'module_url=f"{FRONTEND_URL_PATH}/intentional-panel.js"' in source
