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
    "engine.py",
    "ha_adapter.py",
    "intent.py",
    "lifecycle.py",
    "presentation.py",
    "records.py",
    "reconciliation.py",
    "selectors.py",
    "templates.py",
    "when_parser.py",
    "yaml_loader.py",
)


def main() -> None:
    BUNDLED.mkdir(parents=True, exist_ok=True)
    for module in MODULES:
        text = (SOURCE / module).read_text(encoding="utf-8")
        text = text.replace("from intentional.", "from .")
        (BUNDLED / module).write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
