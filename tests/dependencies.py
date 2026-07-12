"""Dependency gates for suites that are optional locally but mandatory in CI."""

from __future__ import annotations

import importlib
import os

import pytest


def require_test_dependency(module: str, *, reason: str) -> None:
    """Skip optional local suites, but fail when CI declares them mandatory."""
    if os.environ.get("INTENTIONAL_REQUIRE_HA_TESTS") == "1":
        importlib.import_module(module)
        return
    pytest.importorskip(module, reason=reason)
