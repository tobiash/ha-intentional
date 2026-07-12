"""Pure tests for deep Target projection and timeline simulation."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from intentional.engine import Engine
from intentional.projection import (
    REDACTED,
    explain_card,
    preview_targets,
    redact_sensitive,
    target_projection,
)
from intentional.reconciliation import Reconciliation
from intentional.simulation import simulate_timeline, validate_simulation_input
from intentional.target_policy import TargetPolicy
from intentional.yaml_loader import Rule


def _engine() -> Engine:
    engine = Engine(clock_fn=lambda: 0)
    engine.load_rules(
        [
            Rule(
                id="baseline",
                when="input_boolean.room == 'on'",
                target="light.room",
                set={"state": "on", "brightness_pct": 80},
                confidence=0.5,
            ),
            Rule(
                id="limit",
                when="input_boolean.room == 'on'",
                target="light.room",
                cap={"brightness_pct": 40},
                confidence=0.8,
            ),
        ]
    )
    engine.update_state("input_boolean.room", "on")
    engine.evaluate_all()
    return engine


def test_target_projection_explains_fields_actual_match_and_manual_reveal() -> None:
    engine = _engine()
    engine.emit_user_intent("light.room", {"state": "on", "brightness_pct": 60}, ttl_ms=5_000)
    actual = SimpleNamespace(entity_id="light.room", state="on", attributes={"brightness": 102})
    reconciliation = Reconciliation(
        drift_override_ttl_ms=5_000,
        drift_confirmation_ms=100,
        service_failure_backoff_ms=1_000,
    )

    record = target_projection(
        engine, "light.room", actual_state=actual, reconciliation=reconciliation
    )

    brightness = next(field for field in record["fields"] if field["field"] == "brightness_pct")
    assert brightness["provider"]["authority"] == "user"
    assert brightness["losing_providers"][0]["rule_id"] == "baseline"
    assert brightness["modifiers"] == [
        {
            "operation": "cap",
            "value": 40,
            "rule_id": "limit",
            "authority": "automation",
        }
    ]
    assert record["plan_match"] == "match"
    assert record["manual_override"]["remaining_ms"] == 5_000
    assert record["manual_override"]["revealed_after_withdrawal"]["value"]["brightness_pct"] == 40
    assert record["reconciliation"]["owned"] is False


def test_target_projection_redacts_non_reconciliation_attributes() -> None:
    engine = _engine()
    actual = SimpleNamespace(
        entity_id="light.room",
        state="on",
        attributes={"brightness": 100, "access_token": "secret", "friendly_name": "Room"},
    )

    record = target_projection(engine, "light.room", actual_state=actual)

    assert record["actual"]["attributes"] == {"brightness": 100}


def test_recursive_redaction_preserves_policy_explanation() -> None:
    secret = "never-serialize-this-value"

    projected = redact_sensitive(
        {
            "desired": {
                "state": "unlocked",
                "code": secret,
                "nested": {"access_token": secret, "opaque_credential": secret},
            },
            "policy_denial": {
                "code": "field_not_allowed",
                "message": "code is not allowed",
            },
            "service_plan": [{"domain": "lock", "service": "unlock", "data": {"code": secret}}],
            "fields": [{"field": "alarm_code", "value": secret}],
        }
    )

    assert secret not in str(projected)
    assert projected["desired"]["code"] == REDACTED
    assert projected["desired"]["nested"]["access_token"] == REDACTED
    assert projected["service_plan"][0]["data"] == REDACTED
    assert projected["fields"][0]["value"] == REDACTED
    assert projected["policy_denial"] == {
        "code": "field_not_allowed",
        "message": "code is not allowed",
    }


def test_all_non_admin_projections_omit_secret_values() -> None:
    secret = "projection-secret-7264"
    engine = Engine(clock_fn=lambda: 0)
    engine.load_rules(
        [
            Rule(
                id="secure",
                when="true",
                target="lock.front",
                set={"state": "unlocked", "code": secret},
            )
        ]
    )
    engine.evaluate_all()

    payloads = [
        target_projection(engine, "lock.front"),
        preview_targets(engine),
        explain_card(engine),
    ]

    assert all(secret not in str(payload) for payload in payloads)
    assert payloads[0]["desired"]["state"] == "unlocked"


@pytest.mark.asyncio
async def test_simulation_models_rejection_retry_drift_override_expiry_and_restart() -> None:
    engine = _engine()
    steps = await simulate_timeline(
        engine,
        [
            {"actual": {"light.room": {"state": "off"}}, "reject_calls": True},
            {"advance_ms": 500},
            {
                "advance_ms": 500,
                "actual": {
                    "light.room": {
                        "state": "on",
                        "attributes": {"brightness": 50},
                        "user_id": "user",
                    }
                },
            },
            {
                "advance_ms": 100,
                "actual": {
                    "light.room": {
                        "state": "on",
                        "attributes": {"brightness": 50},
                        "user_id": "user",
                    }
                },
            },
            {
                "advance_ms": 100,
                "actual": {
                    "light.room": {
                        "state": "on",
                        "attributes": {"brightness": 50},
                        "user_id": "user",
                    }
                },
            },
            {"restart": True},
            {"advance_ms": 5_000},
        ],
        reconciliation_options={
            "drift_confirmation_ms": 100,
            "drift_override_ttl_ms": 5_000,
            "service_failure_backoff_ms": 1_000,
            "drift_transition_grace_ms": 0,
        },
    )

    assert steps[0]["calls"][0]["rejected"] is True
    assert steps[1]["targets"][0]["reconciliation"]["retry"]["remaining_ms"] == 500
    assert any(event["kind"] == "service_retry_recovered" for event in steps[2]["events"])
    assert any(event["kind"] == "drift_promoted" for event in steps[4]["events"])
    assert steps[4]["targets"][0]["manual_override"] is not None
    assert steps[5]["checkpoint"] == "restart"
    assert steps[6]["targets"][0]["manual_override"] is None


@pytest.mark.asyncio
async def test_simulation_models_pause_and_global_disable() -> None:
    engine = _engine()
    steps = await simulate_timeline(
        engine,
        [
            {},
            {"pause_rule_ids": ["baseline", "limit"]},
            {"resume_rule_ids": ["baseline", "limit"]},
            {"enabled": False},
        ],
    )

    assert steps[0]["active_targets"] == ["light.room"]
    assert steps[1]["active_targets"] == []
    assert steps[2]["active_targets"] == ["light.room"]
    assert steps[3]["active_targets"] == []


@pytest.mark.asyncio
async def test_simulated_restart_uses_fresh_engine_without_duplicate_intents() -> None:
    engine = _engine()
    engine.emit_user_intent("light.room", {"state": "off"}, ttl_ms=5_000)

    steps = await simulate_timeline(engine, [{}, {"restart": True}])

    assert len(steps[0]["targets"][0]["fields"][0]["losing_providers"]) == len(
        steps[1]["targets"][0]["fields"][0]["losing_providers"]
    )
    assert engine.active_intent_count() == 3


@pytest.mark.parametrize(
    "timeline,options,message",
    [
        ([{"advance_ms": True}], {}, "non-negative integer"),
        ([{"actual": {"light.x": {"attributes": []}}}], {}, "attributes must be a mapping"),
        ([{"unknown": True}], {}, "unknown fields"),
        ([], {"service_failure_backoff_ms": "soon"}, "non-negative integer"),
        (
            [],
            {"service_failure_backoff_ms": 10, "service_failure_backoff_max_ms": 5},
            "at least the base",
        ),
    ],
)
def test_simulation_input_is_strict(timeline, options, message) -> None:
    with pytest.raises(ValueError, match=message):
        validate_simulation_input(timeline, options)


@pytest.mark.asyncio
async def test_simulation_and_explanation_expose_policy_denial() -> None:
    engine = Engine(clock_fn=lambda: 0)
    engine.load_rules(
        [
            Rule(
                id="unlock",
                when="true",
                target="lock.front",
                set={"state": "unlocked"},
            )
        ]
    )
    engine.load_rules(
        engine.loaded_rules(),
        target_policies={
            "lock.front": TargetPolicy(forbidden_automatic_states=frozenset({"unlocked"}))
        },
    )
    engine.evaluate_all()

    steps = await simulate_timeline(engine, [{}])

    assert steps[0]["events"][0]["kind"] == "service_denied_target_policy"
    assert steps[0]["targets"][0]["policy_denial"]["code"] == "automatic_state_forbidden"
    assert (
        engine.explain_target("lock.front")["policy_denial"]["code"] == "automatic_state_forbidden"
    )


@pytest.mark.asyncio
async def test_simulation_requires_selector_membership_and_carries_it_across_restart() -> None:
    from intentional.yaml_loader import load_rules_from_string

    rules = load_rules_from_string("""
rules:
  - id: selected
    while: {input_boolean.ready: on}
    intent:
      select:
        - domain: light
          area: office
          state: on
""")
    engine = Engine(clock_fn=lambda: 0)
    engine.load_rules(rules)
    engine.update_state("input_boolean.ready", "on")

    with pytest.raises(ValueError, match="Missing simulated selector memberships"):
        await simulate_timeline(engine, [{}])

    steps = await simulate_timeline(
        engine,
        [{}, {"restart": True}],
        selector_memberships=[
            {
                "selector": {"domain": "light", "area": "office"},
                "targets": ["light.desk", "light.ceiling"],
            }
        ],
    )

    assert steps[0]["active_targets"] == ["light.ceiling", "light.desk"]
    assert steps[1]["active_targets"] == ["light.ceiling", "light.desk"]


def test_simulation_bounds_selector_expansion() -> None:
    memberships = [
        {
            "selector": {"domain": "light"},
            "targets": [f"light.member_{index}" for index in range(201)],
        }
    ]

    with pytest.raises(ValueError, match="expand to at most"):
        validate_simulation_input([], {}, selector_memberships=memberships)
