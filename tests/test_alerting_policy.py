from __future__ import annotations

from datetime import UTC, datetime

import pytest

from intentional.alerting.policy import (
    AlertingPolicyRepository,
    receiver_destinations,
    simulate_alerting_policy,
)


class _PolicyStore:
    def __init__(self) -> None:
        self.data = None
        self.save_count = 0

    async def async_load(self):
        return self.data

    async def async_save(self, data) -> None:
        self.data = data
        self.save_count += 1


class _FailingPolicyStore(_PolicyStore):
    async def async_load(self):
        raise OSError("storage unavailable")


@pytest.mark.asyncio
async def test_policy_store_v1_shape_is_rewritten_with_current_schema() -> None:
    store = _PolicyStore()
    store.data = {
        "contents": """
route: {id: root, receiver: household}
receivers:
  - {name: household, destinations: [{type: persistent_notification}]}
""",
        "generation": "legacy",
        "history": [],
    }
    repository = AlertingPolicyRepository(store)

    await repository.async_load()

    assert repository.health()["status"] == "ok"
    assert store.data["version"] == 2


def test_policy_routes_fallback_and_continued_critical_alerts() -> None:
    policy = """
route:
  id: root
  receiver: household
  group_by: [alertname, area]
  group_wait: 30s
  group_interval: 5m
  repeat_interval: 4h
  routes:
    - id: critical
      matchers: ['severity="critical"']
      receiver: urgent
      group_wait: 0s
      continue: true
    - id: security
      matchers: ['category="security"']
      receiver: security
receivers:
  - {name: household, destinations: [{type: persistent_notification}]}
  - {name: urgent, destinations: [{type: notify_entity, entity_id: notify.alice}]}
  - {name: security, destinations: [{type: notify_entity, entity_id: notify.security}]}
"""

    result = simulate_alerting_policy(policy, [
        {
            "alertname": "FreezerHigh",
            "severity": "warning",
            "area": "kitchen",
            "category": "appliance",
        },
        {
            "alertname": "SmokeDetected",
            "severity": "critical",
            "area": "hall",
            "category": "security",
        },
    ])
    routing_result = [
        {
            **{key: value for key, value in item.items() if key != "fanout"},
            "routes": [
                {
                    key: value
                    for key, value in route.items()
                    if key not in {"group_key", "destinations"}
                }
                for route in item["routes"]
            ],
        }
        for item in result
    ]

    assert routing_result == [
        {
            "labels": {
                "alertname": "FreezerHigh",
                "severity": "warning",
                "area": "kitchen",
                "category": "appliance",
            },
            "routes": [{
                "route_id": "root",
                "receiver": "household",
                "group_by": ["alertname", "area"],
                "group_wait_ms": 30_000,
                "group_interval_ms": 300_000,
                "repeat_interval_ms": 14_400_000,
                "send_resolved": True,
            }],
        },
        {
            "labels": {
                "alertname": "SmokeDetected",
                "severity": "critical",
                "area": "hall",
                "category": "security",
            },
            "routes": [
                {
                    "route_id": "critical",
                    "receiver": "urgent",
                    "group_by": ["alertname", "area"],
                    "group_wait_ms": 0,
                    "group_interval_ms": 300_000,
                    "repeat_interval_ms": 14_400_000,
                    "send_resolved": True,
                },
                {
                    "route_id": "security",
                    "receiver": "security",
                    "group_by": ["alertname", "area"],
                    "group_wait_ms": 30_000,
                    "group_interval_ms": 300_000,
                    "repeat_interval_ms": 14_400_000,
                    "send_resolved": True,
                },
            ],
        },
    ]


def test_policy_supports_shared_safe_matcher_operators() -> None:
    policy = """
route:
  id: root
  receiver: fallback
  routes:
    - id: matched
      receiver: selected
      matchers:
        - 'severity="critical"'
        - 'area!="garage"'
        - 'alertname=~"^Smoke.*$"'
        - 'category!~"^test.*$"'
receivers:
  - {name: fallback, destinations: [{type: persistent_notification}]}
  - {name: selected, destinations: [{type: persistent_notification}]}
"""

    result = simulate_alerting_policy(policy, [{
        "alertname": "SmokeDetected",
        "severity": "critical",
        "area": "hall",
        "category": "security",
    }])

    assert result[0]["routes"][0]["receiver"] == "selected"

    with pytest.raises(ValueError, match="unsafe regex"):
        simulate_alerting_policy(
            policy.replace('^Smoke.*$', '^(Smoke+)+$'),
            [{"alertname": "Smoke", "severity": "critical", "area": "hall", "category": "security"}],
        )


def test_policy_regex_matching_does_not_use_the_backtracking_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import intentional.alerting.policy as policy_module

    monkeypatch.setattr(
        policy_module.re,
        "compile",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("unsafe regex engine used")
        ),
    )
    policy = """
route:
  id: root
  receiver: fallback
  routes:
    - id: smoke
      receiver: selected
      matchers: ['alertname=~"^Smoke.*$"']
receivers:
  - {name: fallback, destinations: [{type: persistent_notification}]}
  - {name: selected, destinations: [{type: persistent_notification}]}
"""

    result = simulate_alerting_policy(policy, [{"alertname": "SmokeDetected"}])

    assert result[0]["routes"][0]["receiver"] == "selected"


def test_policy_groups_alerts_after_routing_by_route_receiver_and_selected_labels() -> None:
    policy = """
route:
  id: root
  receiver: household
  group_by: [alertname, area]
receivers:
  - {name: household, destinations: [{type: persistent_notification}]}
"""

    result = simulate_alerting_policy(policy, [
        {"alertname": "Smoke", "area": "hall", "source": "upstairs"},
        {"alertname": "Smoke", "area": "hall", "source": "downstairs"},
        {"alertname": "Smoke", "area": "kitchen", "source": "downstairs"},
    ])

    first = result[0]["routes"][0]["group_key"]
    second = result[1]["routes"][0]["group_key"]
    third = result[2]["routes"][0]["group_key"]
    assert first == second
    assert first != third
    assert first["route_id"] == "root"
    assert first["labels"] == {"alertname": "Smoke", "area": "hall"}
    assert first["receiver_revision"]


def test_policy_explains_active_and_mute_interval_suppression() -> None:
    policy = """
route:
  id: root
  receiver: fallback
  routes:
    - id: urgent
      receiver: selected
      matchers: ['severity="critical"']
      active_intervals: [occupied-hours]
      mute_intervals: [overnight]
receivers:
  - {name: fallback, destinations: [{type: persistent_notification}]}
  - {name: selected, destinations: [{type: persistent_notification}]}
active_intervals:
  - name: occupied-hours
    timezone: Europe/Amsterdam
    weekdays: [mon, tue, wed, thu, fri]
    times: [{start: "08:00", end: "23:59"}]
mute_intervals:
  - name: overnight
    timezone: Europe/Amsterdam
    weekdays: [mon, tue, wed, thu, fri, sat, sun]
    times: [{start: "22:00", end: "07:00"}]
"""
    alert = {"alertname": "Smoke", "severity": "critical", "area": "hall"}

    active = simulate_alerting_policy(
        policy, [alert], at=datetime(2026, 7, 13, 15, tzinfo=UTC)
    )
    muted = simulate_alerting_policy(
        policy, [alert], at=datetime(2026, 7, 13, 21, tzinfo=UTC)
    )
    inactive = simulate_alerting_policy(
        policy, [alert], at=datetime(2026, 7, 18, 10, tzinfo=UTC)
    )

    assert "suppression" not in active[0]["routes"][0]
    assert muted[0]["routes"][0]["suppression"] == {
        "reason": "mute_interval",
        "interval": "overnight",
    }
    assert inactive[0]["routes"][0]["suppression"] == {
        "reason": "inactive_interval",
        "intervals": ["occupied-hours"],
    }


def test_policy_explains_inhibition_by_a_matching_source_alert() -> None:
    policy = """
route:
  id: root
  receiver: household
receivers:
  - {name: household, destinations: [{type: persistent_notification}]}
inhibit_rules:
  - source_matchers: ['alertname="GatewayDown"']
    target_matchers: ['category="connectivity"']
    equal: [area]
"""

    result = simulate_alerting_policy(policy, [
        {"alertname": "GatewayDown", "category": "infrastructure", "area": "hall"},
        {"alertname": "SensorOffline", "category": "connectivity", "area": "hall"},
        {"alertname": "HubOffline", "category": "connectivity", "area": "kitchen"},
    ])

    assert "suppression" not in result[0]["routes"][0]
    assert result[1]["routes"][0]["suppression"] == {
        "reason": "inhibition",
        "rule": 0,
        "source_alert": 0,
        "equal": {"area": "hall"},
    }
    assert "suppression" not in result[2]["routes"][0]


async def test_policy_repository_previews_and_publishes_with_generation_control() -> None:
    store = _PolicyStore()
    repository = AlertingPolicyRepository(store)
    await repository.async_load()
    initial_generation = repository.generation
    policy = """
route: {id: root, receiver: household}
receivers:
  - {name: household, destinations: [{type: persistent_notification}]}
"""

    preview = repository.preview(
        policy, [{"alertname": "Smoke", "area": "hall"}]
    )

    assert preview["current_generation"] == initial_generation
    assert preview["candidate_generation"] != initial_generation
    assert preview["alerts"][0]["routes"][0]["receiver"] == "household"
    assert repository.generation == initial_generation
    assert store.save_count == 0

    published = await repository.async_publish(
        policy, expected_generation=initial_generation
    )

    assert published == {"generation": preview["candidate_generation"]}
    assert repository.generation == preview["candidate_generation"]
    assert store.save_count == 1
    assert await repository.async_publish(
        policy, expected_generation=initial_generation
    ) == {"error": "generation_mismatch"}
    assert store.save_count == 1


async def test_policy_repository_fails_closed_when_storage_cannot_load() -> None:
    repository = AlertingPolicyRepository(_FailingPolicyStore())

    await repository.async_load()

    assert repository.contents is None
    assert repository.health() == {
        "status": "degraded",
        "current_error": "policy_store_load_failed",
    }


async def test_policy_repository_records_history_and_rolls_back_atomically() -> None:
    store = _PolicyStore()
    repository = AlertingPolicyRepository(store)
    await repository.async_load()
    first = """
route: {id: root, receiver: first}
receivers:
  - {name: first, destinations: [{type: persistent_notification}]}
"""
    second = """
route: {id: root, receiver: second}
receivers:
  - {name: second, destinations: [{type: persistent_notification}]}
"""
    initial_generation = repository.generation
    first_result = await repository.async_publish(
        first, expected_generation=initial_generation
    )
    second_result = await repository.async_publish(
        second, expected_generation=first_result["generation"]
    )

    assert repository.list_history() == [{"generation": first_result["generation"]}]
    assert repository.read_history(first_result["generation"]) == {
        "generation": first_result["generation"],
        "contents": first,
    }
    assert await repository.async_rollback(
        first_result["generation"], expected_generation=initial_generation
    ) == {"error": "generation_mismatch"}

    rolled_back = await repository.async_rollback(
        first_result["generation"],
        expected_generation=second_result["generation"],
    )

    assert rolled_back == {
        "generation": first_result["generation"],
        "restored_generation": first_result["generation"],
    }
    assert repository.contents == first
    assert repository.list_history()[0] == {
        "generation": second_result["generation"]
    }
    assert store.save_count == 3


async def test_policy_publication_requires_confirmation_for_large_fanout_spike() -> None:
    store = _PolicyStore()
    repository = AlertingPolicyRepository(store)
    await repository.async_load()
    initial = """
route: {id: root, receiver: one}
receivers:
  - {name: one, destinations: [{type: persistent_notification}]}
"""
    initial_result = await repository.async_publish(
        initial, expected_generation=repository.generation
    )
    destinations = ", ".join(
        f"{{type: notify_entity, entity_id: notify.user{index}}}" for index in range(5)
    )
    expanded = f"""
route: {{id: root, receiver: many}}
receivers:
  - name: many
    destinations: [{destinations}]
"""
    alerts = [{"alertname": "Smoke", "area": "hall"}]

    blocked = await repository.async_publish(
        expanded,
        expected_generation=initial_result["generation"],
        alerts=alerts,
    )

    assert blocked["error"] == "confirmation_required"
    assert blocked["preview"]["fanout"] == 5
    published = await repository.async_publish(
        expanded,
        expected_generation=initial_result["generation"],
        alerts=alerts,
        confirm_spike=True,
    )
    assert "generation" in published


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("repeat_intervall", "4h", "unsupported route fields"),
        ("group_wait", "500ms", "group_wait"),
        ("group_interval", "59s", "group_interval"),
        ("repeat_interval", -1, "repeat_interval"),
    ],
)
def test_policy_rejects_unknown_fields_and_unsafe_route_timings(
    field: str, value: object, message: str
) -> None:
    policy = f"""
route:
  id: root
  receiver: household
  {field}: {value}
receivers:
  - {{name: household, destinations: [{{type: persistent_notification}}]}}
"""

    with pytest.raises(ValueError, match=message):
        simulate_alerting_policy(policy, [])


def test_policy_inherits_send_resolved() -> None:
    policy = """
route:
  id: root
  receiver: household
  send_resolved: false
  routes:
    - id: child
      matchers: ['severity="critical"']
receivers:
  - {name: household, destinations: [{type: persistent_notification}]}
"""

    result = simulate_alerting_policy(policy, [{"severity": "critical"}])

    assert result[0]["routes"][0]["send_resolved"] is False


def test_policy_uses_configured_default_timezone_for_intervals() -> None:
    policy = """
route:
  id: root
  receiver: household
  active_intervals: [morning]
receivers:
  - {name: household, destinations: [{type: persistent_notification}]}
active_intervals:
  - name: morning
    weekdays: [mon]
    times: [{start: "08:00", end: "09:00"}]
"""

    result = simulate_alerting_policy(
        policy,
        [{"alertname": "WakeUp"}],
        at=datetime(2026, 7, 13, 6, 30, tzinfo=UTC),
        default_timezone="Europe/Amsterdam",
    )

    assert "suppression" not in result[0]["routes"][0]


def test_policy_inherits_continue_through_nested_routes() -> None:
    policy = """
route:
  id: root
  receiver: fallback
  continue: true
  routes:
    - id: parent
      matchers: ['severity="critical"']
      routes:
        - id: security
          receiver: security
          matchers: ['category="security"']
        - id: hall
          receiver: hall
          matchers: ['area="hall"']
receivers:
  - {name: fallback, destinations: [{type: persistent_notification}]}
  - {name: security, destinations: [{type: notify_entity, entity_id: notify.security}]}
  - {name: hall, destinations: [{type: notify_entity, entity_id: notify.hall}]}
"""

    result = simulate_alerting_policy(policy, [{
        "severity": "critical",
        "category": "security",
        "area": "hall",
    }])

    assert [route["route_id"] for route in result[0]["routes"]] == ["security", "hall"]


def test_policy_rejects_duplicate_route_ids() -> None:
    policy = """
route:
  id: root
  receiver: fallback
  routes:
    - {id: duplicate, receiver: first}
    - {id: duplicate, receiver: second}
receivers:
  - {name: fallback, destinations: [{type: persistent_notification}]}
  - {name: first, destinations: [{type: notify_entity, entity_id: notify.first}]}
  - {name: second, destinations: [{type: notify_entity, entity_id: notify.second}]}
"""

    with pytest.raises(ValueError, match="route IDs must be unique"):
        simulate_alerting_policy(policy, [])


def test_policy_deduplicates_receiver_destinations_by_first_route() -> None:
    policy = """
route:
  id: root
  receiver: household
  routes:
    - id: first
      matchers: ['severity="critical"']
      continue: true
    - id: second
      matchers: ['category="security"']
receivers:
  - {name: household, destinations: [{type: notify_entity, entity_id: notify.family}]}
"""

    result = simulate_alerting_policy(policy, [{
        "severity": "critical",
        "category": "security",
    }])

    assert [route["route_id"] for route in result[0]["routes"]] == ["first", "second"]
    assert result[0]["fanout"] == [{
        "route_id": "first",
        "receiver": "household",
        "destination": {"type": "notify_entity", "entity_id": "notify.family"},
    }]


def test_policy_enforces_authored_matcher_and_destination_caps() -> None:
    matchers = "\n".join(f"    - 'label{index}=\"value\"'" for index in range(17))
    destinations = ", ".join(
        f"{{type: notify_entity, entity_id: notify.user{index}}}" for index in range(9)
    )
    too_many_matchers = f"""
route:
  id: root
  receiver: household
  matchers:
{matchers}
receivers:
  - {{name: household, destinations: [{{type: persistent_notification}}]}}
"""
    too_many_destinations = f"""
route: {{id: root, receiver: household}}
receivers:
  - name: household
    destinations: [{destinations}]
"""

    with pytest.raises(ValueError, match="at most 16 matchers"):
        simulate_alerting_policy(too_many_matchers, [])
    with pytest.raises(ValueError, match="at most 8 destinations"):
        simulate_alerting_policy(too_many_destinations, [])


def test_policy_warns_when_duplicate_destination_fanout_is_allowed() -> None:
    policy = """
route:
  id: root
  receiver: household
  routes:
    - id: first
      matchers: ['severity="critical"']
      continue: true
    - id: second
      matchers: ['category="security"']
receivers:
  - name: household
    destinations:
      - type: notify_entity
        entity_id: notify.family
        allow_duplicate: true
"""

    result = simulate_alerting_policy(policy, [{
        "severity": "critical",
        "category": "security",
    }])

    assert len(result[0]["fanout"]) == 2
    assert result[0]["warnings"] == [{
        "code": "duplicate_fanout",
        "route_id": "second",
        "receiver": "household",
    }]


def test_policy_exposes_only_validated_named_receiver_destinations() -> None:
    policy = """
route: {id: root, receiver: household}
receivers:
  - {name: household, destinations: [{type: notify_entity, entity_id: notify.family}]}
"""

    assert receiver_destinations(policy, "household") == [
        {"type": "notify_entity", "entity_id": "notify.family"}
    ]
    with pytest.raises(ValueError, match="Receiver not found"):
        receiver_destinations(policy, "missing")


def test_policy_supports_no_repeats_and_rejects_unsafe_destinations() -> None:
    policy = """
route: {id: root, receiver: household, repeat_interval: never}
receivers:
  - {name: household, destinations: [{type: notify_entity, entity_id: notify.family}]}
"""
    result = simulate_alerting_policy(
        policy,
        [{"alertname": "FreezerHigh"}],
        at=datetime(2026, 7, 13, 12, tzinfo=UTC),
    )
    assert result[0]["routes"][0]["repeat_interval_ms"] is None

    with pytest.raises(ValueError, match="unsupported fields"):
        simulate_alerting_policy(
            policy.replace(
                "entity_id: notify.family",
                "entity_id: notify.family, data: {secret: unsafe}",
            ),
            [{"alertname": "FreezerHigh"}],
        )


def test_policy_rejects_static_inhibition_self_reference_and_cycle() -> None:
    base = """
route: {id: root, receiver: household}
receivers:
  - {name: household, destinations: [{type: persistent_notification}]}
inhibit_rules:
{rules}
"""
    self_reference = """
  - source_matchers: ['severity="critical"']
    target_matchers: ['severity="critical"']
    equal: []
"""
    with pytest.raises(ValueError, match="cannot inhibit itself"):
        simulate_alerting_policy(
            base.replace("{rules}", self_reference), [{"severity": "critical"}]
        )

    cycle = """
  - source_matchers: ['service="power"']
    target_matchers: ['service="network"']
    equal: []
  - source_matchers: ['service="network"']
    target_matchers: ['service="power"']
    equal: []
"""
    with pytest.raises(ValueError, match="cycle"):
        simulate_alerting_policy(
            base.replace("{rules}", cycle), [{"service": "power"}]
        )
