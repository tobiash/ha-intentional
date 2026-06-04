"""Shared pytest fixtures and configuration for the test suite.

Most fixtures are defined in the test modules that use them
(fixtures like ``rule_dir`` are local to ``test_integration.py``).
This file holds only what's genuinely shared.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make the engine importable as a top-level package for tests
# that want to do `import intentional` or `from intentional import ...`.
# This mirrors the layout when HACS installs the integration.
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

# Make the integration importable as a top-level package.
# Tests can then `import api`, `from rule_files import ...`, etc.
sys.path.insert(0, str(Path(__file__).parent.parent / "custom_components" / "intentional"))
