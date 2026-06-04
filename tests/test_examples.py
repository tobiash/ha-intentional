"""Smoke tests: ensure all example YAML files parse without errors.

These tests exist so that example files in the `examples/` directory
stay in sync with the schema. If a refactor breaks a field, these
tests will catch it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

EXAMPLES_DIR = Path(__file__).parent.parent / "examples"


def test_examples_directory_exists() -> None:
    assert EXAMPLES_DIR.exists()
    assert EXAMPLES_DIR.is_dir()


@pytest.mark.parametrize(
    "yaml_file",
    sorted(EXAMPLES_DIR.glob("*.yaml")),
    ids=lambda p: p.name,
)
def test_example_yaml_parses(yaml_file: Path) -> None:
    """Each example file should parse cleanly with no schema errors."""
    rules = load_rules_from_file(yaml_file)
    assert len(rules) >= 1, f"{yaml_file.name} contains no rules"


def test_examples_have_unique_ids_across_files() -> None:
    """Rule IDs should be unique across all example files.

    This is a soft requirement — users can copy examples and rename,
    but the canonical examples should be unique.
    """
    all_rules = []
    for yaml_file in EXAMPLES_DIR.glob("*.yaml"):
        all_rules.extend(load_rules_from_file(yaml_file))

    ids = [r.id for r in all_rules]
    duplicates = [id_ for id_ in ids if ids.count(id_) > 1]
    assert not duplicates, f"Duplicate rule IDs in examples: {duplicates}"


def load_rules_from_file(path: Path):
    """Helper that wraps load_rules for a single file."""
    from intentional.yaml_loader import load_rules_from_string
    return load_rules_from_string(path.read_text(encoding="utf-8"), file=path)
