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
# In CI this is also covered by PYTHONPATH=src in the workflow, but we set
# it here so the tests work without the env var.
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

# Integration tests: need custom_components/ on sys.path so
# `from custom_components.intentional.api import ...` works.
if str(CUSTOM_COMPONENTS_DIR) not in sys.path:
    sys.path.insert(0, str(CUSTOM_COMPONENTS_DIR))
