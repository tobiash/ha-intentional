"""Generated service plans must satisfy current Home Assistant contracts."""

from types import SimpleNamespace

import pytest

from tests.dependencies import require_test_dependency

require_test_dependency("homeassistant", reason="homeassistant not installed")

from homeassistant.components.light import LIGHT_TURN_ON_SCHEMA  # noqa: E402
from homeassistant.helpers import config_validation as cv  # noqa: E402

from custom_components.intentional.document_validation import validate_document  # noqa: E402
from intentional.ha_adapter import (  # noqa: E402
    service_calls_for_resolved_target,
    service_plan_matches_state,
    service_plan_signature,
)


@pytest.mark.parametrize("mired,kelvin", [(370, 2702), (250, 4000), (400, 2500)])
def test_mired_authoring_dispatches_kelvin_and_matches_ha_state(mired, kelvin):
    calls = service_calls_for_resolved_target("light.desk", {"color_temp_mired": mired})
    domain, service, data = calls[0]
    assert (domain, service) == ("light", "turn_on")
    assert cv.make_entity_service_schema(LIGHT_TURN_ON_SCHEMA)(data)["color_temp_kelvin"] == kelvin
    assert "color_temp" not in data
    actual = SimpleNamespace(
        entity_id="light.desk", state="on",
        attributes={"color_mode": "color_temp", "color_temp_kelvin": kelvin},
    )
    assert service_plan_matches_state(service_plan_signature(calls), actual)
    actual.attributes["color_temp_kelvin"] = kelvin + 100
    assert not service_plan_matches_state(service_plan_signature(calls), actual)


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), True])
def test_invalid_mired_is_rejected(value):
    with pytest.raises(ValueError, match="finite positive"):
        service_calls_for_resolved_target("light.desk", {"color_temp_mired": value})


@pytest.mark.parametrize("body", [
    "intent: {climate.office: {aux_heat: true}}",
    "when: 'true'\n  emit: {target: climate.office, set: {aux_heat: true}}",
])
def test_removed_aux_heat_rejected_before_dispatch(body):
    _, findings = validate_document(f"- id: heat\n  {body}\n")
    assert findings["errors"]
    assert "preset_mode" in str(findings["errors"])


def test_removed_aux_heat_cannot_generate_a_service_plan():
    with pytest.raises(ValueError, match="preset_mode"):
        service_calls_for_resolved_target("climate.office", {"aux_heat": True})
