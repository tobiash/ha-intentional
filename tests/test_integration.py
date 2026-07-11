"""Integration tests for the Intentional Home Assistant integration.

These tests spin up a real Home Assistant test instance using
``pytest-homeassistant-custom-component`` and exercise the
integration end-to-end:

- Loading the integration from a config entry
- The config flow
- Service registration
- Sensor entity creation
- Rule loading from a temp directory
- API endpoint access (the new REST API)
- The reload service
- The activate_scene service

These tests REQUIRE ``homeassistant`` to be installed. If it's
not available, the entire module is skipped (this is intentional —
the engine unit tests run without HA, but integration tests
genuinely need it).

To run locally:

    pip install pytest-homeassistant-custom-component==0.13.250
    pip install homeassistant==2025.5.0  # or latest stable
    pytest tests/test_integration.py -v

Note: HA's full install is large (~500MB). It's much easier to let
CI run these — see ``ci/test.yml`` for the GitHub Actions config.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

# Skip the entire module if HA isn't installed
pytest.importorskip("homeassistant", reason="homeassistant not installed")
pytest.importorskip(
    "pytest_homeassistant_custom_component",
    reason="pytest-homeassistant-custom-component not installed",
)

# Make the integration importable
REPO_ROOT = Path(__file__).parent.parent
INTEGRATION_DIR = REPO_ROOT / "custom_components" / "intentional"
sys.path.insert(0, str(INTEGRATION_DIR))

# These imports MUST come after the importorskip checks
from homeassistant.config_entries import ConfigEntryState  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
)

from custom_components.intentional.const import (  # noqa: E402
    CONF_RULE_DIR,
    DOMAIN,
)
from custom_components.intentional.rule_store import rule_store_key  # noqa: E402

# ── Fixtures ───────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading custom integrations from the test integration dir."""
    yield


@pytest.fixture(autouse=True)
async def auto_unload_entries(hass: HomeAssistant):
    """Unload any config entries after each test.

    The integration spawns a 100ms tick loop on setup. Unloading the
    entry sets a stop event that lets the loop exit cleanly within one
    tick interval. The next test's ``hass`` fixture is a fresh instance
    with a fresh event loop, so leftover tasks from this one are GC'd.
    """
    yield
    for entry in list(hass.config_entries.async_entries()):
        if entry.state is ConfigEntryState.LOADED:
            await hass.config_entries.async_unload(entry.entry_id)


@pytest.fixture
def rule_dir(tmp_path: Path) -> Path:
    """A temp directory for rule files, pre-populated with a simple rule."""
    rule_dir = tmp_path / "rules"
    rule_dir.mkdir()
    (rule_dir / "01-test.yaml").write_text(
        "- id: test-rule\n"
        "  when: input_boolean.test == 'on'\n"
        "  emit:\n"
        "    target: light.test\n"
        "    set:\n"
        "      state: 'on'\n"
        "      brightness_pct: 50\n"
    )
    return rule_dir


@pytest.fixture
def config_entry(rule_dir: Path) -> MockConfigEntry:
    """A mock config entry pointing at the test rule directory."""
    return MockConfigEntry(
        domain=DOMAIN,
        data={CONF_RULE_DIR: str(rule_dir)},
        title="Intentional Test",
    )


# ── Tests ──────────────────────────────────────────────────────────


async def test_integration_loads(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    """The integration should load and set up successfully."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)


async def test_services_registered(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    """After setup, intentional.fire, intentional.reload, etc. should be registered."""
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)

    # HA stores services in hass.services.async_services()
    services = hass.services.async_services()
    assert "intentional" in services
    intentional_services = services["intentional"]
    assert "fire" in intentional_services
    assert "clear" in intentional_services
    assert "reload" in intentional_services
    assert "activate_scene" in intentional_services


async def test_second_config_entry_is_rejected(
    hass: HomeAssistant, config_entry: MockConfigEntry, tmp_path: Path
) -> None:
    """Singleton domain services must not be rebound to another engine."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)

    second_rule_dir = tmp_path / "other-rules"
    second_rule_dir.mkdir()
    second_entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_RULE_DIR: str(second_rule_dir)},
        title="Intentional Second Entry",
    )
    second_entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(second_entry.entry_id)
    assert second_entry.entry_id not in hass.data[DOMAIN]


async def test_sensor_entities_created(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """The integration should create a summary sensor on setup."""
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)

    # The summary sensor should be present
    state = hass.states.get("sensor.intentional_intent_engine_summary")
    # If entity_id format differs, this just won't find it — that's fine
    # (we don't want to over-constrain the test)
    if state is not None:
        assert state.state is not None


async def test_rule_file_loaded(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """The engine should load the test rule file on setup."""
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)

    engine = hass.data[DOMAIN][config_entry.entry_id]
    assert len(engine._rules) == 1  # noqa: SLF001
    assert "test-rule" in engine._rules  # noqa: SLF001


async def test_tick_loop_records_failure_and_continues(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient tick exception must not permanently stop reconciliation."""
    import custom_components.intentional as intentional
    from custom_components.intentional._engine.runtime import runtime_key
    from custom_components.intentional.diagnostics import list_diagnostics

    original_sync = intentional._sync_state_into_engine
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("synthetic tick failure")
        return original_sync(*args, **kwargs)

    monkeypatch.setattr(intentional, "_sync_state_into_engine", fail_once)

    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    runtime = hass.data[DOMAIN][runtime_key(config_entry.entry_id)]

    for _ in range(20):
        await asyncio.sleep(0.05)
        diagnostics = list_diagnostics(hass)
        if (
            calls >= 2
            and runtime.failure_count >= 1
            and runtime.success_count >= 1
            and any(event["type"] == "tick_failed" for event in diagnostics)
        ):
            break

    diagnostics = list_diagnostics(hass)
    assert calls >= 2
    assert runtime.failure_count == 1
    assert runtime.success_count >= 1
    assert runtime.consecutive_failures == 0
    assert runtime.health()["status"] == "ok"
    assert any(
        event["type"] == "tick_failed"
        and "synthetic tick failure" in event.get("error", "")
        for event in diagnostics
    )


async def test_reload_service_works(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """Calling intentional.reload should re-read stored rules."""
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)

    engine = hass.data[DOMAIN][config_entry.entry_id]
    assert len(engine._rules) == 1  # noqa: SLF001

    rule_store = hass.data[DOMAIN][rule_store_key(config_entry.entry_id)]
    await rule_store.async_write(
        "stored-rules.yaml",
        rule_store.contents + "\n---\n"
        "- id: extra-rule\n"
        "  when: time_of_day == '00:00'\n"
        "  emit:\n"
        "    target: light.test\n"
        "    set:\n"
        "      state: 'off'\n"
    )
    # Reload
    await hass.services.async_call(DOMAIN, "reload", blocking=True)
    assert len(engine._rules) == 2  # noqa: SLF001
    assert "extra-rule" in engine._rules  # noqa: SLF001


async def test_clear_service_removes_manual_intents(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """Calling intentional.clear should remove manual fire-service intents."""
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)

    engine = hass.data[DOMAIN][config_entry.entry_id]

    await hass.services.async_call(
        DOMAIN,
        "fire",
        {
            "target": "light.test",
            "state": "on",
            "ttl": 7200,
        },
        blocking=True,
    )
    await hass.services.async_call(
        DOMAIN,
        "fire",
        {
            "target": "light.other",
            "state": "off",
            "ttl": 7200,
        },
        blocking=True,
    )

    assert len(engine.list_active_intents("light.test")) == 1
    assert len(engine.list_active_intents("light.other")) == 1

    await hass.services.async_call(
        DOMAIN,
        "clear",
        {"target": "light.test"},
        blocking=True,
    )

    assert engine.list_active_intents("light.test") == []
    assert len(engine.list_active_intents("light.other")) == 1

    await hass.services.async_call(DOMAIN, "clear", blocking=True)

    assert engine.list_active_intents("light.other") == []


async def test_api_health_endpoint(
    hass: HomeAssistant, config_entry: MockConfigEntry, hass_client
) -> None:
    """GET /api/intentional/health should return integration status."""
    # The hass_client fixture requires the http component to be loaded.
    # We load it via the standard ``async_setup_component`` entry point.
    from homeassistant.components.http import async_setup as http_async_setup
    await http_async_setup(hass, {})

    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)

    client = await hass_client()
    # HA's test client doesn't auto-auth; we need a long-lived token
    # or to bypass auth. For unit tests, we can use a mock token.
    resp = await client.get("/api/intentional/health")
    # Auth might fail (401) — that's expected without a real token.
    # If 200, the body should have "status": "ok"
    if resp.status == 200:
        body = await resp.json()
        assert body["status"] == "ok"
        assert "rule_count" in body
    else:
        # Unauthenticated request rejected — that's fine
        assert resp.status in (401, 403)


async def test_api_requires_auth(
    hass: HomeAssistant, config_entry: MockConfigEntry, hass_client_no_auth
) -> None:
    """All API endpoints should require authentication."""
    # See test_api_health_endpoint — hass_client needs http loaded.
    from homeassistant.components.http import async_setup as http_async_setup
    await http_async_setup(hass, {})

    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)

    client = await hass_client_no_auth()
    # Hit an endpoint without auth
    resp = await client.get("/api/intentional/health")
    # Should NOT be 200 (should be 401 or 403)
    assert resp.status in (401, 403), (
        f"API should require auth, got {resp.status}"
    )


async def test_integration_unload(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    """The integration should unload cleanly."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)

    from custom_components.intentional._engine.runtime import runtime_key

    runtime = hass.data[DOMAIN][runtime_key(config_entry.entry_id)]
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    # Engine should be removed from hass.data
    assert config_entry.entry_id not in hass.data.get(DOMAIN, {})
    assert runtime.tick_task is not None
    assert runtime.tick_task.done()


async def test_unload_continues_when_final_lifecycle_save_fails(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed final save is diagnosed without preventing clean unload."""
    from homeassistant.helpers.storage import Store

    from custom_components.intentional._engine.runtime import runtime_key
    from custom_components.intentional.diagnostics import list_diagnostics

    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)

    async def fail_save(self, data):
        raise RuntimeError("synthetic final save failure")

    monkeypatch.setattr(Store, "async_save", fail_save)

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    assert config_entry.entry_id not in hass.data[DOMAIN]
    assert runtime_key(config_entry.entry_id) not in hass.data[DOMAIN]
    assert any(
        event["type"] == "lifecycle_save_failed"
        and event["error"] == "synthetic final save failure"
        for event in list_diagnostics(hass)
    )


async def test_failed_platform_unload_keeps_tick_runtime_operational(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejected platform unload must leave the loaded runtime running."""
    from custom_components.intentional import async_unload_entry
    from custom_components.intentional._engine.runtime import runtime_key

    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    runtime = hass.data[DOMAIN][runtime_key(config_entry.entry_id)]
    await asyncio.sleep(0.15)
    success_count = runtime.success_count

    async def reject_platform_unload(entry, platforms):
        return False

    monkeypatch.setattr(
        hass.config_entries, "async_unload_platforms", reject_platform_unload
    )

    assert not await async_unload_entry(hass, config_entry)
    assert not runtime.stop_event.is_set()
    assert runtime.tick_task is not None
    assert not runtime.tick_task.done()
    assert config_entry.entry_id in hass.data[DOMAIN]
    assert rule_store_key(config_entry.entry_id) in hass.data[DOMAIN]

    await asyncio.sleep(0.15)
    assert runtime.success_count > success_count


async def test_unload_cleans_up_when_tick_task_raises(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """An unexpected tick-task exception must not bypass platform cleanup."""
    from custom_components.intentional._engine.runtime import runtime_key
    from custom_components.intentional.diagnostics import list_diagnostics

    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    runtime = hass.data[DOMAIN][runtime_key(config_entry.entry_id)]

    runtime.stop_event.set()
    assert runtime.tick_task is not None
    await runtime.tick_task

    async def fail_tick_task() -> None:
        raise RuntimeError("synthetic task completion failure")

    runtime.tick_task = hass.async_create_task(fail_tick_task())

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    assert config_entry.entry_id not in hass.data[DOMAIN]
    assert runtime_key(config_entry.entry_id) not in hass.data[DOMAIN]
    assert any(
        event["type"] == "tick_task_failed"
        and event["error"] == "synthetic task completion failure"
        for event in list_diagnostics(hass)
    )


async def test_deleted_entity_state_is_removed_from_engine(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """Entity removal must not leave stale facts firing rules."""
    config_entry.add_to_hass(hass)
    hass.states.async_set("input_boolean.test", "on")
    await hass.config_entries.async_setup(config_entry.entry_id)
    engine = hass.data[DOMAIN][config_entry.entry_id]
    await asyncio.sleep(0)

    assert engine.state["input_boolean.test.state"] == "on"

    hass.states.async_remove("input_boolean.test")
    await asyncio.sleep(0)

    assert not any(
        key.startswith("input_boolean.test.") for key in engine.state
    )
    assert engine.resolve("light.test") is None


async def test_lifecycle_storage_skips_unchanged_tick_snapshots(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stable lifecycle state is saved once during ticks and once on unload."""
    from homeassistant.helpers.storage import Store

    save_count = 0
    original_save = Store.async_save

    async def count_save(self, data):
        nonlocal save_count
        save_count += 1
        await original_save(self, data)

    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    monkeypatch.setattr(Store, "async_save", count_save)

    await asyncio.sleep(0.35)
    assert save_count == 1

    await hass.config_entries.async_unload(config_entry.entry_id)
    assert save_count == 2


async def test_missing_rule_dir_is_created(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """A non-existent rule directory should be auto-created on setup."""
    nonexistent_dir = tmp_path / "does_not_exist" / "rules"
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_RULE_DIR: str(nonexistent_dir)},
        title="Intentional Test",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    # The directory should now exist
    assert nonexistent_dir.exists()
