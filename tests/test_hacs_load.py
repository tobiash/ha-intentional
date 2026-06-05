"""Smoke-load test that simulates a HACS install.

The other integration tests (test_integration.py, test_config_flow.py)
work by adding ``custom_components/intentional/`` to ``sys.path``
themselves — which is why they don't catch the v0.3.1 bug
(``from _engine import ...`` in rule_files.py that fails to resolve
in production).

A HACS install puts ``/config/custom_components/`` on ``sys.path``
(HA's loader sets this up), and the integration's own directory
is NOT on the path. To catch this class of bug, we replicate
that exact sys.path configuration and verify the integration
imports cleanly.

This test is the "best" check in the v0.1.4 release-checklist
postmortem: a 30-second test that catches the bug class that
otherwise slips through 178 passing pytest assertions.

This test requires homeassistant to be installed. Without it, the
HACS path assertion would fail before it reaches the import class
it is meant to guard.
"""

from __future__ import annotations

import subprocess as sp
import sys
import textwrap
from pathlib import Path

import pytest

pytest.importorskip("homeassistant", reason="homeassistant not installed")

REPO_ROOT = Path(__file__).parent.parent
CUSTOM_COMPONENTS_DIR = REPO_ROOT / "custom_components"
INTEGRATION_DIR = CUSTOM_COMPONENTS_DIR / "intentional"
INTEGRATION_NAME = "intentional"


def test_integration_loads_with_hacs_sys_path() -> None:
    """The integration must import cleanly when ``custom_components/``
    is on sys.path but the integration's own directory is NOT.

    This simulates exactly what HACS users experience: HA's loader
    adds ``/config/custom_components/`` to sys.path and never adds
    the integration's own dir. The v0.3.1 release failed this test
    because ``rule_files.py`` had ``from _engine import ...`` (bare
    top-level import) which only resolves when the integration dir
    is on sys.path.
    """
    # We use a subprocess to avoid polluting the parent pytest
    # session's sys.modules / sys.path state. The HA test harness
    # in pytest-homeassistant-custom-component caches loaded
    # integration state in module namespaces that survives any
    # in-process cleanup, so the only clean way to test "does the
    # integration load in a HACS-like environment?" is to actually
    # run in a fresh interpreter.

    test_code = textwrap.dedent(
        """
        import importlib, sys
        # Mimic HACS: custom_components/ on path, integration dir NOT
        cc = {cc_dir!r}
        int_dir = {int_dir!r}
        if cc not in sys.path:
            sys.path.insert(0, cc)
        # Purge any cached imports
        for k in list(sys.modules.keys()):
            if k == "custom_components" or k.startswith("custom_components.intentional"):
                del sys.modules[k]
        # Verify the test setup
        assert cc in sys.path, f"{{cc!r}} not in sys.path"
        assert int_dir not in sys.path, f"{{int_dir!r}} should NOT be in sys.path"
        # Now try the import
        try:
            importlib.import_module("custom_components.intentional")
        except ImportError as e:
            print(f"FAIL_INIT: {{type(e).__name__}}: {{e}}")
            sys.exit(1)
        for k in list(sys.modules.keys()):
            if k.startswith("custom_components.intentional"):
                del sys.modules[k]
        try:
            importlib.import_module("custom_components.intentional.config_flow")
        except ImportError as e:
            print(f"FAIL_CF: {{type(e).__name__}}: {{e}}")
            sys.exit(1)
        print("OK")
        """
    ).format(
        cc_dir=str(CUSTOM_COMPONENTS_DIR),
        int_dir=str(INTEGRATION_DIR),
    )

    r = sp.run(
        [sys.executable, "-c", test_code],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, (
        f"Integration failed to load with HACS-like sys.path. "
        f"This is the v0.3.1 bug class — likely a bare import of "
        f"the bundled _engine subpackage. Output: {r.stdout!r} "
        f"Stderr: {r.stderr!r}"
    )
    assert "OK" in r.stdout, f"Unexpected output: {r.stdout!r}"
