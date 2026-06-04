#!/usr/bin/env python3
"""Verify the bundled engine subpackage is in sync with the source.

Catches the failure mode where someone updates ``src/intentional/``
but forgets to copy the changes to ``custom_components/intentional/_engine/``.
If the two diverge, the integration installed by HACS will be missing
the new code.

Run as part of CI before the test suite. Exits non-zero if drift
is detected.

Usage:
    python ci/check-bundle-sync.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
SOURCE_DIR = REPO_ROOT / "src" / "intentional"
BUNDLE_DIR = REPO_ROOT / "custom_components" / "intentional" / "_engine"


def check_files_match(source: Path, bundle: Path, name: str) -> list[str]:
    """Return a list of error messages if the files have unauthorized drift.

    The bundle is allowed to differ from the source in two ways:
    1. Absolute imports (``from intentional.X import Y``) are converted
       to relative imports (``from .X import Y``). This is required for
       the bundle to work when installed by HACS.
    2. Nothing else — any other difference means the bundle is stale.

    To check #1 without false positives, we normalize both files
    (replace ``from intentional.`` with ``from .``) and compare the
    normalized forms.
    """
    errors: list[str] = []
    if not source.exists():
        errors.append(f"Source file missing: {source}")
        return errors
    if not bundle.exists():
        errors.append(f"Bundle file missing: {bundle} (source exists)")
        return errors
    src_text = source.read_text()
    bundle_text = bundle.read_text()
    # Normalize: convert absolute imports to relative for comparison
    src_normalized = src_text.replace("from intentional.", "from .")
    bundle_normalized = bundle_text.replace("from intentional.", "from .")
    if src_normalized != bundle_normalized:
        # Show a helpful diff hint
        errors.append(
            f"File contents differ (excluding import conversion): {name}\n"
            f"  Source: {source}\n"
            f"  Bundle: {bundle}\n"
            f"  Run: cp {source} {bundle}\n"
            f"  Then: convert any 'from intentional.X' to 'from .X' in the bundle."
        )
    return errors


def check_internal_imports(bundle: Path) -> list[str]:
    """Verify bundle files use relative imports, not absolute 'intentional.X'."""
    errors: list[str] = []
    for py_file in bundle.glob("*.py"):
        text = py_file.read_text()
        for i, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if (stripped.startswith("from intentional.")
                or stripped.startswith("import intentional")):
                errors.append(
                    f"{py_file.name}:{i}: absolute 'intentional' import "
                    f"found in bundle. Convert to relative: {line!r}"
                )
    return errors


def main() -> int:
    if not SOURCE_DIR.exists():
        print(f"ERROR: source directory not found: {SOURCE_DIR}", file=sys.stderr)
        return 1
    if not BUNDLE_DIR.exists():
        print(f"ERROR: bundle directory not found: {BUNDLE_DIR}", file=sys.stderr)
        return 1

    errors: list[str] = []

    # 1. Check that every source file has a matching bundle file
    source_files = sorted(p.name for p in SOURCE_DIR.glob("*.py"))
    bundle_files = sorted(p.name for p in BUNDLE_DIR.glob("*.py"))
    if set(source_files) != set(bundle_files):
        missing = set(source_files) - set(bundle_files)
        extra = set(bundle_files) - set(source_files)
        if missing:
            errors.append(
                f"Source files missing from bundle: {sorted(missing)}\n"
                f"  Run: cp {SOURCE_DIR}/<file> {BUNDLE_DIR}/<file>"
            )
        if extra:
            errors.append(
                f"Bundle files missing from source: {sorted(extra)}\n"
                f"  Either remove from bundle or add to source."
            )

    # 2. Check that matching files have identical contents
    for name in source_files:
        if name in bundle_files:
            errors.extend(check_files_match(
                SOURCE_DIR / name, BUNDLE_DIR / name, name
            ))

    # 3. Check that bundle uses relative imports
    errors.extend(check_internal_imports(BUNDLE_DIR))

    if errors:
        print("Bundle sync check FAILED:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        print(
            "\nTo fix: copy source files to bundle:\n"
            f"  for f in {SOURCE_DIR}/*.py; do "
            f"cp \"$f\" {BUNDLE_DIR}/; done\n"
            "Then convert any absolute 'from intentional.X' imports to "
            "relative 'from .X import Y'.",
            file=sys.stderr,
        )
        return 1

    print(
        f"Bundle sync OK: {len(source_files)} files match "
        f"({SOURCE_DIR} ↔ {BUNDLE_DIR})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
