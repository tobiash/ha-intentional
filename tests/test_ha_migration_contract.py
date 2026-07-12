"""Home Assistant automation discovery compatibility contract."""

from tests.dependencies import require_test_dependency

require_test_dependency("homeassistant", reason="homeassistant not installed")

from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.setup import async_setup_component  # noqa: E402


async def test_loaded_automation_exposes_raw_config_to_discovery(
    hass: HomeAssistant,
) -> None:
    from custom_components.intentional.api import _loaded_automations

    source = {
        "id": "migration-contract",
        "alias": "Migration contract",
        "triggers": [
            {"trigger": "state", "entity_id": "binary_sensor.hall", "to": "on"}
        ],
        "actions": [
            {"action": "light.turn_on", "target": {"entity_id": "light.hall"}}
        ],
    }
    assert await async_setup_component(hass, "automation", {"automation": [source]})
    await hass.async_block_till_done()

    entities = tuple(hass.data["automation"].entities)
    entity = next(item for item in entities if item.entity_id == "automation.migration_contract")
    assert isinstance(entity.raw_config, dict)
    assert entity.raw_config["id"] == "migration-contract"
    assert _loaded_automations(hass)[entity.entity_id] == dict(entity.raw_config)
