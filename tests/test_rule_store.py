from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml

from tests.dependencies import require_test_dependency

require_test_dependency("homeassistant", reason="homeassistant not installed")

from custom_components.intentional.rule_files import _read_rule_dir_as_yaml  # noqa: E402
from custom_components.intentional.rule_store import (  # noqa: E402
    RULE_STORE_FILENAME,
    StorageRuleStore,
)
from intentional.yaml_loader import load_rules_from_string  # noqa: E402


class _FakeStore:
    def __init__(self, hass, version, key):
        self.hass = hass
        self.version = version
        self.key = key
        self.saved: dict | None = None
        self.save_count = 0
        self.fail_saves = 0

    async def async_load(self):
        return self.saved

    async def async_save(self, data):
        if self.fail_saves:
            self.fail_saves -= 1
            raise RuntimeError("synthetic save failure")
        self.saved = data
        self.save_count += 1


@pytest.fixture
def fake_hass():
    async def async_add_executor_job(func, *args):
        return func(*args)

    return SimpleNamespace(async_add_executor_job=async_add_executor_job)


def _coordinator(store, hass):
    from custom_components.intentional.rule_mutation import RuleMutationCoordinator

    async def commit(_contents):
        await hass.services.async_call("intentional", "reload", blocking=True)

    return RuleMutationCoordinator(store, lambda contents: contents, commit)


def test_read_rule_dir_as_yaml_imports_yaml_files(tmp_path: Path) -> None:
    (tmp_path / "b.yaml").write_text(
        "- id: b\n  observe: { input_boolean.b: 'on' }\n  intent: { light.b: { state: 'on' } }\n",
        encoding="utf-8",
    )
    (tmp_path / "a.yml").write_text(
        "- id: a\n  observe: { input_boolean.a: 'on' }\n  intent: { light.a: { state: 'on' } }\n",
        encoding="utf-8",
    )
    (tmp_path / "ignored.txt").write_text("not yaml", encoding="utf-8")

    contents = _read_rule_dir_as_yaml(str(tmp_path))
    rules = load_rules_from_string(contents)

    assert [rule.id for rule in rules] == ["a", "b"]


def test_read_rule_dir_as_yaml_missing_dir_is_empty(tmp_path: Path) -> None:
    assert _read_rule_dir_as_yaml(str(tmp_path / "missing")) == ""


@pytest.mark.parametrize(
    "contents",
    [
        "[]\n",
        "[invalid yaml",
        "- id: first\n  when: 'true'\n"
        "  emit: {target: light.first, set: {state: on}}\n"
        "- id: second\n  enabled: false\n  when: 'true'\n"
        "  emit: {target: light.second, set: {state: on}}\n",
    ],
)
def test_list_rules_caches_parsing_and_generation(monkeypatch, fake_hass, contents) -> None:
    from custom_components.intentional import rule_store as rule_store_module

    monkeypatch.setattr(rule_store_module, "Store", _FakeStore)
    store = StorageRuleStore(fake_hass, "entry-1")
    store._contents = contents
    expected_generation = rule_store_module._generation_for(contents)
    parse = Mock(wraps=rule_store_module.load_rules_from_string)
    raw_parse = Mock(wraps=rule_store_module._raw_rule_items_from_yaml)
    digest = Mock(wraps=rule_store_module.sha256)
    monkeypatch.setattr(rule_store_module, "load_rules_from_string", parse)
    monkeypatch.setattr(rule_store_module, "_raw_rule_items_from_yaml", raw_parse)
    monkeypatch.setattr(rule_store_module, "sha256", digest)

    expected = store.list_rules()
    raw_parse_count = raw_parse.call_count
    for _ in range(3):
        assert store.list_rules() == expected
        assert store.generation == expected_generation
    assert parse.call_count == 1
    assert raw_parse.call_count == raw_parse_count
    assert digest.call_count == 1


async def test_list_rules_cache_invalidation_and_mutation_isolation(
    monkeypatch, fake_hass, tmp_path
) -> None:
    from custom_components.intentional import rule_store as rule_store_module

    monkeypatch.setattr(rule_store_module, "Store", _FakeStore)
    store = StorageRuleStore(fake_hass, "entry-1")
    contents = (
        "- id: first\n  when: 'true'\n"
        "  emit: {target: light.first, set: {state: on}}\n"
    )

    def assert_rules(rule_id, enabled=True):
        generation = rule_store_module._generation_for(store.contents)
        expected = [] if rule_id is None else [{
            "id": rule_id,
            "filename": RULE_STORE_FILENAME,
            "enabled": enabled,
            "generation": generation,
            "source": "storage",
        }]
        assert store.generation == generation
        result = store.list_rules()
        assert result == expected
        if result:
            result[0]["id"] = "mutated"
            result[0]["enabled"] = not enabled
        result.append({"id": "injected"})
        assert store.list_rules() == expected
        result = store.list_rules()
        result.clear()
        assert store.list_rules() == expected

    assert_rules(None)
    (tmp_path / "rules.yaml").write_text(contents, encoding="utf-8")
    await store.async_load_or_import(str(tmp_path))
    assert_rules("first")
    snapshot = store.snapshot()
    original_generation = store.generation

    assert await store.async_write(RULE_STORE_FILENAME, contents.replace("first", "second")) is None
    assert_rules("second")
    result = await store.async_rollback(original_generation, expected_generation=store.generation)
    assert "error" not in result
    assert_rules("first")
    result = await store.async_set_rule_enabled("first", False)
    assert "error" not in result
    assert_rules("first", False)
    result = await store.async_patch_rule_by_id(
        "first", contents, expected_generation=store.generation
    )
    assert "error" not in result
    assert_rules("first")
    assert await store.async_delete(RULE_STORE_FILENAME) is None
    assert_rules(None)
    await store.async_restore(snapshot)
    assert_rules("first")

    # Even a comment-only change must refresh the generation in cached metadata.
    previous_generation = store.generation
    assert await store.async_write(RULE_STORE_FILENAME, store.contents + "# comment\n") is None
    assert store.generation != previous_generation
    assert_rules("first")
    store._store.saved = {"contents": contents.replace("first", "loaded")}
    await store.async_load_or_import(str(tmp_path))
    assert_rules("loaded")


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


async def test_storage_rule_store_no_ops_do_not_save_or_grow_history(
    monkeypatch, fake_hass
) -> None:
    from custom_components.intentional import rule_store as rule_store_module

    monkeypatch.setattr(rule_store_module, "Store", _FakeStore)
    store = StorageRuleStore(fake_hass, "entry-1")
    await store.async_load_or_import("/missing")
    backing_store = store._store
    initial_save_count = backing_store.save_count

    assert await store.async_write(RULE_STORE_FILENAME, store.contents) is None
    assert await store.async_delete(RULE_STORE_FILENAME) is None

    assert backing_store.save_count == initial_save_count
    assert store.list_history() == []


async def test_storage_rule_store_rejects_document_preflight_errors_atomically(
    monkeypatch, fake_hass
) -> None:
    from custom_components.intentional import rule_store as rule_store_module

    monkeypatch.setattr(rule_store_module, "Store", _FakeStore)
    store = StorageRuleStore(fake_hass, "entry-1")
    await store.async_load_or_import("/missing")
    original = store.contents

    error = await store.async_write(
        RULE_STORE_FILENAME,
        "- id: press\n  when: 'true'\n  emit: {target: button.restart, set: {state: on}}\n",
    )

    assert "Document preflight failed" in error
    assert store.contents == original


@pytest.mark.parametrize(
    "invalid_fragment",
    [
        "  when: sensor.room ==\n  emit: {target: light.room, set: {state: on}}\n",
        "  when: 'true'\n  emit: {target: light.room, set: {brightness: '{{ broken'}}}\n",
    ],
)
async def test_storage_rule_store_rejects_invalid_expressions_and_templates_atomically(
    monkeypatch, fake_hass, invalid_fragment: str
) -> None:
    from custom_components.intentional import rule_store as rule_store_module

    monkeypatch.setattr(rule_store_module, "Store", _FakeStore)
    store = StorageRuleStore(fake_hass, "entry-1")
    await store.async_load_or_import("/missing")
    snapshot = (store.contents, store.generation, store.list_history(include_contents=True))

    error = await store.async_write(
        RULE_STORE_FILENAME,
        "- id: invalid\n" + invalid_fragment,
    )

    assert error is not None
    assert (store.contents, store.generation, store.list_history(include_contents=True)) == snapshot


async def test_mutation_reload_failure_restores_content_generation_and_history(
    monkeypatch, fake_hass
) -> None:
    from custom_components.intentional import rule_store as rule_store_module
    from custom_components.intentional.rule_mutation import mutate_and_reload

    monkeypatch.setattr(rule_store_module, "Store", _FakeStore)
    store = StorageRuleStore(fake_hass, "entry-1")
    await store.async_load_or_import("/missing")
    before = (store.contents, store.generation, store.list_history(include_contents=True))
    reload_calls = 0

    async def async_call(*_args, **_kwargs):
        nonlocal reload_calls
        reload_calls += 1
        if reload_calls == 1:
            raise RuntimeError("synthetic reload failure")

    hass = SimpleNamespace(services=SimpleNamespace(async_call=async_call))
    result, reload_error = await mutate_and_reload(
        _coordinator(store, hass),
        lambda: store.async_write(
            RULE_STORE_FILENAME,
            "- id: next\n  when: 'true'\n  emit: {target: light.next, set: {state: on}}\n",
        ),
        expected_generation=store.generation,
    )

    assert result is None
    assert str(reload_error) == "synthetic reload failure"
    assert reload_calls == 2
    assert (store.contents, store.generation, store.list_history(include_contents=True)) == before
    assert store._store.saved == {
        "contents": before[0],
        "generation": before[1],
        "history": [],
    }


async def test_manual_reload_waits_for_mutation_prepare_commit_boundary(
    monkeypatch, fake_hass
) -> None:
    from custom_components.intentional import rule_store as rule_store_module
    from custom_components.intentional.rule_mutation import RuleMutationCoordinator

    monkeypatch.setattr(rule_store_module, "Store", _FakeStore)
    store = StorageRuleStore(fake_hass, "entry-1")
    await store.async_load_or_import("/missing")
    original = store.contents
    replacement = "- id: next\n  when: 'true'\n  emit: {target: light.next, set: {state: on}}\n"
    mutation_written = asyncio.Event()
    release_mutation = asyncio.Event()
    committed: list[str] = []

    async def mutation():
        result = await store.async_write(RULE_STORE_FILENAME, replacement)
        mutation_written.set()
        await release_mutation.wait()
        return result

    async def commit(contents: str) -> None:
        committed.append(contents)

    coordinator = RuleMutationCoordinator(store, lambda contents: contents, commit)
    mutation_task = asyncio.create_task(
        coordinator.async_mutate_and_reload(
            mutation, expected_generation=store.generation
        )
    )
    await mutation_written.wait()
    reload_task = asyncio.create_task(coordinator.async_reload())
    await asyncio.sleep(0)

    assert committed == []
    assert not reload_task.done()
    assert original != store.contents

    release_mutation.set()
    await mutation_task
    await reload_task

    assert committed == [replacement, replacement]


@pytest.mark.parametrize("mutation_name", ["write", "patch", "delete", "rollback", "enabled"])
async def test_mutation_save_failure_restores_exact_snapshot_and_allows_retry(
    monkeypatch, fake_hass, mutation_name: str
) -> None:
    from custom_components.intentional import rule_store as rule_store_module
    from custom_components.intentional.rule_mutation import mutate_and_reload

    monkeypatch.setattr(rule_store_module, "Store", _FakeStore)
    store = StorageRuleStore(fake_hass, "entry-1")
    await store.async_load_or_import("/missing")
    original = (
        "- id: original\n"
        "  when: 'true'\n"
        "  emit: {target: light.original, set: {state: on}}\n"
    )
    replacement = (
        "- id: original\n"
        "  when: 'false'\n"
        "  emit: {target: light.original, set: {state: off}}\n"
    )
    await store.async_write(RULE_STORE_FILENAME, original)
    rollback_generation = store.list_history()[0]["generation"]
    before = (store.contents, store.generation, store.list_history(include_contents=True))
    runtime_rules = await store.async_rules()
    reload_calls = 0

    async def async_call(*_args, **_kwargs):
        nonlocal reload_calls, runtime_rules
        reload_calls += 1
        runtime_rules = await store.async_rules()

    hass = SimpleNamespace(services=SimpleNamespace(async_call=async_call))

    def mutation():
        if mutation_name == "write":
            return store.async_write(RULE_STORE_FILENAME, replacement)
        if mutation_name == "patch":
            return store.async_patch_rule_by_id(
                "original", replacement, expected_generation=store.generation
            )
        if mutation_name == "delete":
            return store.async_delete(RULE_STORE_FILENAME)
        if mutation_name == "rollback":
            return store.async_rollback(
                rollback_generation, expected_generation=store.generation
            )
        return store.async_set_rule_enabled("original", False)

    store._store.fail_saves = 1
    with pytest.raises(RuntimeError, match="synthetic save failure"):
        await mutate_and_reload(
            _coordinator(store, hass), mutation, expected_generation=store.generation
        )

    assert (store.contents, store.generation, store.list_history(include_contents=True)) == before
    assert [rule.id for rule in runtime_rules] == ["original"]
    assert reload_calls == 0

    result, reload_error = await mutate_and_reload(
        _coordinator(store, hass), mutation, expected_generation=store.generation
    )

    assert result is None or not (isinstance(result, dict) and "error" in result)
    assert reload_error is None
    assert reload_calls == 1


async def test_mutation_recovery_save_failure_keeps_original_error_and_memory_snapshot(
    monkeypatch, fake_hass
) -> None:
    from custom_components.intentional import rule_store as rule_store_module
    from custom_components.intentional.rule_mutation import mutate_and_reload

    monkeypatch.setattr(rule_store_module, "Store", _FakeStore)
    store = StorageRuleStore(fake_hass, "entry-1")
    await store.async_load_or_import("/missing")
    before = (store.contents, store.generation, store.list_history(include_contents=True))
    reload_calls = 0

    async def async_call(*_args, **_kwargs):
        nonlocal reload_calls
        reload_calls += 1

    hass = SimpleNamespace(services=SimpleNamespace(async_call=async_call))
    contents = (
        "- id: next\n"
        "  when: 'true'\n"
        "  emit: {target: light.next, set: {state: on}}\n"
    )
    def mutation():
        return store.async_write(RULE_STORE_FILENAME, contents)

    store._store.fail_saves = 2

    with pytest.raises(RuntimeError, match="synthetic save failure") as raised:
        await mutate_and_reload(
            _coordinator(store, hass), mutation, expected_generation=store.generation
        )

    assert raised.value.__notes__ == [
        "Rule store recovery failed: RuntimeError('synthetic save failure')"
    ]
    assert (store.contents, store.generation, store.list_history(include_contents=True)) == before
    assert reload_calls == 0

    result, reload_error = await mutate_and_reload(
        _coordinator(store, hass), mutation, expected_generation=store.generation
    )
    assert result is None
    assert reload_error is None
    assert reload_calls == 1


async def test_storage_rule_store_no_op_mutations_preserve_snapshot(monkeypatch, fake_hass) -> None:
    from custom_components.intentional import rule_store as rule_store_module

    monkeypatch.setattr(rule_store_module, "Store", _FakeStore)
    store = StorageRuleStore(fake_hass, "entry-1")
    await store.async_load_or_import("/missing")
    contents = (
        "- id: next\n"
        "  observe: { input_boolean.next: 'on' }\n"
        "  intent: { light.next: { state: 'on' } }\n"
    )
    await store.async_write(RULE_STORE_FILENAME, contents)
    generation = store.generation
    history = store.list_history(include_contents=True)
    save_count = store._store.save_count

    enabled_result = await store.async_set_rule_enabled("next", True)
    patch_result = await store.async_patch_rule_by_id(
        "next", contents, expected_generation=generation
    )
    rollback_result = await store.async_rollback(generation, expected_generation=generation)

    assert enabled_result["generation"] == generation
    assert patch_result["generation"] == generation
    assert rollback_result == {
        "filename": RULE_STORE_FILENAME,
        "generation": generation,
        "restored_generation": generation,
    }
    assert store.contents == contents
    assert store.list_history(include_contents=True) == history
    assert store._store.save_count == save_count


async def test_storage_rule_store_no_op_patch_still_checks_generation(
    monkeypatch, fake_hass
) -> None:
    from custom_components.intentional import rule_store as rule_store_module

    monkeypatch.setattr(rule_store_module, "Store", _FakeStore)
    store = StorageRuleStore(fake_hass, "entry-1")
    await store.async_load_or_import("/missing")
    contents = (
        "- id: next\n"
        "  observe: { input_boolean.next: 'on' }\n"
        "  intent: { light.next: { state: 'on' } }\n"
    )
    await store.async_write(RULE_STORE_FILENAME, contents)

    result = await store.async_patch_rule_by_id("next", contents, expected_generation="stale")

    assert result == {"error": "generation_mismatch"}


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


async def test_storage_rule_store_rollback_checks_expected_generation(
    monkeypatch, fake_hass
) -> None:
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


async def test_storage_rule_store_patch_replaces_authored_rule_only(monkeypatch, fake_hass) -> None:
    from custom_components.intentional import rule_store as rule_store_module

    monkeypatch.setattr(rule_store_module, "Store", _FakeStore)
    store = StorageRuleStore(fake_hass, "entry-1")
    await store.async_load_or_import("/missing")
    await store.async_write(
        RULE_STORE_FILENAME,
        """- id: office-lights
  observe:
    binary_sensor.office_occupancy: on
  intent:
    light.left:
      state: on
    light.right:
      state: on
- id: untouched
  observe:
    input_boolean.untouched: on
  intent:
    light.untouched:
      state: on
""",
    )
    generation = store.generation

    result = await store.async_patch_rule_by_id(
        "office-lights",
        """- id: office-lights
  observe:
    binary_sensor.office_occupancy: on
  intent:
    light.left:
      state: off
    light.right:
      state: off
""",
        expected_generation=generation,
    )

    assert "error" not in result
    docs = list(yaml.safe_load_all(store.contents))
    assert docs == [
        [
            {
                "id": "office-lights",
                "observe": {"binary_sensor.office_occupancy": True},
                "intent": {
                    "light.left": {"state": False},
                    "light.right": {"state": False},
                },
            },
            {
                "id": "untouched",
                "observe": {"input_boolean.untouched": True},
                "intent": {"light.untouched": {"state": True}},
            },
        ]
    ]
    assert [rule.id for rule in load_rules_from_string(store.contents)] == [
        "office-lights:light.left",
        "office-lights:light.right",
        "untouched",
    ]
