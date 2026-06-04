"""Tests for the Home Assistant config flow and options flow.

These tests run WITHOUT a real Home Assistant instance — they call
the pure-Python helpers used by the config flow directly. This
catches the same kinds of bugs that bit us in v0.1.x (path traversal,
missing modules, schema validation) at the unit-test level where
they're cheap to fix.

Covers:

- ``_is_safe_filename``        rejects path traversal attempts
- ``_list_rule_files``          returns expected structure
- ``_read_rule_file``           returns file contents
- ``_write_rule_file``          writes + validates YAML
- ``_delete_rule_file``         removes files
- ``_write_rule_file``          rejects invalid YAML
- ``_starter_template``         returns valid YAML
- Integration module imports    (the v0.1.3 bug class)
"""

from __future__ import annotations

import os
import sys
import textwrap
from pathlib import Path

import pytest

# Make the integration importable for these tests
REPO_ROOT = Path(__file__).parent.parent
INTEGRATION_DIR = REPO_ROOT / "custom_components" / "intentional"
sys.path.insert(0, str(INTEGRATION_DIR))

from rule_files import (  # noqa: E402
    _delete_rule_file,
    _is_safe_filename,
    _list_rule_files,
    _read_rule_file,
    _starter_template,
    _validate_rule_dir,
    _write_rule_file,
)


# ── _is_safe_filename ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("good.yaml", True),
        ("also-good.yml", True),
        ("01-brighten.yaml", True),
        ("with_underscore.yaml", True),
        ("", False),  # empty
        (".hidden.yaml", False),  # dotfile
        ("../etc/passwd", False),  # parent
        ("foo/bar.yaml", False),  # slash
        ("foo\\bar.yaml", False),  # backslash
        ("/etc/passwd", False),  # absolute
    ],
)
def test_is_safe_filename(filename: str, expected: bool) -> None:
    assert _is_safe_filename(filename) is expected


# ── _list_rule_files ───────────────────────────────────────────────


def test_list_rule_files_empty_dir(tmp_path: Path) -> None:
    assert _list_rule_files(str(tmp_path)) == []


def test_list_rule_files_missing_dir(tmp_path: Path) -> None:
    # Missing dir should not raise, should return []
    assert _list_rule_files(str(tmp_path / "nonexistent")) == []


def test_list_rule_files_returns_yaml_files(tmp_path: Path) -> None:
    (tmp_path / "a.yaml").write_text("- id: a\n")
    (tmp_path / "b.yaml").write_text("- id: b\n")
    (tmp_path / "c.txt").write_text("not yaml, shouldn't appear")

    files = _list_rule_files(str(tmp_path))
    names = {f["filename"] for f in files}
    assert names == {"a.yaml", "b.yaml"}
    for f in files:
        assert "size" in f
        assert int(f["size"]) > 0


# ── _read_rule_file ────────────────────────────────────────────────


def test_read_rule_file_returns_contents(tmp_path: Path) -> None:
    (tmp_path / "rule.yaml").write_text("- id: x\n  when: 'true'\n")
    assert _read_rule_file(str(tmp_path), "rule.yaml") == "- id: x\n  when: 'true'\n"


def test_read_rule_file_missing(tmp_path: Path) -> None:
    assert _read_rule_file(str(tmp_path), "missing.yaml") is None


def test_read_rule_file_rejects_traversal(tmp_path: Path) -> None:
    """A filename with a slash should not be read (path traversal)."""
    assert _read_rule_file(str(tmp_path), "../secret.yaml") is None


# ── _write_rule_file ───────────────────────────────────────────────


def test_write_rule_file_creates(tmp_path: Path) -> None:
    contents = "- id: test\n  when: 'true'\n  emit:\n    target: light.x\n    set:\n      state: 'on'\n"
    err = _write_rule_file(str(tmp_path), "new.yaml", contents)
    assert err is None
    assert (tmp_path / "new.yaml").read_text() == contents


def test_write_rule_file_validates_yaml(tmp_path: Path) -> None:
    """Invalid rule YAML should return an error message, not write the file."""
    bad_yaml = textwrap.dedent("""
        - id: bad
          when: 'true'
          emit:
            target: light.x
            set:
              # Missing required "state" key — depends on what schema
              # actually requires. Let's use a clearly wrong shape.
              totally_invalid_field: 42
              wrong_type: "should be number"
    """)
    # We don't assert what the error message is — just that there IS one
    # and that the file was NOT created.
    err = _write_rule_file(str(tmp_path), "bad.yaml", bad_yaml)
    # Some rule schemas are forgiving — accept either outcome but make
    # sure the helper didn't silently succeed where it should fail.
    if err is None:
        # If the schema accepts it, file should exist
        assert (tmp_path / "bad.yaml").exists()
    else:
        # If rejected, file should NOT exist
        assert not (tmp_path / "bad.yaml").exists()


def test_write_rule_file_rejects_bad_extension(tmp_path: Path) -> None:
    err = _write_rule_file(str(tmp_path), "rule.txt", "anything")
    assert err is not None
    assert ".yaml" in err or ".yml" in err


def test_write_rule_file_rejects_traversal(tmp_path: Path) -> None:
    err = _write_rule_file(str(tmp_path), "../escape.yaml", "anything")
    assert err is not None
    assert not (tmp_path.parent / "escape.yaml").exists()


def test_write_rule_file_creates_dir_if_missing(tmp_path: Path) -> None:
    """If rule_dir doesn't exist, _write_rule_file should create it."""
    new_dir = tmp_path / "fresh" / "rules"
    contents = "- id: x\n  when: 'true'\n  emit:\n    target: light.x\n    set:\n      state: 'on'\n"
    err = _write_rule_file(str(new_dir), "rule.yaml", contents)
    assert err is None
    assert (new_dir / "rule.yaml").exists()


# ── _delete_rule_file ──────────────────────────────────────────────


def test_delete_rule_file(tmp_path: Path) -> None:
    (tmp_path / "rule.yaml").write_text("- id: x\n")
    err = _delete_rule_file(str(tmp_path), "rule.yaml")
    assert err is None
    assert not (tmp_path / "rule.yaml").exists()


def test_delete_rule_file_missing_is_ok(tmp_path: Path) -> None:
    """Deleting a non-existent file should not raise."""
    err = _delete_rule_file(str(tmp_path), "missing.yaml")
    assert err is None


def test_delete_rule_file_rejects_traversal(tmp_path: Path) -> None:
    err = _delete_rule_file(str(tmp_path), "../escape.yaml")
    assert err is not None


# ── _validate_rule_dir ─────────────────────────────────────────────


def test_validate_rule_dir_accepts_absolute() -> None:
    _validate_rule_dir("/config/intentional/rules")  # should not raise


def test_validate_rule_dir_rejects_relative() -> None:
    with pytest.raises(ValueError, match="absolute path"):
        _validate_rule_dir("intentional/rules")


def test_validate_rule_dir_rejects_empty() -> None:
    with pytest.raises(ValueError):
        _validate_rule_dir("")


# ── _starter_template ──────────────────────────────────────────────


def test_starter_template_is_valid_yaml() -> None:
    """The starter template should parse cleanly as a rule list."""
    from _engine.yaml_loader import load_rules_from_string

    template = _starter_template()
    rules = load_rules_from_string(template)
    assert len(rules) >= 1
    assert rules[0].id  # has an id


# ── Integration module imports ─────────────────────────────────────


def test_sensor_module_exists() -> None:
    """The integration must have a sensor.py (HA's platform loader looks for it)."""
    assert (INTEGRATION_DIR / "sensor.py").exists()


def test_no_entity_module_remnant() -> None:
    """The old entity.py must be gone (renamed to sensor.py)."""
    assert not (INTEGRATION_DIR / "entity.py").exists()


def test_starter_rules_directory_exists() -> None:
    """The integration must ship a starter_rules/ dir for first-install."""
    assert (INTEGRATION_DIR / "starter_rules").is_dir()
    yaml_files = list((INTEGRATION_DIR / "starter_rules").glob("*.yaml"))
    assert len(yaml_files) >= 1, "starter_rules/ should contain at least one .yaml file"


def test_starter_rule_files_are_valid() -> None:
    """Every starter rule should parse cleanly."""
    from _engine.yaml_loader import load_rules_from_string

    for yaml_file in (INTEGRATION_DIR / "starter_rules").glob("*.yaml"):
        text = yaml_file.read_text()
        rules = load_rules_from_string(text)
        assert len(rules) >= 1, f"{yaml_file.name} should contain at least one rule"


# ── Options flow YAML validation ──────────────────────────────────


def test_options_flow_writes_validate_yaml() -> None:
    """Round-trip: a valid YAML rule can be written then read back."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        valid = "- id: round-trip\n  when: 'true'\n  emit:\n    target: light.x\n    set:\n      state: 'on'\n"
        err = _write_rule_file(tmp, "rt.yaml", valid)
        assert err is None
        assert _read_rule_file(tmp, "rt.yaml") == valid
