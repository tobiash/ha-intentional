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

# The integration (``custom_components/intentional/``) bundles its own
# copy of the engine as ``custom_components/intentional/_engine/`` so
# HACS installs it. The source repo also has ``src/intentional/`` for
# development. These are *two separate copies* of the same package:
#
#   - src/intentional/                → top-level package, used by tests
#   - custom_components/intentional/_engine/ → bundled subpackage
#
# Putting BOTH on sys.path would create a name collision (two
# ``intentional`` packages). The convention is:
#
#   - Tests that exercise the engine (test_intent.py, test_compositor.py,
#     test_engine.py, etc.) use ``src/intentional/`` via
#     ``PYTHONPATH=src`` — see pyproject.toml pytest config.
#   - Tests that exercise the integration (test_api.py,
#     test_integration.py, test_config_flow.py) need
#     ``custom_components.intentional`` to be importable so that the
#     integration's relative imports (``from .const import ...``)
#     resolve correctly. This is how HACS loads it in production.
#
# We therefore add only ``custom_components/`` to sys.path here. The
# engine tests use the source layout, the integration tests use the
# bundled layout, and the two never collide.

sys.path.insert(0, str(CUSTOM_COMPONENTS_DIR))
