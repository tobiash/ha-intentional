"""Shared pytest fixtures and configuration for the test suite.

Most fixtures are defined in the test modules that use them
(fixtures like ``rule_dir`` are local to ``test_integration.py``).
This file holds only what's genuinely shared.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
SRC_DIR = REPO_ROOT / "src"
CUSTOM_COMPONENTS_DIR = REPO_ROOT / "custom_components"

# Two layouts live in this repo:
#
#   - src/intentional/                → engine source (top-level package)
#   - custom_components/intentional/  → HA integration (bundles _engine/)
#
# Engine tests (test_intent.py, test_compositor.py, test_engine.py, ...)
# import via ``from intentional.intent import Intent`` (top-level).
# They need ``src/`` on sys.path.
#
# Integration tests (test_api.py, test_integration.py, test_config_flow.py)
# import via ``from custom_components.intentional.api import ...``. They
# need ``custom_components/`` on sys.path so the integration's relative
# imports (``from .const import ...``) resolve correctly. This mirrors
# how HACS loads the integration in production.
#
# Adding BOTH to sys.path is fine because they live at different
# positions in the package hierarchy — ``intentional`` is a top-level
# package (from src/), and ``custom_components.intentional`` is a
# subpackage (from custom_components/).

# Engine tests: need src/ on sys.path so `from intentional.X import Y` works.
# Insert at position 0 so the bare name `intentional` resolves to the engine
# source, not the HA integration (which lives at custom_components/intentional/).
# In CI this is also covered by the editable install in the workflow, but we
# set it here so the tests work without an install.
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

# Integration tests need TWO things on sys.path:
#
#   1. ``custom_components/`` so ``from custom_components.intentional.X``
#      resolves correctly (used by test_api.py, test_integration.py).
#   2. ``custom_components/intentional/`` itself, because the HA test
#      harness (``pytest-homeassistant-custom-component``) needs the
#      integration to be importable both as ``custom_components.intentional``
#      AND as if it were a top-level integration. Adding the integration
#      dir lets the test harness discover it the way HA does on the
#      user side.
#
# WARNING: This sys.path entry is what hid the v0.3.1 bug, where
# ``rule_files.py`` had ``from _engine import ...`` (a bare top-level
# import) that resolved in tests but failed on HACS user installs.
# We accept that test_integration.py needs this entry, but we ALSO
# have a dedicated smoke-load test (test_hacs_load.py) that strips
# this entry from sys.path and verifies the integration still loads.
# That test is what catches the v0.3.1 bug class going forward.
#
# We use ``append`` (not insert at 0) so neither entry shadows the
# engine ``intentional`` package. Position doesn't matter for the
# integration tests because they use the fully-qualified name.
if str(CUSTOM_COMPONENTS_DIR) not in sys.path:
    sys.path.append(str(CUSTOM_COMPONENTS_DIR))
INTEGRATION_DIR = REPO_ROOT / "custom_components" / "intentional"
if str(INTEGRATION_DIR) not in sys.path:
    sys.path.append(str(INTEGRATION_DIR))
