"""Pure-Python helpers for the Home Assistant config flow.

This module is deliberately kept free of Home Assistant imports so it
can be unit-tested in isolation. The config_flow module imports from
here; tests can too, without needing a full HA install.

The functions handle:

- Validating user-supplied rule-directory paths
- Listing rule files in a directory
- Reading, writing, and deleting rule files
- Rejecting path-traversal attempts (e.g. ``../etc/passwd``)
- Validating YAML contents before writing

If we ever add a "rule editor panel" to the HA frontend, the same
helpers would back it.
"""

from __future__ import annotations

import logging
from pathlib import Path

from _engine import RuleLoadError
from _engine.yaml_loader import load_rules_from_string

_LOGGER = logging.getLogger(__name__)


def _validate_rule_dir(rule_dir: str) -> None:
    """Best-effort validation of the rule directory path.

    We don't require the directory to exist at config time — the user
    may want to create it later. We just sanity-check the path string.
    """
    if not rule_dir or not isinstance(rule_dir, str):
        raise ValueError("Rule directory must be a non-empty string")
    if not rule_dir.startswith("/"):
        raise ValueError(
            f"Rule directory must be an absolute path (got {rule_dir!r}). "
            "On Home Assistant OS, the default is /config/intentional/rules/."
        )


def _is_safe_filename(filename: str) -> bool:
    """Reject path traversal attempts and hidden files.

    A "safe" filename:
    - Is non-empty
    - Does not start with a dot (no dotfiles)
    - Contains no path separators (``/`` or ``\\``)
    - Is not an absolute path

    This is intentionally conservative. If a user has a real need for
    nested rule directories, we can add a proper "safe subdirectory"
    helper later.
    """
    return bool(filename) and (
        not filename.startswith(".")
        and "/" not in filename
        and "\\" not in filename
    )


def _list_rule_files(rule_dir: str) -> list[dict[str, str]]:
    """Return a list of {filename, size} dicts for all .yaml files in rule_dir.

    Safe against missing/unreadable directories: returns [] in that case.
    """
    try:
        path = Path(rule_dir)
        if not path.exists() or not path.is_dir():
            return []
        files = []
        for entry in sorted(path.glob("*.yaml")):
            if entry.is_file():
                files.append({
                    "filename": entry.name,
                    "size": str(entry.stat().st_size),
                })
        return files
    except OSError as err:
        _LOGGER.warning("Could not list rule files in %s: %s", rule_dir, err)
        return []


def _read_rule_file(rule_dir: str, filename: str) -> str | None:
    """Safely read a rule file's contents. Returns None on error."""
    if not _is_safe_filename(filename):
        _LOGGER.warning("Refusing to read suspicious filename: %r", filename)
        return None
    try:
        path = Path(rule_dir) / filename
        if not path.exists() or not path.is_file():
            return None
        return path.read_text(encoding="utf-8")
    except OSError as err:
        _LOGGER.warning("Could not read %s/%s: %s", rule_dir, filename, err)
        return None


def _write_rule_file(rule_dir: str, filename: str, contents: str) -> str | None:
    """Write contents to filename in rule_dir. Returns error message or None.

    Validates the contents by attempting to parse them as rules.
    Returns a human-readable error message string if anything goes wrong
    (path traversal, bad extension, invalid YAML, IO error).
    """
    if not _is_safe_filename(filename):
        return f"Invalid filename: {filename!r}"
    if not filename.endswith(".yaml") and not filename.endswith(".yml"):
        return "Filename must end in .yaml or .yml"
    # Validate YAML by attempting to parse it
    try:
        load_rules_from_string(contents)
    except RuleLoadError as err:
        return f"Rule validation failed: {err}"
    try:
        path = Path(rule_dir)
        path.mkdir(parents=True, exist_ok=True)
        (path / filename).write_text(contents, encoding="utf-8")
        return None
    except OSError as err:
        return f"Could not write file: {err}"


def _delete_rule_file(rule_dir: str, filename: str) -> str | None:
    """Delete filename from rule_dir. Returns error message or None."""
    if not _is_safe_filename(filename):
        return f"Invalid filename: {filename!r}"
    try:
        path = Path(rule_dir) / filename
        if not path.exists():
            return None
        path.unlink()
        return None
    except OSError as err:
        return f"Could not delete file: {err}"


def _starter_template() -> str:
    """Return a starter YAML rule template for new files.

    The template is intentionally a working example so the user can
    save it as-is, then call ``intentional.reload`` and see something
    happen (it just emits a "test" intent that shows up in the
    summary sensor). The user can then edit it.
    """
    return (
        "# New rule file\n"
        "# Edit and save — the integration will validate and reload.\n"
        "# Tip: Developer Tools → Actions → intentional.reload to apply.\n"
        "\n"
        "- id: example-rule\n"
        "  when: time_of_day == '12:00'\n"
        "  emit:\n"
        "    target: light.example\n"
        "    set:\n"
        "      state: 'on'\n"
    )
