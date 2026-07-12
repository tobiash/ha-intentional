import importlib.util
import math
from copy import deepcopy
from pathlib import Path

import yaml

_SPEC = importlib.util.spec_from_file_location(
    "ha_migration", Path(__file__).parents[1] / "custom_components/intentional/ha_migration.py"
)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
convert_automation = _MODULE.convert_automation


def test_converts_multiple_triggers_and_actions_deterministically_without_mutation() -> None:
    source = {
        "id": "Hall Lights",
        "trigger": [
            {"platform": "state", "entity_id": "binary_sensor.hall", "to": "on", "for": "2s"},
            {"platform": "numeric_state", "entity_id": "sensor.lux", "above": 10, "below": 30},
        ],
        "action": [
            {"service": "switch.turn_off", "target": {"entity_id": "switch.fan"}},
            {"service": "light.turn_on", "target": {"entity_id": ["light.hall", "light.steps"]}, "data": {"brightness_pct": 60}},
        ],
    }
    original = deepcopy(source)

    first = convert_automation(source, source_entity_id="automation.hall_lights")
    second = convert_automation(source, source_entity_id="automation.hall_lights")

    assert source == original
    assert first == second
    assert first["supported"] is True
    assert first["source_mutated"] is False
    rules = yaml.safe_load(first["yaml"])
    assert [rule["id"] for rule in rules] == ["migrate-hall-lights-trigger-1", "migrate-hall-lights-trigger-2"]
    assert rules[0]["after"] == "2s"
    assert rules[0]["intent"]["light.hall"] == {"state": "on", "brightness_pct": 60}
    assert rules[1]["while"]["sensor.lux"] == {"gt": 10, "lt": 30}


def test_rejects_unsafe_shapes_and_conflicts() -> None:
    cases = [
        {"condition": [{"condition": "state"}], "trigger": {}, "action": {}},
        {"trigger": {"platform": "state", "entity_id": "binary_sensor.x"}, "action": {}},
        {"trigger": {"platform": "state", "entity_id": "binary_sensor.x", "to": "on"}, "action": {"device_id": "x"}},
        {"trigger": {"platform": "state", "entity_id": "binary_sensor.x", "to": "on"}, "action": [{"service": "light.turn_on", "target": {"entity_id": "light.x"}}, {"service": "light.turn_off", "target": {"entity_id": "light.x"}}]},
    ]
    for source in cases:
        result = convert_automation(source, source_entity_id="automation.x")
        assert result["supported"] is False
        assert result["yaml"] == ""
        assert result["diagnostics"][0]["severity"] == "error"


def test_rejects_attribute_and_from_triggers_and_non_durable_action_data() -> None:
    triggers = [
        {"platform": "state", "entity_id": "sensor.x", "attribute": "mode", "to": "on"},
        {"platform": "numeric_state", "entity_id": "sensor.x", "attribute": "level", "above": 1},
        {"platform": "state", "entity_id": "sensor.x", "from": "off", "to": "on"},
    ]
    data_values = [{"transition": 1}, {"flash": "short"}, {"unknown": 1}, {"brightness": "!secret value"}]
    for trigger in triggers:
        source = {"trigger": trigger, "action": {"service": "light.turn_on", "target": {"entity_id": "light.x"}}}
        assert convert_automation(source, source_entity_id="automation.x")["supported"] is False
    for data in data_values:
        source = {"trigger": {"platform": "state", "entity_id": "sensor.x", "to": "on"}, "action": {"service": "light.turn_on", "target": {"entity_id": "light.x"}, "data": data}}
        result = convert_automation(source, source_entity_id="automation.x")
        assert result["supported"] is False
        assert result["yaml"] == ""


def test_rejects_non_finite_trigger_and_action_numbers_before_serialization() -> None:
    for value in (math.nan, math.inf, -math.inf):
        sources = [
            {
                "trigger": {"platform": "numeric_state", "entity_id": "sensor.x", "above": value},
                "action": {"service": "light.turn_on", "target": {"entity_id": "light.x"}},
            },
            {
                "trigger": {"platform": "state", "entity_id": "sensor.x", "to": "on"},
                "action": {
                    "service": "light.turn_on",
                    "target": {"entity_id": "light.x"},
                    "data": {"brightness": value},
                },
            },
        ]
        for source in sources:
            result = convert_automation(source, source_entity_id="automation.x")
            assert result["supported"] is False
            assert result["yaml"] == ""
            assert result["starter_timeline"] == []
            assert "Non-finite" in result["diagnostics"][0]["message"]
