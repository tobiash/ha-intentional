"""Static regression guard: no sync filesystem call from config_flow.

History
-------
v0.3.0..v0.3.3 shipped a config flow where every rule-files helper
(``_list_rule_files``, ``_read_rule_file``, ``_write_rule_file``,
``_delete_rule_file``) was called *synchronously* from async handlers.
HA detects blocking I/O on the event loop and either logs warnings
or, worse, returns HTTP 500. The fix in v0.3.4 routes every such
call through ``hass.async_add_executor_job`` via the ``_*_in_executor``
module-level helpers in ``config_flow``.

This test enforces that pattern by AST-inspecting ``config_flow.py``:

- Every call to a rule_files sync helper must be wrapped in
  ``self.hass.async_add_executor_job(...)`` — i.e. it must be a
  *direct* call on a ``_*_in_executor`` helper (not the raw
  ``_list_rule_files`` etc.).
- The only acceptable direct calls to ``_validate_rule_dir`` are
  fine because it's pure-string validation with no I/O.

If a future maintainer reintroduces a sync call, this test fails
at the line number — the failure message is the line itself.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

CONFIG_FLOW = (
    Path(__file__).parent.parent
    / "custom_components"
    / "intentional"
    / "config_flow.py"
)


def _get_sync_rule_file_helpers(tree: ast.Module) -> set[str]:
    """Return the set of sync rule_files helpers imported in this module."""
    sync_helpers = {
        "_list_rule_files",
        "_read_rule_file",
        "_write_rule_file",
        "_delete_rule_file",
    }
    found: set[str] = set()
    for node in ast.walk(tree):
        # ``from .rule_files import X, Y`` shows up as
        # ImportFrom(module="rule_files", level=1). We accept both
        # absolute (``from .rule_files``) and relative ``from rule_files``
        # just in case.
        if not isinstance(node, ast.ImportFrom):
            continue
        module = node.module or ""
        if module == "rule_files" or module.endswith(".rule_files"):
            for alias in node.names:
                if alias.name in sync_helpers:
                    found.add(alias.name)
    return found


def _all_call_sites(tree: ast.Module) -> list[tuple[int, str]]:
    """Return [(line, callee_name), ...] for every function call in the file."""
    sites: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            sites.append((node.lineno, node.func.id))
    return sites


def test_no_sync_rule_files_called_from_config_flow() -> None:
    """No sync rule_files helper may be called directly from config_flow.

    Every call must go through a ``_*_in_executor`` helper. The only
    exception is ``_validate_rule_dir`` (pure-string validation, no I/O).
    """
    source = CONFIG_FLOW.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(CONFIG_FLOW))

    sync_helpers = _get_sync_rule_file_helpers(tree)
    call_sites = _all_call_sites(tree)

    # Group violations by line for a useful failure message.
    violations: list[tuple[int, str]] = []
    for line, callee in call_sites:
        if callee in sync_helpers:
            violations.append((line, callee))

    assert not violations, (
        f"\n\nconfig_flow.py calls sync rule_files helpers directly — this\n"
        f"blocks the event loop and triggers HA blocking-I/O warnings +\n"
        f"500 errors. Wrap every such call in self.hass.async_add_executor_job\n"
        f"via the _*_in_executor module-level helpers. Offending lines:\n\n"
        + "\n".join(
            f"  line {line}: {callee}(...)" for line, callee in violations
        )
        + "\n\n"
        f"Accepted: _validate_rule_dir (pure-string validation, no I/O)\n"
        f"Accepted: _list_in_executor / _read_in_executor / "
        f"_write_in_executor / _delete_in_executor (executor wrappers)\n"
    )


def test_validate_rule_dir_may_be_called_sync() -> None:
    """_validate_rule_dir is pure-string validation — sync is fine.

    This test documents the one exception. If you ever add I/O to
    ``_validate_rule_dir``, change this test and also fix the rule.
    """
    source = CONFIG_FLOW.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(CONFIG_FLOW))

    sync_calls = [
        (line, callee)
        for line, callee in _all_call_sites(tree)
        if callee == "_validate_rule_dir"
    ]
    # The two expected sync callsites: initial setup + general options.
    assert len(sync_calls) >= 1, (
        "_validate_rule_dir is no longer called sync — did you move it "
        "to an executor? If so, also update this test."
    )


def test_executor_wrappers_use_hass_async_add_executor_job() -> None:
    """Every _*_in_executor helper must use hass.async_add_executor_job.

    Defends against someone 'optimizing' the wrapper back to a direct
    call ("it's only one function, why bother with the executor").
    """
    source = CONFIG_FLOW.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(CONFIG_FLOW))

    executor_helpers = {
        "_list_in_executor",
        "_read_in_executor",
        "_write_in_executor",
        "_delete_in_executor",
    }
    found_helpers: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name in executor_helpers:
            found_helpers.add(node.name)
            # Body must contain a call to .async_add_executor_job
            has_executor_call = any(
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and child.func.attr == "async_add_executor_job"
                for child in ast.walk(node)
            )
            assert has_executor_call, (
                f"{node.name} must use hass.async_add_executor_job — "
                f"it exists specifically to keep blocking I/O off the loop."
            )

    missing = executor_helpers - found_helpers
    assert not missing, (
        f"config_flow.py is missing the executor wrappers: {missing}. "
        f"Each is a one-liner: `return await hass.async_add_executor_job(...)`. "
        f"They were added in v0.3.4 to fix the blocking-I/O 500s."
    )


def test_no_invalid_text_selector_keys() -> None:
    """Text selectors must only use keys defined in TextSelectorConfig.

    v0.2.0..v0.3.3 used ``{"text": {"multiline": True, "rows": 20}}`` —
    ``rows`` is NOT a valid TextSelector key in HA. HA validates
    selector config at show_form time and raises InvalidData, which
    the user sees as a 500. Valid keys are exactly:
        read_only, multiline, prefix, suffix, type, autocomplete, multiple
    """
    import re

    source = CONFIG_FLOW.read_text(encoding="utf-8")
    # Find every {"text": {...}} literal in the file
    text_selector_pattern = re.compile(
        r'\{\s*"text"\s*:\s*\{([^}]*)\}\s*\}', re.DOTALL
    )
    valid_keys = {
        "read_only",
        "multiline",
        "prefix",
        "suffix",
        "type",
        "autocomplete",
        "multiple",
    }
    violations: list[tuple[int, str]] = []
    for line_no, line in enumerate(source.splitlines(), start=1):
        for match in text_selector_pattern.finditer(line):
            inner = match.group(1)
            # Extract keys from "key": value pairs
            for key_match in re.finditer(r'"(\w+)"\s*:', inner):
                key = key_match.group(1)
                if key not in valid_keys:
                    violations.append((line_no, key))

    assert not violations, (
        f"\n\nconfig_flow.py uses invalid keys in text selectors — HA's\n"
        f"TextSelectorConfig only accepts: {sorted(valid_keys)}.\n"
        f"Invalid keys raise InvalidData → 500. Offending lines:\n\n"
        + "\n".join(f"  line {line}: key {key!r}" for line, key in violations)
    )


if __name__ == "__main__":
    # Allow running this file directly for a quick check.
    pytest.main([__file__, "-v"])
