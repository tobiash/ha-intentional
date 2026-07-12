from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.dependencies import require_test_dependency

require_test_dependency("homeassistant", reason="homeassistant not installed")

from custom_components.intentional import automatic_rollback as rollback_module  # noqa: E402
from custom_components.intentional.automatic_rollback import AutomaticRollback  # noqa: E402


class FakeStore:
    saved_by_key = {}

    def __init__(self, _hass, _version, key):
        self.key = key

    async def async_load(self):
        return self.saved_by_key.get(self.key)

    async def async_save(self, data):
        self.saved_by_key[self.key] = dict(data)


class Rules:
    def __init__(self):
        self.generation = "new"
        self.history = {"old": {"contents": "old"}}

    def read_history(self, generation):
        return self.history.get(generation)

    def list_history(self):
        return [{"generation": "old"}]

    async def async_rollback(self, generation, **_kwargs):
        if generation not in self.history:
            return {"error": "history_not_found"}
        self.generation = generation
        return {"generation": generation}


class Coordinator:
    def __init__(self, rules, fail=False):
        self.rules = rules
        self.fail = fail
        self.calls = 0

    async def async_mutate_and_reload(self, mutation, **_kwargs):
        self.calls += 1
        if self.fail:
            raise RuntimeError("rollback storage failed")
        return await mutation(), None


@pytest.fixture(autouse=True)
def fake_store(monkeypatch):
    FakeStore.saved_by_key = {}
    monkeypatch.setattr(rollback_module, "Store", FakeStore)


async def make_guard(*, fail=False):
    rules = Rules()
    guard = AutomaticRollback(SimpleNamespace(), "entry", rules)
    coordinator = Coordinator(rules, fail=fail)
    guard.set_coordinator(coordinator)
    await guard.async_arm("new", "old", 7)
    return guard, rules, coordinator


async def test_three_identical_fenced_internal_failures_roll_back_once():
    guard, rules, coordinator = await make_guard()
    for _ in range(3):
        await guard.async_failure(RuntimeError("deterministic fault"), stage="evaluation", generation="new", revision=7)

    assert rules.generation == "old"
    assert coordinator.calls == 1
    assert guard.health()["state"] == "rolled_back"
    await guard.async_failure(RuntimeError("deterministic fault"), stage="evaluation", generation="new", revision=7)
    assert coordinator.calls == 1


async def test_fence_and_fingerprint_must_remain_identical():
    guard, rules, coordinator = await make_guard()
    await guard.async_failure(RuntimeError("a"), stage="evaluation", generation="new", revision=7)
    await guard.async_failure(RuntimeError("b"), stage="evaluation", generation="new", revision=7)
    await guard.async_failure(RuntimeError("b"), stage="evaluation", generation="new", revision=8)
    await guard.async_failure(RuntimeError("b"), stage="evaluation", generation="new", revision=7)
    assert rules.generation == "new"
    assert coordinator.calls == 0


async def test_ten_successful_ticks_disarm():
    guard, _rules, _coordinator = await make_guard()
    revision = 7
    for _ in range(10):
        await guard.async_success(generation="new", revision=revision, next_revision=revision + 1)
        revision += 1
    assert guard.health() == {"state": "disarmed", "success_ticks": 10, "consecutive_failures": 0, "last_error": None, "reason": "stable"}


@pytest.mark.parametrize("stage", ["composition", "translation", "lifecycle", "service", "storage", "network", "drift", "policy", "user"])
async def test_ineligible_failures_never_count(stage):
    guard, _rules, coordinator = await make_guard()
    for _ in range(4):
        await guard.async_failure(RuntimeError("same"), stage=stage, generation="new", revision=7)
    assert coordinator.calls == 0
    assert guard.health()["consecutive_failures"] == 0


async def test_dispatch_disqualifies_generation():
    guard, _rules, coordinator = await make_guard()
    await guard.async_failure(RuntimeError("same"), stage="evaluation", generation="new", revision=7, dispatch_attempted=True)
    await guard.async_disqualify("effect_or_scene_call_attempted")
    assert guard.health()["state"] == "disarmed"
    assert coordinator.calls == 0


async def test_failed_rollback_is_terminal_across_restart_without_retry():
    guard, _rules, coordinator = await make_guard(fail=True)
    for _ in range(3):
        await guard.async_failure(RuntimeError("same"), stage="evaluation", generation="new", revision=7)
    assert guard.health()["state"] == "manual_intervention_required"
    assert coordinator.calls == 1

    restarted = AutomaticRollback(SimpleNamespace(), "entry", Rules())
    restarted_coordinator = Coordinator(restarted._rules)
    restarted.set_coordinator(restarted_coordinator)
    await restarted.async_load()
    for _ in range(3):
        await restarted.async_failure(RuntimeError("same"), stage="evaluation", generation="new", revision=7)
    assert restarted.health()["state"] == "manual_intervention_required"
    assert restarted_coordinator.calls == 0
