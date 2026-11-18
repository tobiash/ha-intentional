"""Native hold entities work through real Home Assistant service validation."""

import pytest

pytest.importorskip("homeassistant")

from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.intentional.const import CONF_RULE_DIR, DOMAIN


@pytest.mark.asyncio
async def test_native_hold_controls_end_to_end(hass, enable_custom_integrations, tmp_path):
    (tmp_path / "rules.yaml").write_text('''
targets:
  light.test:
    manual_override: {resume: activation, hold_controls: true}
rules:
  - id: ambient
    while: {input_boolean.test: on}
    intent: {light.test: {state: on, brightness_pct: 40}}
''')
    entry = MockConfigEntry(domain=DOMAIN, title="Intentional", data={CONF_RULE_DIR: str(tmp_path)})
    entry.add_to_hass(hass)
    hass.states.async_set("light.test", "on", {"friendly_name": "Test light", "brightness": 255, "color_mode": "rgb", "rgb_color": [100, 0, 255]})
    hass.states.async_set("input_boolean.test", "on")
    assert await hass.config_entries.async_setup(entry.entry_id)
    try:
        await hass.async_block_till_done()
        engine = hass.data[DOMAIN][entry.entry_id]
        registry = er.async_get(hass)

        def entity(domain, kind):
            result = registry.async_get_entity_id(domain, DOMAIN, f"{entry.entry_id}_hold_light.test_{kind}")
            assert result is not None
            return result

        duration = entity("select", "hold_duration")
        status = entity("sensor", "manual_control_status")
        assert hass.states.get(duration).state == "1 hour"
        assert engine.list_active_user_intents() == []
        await hass.services.async_call("select", "select_option", {"entity_id": duration, "option": "2 hours"}, blocking=True)
        assert engine.list_active_user_intents() == []
        await hass.services.async_call("button", "press", {"entity_id": entity("button", "hold_off")}, blocking=True)
        await hass.async_block_till_done()
        assert engine.resolve("light.test").value["state"] == "off"
        manual = engine.list_active_user_intents()[0]
        assert manual.authority.value == "hold"
        assert manual.ttl_ms == 7200000
        assert hass.states.get(status).state == "held_off"
        await hass.services.async_call("button", "press", {"entity_id": entity("button", "resume_automation")}, blocking=True)
        assert not engine.list_active_user_intents()
        assert not engine.dismissed_rule_ids("light.test")
        await hass.services.async_call("select", "select_option", {"entity_id": duration, "option": "Until resumed"}, blocking=True)
        await hass.services.async_call("button", "press", {"entity_id": entity("button", "hold_current_settings")}, blocking=True)
        manual = engine.list_active_user_intents()[0]
        assert manual.ttl_ms is None
        assert manual.set == {"state": "on", "brightness": 255, "rgb_color": [100, 0, 255]}
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        engine = hass.data[DOMAIN][entry.entry_id]
        assert hass.states.get(duration).state == "Until resumed"
        assert engine.list_active_user_intents()[0].authority.value == "hold"
    finally:
        await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.asyncio
async def test_unavailable_current_settings_is_not_captured(hass):
    from homeassistant.exceptions import HomeAssistantError

    from custom_components.intentional._engine.engine import Engine
    from custom_components.intentional._engine.target_policy import TargetPolicy
    from custom_components.intentional.hold_controls import HoldButton
    entry = MockConfigEntry(domain=DOMAIN)
    engine = Engine()
    engine.load_rules([], target_policies={"light.test": TargetPolicy(hold_controls=True)})
    hass.data[DOMAIN] = {entry.entry_id: engine}
    hass.states.async_set("light.test", "unavailable")
    button = HoldButton(hass, entry, "light.test", "current")
    with pytest.raises(HomeAssistantError, match="unknown or unavailable"):
        await button.async_press()
    assert not engine.list_active_user_intents()


@pytest.mark.asyncio
async def test_named_scope_controls_all_members_and_resume_only_that_scope(hass):
    from custom_components.intentional._engine.engine import Engine
    from custom_components.intentional._engine.target_policy import TargetPolicy
    from custom_components.intentional.hold_controls import HoldButton, HoldStatus
    entry = MockConfigEntry(domain=DOMAIN)
    engine = Engine()
    engine.load_rules([], target_policies={t: TargetPolicy(hold_controls="Living room lights") for t in ("light.one", "light.two")})
    hass.data[DOMAIN] = {entry.entry_id: engine}

    async def fire(call):
        engine.emit_manual_intent(call.data["target"], {"state": call.data["state"]}, hold=call.data["hold"], resume=call.data["resume"], ttl_ms=call.data["ttl"] * 1000)

    async def clear(call):
        engine.clear_user_intents(call.data["target"])

    hass.services.async_register(DOMAIN, "fire", fire)
    hass.services.async_register(DOMAIN, "clear", clear)
    button = HoldButton(hass, entry, "scope:Living room lights", "off")
    await button.async_press()
    assert {i.target for i in engine.list_active_user_intents()} == {"light.one", "light.two"}
    status = HoldStatus(hass, entry, "scope:Living room lights")
    assert status.native_value == "held_off"
    assert status.extra_state_attributes["ends_at"] is not None
    engine.emit_manual_intent("light.other", {"state": "off"}, hold=True)
    await HoldButton(hass, entry, "scope:Living room lights", "resume").async_press()
    assert status.native_value == "automatic"
    assert [i.target for i in engine.list_active_user_intents()] == ["light.other"]
