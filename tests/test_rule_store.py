from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("homeassistant", reason="homeassistant not installed")

from custom_components.intentional.rule_files import _read_rule_dir_as_yaml
from intentional.yaml_loader import load_rules_from_string


def test_read_rule_dir_as_yaml_imports_yaml_files(tmp_path: Path) -> None:
    (tmp_path / "b.yaml").write_text(
        "- id: b\n"
        "  observe: { input_boolean.b: 'on' }\n"
        "  intent: { light.b: { state: 'on' } }\n",
        encoding="utf-8",
    )
    (tmp_path / "a.yml").write_text(
        "- id: a\n"
        "  observe: { input_boolean.a: 'on' }\n"
        "  intent: { light.a: { state: 'on' } }\n",
        encoding="utf-8",
    )
    (tmp_path / "ignored.txt").write_text("not yaml", encoding="utf-8")

    contents = _read_rule_dir_as_yaml(str(tmp_path))
    rules = load_rules_from_string(contents)

    assert [rule.id for rule in rules] == ["a", "b"]


def test_read_rule_dir_as_yaml_missing_dir_is_empty(tmp_path: Path) -> None:
    assert _read_rule_dir_as_yaml(str(tmp_path / "missing")) == ""
