"""Tests for repo-local agent skills."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
SKILL_PATH = REPO_ROOT / ".agents" / "skills" / "intentional-api" / "SKILL.md"


def test_intentional_api_skill_is_bundled() -> None:
    source = SKILL_PATH.read_text()

    assert "name: intentional-api" in source
    assert "description:" in source
    assert "Use when" in source
    assert "/api/intentional/rules/document" in source
    assert "expected_generation" in source
    assert "/api/intentional/dry-run" in source
    assert "/api/intentional/simulate" in source
    assert "/api/intentional/rules/rollback" in source
    assert "Never echo the token" in source
    assert "hold.until" in source
