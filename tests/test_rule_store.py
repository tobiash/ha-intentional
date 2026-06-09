from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("homeassistant", reason="homeassistant not installed")

from custom_components.intentional.rule_files import _read_rule_dir_as_yaml
from custom_components.intentional.rule_store import RULE_STORE_FILENAME, StorageRuleStore
from intentional.yaml_loader import load_rules_from_string


class _FakeStore:
    saved: dict | None = None

    def __init__(self, hass, version, key):
        self.hass = hass
        self.version = version
        self.key = key

    async def async_load(self):
        return self.saved

    async def async_save(self, data):
        self.saved = data


@pytest.fixture
def fake_hass():
    async def async_add_executor_job(func, *args):
        return func(*args)

    return SimpleNamespace(async_add_executor_job=async_add_executor_job)


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


async def test_storage_rule_store_records_history_on_write(monkeypatch, fake_hass) -> None:
    from custom_components.intentional import rule_store as rule_store_module

    monkeypatch.setattr(rule_store_module, "Store", _FakeStore)
    store = StorageRuleStore(fake_hass, "entry-1")
    await store.async_load_or_import("/missing")
    original_generation = store.generation

    err = await store.async_write(
        RULE_STORE_FILENAME,
        "- id: next\n  observe: { input_boolean.next: 'on' }\n  intent: { light.next: { state: 'on' } }\n",
    )

    assert err is None
    history = store.list_history()
    assert len(history) == 1
    assert history[0]["generation"] == original_generation
    assert history[0]["rule_count"] == 0
    assert "contents" not in history[0]
    snapshot = store.read_history(original_generation)
    assert snapshot is not None
    assert snapshot["contents"] == "[]\n"


async def test_storage_rule_store_rolls_back_to_history(monkeypatch, fake_hass) -> None:
    from custom_components.intentional import rule_store as rule_store_module

    monkeypatch.setattr(rule_store_module, "Store", _FakeStore)
    store = StorageRuleStore(fake_hass, "entry-1")
    await store.async_load_or_import("/missing")
    original_generation = store.generation
    await store.async_write(
        RULE_STORE_FILENAME,
        "- id: next\n  observe: { input_boolean.next: 'on' }\n  intent: { light.next: { state: 'on' } }\n",
    )
    edited_generation = store.generation

    result = await store.async_rollback(
        original_generation,
        expected_generation=edited_generation,
    )

    assert "error" not in result
    assert result["generation"] == original_generation
    assert result["restored_generation"] == original_generation
    assert store.contents == "[]\n"
    history_generations = [record["generation"] for record in store.list_history()]
    assert edited_generation in history_generations


async def test_storage_rule_store_rollback_checks_expected_generation(monkeypatch, fake_hass) -> None:
    from custom_components.intentional import rule_store as rule_store_module

    monkeypatch.setattr(rule_store_module, "Store", _FakeStore)
    store = StorageRuleStore(fake_hass, "entry-1")
    await store.async_load_or_import("/missing")
    await store.async_write(
        RULE_STORE_FILENAME,
        "- id: next\n  observe: { input_boolean.next: 'on' }\n  intent: { light.next: { state: 'on' } }\n",
    )
    history_generation = store.list_history()[0]["generation"]

    result = await store.async_rollback(
        history_generation,
        expected_generation="stale",
    )

    assert result == {"error": "generation_mismatch"}
