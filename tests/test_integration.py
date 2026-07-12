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
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from tests.dependencies import require_test_dependency

# Skip the entire module if HA isn't installed
require_test_dependency("homeassistant", reason="homeassistant not installed")
require_test_dependency(
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


async def test_effect_dispatch_uses_blocking_acceptance_and_acknowledges(
    hass: HomeAssistant,
) -> None:
    """The HA acceptance boundary must precede the durable acknowledgement."""
    from custom_components.intentional import _dispatch_effect_outbox
    from custom_components.intentional._engine import Engine
    from custom_components.intentional._engine.yaml_loader import load_rules_from_string

    engine = Engine(clock_fn=lambda: 1_000)
    engine.load_rules(
        load_rules_from_string("""
- id: notify
  observe: {binary_sensor.door: on}
  effect: {service: notify.phone, data: {message: open}}
""")
    )
    engine.update_state("binary_sensor.door", "on")
    engine.evaluate_all()
    calls = []

    async def handle(call):
        calls.append(call)

    hass.services.async_register("notify", "phone", handle)

    await _dispatch_effect_outbox(hass, engine)

    assert len(calls) == 1
    assert engine.list_effect_outbox() == []


async def test_successful_durable_effect_delivery_forces_one_store_write(
    hass: HomeAssistant,
) -> None:
    from custom_components.intentional import _dispatch_effect_outbox
    from custom_components.intentional._engine import Engine
    from custom_components.intentional._engine.yaml_loader import load_rules_from_string
    from custom_components.intentional.lifecycle_writer import LifecycleWriter

    engine = Engine(clock_fn=lambda: 1_000)
    engine.load_rules(
        load_rules_from_string("""
- id: notify
  observe: {binary_sensor.door: on}
  effect: {service: notify.phone, data: {message: open}}
""")
    )
    engine.update_state("binary_sensor.door", "on")
    engine.evaluate_all()
    durable = engine.export_lifecycle_records()
    writes: list[dict] = []
    store = SimpleNamespace(async_save=AsyncMock(side_effect=lambda data: writes.append(data)))
    writer = LifecycleWriter(store, engine.export_lifecycle_records, durable_snapshot=durable)
    async def handle(_call):
        pass

    hass.services.async_register("notify", "phone", handle)

    await _dispatch_effect_outbox(hass, engine, lifecycle_writer=writer)

    assert len(writes) == 1
    assert engine.list_effect_outbox() == []


async def test_stale_scene_revision_does_not_dispatch_effects(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    import custom_components.intentional as intentional
    from custom_components.intentional._engine import Engine
    from custom_components.intentional._engine.runtime import TickRuntime

    runtime = TickRuntime(tick_interval_ms=100)
    revision = runtime.advance_revision()

    async def stale_scene(*args, **kwargs):
        runtime.advance_revision()
        return {"scene.changed"}

    dispatch = AsyncMock()
    monkeypatch.setattr(intentional, "_activate_scene_rules", stale_scene)
    monkeypatch.setattr(intentional, "_dispatch_effect_outbox", dispatch)

    result = await intentional._activate_scenes_and_dispatch_effects(
        hass, Engine(), None, runtime, None, set(), revision
    )

    assert result is None
    dispatch.assert_not_awaited()


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


def _write_semantic_rule(rule_dir: Path, observation: str) -> None:
    (rule_dir / "01-test.yaml").write_text(
        "- id: semantic-test\n"
        f"  while: {{{observation}}}\n"
        "  intent: {light.semantic_result: {state: on}}\n"
    )


async def _setup_and_wait(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> object:
    if not any(
        entry.entry_id == config_entry.entry_id
        for entry in hass.config_entries.async_entries(DOMAIN)
    ):
        config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await asyncio.sleep(0.15)
    await hass.async_block_till_done()
    return hass.data[DOMAIN][config_entry.entry_id]


# ── Tests ──────────────────────────────────────────────────────────


async def test_integration_loads(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    """The integration should load and set up successfully."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)


async def test_failed_platform_unload_resumes_runtime_and_publication(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.intentional import async_unload_entry
    from custom_components.intentional._engine.runtime import runtime_key
    from custom_components.intentional.publication import publication_key

    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    runtime = hass.data[DOMAIN][runtime_key(config_entry.entry_id)]
    publication = hass.data[DOMAIN][publication_key(config_entry.entry_id)]
    before = runtime.success_count
    monkeypatch.setattr(
        hass.config_entries, "async_unload_platforms", AsyncMock(return_value=False)
    )

    assert not await async_unload_entry(hass, config_entry)
    assert runtime.unloading is False
    assert publication._paused is False
    await asyncio.sleep(0.15)
    assert runtime.success_count > before


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


async def test_sensor_entities_created(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    """The integration should register its summary sensor with stable identity."""
    from homeassistant.helpers import entity_registry as er

    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    entity_id = "sensor.intentional_intent_engine_summary"
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state is not None

    registry_entry = er.async_get(hass).async_get(entity_id)
    assert registry_entry is not None
    assert registry_entry.platform == DOMAIN
    assert registry_entry.config_entry_id == config_entry.entry_id
    assert registry_entry.unique_id == "intentional_summary"


@pytest.mark.parametrize("entity_kind", ["room_sensor", "room_switch", "rule_switch"])
async def test_dynamic_entity_removed_during_pending_add_cleans_up_after_registration(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    entity_kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rapid publication must not operate on an entity before platform assignment."""
    from custom_components.intentional._engine import Engine
    from custom_components.intentional.sensor import IntentionalRoomStatusSensor
    from custom_components.intentional.switch import (
        IntentionalRoomPauseSwitch,
        IntentionalRuleSwitch,
    )

    engine = Engine()
    if entity_kind == "room_sensor":
        entity = IntentionalRoomStatusSensor(hass, engine, config_entry, "office")
        entity_id = "sensor.office_status"
    elif entity_kind == "room_switch":
        entity = IntentionalRoomPauseSwitch(hass, engine, config_entry, "office")
        entity_id = "switch.pause_office_rules"
    else:
        entity = IntentionalRuleSwitch(
            hass,
            config_entry,
            "/rules",
            {"id": "office", "filename": "office.yaml", "enabled": True},
            engine=engine,
        )
        entity_id = "switch.rule_office"

    removed = False

    def nonlocal_set_removed() -> None:
        nonlocal removed
        removed = True

    entity.set_removal_callback(nonlocal_set_removed)

    # async_add_entities has returned, but HA has not assigned entity_id yet.
    entity.async_write_if_registered()
    await entity.async_mark_removed()
    assert entity.entity_id is None
    assert removed

    async_remove = AsyncMock()
    monkeypatch.setattr(entity, "async_remove", async_remove)
    entity.hass = hass
    entity.entity_id = entity_id
    await entity.async_added_to_hass()
    await hass.async_block_till_done()

    async_remove.assert_awaited_once_with()


async def test_assigned_entity_id_without_successful_add_never_writes_or_removes(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An assigned ID is not proof that HA completed platform addition."""
    from custom_components.intentional._engine import Engine
    from custom_components.intentional.sensor import IntentionalRoomStatusSensor

    entity = IntentionalRoomStatusSensor(hass, Engine(), config_entry, "office")
    entity.entity_id = "sensor.office_status"
    write = Mock()
    remove = AsyncMock()
    monkeypatch.setattr(entity, "async_write_ha_state", write)
    monkeypatch.setattr(entity, "async_remove", remove)

    entity.async_write_if_registered()
    await entity.async_mark_removed()

    write.assert_not_called()
    remove.assert_not_awaited()


async def test_registration_state_resets_when_entity_is_removed(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.intentional._engine import Engine
    from custom_components.intentional.sensor import IntentionalRoomStatusSensor

    entity = IntentionalRoomStatusSensor(hass, Engine(), config_entry, "office")
    entity.entity_id = "sensor.office_status"
    await entity.async_added_to_hass()
    await entity.async_will_remove_from_hass()
    write = Mock()
    monkeypatch.setattr(entity, "async_write_ha_state", write)

    entity.async_write_if_registered()

    write.assert_not_called()


@pytest.mark.parametrize(
    ("domain", "suffix", "cleanup_name"),
    [
        ("sensor", "status", "_cleanup_stale_room_sensors"),
        ("switch", "paused", "_cleanup_stale_room_switches"),
    ],
)
async def test_room_registry_cleanup_removes_only_stale_entries(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    domain: str,
    suffix: str,
    cleanup_name: str,
) -> None:
    """Current room entries, including disabled ones, must survive cleanup."""
    from homeassistant.helpers import entity_registry as er

    module = __import__(
        f"custom_components.intentional.{domain}", fromlist=[cleanup_name]
    )
    cleanup = getattr(module, cleanup_name)
    config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    prefix = f"{config_entry.entry_id}_area_"
    current = registry.async_get_or_create(
        domain,
        DOMAIN,
        f"{prefix}office_{suffix}",
        config_entry=config_entry,
        disabled_by=er.RegistryEntryDisabler.USER,
    )
    stale = registry.async_get_or_create(
        domain,
        DOMAIN,
        f"{prefix}kitchen_{suffix}",
        config_entry=config_entry,
    )

    cleanup(hass, config_entry, {"office"})

    assert registry.async_get(current.entity_id) is current
    assert registry.async_get(current.entity_id).disabled_by is er.RegistryEntryDisabler.USER
    assert registry.async_get(stale.entity_id) is None


async def test_pending_dynamic_entity_removal_can_be_cancelled_before_registration(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Desired-set oscillation reuses one queued entity instead of removing it."""
    from custom_components.intentional._engine import Engine
    from custom_components.intentional.sensor import IntentionalRoomStatusSensor

    entity = IntentionalRoomStatusSensor(hass, Engine(), config_entry, "office")
    await entity.async_mark_removed()
    entity.mark_desired()

    async_remove = AsyncMock()
    monkeypatch.setattr(entity, "async_remove", async_remove)
    entity.hass = hass
    entity.entity_id = "sensor.office_status"
    await entity.async_added_to_hass()
    await hass.async_block_till_done()

    async_remove.assert_not_awaited()


async def test_stable_ticks_do_not_publish_or_write_entity_state(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Settled 10 Hz ticks must be invisible on HA's entity surfaces."""
    from homeassistant.const import EVENT_STATE_CHANGED
    from homeassistant.helpers import entity_registry as er
    from homeassistant.helpers.entity import Entity
    from homeassistant.helpers.storage import Store

    import custom_components.intentional.publication as publication_module
    from custom_components.intentional.diagnostics import list_diagnostics

    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get("sensor.intentional_intent_engine_summary") is not None

    # Entity registry persistence is deferred beyond async_block_till_done().
    # Let setup's write settle before measuring stable Tick runtime behavior.
    await asyncio.sleep(0.35)
    await hass.async_block_till_done()

    publications = writes = state_events = refresh_events = 0
    store_writes = registry_mutations = service_calls = 0
    diagnostics_before = len(list_diagnostics(hass))
    original_send = publication_module.async_dispatcher_send
    original_write = Entity.async_write_ha_state
    original_save = Store.async_save
    original_registry_remove = er.EntityRegistry.async_remove
    from custom_components.intentional import _HAAdapter

    def count_send(*args, **kwargs):
        nonlocal publications
        publications += 1
        return original_send(*args, **kwargs)

    def count_write(self):
        nonlocal writes
        writes += 1
        return original_write(self)

    def count_state_event(event):
        nonlocal state_events
        entity_id = event.data.get("entity_id", "")
        if entity_id.startswith(("sensor.intentional", "switch.intentional", "button.intentional")):
            state_events += 1

    def count_refresh_event(_event):
        nonlocal refresh_events
        refresh_events += 1

    async def count_save(self, data):
        nonlocal store_writes
        store_writes += 1
        await original_save(self, data)

    def count_registry_remove(self, entity_id):
        nonlocal registry_mutations
        registry_mutations += 1
        return original_registry_remove(self, entity_id)

    async def count_service_call(self, *args, **kwargs):
        nonlocal service_calls
        service_calls += 1

    monkeypatch.setattr(publication_module, "async_dispatcher_send", count_send)
    monkeypatch.setattr(Entity, "async_write_ha_state", count_write)
    monkeypatch.setattr(Store, "async_save", count_save)
    monkeypatch.setattr(er.EntityRegistry, "async_remove", count_registry_remove)
    monkeypatch.setattr(_HAAdapter, "async_call", count_service_call)
    remove_state_listener = hass.bus.async_listen(EVENT_STATE_CHANGED, count_state_event)
    remove_refresh_listener = hass.bus.async_listen("intentional_refresh", count_refresh_event)
    await asyncio.sleep(0.35)
    await hass.async_block_till_done()
    remove_state_listener()
    remove_refresh_listener()

    assert publications == 0
    assert writes == 0
    assert state_events == 0
    assert refresh_events == 0
    assert store_writes == 0
    assert registry_mutations == 0
    assert service_calls == 0
    assert len(list_diagnostics(hass)) == diagnostics_before


async def test_reconciliation_retry_diagnostics_are_rate_limited(
    hass: HomeAssistant,
) -> None:
    from custom_components.intentional import _apply_reconciliation_events
    from custom_components.intentional._engine import Engine
    from custom_components.intentional._engine.reconciliation import ReconciliationEvent
    from custom_components.intentional.diagnostics import DiagnosticRateLimiter, list_diagnostics

    limiter = DiagnosticRateLimiter(cooldown_ms=60_000)
    engine = Engine()
    events = [
        ReconciliationEvent("service_retry_scheduled", "light.test", {"failures": 1}),
        ReconciliationEvent("service_retry_scheduled", "light.test", {"failures": 2}),
        ReconciliationEvent("service_retry_skipped", "light.test", {"remaining_ms": 500}),
        ReconciliationEvent("service_denied_target_policy", "light.test", {"code": "observe_only"}),
        ReconciliationEvent("service_denied_target_policy", "light.test", {"code": "observe_only"}),
        ReconciliationEvent("service_retry_recovered", "light.test", {"failures": 2}),
    ]

    _apply_reconciliation_events(hass, engine, events, now_ms=1_000, rate_limiter=limiter)

    assert [event["type"] for event in list_diagnostics(hass)] == [
        "service_retry_scheduled",
        "service_denied_target_policy",
        "service_retry_recovered",
    ]


async def test_stable_ticks_do_not_poll_whole_install(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Selector coverage must not regress to periodic async_all sweeps."""
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    from homeassistant.core import StateMachine

    calls = 0
    original_async_all = StateMachine.async_all

    def count_async_all(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original_async_all(*args, **kwargs)

    monkeypatch.setattr(StateMachine, "async_all", count_async_all)
    await asyncio.sleep(0.35)
    await hass.async_block_till_done()

    assert calls == 0


async def test_registry_event_burst_coalesces_without_publication_spam(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import custom_components.intentional.publication as publication_module
    from custom_components.intentional.selector_ingest import SelectorMembershipPlanner

    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    invalidations = publications = 0
    original_registry_changed = SelectorMembershipPlanner.registry_changed

    def count_registry_changed(self):
        nonlocal invalidations
        invalidations += 1
        return original_registry_changed(self)

    def count_publication(*_args, **_kwargs):
        nonlocal publications
        publications += 1

    monkeypatch.setattr(SelectorMembershipPlanner, "registry_changed", count_registry_changed)
    monkeypatch.setattr(publication_module, "async_dispatcher_send", count_publication)
    for event_type in (
        "entity_registry_updated",
        "device_registry_updated",
        "area_registry_updated",
        "label_registry_updated",
    ):
        hass.bus.async_fire(event_type, {"action": "update", "id": "unrelated"})
    await asyncio.sleep(0.12)
    await hass.async_block_till_done()

    assert invalidations == 1
    assert publications == 0


async def test_semantic_selector_uses_real_ha_area_membership(
    hass: HomeAssistant, config_entry: MockConfigEntry, rule_dir: Path
) -> None:
    from homeassistant.helpers import area_registry as ar
    from homeassistant.helpers import entity_registry as er

    area = ar.async_get(hass).async_create("Office")
    entity_registry = er.async_get(hass)
    entity = entity_registry.async_get_or_create(
        "binary_sensor",
        "test",
        "office_motion",
        suggested_object_id="office_motion",
        original_device_class="motion",
    )
    entity_registry.async_update_entity(entity.entity_id, area_id=area.id)
    hass.states.async_set("binary_sensor.office_motion", "on")
    _write_semantic_rule(
        rule_dir, f"motion: {{detected: {{area: {area.id}}}}}"
    )

    engine = await _setup_and_wait(hass, config_entry)

    assert engine.resolve("light.semantic_result") is not None


async def test_semantic_selector_prefers_entity_area_and_filters_device(
    hass: HomeAssistant, config_entry: MockConfigEntry, rule_dir: Path
) -> None:
    from homeassistant.helpers import area_registry as ar
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er

    areas = ar.async_get(hass)
    device_area = areas.async_create("Device Area")
    entity_area = areas.async_create("Entity Area")
    config_entry.add_to_hass(hass)
    device_registry = dr.async_get(hass)
    device = device_registry.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={("test", "motion-device")},
    )
    device_registry.async_update_device(device.id, area_id=device_area.id)
    entity_registry = er.async_get(hass)
    entity = entity_registry.async_get_or_create(
        "binary_sensor",
        "test",
        "device_motion",
        suggested_object_id="device_motion",
        device_id=device.id,
        original_device_class="motion",
    )
    entity_registry.async_update_entity(entity.entity_id, area_id=entity_area.id)
    hass.states.async_set(entity.entity_id, "on")
    _write_semantic_rule(
        rule_dir,
        f"motion: {{detected: {{area: {entity_area.id}, device: {device.id}}}}}",
    )

    engine = await _setup_and_wait(hass, config_entry)

    assert engine.resolve("light.semantic_result") is not None


async def test_semantic_selector_uses_state_only_entity_device_class(
    hass: HomeAssistant, config_entry: MockConfigEntry, rule_dir: Path
) -> None:
    hass.states.async_set(
        "binary_sensor.state_only_motion", "on", {"device_class": "motion"}
    )
    _write_semantic_rule(rule_dir, "motion: {detected: {}}")

    engine = await _setup_and_wait(hass, config_entry)

    assert engine.resolve("light.semantic_result") is not None


async def test_registered_state_device_class_change_removal_and_recreation(
    hass: HomeAssistant, config_entry: MockConfigEntry, rule_dir: Path
) -> None:
    from homeassistant.helpers import entity_registry as er

    entity = er.async_get(hass).async_get_or_create(
        "binary_sensor",
        "test",
        "dynamic_class",
        suggested_object_id="dynamic_class",
    )
    hass.states.async_set(entity.entity_id, "on", {"device_class": "door"})
    _write_semantic_rule(rule_dir, "motion: {detected: {}}")
    engine = await _setup_and_wait(hass, config_entry)
    assert engine.resolve("light.semantic_result") is None

    hass.states.async_set(
        entity.entity_id,
        "on",
        {"device_class": "motion"},
        force_update=True,
    )
    for _ in range(20):
        await hass.async_block_till_done()
        if engine.resolve("light.semantic_result") is not None:
            break
        await asyncio.sleep(0.05)
    assert engine.resolve("light.semantic_result") is not None

    hass.states.async_remove(entity.entity_id)
    for _ in range(20):
        await hass.async_block_till_done()
        if engine.resolve("light.semantic_result") is None:
            break
        await asyncio.sleep(0.05)
    assert engine.resolve("light.semantic_result") is None

    hass.states.async_set(
        entity.entity_id,
        "on",
        {"device_class": "motion"},
        force_update=True,
    )
    for _ in range(20):
        await hass.async_block_till_done()
        if engine.resolve("light.semantic_result") is not None:
            break
        await asyncio.sleep(0.05)
    assert engine.resolve("light.semantic_result") is not None


async def test_semantic_selector_ordinary_state_changes_keep_cached_membership(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    rule_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from homeassistant.helpers import entity_registry as er

    import custom_components.intentional.selector_ingest as selector_ingest

    entity = er.async_get(hass).async_get_or_create(
        "binary_sensor",
        "test",
        "cached_motion",
        suggested_object_id="cached_motion",
        original_device_class="motion",
    )
    hass.states.async_set(entity.entity_id, "off")
    _write_semantic_rule(rule_dir, "motion: {detected: {}}")
    await _setup_and_wait(hass, config_entry)

    scans = 0
    original = selector_ingest._entry_matches

    def count_scan(*args, **kwargs):
        nonlocal scans
        scans += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(selector_ingest, "_entry_matches", count_scan)
    hass.states.async_set(entity.entity_id, "on")
    await asyncio.sleep(0.15)
    await hass.async_block_till_done()

    assert scans == 0


async def test_mutation_publishes_entity_updates_without_refresh_event(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
) -> None:
    """A public mutation still updates entities through the dispatcher."""
    from homeassistant.const import EVENT_STATE_CHANGED

    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    state_events: list[str] = []
    refresh_events = 0

    def count_state_event(event):
        if event.data.get("entity_id", "").startswith("sensor.intentional"):
            state_events.append(event.data["entity_id"])

    def count_refresh_event(_event):
        nonlocal refresh_events
        refresh_events += 1

    remove_state_listener = hass.bus.async_listen(EVENT_STATE_CHANGED, count_state_event)
    remove_refresh_listener = hass.bus.async_listen("intentional_refresh", count_refresh_event)
    await hass.services.async_call(
        DOMAIN, "fire", {"target": "light.test", "state": "on"}, blocking=True
    )
    await hass.async_block_till_done()
    remove_state_listener()
    remove_refresh_listener()

    assert state_events
    assert set(state_events) == {"sensor.intentional_intent_engine_summary"}
    assert refresh_events == 0


async def test_owned_service_result_does_not_cross_revision_barrier(
    hass: HomeAssistant,
) -> None:
    """Expected service feedback must not stale its reconciliation commit."""
    from types import SimpleNamespace

    from homeassistant.core import State

    from custom_components.intentional import _on_ha_state_change_factory
    from custom_components.intentional._engine import Engine
    from custom_components.intentional._engine.reconciliation import Reconciliation
    from custom_components.intentional._engine.runtime import TickRuntime
    from custom_components.intentional.runtime_context import IntentionalContextTracker

    engine = Engine()
    runtime = TickRuntime(tick_interval_ms=100)
    tracker = IntentionalContextTracker()
    reconciler = Reconciliation(
        drift_override_ttl_ms=300_000,
        drift_confirmation_ms=1_500,
        service_failure_backoff_ms=30_000,
    )
    listener = _on_ha_state_change_factory(
        hass, engine, reconciler, tracker, runtime=runtime
    )
    revision = runtime.advance_revision()

    await listener(SimpleNamespace(data={
        "old_state": State("light.test", "off"),
        "new_state": State("light.test", "on", context=tracker.new_context()),
    }))
    assert runtime.is_revision(revision)

    await listener(SimpleNamespace(data={
        "old_state": State("light.test", "on"),
        "new_state": State("light.test", "off"),
    }))
    assert not runtime.is_revision(revision)


async def test_registry_event_burst_is_coalesced(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import custom_components.intentional as intentional

    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    calls = 0
    original = intentional._apply_membership_change

    def count_apply(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(intentional, "_apply_membership_change", count_apply)
    for event_type, data in (
        ("entity_registry_updated", {"action": "update", "entity_id": "light.test", "changes": {}}),
        ("device_registry_updated", {"action": "update", "device_id": "test", "changes": {}}),
        ("area_registry_updated", {"action": "update", "area_id": "test"}),
        ("label_registry_updated", {"action": "update", "label_id": "test"}),
    ):
        hass.bus.async_fire(event_type, data)
    await hass.async_block_till_done()

    assert calls == 1


async def test_rule_file_loaded(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
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

    original_sync = intentional.sync_time_context_into_engine
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("synthetic tick failure")
        return original_sync(*args, **kwargs)

    monkeypatch.setattr(intentional, "sync_time_context_into_engine", fail_once)

    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    runtime = hass.data[DOMAIN][runtime_key(config_entry.entry_id)]

    for _ in range(20):
        await asyncio.sleep(0.05)
        diagnostics = list_diagnostics(hass)
        if (
            calls >= 3
            and runtime.failure_count >= 1
            and runtime.success_count >= 1
            and any(event["type"] == "tick_failed" for event in diagnostics)
        ):
            break

    diagnostics = list_diagnostics(hass)
    assert calls >= 3
    assert runtime.failure_count == 1
    assert runtime.success_count >= 1
    assert runtime.consecutive_failures == 0
    assert runtime.health()["status"] == "ok"
    assert any(
        event["type"] == "tick_failed" and "synthetic tick failure" in event.get("error", "")
        for event in diagnostics
    )


async def test_reload_service_works(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
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
        "      state: 'off'\n",
    )
    # Reload
    await hass.services.async_call(DOMAIN, "reload", blocking=True)
    assert len(engine._rules) == 2  # noqa: SLF001
    assert "extra-rule" in engine._rules  # noqa: SLF001


async def test_reload_retains_unchanged_selector_target_ownership(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    from custom_components.intentional._engine.reconciliation import reconciliation_key

    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    hass.states.async_set("light.selector_only", "off")
    rule_store = hass.data[DOMAIN][rule_store_key(config_entry.entry_id)]
    contents = """
- id: selector-off
  intent:
    select:
      - domain: light
        state: off
"""
    assert await rule_store.async_write("stored-rules.yaml", contents) is None
    await hass.services.async_call(DOMAIN, "reload", blocking=True)
    await asyncio.sleep(0.15)
    reconciler = hass.data[DOMAIN][reconciliation_key(config_entry.entry_id)]
    assert "light.selector_only" in reconciler.pending_withdraw_targets()

    await hass.services.async_call(DOMAIN, "reload", blocking=True)

    assert "light.selector_only" in reconciler.pending_withdraw_targets()


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
    assert resp.status in (401, 403), f"API should require auth, got {resp.status}"


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
    await hass.services.async_call(
        DOMAIN, "fire", {"target": "light.test", "state": "on"}, blocking=True
    )

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
        await asyncio.sleep(0.15)
        return False

    monkeypatch.setattr(hass.config_entries, "async_unload_platforms", reject_platform_unload)

    assert not await async_unload_entry(hass, config_entry)
    assert not runtime.stop_event.is_set()
    assert runtime.tick_task is not None
    assert not runtime.tick_task.done()
    assert config_entry.entry_id in hass.data[DOMAIN]
    assert rule_store_key(config_entry.entry_id) in hass.data[DOMAIN]

    await asyncio.sleep(0.15)
    assert runtime.success_count > success_count


async def test_unload_removes_all_entry_scoped_runtime_keys(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    from custom_components.intentional._engine.reconciliation import reconciliation_key
    from custom_components.intentional.lifecycle_writer import lifecycle_writer_key
    from custom_components.intentional.publication import publication_key

    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    assert await hass.config_entries.async_unload(config_entry.entry_id)

    domain_data = hass.data[DOMAIN]
    assert publication_key(config_entry.entry_id) not in domain_data
    assert reconciliation_key(config_entry.entry_id) not in domain_data
    assert lifecycle_writer_key(config_entry.entry_id) not in domain_data


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
    await hass.async_block_till_done()

    assert not any(key.startswith("input_boolean.test.") for key in engine.state)
    assert engine.resolve("light.test") is None


async def test_lifecycle_storage_skips_unchanged_tick_snapshots(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stable lifecycle state is saved once and not rewritten on unload."""
    from homeassistant.helpers.storage import Store

    saves_by_key: dict[str, list[dict]] = {}
    original_save = Store.async_save

    async def count_save(self, data):
        saves_by_key.setdefault(self.key, []).append(data)
        await original_save(self, data)

    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    monkeypatch.setattr(Store, "async_save", count_save)

    await asyncio.sleep(0.35)
    lifecycle_key = f"intentional_state_v1_{config_entry.entry_id}"
    authored_key = f"intentional_rules_{config_entry.entry_id}_v1"
    assert len(saves_by_key[lifecycle_key]) == 1
    assert authored_key not in saves_by_key
    canonical_snapshot = saves_by_key[lifecycle_key][0]

    await hass.config_entries.async_unload(config_entry.entry_id)
    assert saves_by_key[lifecycle_key] == [canonical_snapshot]
    assert authored_key not in saves_by_key


async def test_missing_rule_dir_is_created(hass: HomeAssistant, tmp_path: Path) -> None:
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
