"""Sync pure engine source modules into the Home Assistant bundled package."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "intentional"
BUNDLED = ROOT / "custom_components" / "intentional" / "_engine"

MODULES = (
    "animation.py",
    "capabilities.py",
    "compositor.py",
    "durations.py",
    "engine.py",
    "generation.py",
    "ha_adapter.py",
    "intent.py",
    "lifecycle.py",
    "presentation.py",
    "projection.py",
    "records.py",
    "reconciliation.py",
    "registry.py",
    "rule_lifecycle.py",
    "rule_model.py",
    "runtime.py",
    "schema.py",
    "selectors.py",
    "simulation.py",
    "target_policy.py",
    "templates.py",
    "when_parser.py",
    "yaml_loader.py",
)

# Sub-packages whose .py files are synced individually.
PACKAGES = (
    "adapter",
)


def _sync_text(text: str) -> str:
    return text.replace("from intentional.", "from .")


def main() -> None:
    BUNDLED.mkdir(parents=True, exist_ok=True)
    for module in MODULES:
        text = (SOURCE / module).read_text(encoding="utf-8")
        (BUNDLED / module).write_text(_sync_text(text), encoding="utf-8")
    for pkg in PACKAGES:
        src_pkg = SOURCE / pkg
        dst_pkg = BUNDLED / pkg
        dst_pkg.mkdir(parents=True, exist_ok=True)
        for py_file in sorted(src_pkg.glob("*.py")):
            text = py_file.read_text(encoding="utf-8")
            (dst_pkg / py_file.name).write_text(_sync_text(text), encoding="utf-8")


if __name__ == "__main__":
    main()
