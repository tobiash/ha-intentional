"""Verify the bundled engine subpackage imports and behaves like the source.

Why this test exists
--------------------
The integration ships a bundled copy of the engine at
``custom_components/intentional/_engine/`` so HACS installs don't need
to fetch a separate PyPI package. If that bundled copy ever drifts from
``src/intentional/`` (wrong import paths, missing files, broken
relative imports) the integration will fail at HA load time with
``No module named 'intentional'`` — exactly the kind of error that
v0.1.3 shipped with.

These tests:

- Verify the bundled subpackage is importable as ``._engine``
- Smoke-test that all public symbols (Engine, Rule, Intent, etc.) work
- Cross-check that every file in ``src/intentional/`` is also present
  in ``_engine/`` (catch drift)
- Verify internal relative imports resolve (no ``from intentional.X``
  left over from a half-conversion)
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
BUNDLED_DIR = REPO_ROOT / "custom_components" / "intentional" / "_engine"
SOURCE_DIR = REPO_ROOT / "src" / "intentional"
INTEGRATION_DIR = REPO_ROOT / "custom_components" / "intentional"

# Make the bundled engine importable as a subpackage for the smoke tests
sys.path.insert(0, str(INTEGRATION_DIR))


def test_bundled_engine_directory_exists() -> None:
    assert BUNDLED_DIR.exists()
    assert BUNDLED_DIR.is_dir()


def test_bundled_files_match_source() -> None:
    """Every source file should be present in the bundle (catch drift)."""
    source_files = {p.name for p in SOURCE_DIR.glob("*.py")}
    bundled_files = {p.name for p in BUNDLED_DIR.glob("*.py")}
    # The bundled __init__.py may be a converted version, so we compare
    # the *set* of non-__init__ files at least.
    missing = source_files - bundled_files
    assert not missing, (
        f"Files in src/intentional/ that are missing from "
        f"custom_components/intentional/_engine/: {missing}"
    )


def test_no_absolute_intentional_imports_in_bundle() -> None:
    """No ``from intentional.X import Y`` should remain in the bundle.

    All internal references must be relative (``from .X import Y``) so
    the bundle can be installed anywhere on the Python path.
    """
    offenders: list[str] = []
    for py_file in BUNDLED_DIR.glob("*.py"):
        text = py_file.read_text()
        for i, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            # Skip comments
            if stripped.startswith("#"):
                continue
            if stripped.startswith("from intentional.") or stripped.startswith(
                "import intentional"
            ):
                offenders.append(f"{py_file.name}:{i}: {line}")
    assert not offenders, (
        "Bundle still has absolute 'intentional' imports; "
        "convert to relative ('from .X import Y'):\n"
        + "\n".join(offenders)
    )


def test_no_absolute_intentional_imports_in_integration() -> None:
    """No ``from intentional.X import Y`` should remain in the integration files.

    The integration must use relative imports (``from ._engine.X``) so
    HA can find the bundled engine.
    """
    offenders: list[str] = []
    for py_file in INTEGRATION_DIR.glob("*.py"):
        if py_file.name.startswith("_"):
            continue  # skip _engine subpackage
        text = py_file.read_text()
        for i, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            # We allow `intentional.X` inside docstrings/strings, but
            # actual import statements are not allowed
            if (stripped.startswith("from intentional.") or
                stripped.startswith("import intentional")):
                offenders.append(f"{py_file.name}:{i}: {line}")
    assert not offenders, (
        "Integration has absolute 'intentional' imports; "
        "convert to relative ('from ._engine import ...'):\n"
        + "\n".join(offenders)
    )


def test_bundled_engine_importable() -> None:
    """The bundled engine should be importable as a subpackage."""
    mod = importlib.import_module("_engine")
    assert mod is not None


def test_bundled_engine_public_api() -> None:
    """Public symbols from the source should still be accessible via the bundle."""
    from custom_components.intentional._engine import (
        Engine,
        RuleLoadError,
        load_rules,
    )
    from custom_components.intentional._engine.intent import Authority, Intent
    from custom_components.intentional._engine.yaml_loader import Rule

    # Smoke: build an engine and emit an intent
    engine = Engine()
    assert engine is not None
    rule = Rule(
        id="smoke",
        target="light.x",
        set={"brightness_pct": 50},
        when="true",
    )
    assert rule.id == "smoke"
    intent = Intent(
        target="light.x",
        set={"brightness_pct": 50},
        authority=Authority.USER,
        rule_id="smoke",
        created_at_ms=0,
    )
    assert intent.authority == Authority.USER

    # load_rules / RuleLoadError should be importable even without a path
    assert callable(load_rules)
    assert RuleLoadError is Exception or issubclass(RuleLoadError, Exception)


def test_manifest_declares_pyyaml_requirement() -> None:
    """yaml_loader.py uses PyYAML; manifest.json must declare it so HACS installs it."""
    import json

    manifest = json.loads(
        (INTEGRATION_DIR / "manifest.json").read_text()
    )
    requirements = manifest.get("requirements", [])
    assert any("yaml" in r.lower() for r in requirements), (
        f"manifest.json must declare a PyYAML requirement (yaml_loader.py "
        f"imports yaml). Got: {requirements!r}"
    )
