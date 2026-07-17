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
from intentional.yaml_loader import Rule, load_rules_from_string


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


@pytest.mark.asyncio
async def test_simulation_projects_alert_firing_and_resolution() -> None:
    engine = Engine(clock_fn=lambda: 0)
    engine.load_rules(load_rules_from_string("""
- id: freezer-too-warm
  while:
    sensor.freezer_temperature:
      gt: -10
  alert:
    name: FreezerTemperatureHigh
    severity: critical
    annotations:
      summary: Freezer is too warm
"""))

    steps = await simulate_timeline(engine, [
        {"states": {"sensor.freezer_temperature.state": -18}},
        {"advance_ms": 1_000, "states": {"sensor.freezer_temperature.state": -5}},
        {"advance_ms": 1_000, "states": {"sensor.freezer_temperature.state": -18}},
    ])

    assert [step["alerts"][0]["state"] for step in steps] == [
        "inactive",
        "firing",
        "inactive",
    ]
    transitions = [
        transition
        for step in steps
        for transition in step["alert_transitions"]
    ]
    assert [transition["to"] for transition in transitions] == ["firing", "resolved"]
    assert transitions[0]["instance_id"]
    assert transitions[1]["instance_id"] == transitions[0]["instance_id"]
    assert all(step["active_targets"] == [] for step in steps)
    assert all(step["effects"] == [] for step in steps)
    assert all(step["calls"] == [] for step in steps)


@pytest.mark.asyncio
async def test_simulation_fires_alert_after_its_pending_duration() -> None:
    engine = Engine(clock_fn=lambda: 0)
    engine.load_rules(load_rules_from_string("""
- id: freezer-too-warm
  while: {sensor.freezer_temperature: {gt: -10}}
  alert:
    name: FreezerTemperatureHigh
    severity: warning
    for: 10s
    annotations: {summary: Freezer is too warm}
"""))

    steps = await simulate_timeline(engine, [
        {"states": {"sensor.freezer_temperature.state": -5}},
        {"advance_ms": 9_999},
        {"advance_ms": 1},
    ])

    assert [step["alerts"][0]["state"] for step in steps] == [
        "pending",
        "pending",
        "firing",
    ]
    assert [
        transition["to"]
        for step in steps
        for transition in step["alert_transitions"]
    ] == ["pending", "firing"]


@pytest.mark.asyncio
async def test_alert_pending_duration_is_not_added_to_rule_duration() -> None:
    engine = Engine(clock_fn=lambda: 0)
    engine.load_rules(load_rules_from_string("""
- id: freezer
  while: {sensor.freezer_temperature: {gt: -10}}
  for: 5s
  alert:
    name: FreezerTemperatureHigh
    severity: warning
    for: 10s
    annotations: {summary: Freezer is too warm}
"""))

    steps = await simulate_timeline(engine, [
        {"states": {"sensor.freezer_temperature.state": -5}},
        {"advance_ms": 9_999},
        {"advance_ms": 1},
    ])

    assert [step["alerts"][0]["state"] for step in steps] == [
        "pending",
        "pending",
        "firing",
    ]


@pytest.mark.asyncio
async def test_alert_inherits_rule_pending_duration_when_omitted() -> None:
    engine = Engine(clock_fn=lambda: 0)
    engine.load_rules(load_rules_from_string("""
- id: freezer
  while: {sensor.freezer_temperature: {gt: -10}}
  for: 5s
  alert:
    name: FreezerTemperatureHigh
    severity: warning
    annotations: {summary: Freezer is too warm}
"""))

    steps = await simulate_timeline(engine, [
        {"states": {"sensor.freezer_temperature.state": -5}},
        {"advance_ms": 4_999},
        {"advance_ms": 1},
    ])

    assert [step["alerts"][0]["state"] for step in steps] == [
        "pending",
        "pending",
        "firing",
    ]


@pytest.mark.asyncio
async def test_simulation_creates_new_alert_instance_after_recurrence() -> None:
    engine = Engine(clock_fn=lambda: 0)
    engine.load_rules(load_rules_from_string("""
- id: leak
  while: {binary_sensor.water_leak: "on"}
  alert:
    name: WaterLeak
    severity: critical
    annotations: {summary: Water leak detected}
"""))

    steps = await simulate_timeline(engine, [
        {"states": {"binary_sensor.water_leak.state": "on"}},
        {"states": {"binary_sensor.water_leak.state": "off"}},
        {"states": {"binary_sensor.water_leak.state": "on"}},
    ])

    transitions = [
        transition
        for step in steps
        for transition in step["alert_transitions"]
    ]
    assert [transition["to"] for transition in transitions] == [
        "firing",
        "resolved",
        "firing",
    ]
    assert transitions[1]["instance_id"] == transitions[0]["instance_id"]
    assert transitions[2]["instance_id"] != transitions[0]["instance_id"]


@pytest.mark.asyncio
async def test_simulation_preserves_pending_alert_across_restart() -> None:
    engine = Engine(clock_fn=lambda: 0)
    engine.load_rules(load_rules_from_string("""
- id: freezer
  while: {sensor.freezer_temperature: {gt: -10}}
  alert:
    name: FreezerTemperatureHigh
    severity: warning
    for: 10s
    annotations: {summary: Freezer is too warm}
"""))

    steps = await simulate_timeline(engine, [
        {"states": {"sensor.freezer_temperature.state": -5}},
        {"advance_ms": 5_000, "restart": True},
        {"advance_ms": 4_999},
        {"advance_ms": 1},
    ])

    assert [step["alerts"][0]["state"] for step in steps] == [
        "pending",
        "pending",
        "pending",
        "firing",
    ]
    assert len({step["alerts"][0]["instance_id"] for step in steps}) == 1
    assert steps[1]["checkpoint"] == "restart"


@pytest.mark.asyncio
async def test_simulation_keeps_firing_alert_during_unknown_evidence() -> None:
    engine = Engine(clock_fn=lambda: 0)
    engine.load_rules(load_rules_from_string("""
- id: freezer
  while: {sensor.freezer_temperature: {gt: -10}}
  alert:
    name: FreezerTemperatureHigh
    severity: critical
    annotations: {summary: Freezer is too warm}
"""))

    steps = await simulate_timeline(engine, [
        {"states": {"sensor.freezer_temperature.state": -5}},
        {"advance_ms": 1_000, "states": {"sensor.freezer_temperature.state": "unavailable"}},
        {"advance_ms": 119_999},
        {"advance_ms": 1},
        {"states": {"sensor.freezer_temperature.state": -5}},
        {"states": {"sensor.freezer_temperature.state": -18}},
    ])

    assert [step["alerts"][0]["state"] for step in steps] == [
        "firing",
        "firing",
        "firing",
        "firing",
        "firing",
        "inactive",
    ]
    assert [step["alerts"][0]["evaluation_status"] for step in steps] == [
        "current",
        "grace",
        "grace",
        "stale",
        "current",
        "current",
    ]
    assert [
        transition["to"]
        for step in steps
        for transition in step["alert_transitions"]
    ] == ["firing", "resolved"]


@pytest.mark.asyncio
async def test_simulation_latches_pulse_alert_until_after_latest_pulse() -> None:
    engine = Engine(clock_fn=lambda: 0)
    engine.load_rules(load_rules_from_string("""
- id: doorbell
  when: binary_sensor.doorbell.changed == true and binary_sensor.doorbell == "on"
  alert:
    name: DoorbellPressed
    severity: info
    resolve_after: 5s
    annotations: {summary: Doorbell pressed}
"""))

    steps = await simulate_timeline(engine, [
        {"states": {"binary_sensor.doorbell.state": "off"}},
        {"states": {"binary_sensor.doorbell.state": "on"}},
        {"advance_ms": 4_000},
        {"states": {"binary_sensor.doorbell.state": "off"}},
        {"states": {"binary_sensor.doorbell.state": "on"}},
        {"advance_ms": 4_999},
        {"advance_ms": 1},
    ])

    assert [step["alerts"][0]["state"] for step in steps] == [
        "inactive",
        "firing",
        "firing",
        "firing",
        "firing",
        "firing",
        "inactive",
    ]
    transitions = [
        transition
        for step in steps
        for transition in step["alert_transitions"]
    ]
    assert [transition["to"] for transition in transitions] == ["firing", "resolved"]
    assert transitions[1]["instance_id"] == transitions[0]["instance_id"]


@pytest.mark.asyncio
async def test_simulation_drains_event_pulse_before_resolution_deadline() -> None:
    engine = Engine(clock_fn=lambda: 0)
    engine.load_rules(load_rules_from_string("""
- id: doorbell
  when: event.doorbell.triggered == true
  alert:
    name: DoorbellPressed
    severity: info
    resolve_after: 5s
    annotations: {summary: Doorbell pressed}
"""))

    steps = await simulate_timeline(engine, [
        {"states": {"event.doorbell.triggered": True}},
        {},
        {"advance_ms": 5_000},
    ])

    assert [step["alerts"][0]["state"] for step in steps] == [
        "firing",
        "firing",
        "inactive",
    ]


@pytest.mark.asyncio
async def test_unknown_pulse_evidence_does_not_activate_alert() -> None:
    engine = Engine(clock_fn=lambda: 0)
    engine.load_rules(load_rules_from_string("""
- id: doorbell
  when: event.doorbell.triggered != true
  alert:
    name: DoorbellPressed
    severity: info
    resolve_after: 5s
    annotations: {summary: Doorbell pressed}
"""))

    steps = await simulate_timeline(engine, [{}])

    assert steps[0]["alerts"][0]["state"] == "inactive"
    assert steps[0]["alerts"][0]["evaluation_status"] == "grace"
    assert steps[0]["alert_transitions"] == []


@pytest.mark.asyncio
async def test_intent_retention_does_not_retain_alert() -> None:
    engine = Engine(clock_fn=lambda: 0)
    engine.load_rules(load_rules_from_string("""
- id: motion
  while: {binary_sensor.motion: "on"}
  hold:
    after:
      tiers: [{active_for: 0s, duration: 30s}]
      adjustments: []
      max: 30s
  intent: {light.room: {state: "on"}}
  alert:
    name: MotionDetected
    severity: info
    annotations: {summary: Motion detected}
"""))

    steps = await simulate_timeline(engine, [
        {"states": {"binary_sensor.motion.state": "on"}},
        {"states": {"binary_sensor.motion.state": "off"}},
    ])

    assert steps[1]["active_targets"] == ["light.room"]
    assert steps[1]["alerts"][0]["state"] == "inactive"


@pytest.mark.asyncio
async def test_selector_alert_preserves_firing_state_when_evidence_is_unavailable() -> None:
    engine = Engine(clock_fn=lambda: 0)
    engine.load_rules(load_rules_from_string("""
- id: selected-motion
  observe:
    select:
      mode: any
      entities:
        - {domain: binary_sensor, label: motion, state: "on"}
  alert:
    name: MotionDetected
    severity: warning
    annotations: {summary: Motion detected}
"""))
    memberships = [{
        "selector": {"domain": "binary_sensor", "label": "motion"},
        "targets": ["binary_sensor.hall_motion"],
    }]

    steps = await simulate_timeline(
        engine,
        [
            {"states": {"binary_sensor.hall_motion.state": "on"}},
            {"states": {"binary_sensor.hall_motion.state": "unavailable"}},
        ],
        selector_memberships=memberships,
    )

    assert steps[1]["alerts"][0]["state"] == "firing"
    assert steps[1]["alerts"][0]["evaluation_status"] == "grace"


@pytest.mark.asyncio
async def test_global_disable_resolves_firing_alert() -> None:
    engine = Engine(clock_fn=lambda: 0)
    engine.load_rules(load_rules_from_string("""
- id: leak
  while: {binary_sensor.water_leak: "on"}
  alert:
    name: WaterLeak
    severity: critical
    annotations: {summary: Water leak detected}
"""))

    steps = await simulate_timeline(engine, [
        {"states": {"binary_sensor.water_leak.state": "on"}},
        {"enabled": False},
    ])

    assert [step["alerts"][0]["state"] for step in steps] == ["firing", "inactive"]
    assert steps[1]["alert_transitions"][0]["to"] == "resolved"
    assert steps[1]["alert_transitions"][0]["reason"] == "evaluation_disabled"


@pytest.mark.asyncio
async def test_simulation_projects_durable_notification_retry_across_restart() -> None:
    engine = Engine(clock_fn=lambda: 0)
    engine.load_rules(load_rules_from_string("""
- id: freezer
  while: {binary_sensor.freezer_hot: {is: "on"}}
  alert:
    name: FreezerHigh
    severity: warning
    annotations: {summary: Freezer is too warm}
"""))
    policy = """
route: {id: root, receiver: household, group_wait: 0s, repeat_interval: never}
receivers:
  - {name: household, destinations: [{type: notify_entity, entity_id: notify.family}]}
"""

    steps = await simulate_timeline(
        engine,
        [
            {
                "states": {"binary_sensor.freezer_hot.state": "on"},
                "reject_notifications": True,
            },
            {"advance_ms": 1_000, "restart": True},
        ],
        alerting_policy=policy,
    )

    assert steps[0]["receiver_calls"][0]["result"] == "rejected"
    assert steps[1]["receiver_calls"][0]["obligation_id"] == "simulated-obligation-1"
    assert steps[1]["receiver_calls"][0]["result"] == "accepted"
    assert steps[1]["notification_obligations"][0]["attempt"] == 2
    assert steps[1]["checkpoint"] == "restart"


@pytest.mark.asyncio
async def test_simulation_models_acknowledgment_release_debounce() -> None:
    engine = Engine(clock_fn=lambda: 0)
    engine.load_rules(load_rules_from_string("""
- id: freezer
  while: {binary_sensor.freezer_hot: {is: "on"}}
  alert:
    name: FreezerHigh
    severity: warning
    annotations: {summary: Freezer is too warm}
"""))
    policy = """
route: {id: root, receiver: household, group_wait: 0s, repeat_interval: never}
receivers:
  - {name: household, destinations: [{type: persistent_notification}]}
"""

    steps = await simulate_timeline(
        engine,
        [
            {
                "states": {"binary_sensor.freezer_hot.state": "on"},
                "acknowledge": ["simulated-alert-1"],
            },
            {"revoke_acknowledgment": ["simulated-alert-1"]},
            {"advance_ms": 4_999},
            {"advance_ms": 1},
        ],
        alerting_policy=policy,
    )

    assert steps[0]["receiver_calls"] == []
    assert steps[2]["receiver_calls"] == []
    assert steps[3]["receiver_calls"][0]["result"] == "accepted"


@pytest.mark.asyncio
async def test_rule_pause_resolves_firing_alert_with_operational_reason() -> None:
    engine = Engine(clock_fn=lambda: 0)
    engine.load_rules(load_rules_from_string("""
- id: leak
  while: {binary_sensor.water_leak: "on"}
  alert:
    name: WaterLeak
    severity: critical
    annotations: {summary: Water leak detected}
"""))

    steps = await simulate_timeline(engine, [
        {"states": {"binary_sensor.water_leak.state": "on"}},
        {"pause_rule_ids": ["leak"]},
    ])

    assert steps[1]["alerts"][0]["state"] == "inactive"
    assert steps[1]["alert_transitions"][0]["reason"] == "evaluation_paused"


@pytest.mark.asyncio
async def test_rule_pause_closes_alert_even_when_evidence_is_unknown() -> None:
    engine = Engine(clock_fn=lambda: 0)
    engine.load_rules(load_rules_from_string("""
- id: freezer
  while: {sensor.freezer_temperature: {gt: -10}}
  alert:
    name: FreezerTemperatureHigh
    severity: critical
    annotations: {summary: Freezer is too warm}
"""))

    steps = await simulate_timeline(engine, [
        {"states": {"sensor.freezer_temperature.state": -5}},
        {"states": {"sensor.freezer_temperature.state": "unavailable"}},
        {"pause_rule_ids": ["freezer"]},
    ])

    assert steps[2]["alerts"][0]["state"] == "inactive"
    assert steps[2]["alert_transitions"][0]["reason"] == "evaluation_paused"


@pytest.mark.asyncio
async def test_alert_uses_complete_authored_rule_id() -> None:
    engine = Engine(clock_fn=lambda: 0)
    engine.load_rules(load_rules_from_string("""
- id: kitchen:leak
  while: {binary_sensor.water_leak: "on"}
  alert:
    name: WaterLeak
    severity: critical
    annotations: {summary: Water leak detected}
"""))

    steps = await simulate_timeline(
        engine, [{"states": {"binary_sensor.water_leak.state": "on"}}]
    )

    assert steps[0]["alerts"][0]["rule_id"] == "kitchen:leak"
    assert steps[0]["alerts"][0]["state"] == "firing"


@pytest.mark.asyncio
async def test_alert_inherits_snapshotted_dynamic_rule_duration() -> None:
    engine = Engine(clock_fn=lambda: 0)
    engine.load_rules(load_rules_from_string("""
- id: motion
  while: {binary_sensor.motion: "on"}
  for: {entity: input_number.alert_delay, unit: s, default: 5s}
  alert:
    name: MotionDetected
    severity: warning
    annotations: {summary: Motion detected}
"""))

    steps = await simulate_timeline(engine, [
        {"states": {
            "binary_sensor.motion.state": "on",
            "input_number.alert_delay.state": 10,
        }},
        {"advance_ms": 9_999, "states": {"input_number.alert_delay.state": 1}},
        {"advance_ms": 1},
    ])

    assert [step["alerts"][0]["state"] for step in steps] == [
        "pending",
        "pending",
        "firing",
    ]


@pytest.mark.asyncio
async def test_simulation_effects_dispatch_once_per_activation_across_restart() -> None:
    engine = Engine(clock_fn=lambda: 0)
    engine.load_rules(load_rules_from_string("""
- id: announce
  while: {binary_sensor.ready: on}
  effect: {service: notify.phone, data: {message: ready}}
"""))

    steps = await simulate_timeline(engine, [
        {"states": {"binary_sensor.ready.state": "on"}},
        {},
        {"restart": True},
        {"states": {"binary_sensor.ready.state": "off"}},
        {"states": {"binary_sensor.ready.state": "on"}},
    ])

    assert [len(step["effects"]) for step in steps] == [1, 0, 0, 0, 1]
    assert steps[0]["effects"][0]["service"] == "phone"
    assert steps[4]["effects"][0]["data"] == {"message": "ready"}


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


@pytest.mark.asyncio
async def test_simulation_derives_semantic_edge_pulse_and_keeps_metadata_on_restart() -> None:
    from intentional.yaml_loader import load_rules_from_string

    engine = Engine(clock_fn=lambda: 0)
    engine.load_rules(load_rules_from_string("""
- id: motion-edge
  while: {motion: {detected: {area: office, changed: true}}}
  intent: {light.office: {state: on, ttl: 5s}}
"""))
    steps = await simulate_timeline(
        engine,
        [
            {"states": {"binary_sensor.motion.state": "off"}},
            {"states": {"binary_sensor.motion.state": "on"}},
            {"restart": True},
        ],
        semantic_metadata=[{
            "entity_id": "binary_sensor.motion", "area": "office",
            "original_device_class": "motion",
        }],
    )

    assert steps[0]["active_targets"] == []
    assert steps[1]["active_targets"] == ["light.office"]
    assert steps[2]["active_targets"] == ["light.office"]


def test_schema_describes_semantic_simulation_and_replay_inputs() -> None:
    from intentional.schema import dsl_schema

    schema = dsl_schema()

    assert schema["simulation_endpoints"] == [
        "/api/intentional/simulate",
        "/api/intentional/replay",
    ]
    assert schema["semantic_observations"]["authored_filters"] == [
        "area", "entity", "device", "exclude",
    ]
    assert schema["semantic_observations"]["binary_states"] == {
        "motion": ["detected", "clear"],
        "occupancy": ["occupied", "clear"],
        "door": ["open", "closed"],
        "window": ["open", "closed"],
        "moisture": ["wet", "dry"],
    }
    assert "purpose" in schema["simulation_selector_membership"]["selector_filters"]
    assert schema["simulation_semantic_metadata"]["required_fields"] == ["entity_id"]
    dynamic = schema["dynamic_hold_after"]
    assert dynamic["aliases"] == ["hold.after", "hold.after_when_stops"]
    assert dynamic["required_exact_fields"] == ["tiers", "adjustments", "max"]
    assert dynamic["tiers"]["max_items"] == 64
    assert dynamic["adjustments"]["selection"].startswith("first matching")


def test_schema_completely_describes_new_authoring_and_safety_contracts() -> None:
    from intentional.schema import dsl_schema

    schema = dsl_schema()
    assert schema["retention_profiles"]["allowed_fields"] == [
        "while", "until", "after", "after_when_stops",
    ]
    assert schema["retention_profiles"]["overlay"]["fingerprint_uses_expanded_rule"] is True
    assert schema["time_windows"]["value_required_exact_fields"] == ["from", "until"]
    assert schema["time_windows"]["observation_reference"]["required_exactly_one_operator"] == ["in", "not_in"]
    assert schema["time_windows"]["adjustment_reference"]["required_exact_fields"] == ["window", "add"]
    assert schema["hysteresis"]["operators"] == ["gt", "gte", "lt", "lte"]
    assert schema["hysteresis"]["persistence"].startswith("latch and dwell survive restart")
    assert schema["field_withdrawal"]["adopt_semantics"].startswith("restore")
    assert schema["shadow_target_policy"]["shadow"]["calls_home_assistant_services"] is False


def test_schema_describes_alert_lifecycle_contract() -> None:
    from intentional.schema import dsl_schema

    schema = dsl_schema()

    assert "alert" in schema["top_level_rule_fields"]
    assert schema["alerts"] == {
        "required_fields": ["name", "severity", "annotations.summary"],
        "severity": ["info", "warning", "critical"],
        "states": ["inactive", "pending", "firing"],
        "evaluation_status": ["current", "grace", "stale"],
        "pulse_requires": "resolve_after",
        "api": "/api/intentional/alerts",
        "policy_api": "/api/intentional/alerting/policy",
        "simulate_api": "/api/intentional/alerting/simulate",
        "matcher_operators": ["=", "!=", "=~", "!~"],
    }
    assert schema["semantic_observations"]["power"]["effective_device_class"] == "power"
    assert schema["capabilities"]["preview"]["horizons_ms"]["max_items"] == 32
    assert schema["capabilities"]["diagnostics"]["runtime_event_retention"]["max_items"] == 200
    assert schema["capabilities"]["ha_migration"]["read_only_source"] is True
    assert schema["capabilities"]["rollback"]["history_limit"] == 25


def test_bundled_machine_schema_matches_pure_engine_schema() -> None:
    import importlib.util
    from pathlib import Path

    from intentional.schema import dsl_schema

    path = Path(__file__).parents[1] / "custom_components/intentional/_engine/schema.py"
    spec = importlib.util.spec_from_file_location("bundled_schema", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.dsl_schema() == dsl_schema()


def test_simulation_rejects_duplicate_semantic_metadata_entities() -> None:
    metadata = [
        {"entity_id": "binary_sensor.motion", "device_class": "motion"},
        {"entity_id": "binary_sensor.motion", "device_class": "motion"},
    ]

    with pytest.raises(ValueError, match="duplicates an entity ID"):
        validate_simulation_input([], {}, semantic_metadata=metadata)
def test_simulation_validates_time_of_day() -> None:
    validate_simulation_input([{"time_of_day": "22:00"}], {})
    with pytest.raises(ValueError, match="strict HH:MM"):
        validate_simulation_input([{"time_of_day": "2:00"}], {})


@pytest.mark.asyncio
async def test_simulation_freezes_dynamic_hold_and_preserves_it_on_restart() -> None:
    engine = Engine(clock_fn=lambda: 0)
    engine.load_rules(load_rules_from_string("""
- id: adaptive
  while: {binary_sensor.motion: on}
  hold:
    after:
      tiers: [{active_for: 0s, duration: 30s}]
      adjustments: [{from: "22:00", until: "06:00", add: 5m}]
      max: 10m
  intent:
    light.room: {state: on}
"""))
    steps = await simulate_timeline(engine, [
        {"time_of_day": "22:00", "states": {"binary_sensor.motion.state": "on"}},
        {"advance_ms": 1_000, "states": {"binary_sensor.motion.state": "off"}},
        {"advance_ms": 1_000, "time_of_day": "12:00", "restart": True},
    ])
    frozen = steps[1]["active_rules"][0]["hold_after"]
    assert frozen["duration_ms"] == 330_000
    assert steps[2]["active_rules"][0]["hold_after"]["expires_at_ms"] == frozen["expires_at_ms"]


@pytest.mark.asyncio
async def test_simulation_restart_applies_current_step_time_of_day() -> None:
    engine = Engine(clock_fn=lambda: 0)
    engine.load_rules([Rule(id="noon", when='time_of_day == "12:00"',
                            target="light.room", set={"state": "on"})])

    steps = await simulate_timeline(engine, [{"restart": True, "time_of_day": "12:00"}])

    assert steps[0]["active_rules"][0]["rule_id"] == "noon"
