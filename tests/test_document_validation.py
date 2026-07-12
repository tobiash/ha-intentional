"""Tests for pure document-wide policy preflight."""

import json
from types import SimpleNamespace

import pytest

from tests.dependencies import require_test_dependency

require_test_dependency("homeassistant", reason="homeassistant not installed")

from custom_components.intentional._engine.rule_model import Rule  # noqa: E402
from custom_components.intentional.document_validation import (  # noqa: E402
    document_policy_findings,
)


def test_detects_contradictory_floor_and_cap_across_rules() -> None:
    findings = document_policy_findings(
        [
            Rule(id="warm-floor", when="true", target="light.office", floor={"brightness_pct": 70}),
            Rule(id="energy-cap", when="true", target="light.office", cap={"brightness_pct": 40}),
        ]
    )

    assert findings["errors"] == []
    assert findings["warnings"] == [
        {
            "code": "contradictory_floor_cap",
            "target": "light.office",
            "field": "brightness_pct",
            "floor": 70,
            "cap": 40,
            "rule_ids": ["energy-cap", "warm-floor"],
            "message": "light.office.brightness_pct has floor 70 above cap 40.",
        }
    ]


def test_detects_missing_suppression_ids_and_cycles() -> None:
    findings = document_policy_findings(
        [
            Rule(id="day", when="true", blocks=("night", "missing")),
            Rule(id="night", when="true", blocks=("day",)),
        ]
    )

    assert {error["code"] for error in findings["errors"]} == {
        "missing_suppression_rule",
        "suppression_cycle",
    }
    cycle = next(error for error in findings["errors"] if error["code"] == "suppression_cycle")
    assert cycle["rule_ids"] == ["day", "night"]


def test_uses_authored_ids_for_expanded_rule_suppression() -> None:
    findings = document_policy_findings(
        [
            Rule(id="mode::light.a", authored_rule_id="mode", when="true", target="light.a"),
            Rule(id="override", when="true", blocks=("mode",)),
        ]
    )

    assert findings["errors"] == []


def test_warns_only_when_modifier_field_has_no_document_baseline() -> None:
    findings = document_policy_findings(
        [
            Rule(id="baseline", when="true", target="climate.office", set={"temperature": 20}),
            Rule(
                id="adjust",
                when="true",
                target="climate.office",
                offset={"temperature": 2, "humidity": 5},
            ),
        ]
    )

    modifier_warnings = [
        warning
        for warning in findings["warnings"]
        if warning["code"] == "modifier_without_document_baseline"
    ]
    assert [warning["field"] for warning in modifier_warnings] == ["humidity"]


def test_detects_effect_only_domain_as_durable_target() -> None:
    findings = document_policy_findings(
        [
            Rule(id="press", when="true", target="button.restart", set={"state": "on"}),
        ]
    )

    assert findings["errors"][0]["code"] == "effect_only_durable_target"


def test_warns_without_breaking_legacy_dangerous_targets() -> None:
    findings = document_policy_findings(
        [
            Rule(id="secure", when="true", target="lock.front", set={"state": "locked"}),
        ]
    )

    assert findings["errors"] == []
    assert findings["warnings"][0]["code"] == "dangerous_target_without_policy"


async def test_validate_api_returns_floor_cap_conflict_as_warning() -> None:
    from custom_components.intentional.api import IntentionalValidateView

    contents = """
- id: floor
  while: {input_boolean.test: on}
  intent: {light.office: {brightness_pct: {min: 70}}}
- id: cap
  while: {input_boolean.test: on}
  intent: {light.office: {brightness_pct: {max: 40}}}
"""

    class Request:
        app = {"hass": SimpleNamespace(states=SimpleNamespace(get=lambda _target: None))}

        async def json(self) -> dict[str, str]:
            return {"contents": contents}

    response = await IntentionalValidateView().post(Request())
    body = json.loads(response.body)

    assert response.status == 200
    assert body["valid"] is True
    assert len(body["normalized"]) == 2
    assert body["errors"] == []
    assert body["warnings"][0]["code"] == "contradictory_floor_cap"


async def test_validate_api_warns_for_legacy_dangerous_target() -> None:
    from custom_components.intentional.api import IntentionalValidateView

    class Request:
        app = {"hass": SimpleNamespace(states=SimpleNamespace(get=lambda _target: None))}

        async def json(self) -> dict[str, str]:
            return {
                "contents": "- id: secure\n  when: 'true'\n  emit: {target: lock.front, set: {state: locked}}\n"
            }

    response = await IntentionalValidateView().post(Request())
    body = json.loads(response.body)

    assert body["valid"] is True
    assert any(warning["code"] == "dangerous_target_without_policy" for warning in body["warnings"])


@pytest.mark.parametrize(
    ("fragment", "expected_code"),
    [
        (
            "when: sensor.room ==\n  emit: {target: light.room, set: {state: on}}",
            "rule_validation_error",
        ),
        (
            "when: 'true'\n  emit: {target: light.room, set: {brightness: '{{ broken'}}}",
            "rule_load_error",
        ),
    ],
)
async def test_validate_api_rejects_invalid_expressions_and_templates(
    fragment: str, expected_code: str
) -> None:
    from custom_components.intentional.api import IntentionalValidateView

    class Request:
        app = {"hass": SimpleNamespace(states=SimpleNamespace(get=lambda _target: None))}

        async def json(self) -> dict[str, str]:
            return {"contents": f"- id: invalid\n  {fragment}\n"}

    response = await IntentionalValidateView().post(Request())
    body = json.loads(response.body)

    assert response.status == 400
    assert body["valid"] is False
    assert body["errors"][0]["code"] == expected_code
    assert "failed" in body["errors"][0]["message"].lower()
