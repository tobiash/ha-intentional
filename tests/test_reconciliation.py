"""Interface-level tests for the Reconciliation module.

These tests exercise Reconciliation.on_state_delta and Reconciliation.apply/tick
through the module's interface — a fake adapter and a real engine, no HA required.
They assert on returned events, not internal state. This is the test surface
ADR-0001 creates: the bug-prone decide+apply+promote policy is now reachable
without the async HA harness.
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from typing import Any

import pytest

from intentional.engine import Engine
from intentional.reconciliation import Reconciliation, ReconciliationEvent
from intentional.simulation import simulate_timeline, validate_preview_horizons
from intentional.target_policy import TargetPolicy
from intentional.yaml_loader import Rule


class _FakeAdapter:
    """Minimal HAAdapter fake: records service calls, serves canned state."""

    def __init__(self) -> None:
        self._states: dict[str, Any] = {}
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self._fail_next: bool = False
        self._fail_services: set[str] = set()

    def get_state(self, entity_id: str) -> Any:
        return self._states.get(entity_id)

    async def async_call(
        self, domain: str, service: str, data: dict[str, Any], *, context: Any
    ) -> None:
        if self._fail_next or service in self._fail_services:
            self._fail_next = False
            raise ValueError("service failed")
        self.calls.append((domain, service, data))

    def new_context(self) -> Any:
        return SimpleNamespace(id="test-ctx", parent_id=None, user_id=None)

    def set_state(
        self, entity_id: str, state: str, attributes: dict | None = None, context: Any = None
    ) -> None:
        self._states[entity_id] = SimpleNamespace(
            entity_id=entity_id,
            state=state,
            attributes=attributes or {},
            context=context,
        )


class _NoContextTracker:
    """ContextTracker that never claims ownership."""

    def owns_state(self, state: Any) -> bool:
        return False


class _OwningContextTracker:
    """ContextTracker that claims ownership of a given context id."""

    def __init__(self, owned_id: str) -> None:
        self._owned_id = owned_id

    def owns_state(self, state: Any) -> bool:
        ctx = getattr(state, "context", None)
        return getattr(ctx, "id", None) == self._owned_id


def _make_engine_with_light_rule(*, brightness_pct: int = 60) -> Engine:
    engine = Engine(clock_fn=lambda: 1000)
    engine.load_rules(
        [
            Rule(
                id="desk-on",
                when="input_boolean.work == 'on'",
                target="light.desk",
                set={"state": "on", "brightness_pct": brightness_pct},
            )
        ]
    )
    engine.update_state("input_boolean.work", "on")
    engine.evaluate_all()
    return engine


def _events_of(events: list, kind: str) -> list:
    return [e for e in events if e.kind == kind]


@pytest.mark.asyncio
async def test_observe_only_policy_denies_service_plan_before_dispatch() -> None:
    engine = _make_engine_with_light_rule()
    rule = Rule(
        id="observe",
        when="true",
        target="light.desk",
        set={"state": "on"},
    )
    engine.load_rules([rule], target_policies={"light.desk": TargetPolicy(ownership="observe_only")})
    engine.evaluate_all()
    adapter = _FakeAdapter()
    reconciler = Reconciliation(
        drift_override_ttl_ms=1, drift_confirmation_ms=0, service_failure_backoff_ms=10
    )

    events = await reconciler.apply(engine, adapter, 0)

    assert adapter.calls == []
    assert _events_of(events, "service_denied_target_policy")[0].details["code"] == "observe_only"

    repeated = await reconciler.apply(engine, adapter, 1)
    assert _events_of(repeated, "service_denied_target_policy") == []

    engine.load_rules([rule], target_policies={"light.desk": TargetPolicy(ownership="managed")})
    engine.evaluate_all()
    recovered = await reconciler.apply(engine, adapter, 2)
    assert len(_events_of(recovered, "service_target_policy_recovered")) == 1


@pytest.mark.asyncio
async def test_policy_denies_fields_and_states_without_user_authority() -> None:
    policy = TargetPolicy(
        allowed_fields=frozenset({"state"}), user_authority_states=frozenset({"unlocked"})
    )
    engine = Engine(clock_fn=lambda: 0)
    engine.load_rules(
        [
            Rule(
                id="unlock",
                when="true",
                target="lock.front",
                set={"state": "unlocked", "code": "1234"},
            )
        ]
    )
    engine.load_rules(engine.loaded_rules(), target_policies={"lock.front": policy})
    engine.evaluate_all()
    adapter = _FakeAdapter()
    reconciler = Reconciliation(
        drift_override_ttl_ms=1, drift_confirmation_ms=0, service_failure_backoff_ms=10
    )

    events = await reconciler.apply(engine, adapter, 0)

    assert adapter.calls == []
    assert (
        _events_of(events, "service_denied_target_policy")[0].details["code"] == "field_not_allowed"
    )


@pytest.mark.asyncio
async def test_unavailable_policy_and_retry_ceiling_suppress_dispatch() -> None:
    policy = TargetPolicy(unavailable="skip", max_retries=0)
    engine = Engine(clock_fn=lambda: 0)
    engine.load_rules(
        [
            Rule(
                id="light",
                when="true",
                target="light.desk",
                set={"state": "on"},
            )
        ]
    )
    engine.load_rules(engine.loaded_rules(), target_policies={"light.desk": policy})
    engine.evaluate_all()
    adapter = _FakeAdapter()
    reconciler = Reconciliation(
        drift_override_ttl_ms=1, drift_confirmation_ms=0, service_failure_backoff_ms=10
    )

    events = await reconciler.apply(engine, adapter, 0)
    assert (
        _events_of(events, "service_denied_target_policy")[0].details["code"]
        == "target_unavailable"
    )
    adapter.set_state("light.desk", "off")
    adapter._fail_next = True
    await reconciler.apply(engine, adapter, 1)
    events = await reconciler.apply(engine, adapter, 11)
    assert (
        _events_of(events, "service_denied_target_policy")[0].details["code"]
        == "max_retries_exhausted"
    )


@pytest.mark.asyncio
async def test_unavailable_target_skips_dispatch_by_default() -> None:
    engine = _make_engine_with_light_rule()
    adapter = _FakeAdapter()
    adapter.set_state("light.desk", "unavailable")
    reconciler = Reconciliation(
        drift_override_ttl_ms=1, drift_confirmation_ms=0, service_failure_backoff_ms=10
    )

    events = await reconciler.apply(engine, adapter, 0)

    assert adapter.calls == []
    assert (
        _events_of(events, "service_denied_target_policy")[0].details["code"]
        == "target_unavailable"
    )


@pytest.mark.asyncio
async def test_unavailable_target_can_explicitly_allow_dispatch() -> None:
    engine = _make_engine_with_light_rule()
    engine.load_rules(
        engine.loaded_rules(),
        target_policies={"light.desk": TargetPolicy(unavailable="allow")},
    )
    engine.evaluate_all()
    adapter = _FakeAdapter()
    adapter.set_state("light.desk", "unavailable")
    reconciler = Reconciliation(
        drift_override_ttl_ms=1, drift_confirmation_ms=0, service_failure_backoff_ms=10
    )

    await reconciler.apply(engine, adapter, 0)

    assert adapter.calls[-1][1] == "turn_on"


@pytest.mark.asyncio
async def test_unavailable_target_defers_withdrawal_until_recovery() -> None:
    engine = _make_engine_with_light_rule()
    adapter = _FakeAdapter()
    adapter.set_state("light.desk", "on", {"brightness": 153})
    reconciler = Reconciliation(
        drift_override_ttl_ms=1, drift_confirmation_ms=0, service_failure_backoff_ms=10
    )
    await reconciler.apply(engine, adapter, 0)
    engine.load_rules([])
    engine.evaluate_all()
    adapter.calls.clear()
    adapter.set_state("light.desk", "unavailable")

    unavailable = await reconciler.apply(engine, adapter, 1)

    assert adapter.calls == []
    assert (
        _events_of(unavailable, "service_denied_target_policy")[0].details["code"]
        == "target_unavailable"
    )

    adapter.set_state("light.desk", "on", {"brightness": 153})
    recovered = await reconciler.apply(engine, adapter, 2)

    assert adapter.calls[-1][1] == "turn_off"
    assert _events_of(recovered, "service_applied")


@pytest.mark.asyncio
async def test_opportunistic_ownership_survives_restart_without_withdrawal() -> None:
    engine = Engine(clock_fn=lambda: 0)
    engine.load_rules(
        [
            Rule(
                id="opportunity",
                when="true",
                target="light.desk",
                set={"state": "on"},
            )
        ]
    )
    engine.load_rules(
        engine.loaded_rules(),
        target_policies={"light.desk": TargetPolicy(ownership="opportunistic")},
    )
    engine.evaluate_all()
    adapter = _FakeAdapter()
    adapter.set_state("light.desk", "off")
    reconciler = Reconciliation(
        drift_override_ttl_ms=1, drift_confirmation_ms=0, service_failure_backoff_ms=10
    )
    await reconciler.apply(engine, adapter, 0)
    records = reconciler.export_pending_withdraws(engine)

    restored = Reconciliation(
        drift_override_ttl_ms=1, drift_confirmation_ms=0, service_failure_backoff_ms=10
    )
    restored.restore_pending_withdraws({"pending_withdraws": records})
    engine.load_rules([])
    adapter.calls.clear()
    events = await restored.apply(engine, adapter, 1)

    assert adapter.calls == []
    assert (
        _events_of(events, "withdraw_skipped_target_policy")[0].details["ownership"]
        == "opportunistic"
    )


@pytest.mark.asyncio
async def test_document_policy_applies_to_selector_generated_target_and_withdrawal() -> None:
    from intentional.yaml_loader import load_rules_from_string

    selected = ["light.dynamic"]
    rules = load_rules_from_string("""
targets:
  light.dynamic:
    ownership: opportunistic
rules:
  - id: dynamic
    while: {input_boolean.ready: on}
    intent:
      select:
        - domain: light
          state: on
""")
    engine = Engine(clock_fn=lambda: 0, selector_resolver=lambda _selector: list(selected))
    engine.load_rules(rules)
    engine.update_state("input_boolean.ready", "on")
    engine.evaluate_all()
    adapter = _FakeAdapter()
    adapter.set_state("light.dynamic", "off")
    reconciler = Reconciliation(
        drift_override_ttl_ms=1, drift_confirmation_ms=0, service_failure_backoff_ms=10
    )

    await reconciler.apply(engine, adapter, 0)
    selected.clear()
    engine.evaluate_all()
    adapter.calls.clear()
    events = await reconciler.apply(engine, adapter, 1)

    assert engine.target_policy("light.dynamic").ownership == "opportunistic"
    assert adapter.calls == []
    assert _events_of(events, "withdraw_skipped_target_policy")


# --------------------------------------------------------------------------- #
# on_state_delta
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_on_state_delta_returns_context_ignored_for_intentional_context() -> None:
    """An Intentional-owned state change produces context_ignored, not drift."""
    engine = _make_engine_with_light_rule()
    adapter = _FakeAdapter()
    adapter.set_state("light.desk", "on")
    reconciler = Reconciliation(
        drift_override_ttl_ms=300_000,
        drift_confirmation_ms=0,
        service_failure_backoff_ms=30_000,
    )
    await reconciler.apply(engine, adapter, now_ms=1_000)

    tracker = _OwningContextTracker("intentional-ctx")
    adapter.set_state(
        "light.desk",
        "off",
        context=SimpleNamespace(id="intentional-ctx", parent_id=None, user_id=None),
    )
    events = reconciler.on_state_delta(
        engine, adapter.get_state("light.desk"), tracker, now_ms=2_000
    )

    assert len(_events_of(events, "context_ignored")) == 1
    assert _events_of(events, "drift_promoted") == []


@pytest.mark.asyncio
async def test_on_state_delta_promotes_drift_immediately_without_confirmation() -> None:
    """With confirmation_ms=0, drift is promoted on the first observation."""
    engine = _make_engine_with_light_rule()
    adapter = _FakeAdapter()
    adapter.set_state("light.desk", "on")
    reconciler = Reconciliation(
        drift_override_ttl_ms=300_000,
        drift_confirmation_ms=0,
        service_failure_backoff_ms=30_000,
    )
    await reconciler.apply(engine, adapter, now_ms=1_000)

    adapter.set_state(
        "light.desk", "off", context=SimpleNamespace(id=None, parent_id=None, user_id="user-1")
    )
    events = reconciler.on_state_delta(
        engine, adapter.get_state("light.desk"), _NoContextTracker(), now_ms=4_000
    )

    promoted = _events_of(events, "drift_promoted")
    assert len(promoted) == 1
    assert promoted[0].details["target"] == "light.desk"
    assert promoted[0].details["set"] == {"state": "off"}
    assert promoted[0].details["ttl_ms"] == 300_000


@pytest.mark.asyncio
async def test_availability_recovery_does_not_become_manual_override() -> None:
    engine = _make_engine_with_light_rule()
    adapter = _FakeAdapter()
    adapter.set_state("light.desk", "on")
    reconciler = Reconciliation(
        drift_override_ttl_ms=300_000,
        drift_confirmation_ms=0,
        service_failure_backoff_ms=30_000,
    )
    await reconciler.apply(engine, adapter, now_ms=1_000)

    adapter.set_state("light.desk", "unavailable")
    unavailable = reconciler.on_state_delta(
        engine, adapter.get_state("light.desk"), _NoContextTracker(), now_ms=2_000
    )
    adapter.set_state("light.desk", "off")
    recovered = reconciler.on_state_delta(
        engine, adapter.get_state("light.desk"), _NoContextTracker(), now_ms=100_000
    )
    events = await reconciler.tick(
        engine, adapter, _NoContextTracker(), now_ms=100_001
    )

    assert _events_of(unavailable, "drift_promoted") == []
    assert _events_of(recovered, "drift_promoted") == []
    assert _events_of(events, "service_applied")
    assert adapter.calls[-1][1] == "turn_on"


@pytest.mark.asyncio
async def test_on_state_delta_stages_then_promotes_across_confirmation_window() -> None:
    """With confirmation_ms > 0, drift is staged first, promoted after the window."""
    engine = _make_engine_with_light_rule()
    adapter = _FakeAdapter()
    adapter.set_state("light.desk", "on")
    reconciler = Reconciliation(
        drift_override_ttl_ms=300_000,
        drift_confirmation_ms=1_500,
        service_failure_backoff_ms=30_000,
    )
    await reconciler.apply(engine, adapter, now_ms=1_000)

    adapter.set_state(
        "light.desk", "off", context=SimpleNamespace(id=None, parent_id=None, user_id="user-1")
    )
    state = adapter.get_state("light.desk")

    # First observation: staged, no promotion.
    events = reconciler.on_state_delta(engine, state, _NoContextTracker(), now_ms=4_000)
    assert _events_of(events, "drift_promoted") == []

    # Same observation within the window: still no promotion.
    events = reconciler.on_state_delta(engine, state, _NoContextTracker(), now_ms=4_500)
    assert _events_of(events, "drift_promoted") == []

    # After the confirmation window: promoted.
    events = reconciler.on_state_delta(engine, state, _NoContextTracker(), now_ms=6_000)
    promoted = _events_of(events, "drift_promoted")
    assert len(promoted) == 1
    assert promoted[0].details["set"] == {"state": "off"}


@pytest.mark.asyncio
async def test_on_state_delta_ignores_small_brightness_pct_quantization_echo() -> None:
    """A light echoing 80% as brightness 198 must not become a manual override."""
    engine = _make_engine_with_light_rule(brightness_pct=80)
    adapter = _FakeAdapter()
    reconciler = Reconciliation(
        drift_override_ttl_ms=300_000,
        drift_confirmation_ms=1_500,
        service_failure_backoff_ms=30_000,
    )
    await reconciler.apply(engine, adapter, now_ms=1_000)

    # HA light integrations expose brightness on 0..255, and some devices echo
    # a nearby level rather than the exact brightness_pct-derived value.
    adapter.set_state("light.desk", "on", attributes={"brightness": 198})
    state = adapter.get_state("light.desk")

    events = reconciler.on_state_delta(engine, state, _NoContextTracker(), now_ms=10_000)
    assert _events_of(events, "drift_promoted") == []

    events = await reconciler.tick(engine, adapter, _NoContextTracker(), now_ms=12_000)
    assert _events_of(events, "drift_promoted") == []


@pytest.mark.asyncio
async def test_on_state_delta_promotes_brightness_pct_drift_beyond_tolerance() -> None:
    """A stable brightness change larger than device quantization is manual drift."""
    engine = _make_engine_with_light_rule(brightness_pct=80)
    adapter = _FakeAdapter()
    reconciler = Reconciliation(
        drift_override_ttl_ms=300_000,
        drift_confirmation_ms=1_500,
        service_failure_backoff_ms=30_000,
    )
    await reconciler.apply(engine, adapter, now_ms=1_000)

    adapter.set_state("light.desk", "on", attributes={"brightness": 190})
    state = adapter.get_state("light.desk")

    events = reconciler.on_state_delta(engine, state, _NoContextTracker(), now_ms=10_000)
    assert _events_of(events, "drift_promoted") == []

    events = await reconciler.tick(engine, adapter, _NoContextTracker(), now_ms=12_000)
    promoted = _events_of(events, "drift_promoted")
    assert len(promoted) == 1
    assert promoted[0].details["set"] == {"state": "on", "brightness": 190}


@pytest.mark.asyncio
async def test_on_state_delta_ignores_unreported_applied_attribute() -> None:
    engine = _make_engine_with_light_rule()
    adapter = _FakeAdapter()
    reconciler = Reconciliation(
        drift_override_ttl_ms=300_000,
        drift_confirmation_ms=0,
        service_failure_backoff_ms=30_000,
    )
    await reconciler.apply(engine, adapter, now_ms=1_000)
    adapter.set_state("light.desk", "on")

    events = reconciler.on_state_delta(
        engine, adapter.get_state("light.desk"), _NoContextTracker(), now_ms=10_000
    )

    assert _events_of(events, "drift_promoted") == []


# --------------------------------------------------------------------------- #
# apply
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_apply_skips_when_state_already_matches() -> None:
    """When actual state already matches the service plan, no call is made."""
    engine = _make_engine_with_light_rule()
    adapter = _FakeAdapter()
    adapter.set_state("light.desk", "on", attributes={"brightness": 153})
    reconciler = Reconciliation(
        drift_override_ttl_ms=300_000,
        drift_confirmation_ms=0,
        service_failure_backoff_ms=30_000,
    )

    events = await reconciler.apply(engine, adapter, now_ms=1_000)
    assert len(_events_of(events, "service_skipped_matching_state")) == 1
    assert _events_of(events, "service_applied") == []


@pytest.mark.asyncio
async def test_apply_suppresses_duplicate_calls() -> None:
    """A second apply with the same resolved value does not repeat the call."""
    engine = _make_engine_with_light_rule()
    adapter = _FakeAdapter()
    reconciler = Reconciliation(
        drift_override_ttl_ms=300_000,
        drift_confirmation_ms=0,
        service_failure_backoff_ms=30_000,
    )

    await reconciler.apply(engine, adapter, now_ms=1_000)
    await reconciler.apply(engine, adapter, now_ms=2_000)
    assert len(adapter.calls) == 1


@pytest.mark.asyncio
async def test_apply_does_not_repeat_successful_plan_when_attribute_is_unreported() -> None:
    engine = _make_engine_with_light_rule()
    adapter = _FakeAdapter()
    adapter.set_state("light.desk", "on")
    reconciler = Reconciliation(
        drift_override_ttl_ms=300_000,
        drift_confirmation_ms=0,
        service_failure_backoff_ms=30_000,
    )

    await reconciler.apply(engine, adapter, now_ms=1_000)
    await reconciler.apply(engine, adapter, now_ms=10_000)

    assert len(adapter.calls) == 1


@pytest.mark.asyncio
async def test_apply_retries_successful_plan_when_attribute_is_contradictory() -> None:
    engine = _make_engine_with_light_rule()
    adapter = _FakeAdapter()
    adapter.set_state("light.desk", "on")
    reconciler = Reconciliation(
        drift_override_ttl_ms=300_000,
        drift_confirmation_ms=0,
        service_failure_backoff_ms=30_000,
    )

    await reconciler.apply(engine, adapter, now_ms=1_000)
    adapter.set_state("light.desk", "on", attributes={"brightness": 100})
    await reconciler.apply(engine, adapter, now_ms=10_000)

    assert len(adapter.calls) == 2


@pytest.mark.asyncio
async def test_apply_does_not_skip_initial_plan_when_attribute_is_unreported() -> None:
    engine = _make_engine_with_light_rule()
    adapter = _FakeAdapter()
    adapter.set_state("light.desk", "on")
    reconciler = Reconciliation(
        drift_override_ttl_ms=300_000,
        drift_confirmation_ms=0,
        service_failure_backoff_ms=30_000,
    )

    await reconciler.apply(engine, adapter, now_ms=1_000)

    assert len(adapter.calls) == 1


@pytest.mark.asyncio
async def test_apply_backs_off_on_service_failure() -> None:
    """A failed service call arms backoff; the next apply within backoff is skipped."""
    engine = _make_engine_with_light_rule()
    adapter = _FakeAdapter()
    adapter._fail_next = True
    reconciler = Reconciliation(
        drift_override_ttl_ms=300_000,
        drift_confirmation_ms=0,
        service_failure_backoff_ms=30_000,
    )

    events = await reconciler.apply(engine, adapter, now_ms=1_000)
    assert len(_events_of(events, "service_failed")) == 1
    assert _events_of(events, "service_retry_scheduled")[0].details["delay_ms"] == 30_000

    # Within backoff window: no retry.
    events = await reconciler.apply(engine, adapter, now_ms=10_000)
    assert _events_of(events, "service_applied") == []
    assert _events_of(events, "service_failed") == []
    assert _events_of(events, "service_retry_skipped") == []

    for now_ms in range(10_100, 31_000, 100):
        events = await reconciler.apply(engine, adapter, now_ms=now_ms)
        assert _events_of(events, "service_retry_skipped") == []

    # After backoff: retries.
    events = await reconciler.apply(engine, adapter, now_ms=35_000)
    assert len(_events_of(events, "service_applied")) == 1
    assert len(_events_of(events, "service_retry_recovered")) == 1


@pytest.mark.asyncio
async def test_apply_uses_target_level_bounded_exponential_retry_with_jitter() -> None:
    engine = _make_engine_with_light_rule()
    adapter = _FakeAdapter()
    adapter._fail_services.add("turn_on")
    jitter_inputs: list[tuple[str, int, int]] = []

    def jitter(target: str, attempt: int, delay_ms: int) -> int:
        jitter_inputs.append((target, attempt, delay_ms))
        return delay_ms + 100

    reconciler = Reconciliation(
        drift_override_ttl_ms=300_000,
        drift_confirmation_ms=0,
        service_failure_backoff_ms=1_000,
        service_failure_backoff_max_ms=2_000,
        retry_jitter_fn=jitter,
    )

    first = await reconciler.apply(engine, adapter, now_ms=0)
    second = await reconciler.apply(engine, adapter, now_ms=1_100)
    third = await reconciler.apply(engine, adapter, now_ms=3_100)

    assert jitter_inputs == [
        ("light.desk", 1, 1_000),
        ("light.desk", 2, 2_000),
        ("light.desk", 3, 2_000),
    ]
    assert _events_of(first, "service_retry_scheduled")[0].details["retry_at_ms"] == 1_100
    assert _events_of(second, "service_retry_scheduled")[0].details["retry_at_ms"] == 3_100
    assert _events_of(third, "service_retry_scheduled")[0].details["retry_at_ms"] == 5_100
    assert len(reconciler._service_failure_backoff) == 1


@pytest.mark.asyncio
async def test_changed_signature_cannot_bypass_target_retry() -> None:
    engine = _make_engine_with_light_rule(brightness_pct=60)
    adapter = _FakeAdapter()
    adapter._fail_next = True
    reconciler = Reconciliation(
        drift_override_ttl_ms=300_000,
        drift_confirmation_ms=0,
        service_failure_backoff_ms=30_000,
    )
    await reconciler.apply(engine, adapter, now_ms=1_000)

    engine = _make_engine_with_light_rule(brightness_pct=70)
    events = await reconciler.apply(engine, adapter, now_ms=2_000)

    assert adapter.calls == []
    assert _events_of(events, "service_retry_skipped") == []
    assert len(reconciler._service_failure_backoff) == 1


@pytest.mark.asyncio
async def test_due_distinct_plan_resets_retry_ceiling_and_progress() -> None:
    engine = _make_engine_with_light_rule(brightness_pct=60)
    engine.load_rules(
        engine.loaded_rules(),
        target_policies={"light.desk": TargetPolicy(max_retries=0)},
    )
    engine.evaluate_all()
    adapter = _FakeAdapter()
    adapter._fail_next = True
    reconciler = Reconciliation(
        drift_override_ttl_ms=300_000,
        drift_confirmation_ms=0,
        service_failure_backoff_ms=30_000,
    )
    await reconciler.apply(engine, adapter, now_ms=1_000)
    old_signature = reconciler._service_failure_backoff["light.desk"].signature
    reconciler._service_plan_progress["light.desk"] = (old_signature, 1)

    engine = _make_engine_with_light_rule(brightness_pct=70)
    engine.load_rules(
        engine.loaded_rules(),
        target_policies={"light.desk": TargetPolicy(max_retries=0)},
    )
    engine.evaluate_all()
    before_due = await reconciler.apply(engine, adapter, now_ms=2_000)
    due = await reconciler.apply(engine, adapter, now_ms=31_000)

    assert before_due == []
    assert _events_of(before_due, "service_denied_target_policy") == []
    assert _events_of(due, "service_denied_target_policy") == []
    assert _events_of(due, "service_applied")
    assert reconciler._service_failure_backoff == {}
    assert reconciler._service_plan_progress == {}


@pytest.mark.asyncio
async def test_safety_reducing_opposite_plan_supersedes_target_retry() -> None:
    engine = _make_engine_with_light_rule(brightness_pct=60)
    adapter = _FakeAdapter()
    adapter._fail_next = True
    reconciler = Reconciliation(
        drift_override_ttl_ms=300_000,
        drift_confirmation_ms=0,
        service_failure_backoff_ms=30_000,
    )
    await reconciler.apply(engine, adapter, now_ms=1_000)

    engine.load_rules([Rule(id="off", when="true", target="light.desk", set={"state": "off"})])
    engine.evaluate_all()
    events = await reconciler.apply(engine, adapter, now_ms=2_000)

    assert _events_of(events, "service_retry_superseded")
    assert [service for _domain, service, _data in adapter.calls] == ["turn_off"]


@pytest.mark.asyncio
async def test_retry_state_is_pruned_when_target_becomes_obsolete() -> None:
    engine = _make_engine_with_light_rule()
    adapter = _FakeAdapter()
    adapter._fail_next = True
    reconciler = Reconciliation(
        drift_override_ttl_ms=300_000,
        drift_confirmation_ms=0,
        service_failure_backoff_ms=30_000,
    )
    await reconciler.apply(engine, adapter, now_ms=1_000)

    engine.update_state("input_boolean.work", "off")
    engine.evaluate_all()
    await reconciler.apply(engine, adapter, now_ms=2_000)

    assert reconciler._service_failure_backoff == {}
    assert reconciler._service_plan_progress == {}


@pytest.mark.asyncio
async def test_multicall_retry_resumes_after_successful_prefix() -> None:
    engine = Engine(clock_fn=lambda: 1000)
    engine.load_rules(
        [
            Rule(
                id="player-on",
                when="input_boolean.work == 'on'",
                target="media_player.office",
                set={"state": "on", "volume_level": 0.4, "source": "Desk"},
            )
        ]
    )
    engine.update_state("input_boolean.work", "on")
    engine.evaluate_all()
    adapter = _FakeAdapter()
    adapter._fail_services.add("volume_set")
    reconciler = Reconciliation(
        drift_override_ttl_ms=300_000,
        drift_confirmation_ms=0,
        service_failure_backoff_ms=1_000,
    )

    await reconciler.apply(engine, adapter, now_ms=0)
    adapter._fail_services.clear()
    await reconciler.apply(engine, adapter, now_ms=1_000)

    assert [service for _domain, service, _data in adapter.calls] == [
        "turn_on",
        "volume_set",
        "select_source",
    ]


@pytest.mark.asyncio
async def test_failed_initial_multicall_retains_successful_prefix_for_withdrawal() -> None:
    engine = Engine(clock_fn=lambda: 0)
    engine.load_rules([
        Rule(
            id="player-on",
            when="true",
            target="media_player.office",
            set={"state": "on", "volume_level": 0.4},
            withdraw={"state": "off", "volume_level": 0.2},
        )
    ])
    engine.evaluate_all()
    adapter = _FakeAdapter()
    adapter.set_state("media_player.office", "off", {"volume_level": 0.7})
    adapter._fail_services.add("volume_set")
    reconciler = Reconciliation(
        drift_override_ttl_ms=1, drift_confirmation_ms=0, service_failure_backoff_ms=10
    )

    await reconciler.apply(engine, adapter, now_ms=0)
    engine.load_rules([])
    adapter.set_state("media_player.office", "on", {"volume_level": 0.7})
    adapter._fail_services.clear()
    adapter.calls.clear()
    await reconciler.apply(engine, adapter, now_ms=10)

    assert [service for _domain, service, _data in adapter.calls] == ["turn_off"]
    assert all("volume_level" not in data for _domain, _service, data in adapter.calls)


@pytest.mark.asyncio
async def test_failed_initial_prefix_retains_adopted_field_ownership() -> None:
    engine = Engine(clock_fn=lambda: 0)
    engine.load_rules([
        Rule(
            id="player-on",
            when="true",
            target="media_player.office",
            set={"state": "on", "volume_level": 0.4},
            withdraw={"state": "adopt"},
        )
    ])
    engine.evaluate_all()
    adapter = _FakeAdapter()
    adapter.set_state("media_player.office", "off", {"volume_level": 0.7})
    adapter._fail_services.add("volume_set")
    reconciler = Reconciliation(
        drift_override_ttl_ms=1, drift_confirmation_ms=0, service_failure_backoff_ms=10
    )

    await reconciler.apply(engine, adapter, now_ms=0)
    records = json.loads(json.dumps(reconciler.export_runtime_state(engine)))
    restored = Reconciliation(
        drift_override_ttl_ms=1, drift_confirmation_ms=0, service_failure_backoff_ms=10
    )
    restored.restore_pending_withdraws({"reconciliation": records})
    engine.load_rules([])
    adapter.set_state("media_player.office", "on", {"volume_level": 0.7})
    adapter._fail_services.clear()
    adapter.calls.clear()
    await restored.apply(engine, adapter, now_ms=10)

    assert adapter.calls == [("media_player", "turn_off", {"entity_id": "media_player.office"})]


@pytest.mark.asyncio
async def test_restart_restores_backoff_and_completed_service_plan_prefix() -> None:
    engine = Engine(clock_fn=lambda: 0)
    engine.load_rules([
        Rule(
            id="player-on",
            when="true",
            target="media_player.office",
            set={"state": "on", "volume_level": 0.4, "source": "Desk"},
        )
    ])
    engine.evaluate_all()
    adapter = _FakeAdapter()
    adapter._fail_services.add("volume_set")
    reconciler = Reconciliation(
        drift_override_ttl_ms=1, drift_confirmation_ms=0, service_failure_backoff_ms=1_000
    )
    await reconciler.apply(engine, adapter, now_ms=0)
    persisted = json.loads(json.dumps(reconciler.export_runtime_state(engine)))

    restored = Reconciliation(
        drift_override_ttl_ms=1, drift_confirmation_ms=0, service_failure_backoff_ms=1_000
    )
    restored.restore_pending_withdraws({"reconciliation": persisted})
    adapter._fail_services.clear()
    adapter.calls.clear()
    await restored.apply(engine, adapter, now_ms=999)
    assert adapter.calls == []

    await restored.apply(engine, adapter, now_ms=1_000)
    assert [service for _domain, service, _data in adapter.calls] == [
        "volume_set",
        "select_source",
    ]


@pytest.mark.asyncio
async def test_three_call_failures_accumulate_owned_prefixes_then_withdraw_all() -> None:
    engine = Engine(clock_fn=lambda: 0)
    engine.load_rules([Rule(
        id="player-on", when="true", target="media_player.office",
        set={"state": "on", "volume_level": 0.4, "source": "Desk"},
        withdraw={"state": "on", "volume_level": 0.2, "source": "TV"},
    )])
    engine.evaluate_all()

    class SequencedFailureAdapter(_FakeAdapter):
        failures = {2, 4}

        async def async_call(self, domain, service, data, *, context):
            attempt = len(self.calls) + 1
            self.calls.append((domain, service, data))
            if attempt in self.failures:
                raise ValueError("service failed")

    adapter = SequencedFailureAdapter()
    reconciler = Reconciliation(
        drift_override_ttl_ms=1, drift_confirmation_ms=0, service_failure_backoff_ms=10
    )

    await reconciler.apply(engine, adapter, now_ms=0)
    await reconciler.apply(engine, adapter, now_ms=10)
    engine.load_rules([])
    adapter.failures.clear()
    adapter.calls.clear()
    await reconciler.apply(engine, adapter, now_ms=30)

    assert [service for _domain, service, _data in adapter.calls] == [
        "turn_on", "volume_set",
    ]


@pytest.mark.asyncio
async def test_multi_failure_restart_preserves_earlier_adoption_and_progress() -> None:
    now = [10_000]
    engine = Engine(clock_fn=lambda: now[0])
    engine.load_rules([Rule(
        id="player-on", when="true", target="media_player.office",
        set={"state": "on", "volume_level": 0.4, "source": "Desk"},
        withdraw={"state": "adopt", "volume_level": "adopt", "source": "adopt"},
    )])
    engine.evaluate_all()
    adapter = _FakeAdapter()
    adapter.set_state("media_player.office", "on", {"volume_level": 0.7, "source": "TV"})
    adapter._fail_services.add("volume_set")
    reconciler = Reconciliation(
        drift_override_ttl_ms=1, drift_confirmation_ms=0, service_failure_backoff_ms=10
    )
    await reconciler.apply(engine, adapter, now_ms=10_000)
    adapter._fail_services = {"select_source"}
    now[0] = 10_010
    await reconciler.apply(engine, adapter, now_ms=10_010)

    persisted = json.loads(json.dumps(reconciler.export_runtime_state(engine)))
    restored = Reconciliation(
        drift_override_ttl_ms=1, drift_confirmation_ms=0, service_failure_backoff_ms=10
    )
    restored.restore_pending_withdraws({"reconciliation": persisted}, now_ms=5)
    adapter._fail_services.clear()
    adapter.calls.clear()
    await restored.apply(engine, adapter, now_ms=25)
    adapter.set_state("media_player.office", "on", {"volume_level": 0.4, "source": "Desk"})
    engine.load_rules([])
    await restored.apply(engine, adapter, now_ms=26)

    assert adapter.calls == [
        ("media_player", "select_source", {"entity_id": "media_player.office", "source": "Desk"}),
        ("media_player", "turn_on", {"entity_id": "media_player.office"}),
        ("media_player", "volume_set", {"entity_id": "media_player.office", "volume_level": 0.7}),
        ("media_player", "select_source", {"entity_id": "media_player.office", "source": "TV"}),
    ]


@pytest.mark.asyncio
async def test_restart_rebases_bounded_retry_delay_after_monotonic_epoch_reset() -> None:
    engine = _make_engine_with_light_rule()
    adapter = _FakeAdapter()
    adapter._fail_next = True
    reconciler = Reconciliation(
        drift_override_ttl_ms=1, drift_confirmation_ms=0,
        service_failure_backoff_ms=1_000, service_failure_backoff_max_ms=2_000,
    )
    await reconciler.apply(engine, adapter, now_ms=900_000)
    persisted = json.loads(json.dumps(reconciler.export_runtime_state(
        SimpleNamespace(now_ms=lambda: 900_000)
    )))
    assert persisted["service_failure_backoff"][0]["remaining_ms"] <= 2_000
    assert "retry_at_ms" not in persisted["service_failure_backoff"][0]

    restored = Reconciliation(
        drift_override_ttl_ms=1, drift_confirmation_ms=0,
        service_failure_backoff_ms=1_000, service_failure_backoff_max_ms=2_000,
    )
    restored.restore_pending_withdraws({"reconciliation": persisted}, now_ms=5)
    await restored.apply(engine, adapter, now_ms=1_004)
    assert adapter.calls == []
    await restored.apply(engine, adapter, now_ms=1_005)
    assert len(adapter.calls) == 1


@pytest.mark.asyncio
async def test_retry_export_uses_live_reconciliation_monotonic_clock_not_engine_epoch() -> None:
    engine = Engine(clock_fn=lambda: 1_700_000_000_000)
    engine.load_rules([Rule(id="on", when="true", target="light.desk", set={"state": "on"})])
    engine.evaluate_all()
    adapter = _FakeAdapter()
    adapter._fail_next = True
    reconciler = Reconciliation(
        drift_override_ttl_ms=1, drift_confirmation_ms=0,
        service_failure_backoff_ms=1_000, service_failure_backoff_max_ms=2_000,
    )

    await reconciler.apply(engine, adapter, now_ms=100)
    await reconciler.apply(engine, adapter, now_ms=400)
    persisted = json.loads(json.dumps(reconciler.export_runtime_state(engine)))

    assert persisted["service_failure_backoff"][0]["remaining_ms"] == 700
    restored = Reconciliation(
        drift_override_ttl_ms=1, drift_confirmation_ms=0,
        service_failure_backoff_ms=1_000, service_failure_backoff_max_ms=2_000,
    )
    restored.restore_pending_withdraws({"reconciliation": persisted}, now_ms=5)
    await restored.apply(engine, adapter, now_ms=704)
    assert adapter.calls == []
    await restored.apply(engine, adapter, now_ms=705)
    assert len(adapter.calls) == 1


def test_restore_reconciliation_state_rejects_unknown_version_and_bad_records() -> None:
    reconciler = Reconciliation(
        drift_override_ttl_ms=1, drift_confirmation_ms=0, service_failure_backoff_ms=10
    )
    reconciler.restore_pending_withdraws({
        "reconciliation": {
            "version": 999,
            "service_failure_backoff": [{"target": "light.x", "failures": 1, "retry_at_ms": 10, "signature": []}],
            "service_plan_progress": [{"target": "light.x", "next_call_index": 1, "signature": []}],
        }
    })
    assert reconciler._service_failure_backoff == {}
    assert reconciler._service_plan_progress == {}


@pytest.mark.asyncio
async def test_dispatch_hook_completes_immediately_before_adapter_call() -> None:
    engine = _make_engine_with_light_rule()
    order: list[str] = []

    class OrderedAdapter(_FakeAdapter):
        async def async_call(self, domain, service, data, *, context):
            order.append("adapter")
            await super().async_call(domain, service, data, context=context)

    async def before_dispatch() -> None:
        order.append("hook")

    reconciler = Reconciliation(
        drift_override_ttl_ms=1, drift_confirmation_ms=0, service_failure_backoff_ms=10
    )
    await reconciler.tick(
        engine, OrderedAdapter(), _NoContextTracker(), 0,
        before_dispatch=before_dispatch,
    )
    assert order[:2] == ["hook", "adapter"]


@pytest.mark.asyncio
async def test_revision_change_during_service_call_discards_stale_bookkeeping() -> None:
    engine = _make_engine_with_light_rule()
    entered = asyncio.Event()
    release = asyncio.Event()
    revision = 1

    class BarrierAdapter(_FakeAdapter):
        async def async_call(self, domain, service, data, *, context):
            entered.set()
            await release.wait()
            await super().async_call(domain, service, data, context=context)

    adapter = BarrierAdapter()
    reconciler = Reconciliation(
        drift_override_ttl_ms=300_000,
        drift_confirmation_ms=0,
        service_failure_backoff_ms=1_000,
    )
    task = asyncio.create_task(
        reconciler.tick(
            engine,
            adapter,
            _NoContextTracker(),
            now_ms=0,
            revision_is_current=lambda: revision == 1,
        )
    )
    await entered.wait()
    revision = 2
    release.set()

    events = await task

    assert _events_of(events, "stale_result_discarded")
    assert reconciler._last_applied == {}
    assert reconciler._last_resolved == {}
    assert reconciler._service_failure_backoff == {}


@pytest.mark.asyncio
async def test_apply_withdraws_stale_target_to_off() -> None:
    """When a target becomes inactive, apply issues a withdraw (turn_off)."""
    engine = _make_engine_with_light_rule()
    adapter = _FakeAdapter()
    adapter.set_state("light.desk", "on")
    reconciler = Reconciliation(
        drift_override_ttl_ms=300_000,
        drift_confirmation_ms=0,
        service_failure_backoff_ms=30_000,
    )
    await reconciler.apply(engine, adapter, now_ms=1_000)

    engine.update_state("input_boolean.work", "off")
    engine.evaluate_all()

    adapter.set_state("light.desk", "on", attributes={"brightness": 153})
    await reconciler.apply(engine, adapter, now_ms=2_000)

    withdraw_calls = [c for c in adapter.calls if c[1] == "turn_off"]
    assert len(withdraw_calls) == 1


@pytest.mark.asyncio
async def test_reload_removal_preserves_managed_target_until_withdrawal() -> None:
    engine = _make_engine_with_light_rule()
    adapter = _FakeAdapter()
    reconciler = Reconciliation(
        drift_override_ttl_ms=300_000,
        drift_confirmation_ms=0,
        service_failure_backoff_ms=30_000,
    )
    await reconciler.apply(engine, adapter, now_ms=1_000)

    engine.load_rules([])
    engine.evaluate_all()
    reconciler.retain_targets(set())
    assert reconciler.pending_withdraw_targets() == ("light.desk",)

    await reconciler.apply(engine, adapter, now_ms=2_000)

    assert [service for _domain, service, _data in adapter.calls] == ["turn_on", "turn_off"]


@pytest.mark.asyncio
async def test_reload_target_change_preserves_old_managed_target_until_withdrawal() -> None:
    engine = _make_engine_with_light_rule()
    adapter = _FakeAdapter()
    reconciler = Reconciliation(
        drift_override_ttl_ms=300_000,
        drift_confirmation_ms=0,
        service_failure_backoff_ms=30_000,
    )
    await reconciler.apply(engine, adapter, now_ms=1_000)

    engine.load_rules(
        [
            Rule(
                id="desk-on",
                when="input_boolean.work == 'on'",
                target="light.table",
                set={"state": "on"},
            )
        ]
    )
    engine.evaluate_all()
    reconciler.retain_targets(set())
    assert reconciler.pending_withdraw_targets() == ("light.desk",)

    await reconciler.apply(engine, adapter, now_ms=2_000)

    assert ("light", "turn_off", {"entity_id": "light.desk"}) in adapter.calls
    assert ("light", "turn_on", {"entity_id": "light.table"}) in adapter.calls


@pytest.mark.asyncio
async def test_successful_withdraw_is_not_repeated_when_state_is_unavailable() -> None:
    engine = _make_engine_with_light_rule()
    adapter = _FakeAdapter()
    reconciler = Reconciliation(
        drift_override_ttl_ms=300_000,
        drift_confirmation_ms=0,
        service_failure_backoff_ms=30_000,
    )
    await reconciler.apply(engine, adapter, now_ms=1_000)
    engine.update_state("input_boolean.work", "off")
    engine.evaluate_all()

    await reconciler.apply(engine, adapter, now_ms=2_000)
    await reconciler.apply(engine, adapter, now_ms=10_000)

    assert [call[1] for call in adapter.calls] == ["turn_on", "turn_off"]


@pytest.mark.asyncio
async def test_apply_active_off_intent_turns_light_off() -> None:
    """An explicit active off intent must call light.turn_off even without prior ownership."""
    engine = Engine(clock_fn=lambda: 1000)
    engine.load_rules(
        [
            Rule(
                id="desk-off",
                when="input_boolean.work == 'off'",
                target="light.desk",
                set={"state": "off"},
            )
        ]
    )
    engine.update_state("input_boolean.work", "off")
    engine.evaluate_all()
    adapter = _FakeAdapter()
    adapter.set_state("light.desk", "on", attributes={"brightness": 153})
    reconciler = Reconciliation(
        drift_override_ttl_ms=300_000,
        drift_confirmation_ms=0,
        service_failure_backoff_ms=30_000,
    )

    await reconciler.apply(engine, adapter, now_ms=1_000)

    assert adapter.calls == [("light", "turn_off", {"entity_id": "light.desk"})]


@pytest.mark.asyncio
async def test_restored_resolved_target_withdraws_after_restart_when_rule_is_idle() -> None:
    """Restart/reload recovery keeps enough ownership memory to withdraw idle targets."""
    engine = _make_engine_with_light_rule()
    adapter = _FakeAdapter()
    adapter.set_state("light.desk", "off")
    reconciler = Reconciliation(
        drift_override_ttl_ms=300_000,
        drift_confirmation_ms=0,
        service_failure_backoff_ms=30_000,
    )

    await reconciler.apply(engine, adapter, now_ms=1_000)
    records = reconciler.export_pending_withdraws(engine)

    restarted = _make_engine_with_light_rule()
    restarted.update_state("input_boolean.work", "off")
    restarted.evaluate_all()
    restored = Reconciliation(
        drift_override_ttl_ms=300_000,
        drift_confirmation_ms=0,
        service_failure_backoff_ms=30_000,
    )
    restored.restore_pending_withdraws({"pending_withdraws": records})
    adapter.set_state("light.desk", "on", attributes={"brightness": 153})

    await restored.apply(restarted, adapter, now_ms=2_000)

    withdraw_calls = [c for c in adapter.calls if c[1] == "turn_off"]
    assert len(withdraw_calls) == 1


# --------------------------------------------------------------------------- #
# tick (confirm + apply)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_tick_confirms_pending_drift_candidate() -> None:
    """tick's confirm step promotes a stable drift candidate after the window."""
    engine = _make_engine_with_light_rule()
    adapter = _FakeAdapter()
    adapter.set_state("light.desk", "on")
    reconciler = Reconciliation(
        drift_override_ttl_ms=300_000,
        drift_confirmation_ms=1_500,
        service_failure_backoff_ms=30_000,
    )
    await reconciler.apply(engine, adapter, now_ms=0)

    # Stage a drift candidate via on_state_delta.
    adapter.set_state(
        "light.desk", "off", context=SimpleNamespace(id=None, parent_id=None, user_id="user-1")
    )
    reconciler.on_state_delta(
        engine, adapter.get_state("light.desk"), _NoContextTracker(), now_ms=2_000
    )

    # tick before the confirmation window: no promotion.
    events = await reconciler.tick(engine, adapter, _NoContextTracker(), now_ms=2_500)
    assert _events_of(events, "drift_promoted") == []

    # tick after the confirmation window: promoted.
    events = await reconciler.tick(engine, adapter, _NoContextTracker(), now_ms=4_000)
    promoted = _events_of(events, "drift_promoted")
    assert len(promoted) == 1
    assert len(_events_of(events, "service_skipped_drift_promoted")) == 1
    assert len(adapter.calls) == 1


@pytest.mark.asyncio
async def test_attempt_history_is_bounded_redacted_and_correlated() -> None:
    reconciler = Reconciliation(
        drift_override_ttl_ms=1, drift_confirmation_ms=0, service_failure_backoff_ms=0
    )
    for now_ms in range(260):
        reconciler._record_events([
            ReconciliationEvent("service_applied", "lock.front", {
                "domain": "lock", "service": "unlock",
                "service_data": {"entity_id": "lock.front", "code": "1234"},
            })
        ], now_ms)

    history = reconciler.recent_history(limit=300)
    assert len(history) == 256

    assert reconciler.recent_history(limit=0) == []
    assert history[0]["time_ms"] == 4
    assert len({record["plan_id"] for record in history}) == 1
    assert all(record["call"]["data"]["code"] == "[redacted]" for record in history)


@pytest.mark.asyncio
async def test_stable_matching_skip_does_not_increment_churn_and_windows_expire() -> None:
    engine = _make_engine_with_light_rule()
    adapter = _FakeAdapter()
    adapter.set_state("light.desk", "on", {"brightness": 153})
    reconciler = Reconciliation(
        drift_override_ttl_ms=1, drift_confirmation_ms=0, service_failure_backoff_ms=10
    )
    await reconciler.apply(engine, adapter, 0)

    assert reconciler.churn_status(0)["five_minute"] == {}
    reconciler.record_publication_dispatch(1)
    assert reconciler.churn_status(1)["five_minute"] == {"publication_dispatch": 1}
    assert reconciler.churn_status(3_600_002)["hour"] == {}


@pytest.mark.asyncio
async def test_rule_shadowing_emits_transitions_and_tracks_durations() -> None:
    now = 0
    engine = Engine(clock_fn=lambda: now)
    engine.load_rules([
        Rule(id="low", when="true", target="light.desk", set={"state": "on"}),
        Rule(id="high", when="input_boolean.override == 'on'", target="light.desk", set={"state": "off"}),
    ])
    engine.evaluate_all()
    reconciler = Reconciliation(
        drift_override_ttl_ms=1, drift_confirmation_ms=0, service_failure_backoff_ms=10
    )
    adapter = _FakeAdapter()
    await reconciler.apply(engine, adapter, 0)

    now = 100
    engine.update_state("input_boolean.override", "on")
    engine.evaluate_all()
    events = await reconciler.apply(engine, adapter, 100)
    assert _events_of(events, "rule_fully_shadowed")[0].details == {
        "rule_id": "low", "previous_duration_ms": 100,
    }
    assert reconciler.rule_shadowing_status(150)["low"] == {
        "fully_shadowed": True, "active_duration_ms": 150, "winning_duration_ms": 100,
    }

    now = 200
    engine.update_state("input_boolean.override", "off")
    engine.evaluate_all()
    events = await reconciler.apply(engine, adapter, 200)
    assert _events_of(events, "rule_visible")[0].details["rule_id"] == "low"


@pytest.mark.asyncio
async def test_large_repeated_reconciliation_stays_within_tick_budget_and_resolves_once() -> None:
    target_count = 128
    iterations = 20
    engine = Engine(clock_fn=lambda: 0)
    rules = []
    for index in range(target_count):
        target = f"light.load_{index}"
        rules.extend((
            Rule(id=f"base-{index}", when="true", target=target, set={"state": "on"}),
            Rule(id=f"shadow-{index}", when="input_boolean.churn == 'on'", target=target, set={"state": "off"}),
        ))
    engine.load_rules(rules)
    adapter = _FakeAdapter()
    for index in range(target_count):
        adapter.set_state(f"light.load_{index}", "on")
    reconciler = Reconciliation(
        drift_override_ttl_ms=1, drift_confirmation_ms=0, service_failure_backoff_ms=10
    )
    resolve_calls = 0
    original_resolve = engine.resolve

    def counted_resolve(target: str):
        nonlocal resolve_calls
        resolve_calls += 1
        return original_resolve(target)

    engine.resolve = counted_resolve  # type: ignore[method-assign]
    started = time.perf_counter()
    for iteration in range(iterations):
        engine.update_state("input_boolean.churn", "on" if iteration % 2 else "off")
        engine.evaluate_all()
        await reconciler.apply(engine, adapter, iteration * 100)
    elapsed = time.perf_counter() - started

    assert resolve_calls == target_count * iterations
    assert elapsed / iterations < 0.1
    assert len(reconciler._churn_events) <= 4096
    assert len(reconciler._churn_targets) <= 256


@pytest.mark.parametrize("horizons", [[0] * 33, [86_400_001], [100, 99], [True]])
def test_preview_horizon_bounds(horizons: list[int]) -> None:
    with pytest.raises(ValueError, match="horizons_ms"):
        validate_preview_horizons(horizons)


def test_churn_target_cardinality_is_bounded() -> None:
    reconciler = Reconciliation(
        drift_override_ttl_ms=1, drift_confirmation_ms=0, service_failure_backoff_ms=10
    )
    for index in range(300):
        reconciler._record_churn(index, "call", f"light.target_{index}", None)

    assert len(reconciler._churn_targets) == 256
    assert len({event[2] for event in reconciler._churn_events}) == 256
    assert "light.target_0" not in reconciler._churn_targets


@pytest.mark.asyncio
async def test_lifecycle_preview_is_reconciliation_aware_and_does_not_mutate_source() -> None:
    source = Engine(clock_fn=lambda: 0)
    source.load_rules([
        Rule(
            id="delayed", when="input_boolean.ready == 'on'", for_ms=1_000,
            target="light.desk", set={"state": "on"},
        )
    ])
    source.update_state("input_boolean.ready", "on")
    source.evaluate_all()
    before = source.export_lifecycle_records()
    preview = Engine(clock_fn=lambda: source.now_ms())
    preview.load_rules(source.loaded_rules())
    preview.update_state("input_boolean.ready", "on")
    preview.import_lifecycle_records(before)
    preview.evaluate_all()

    phases = await simulate_timeline(preview, [{"advance_ms": 500}, {"advance_ms": 500}])

    assert phases[0]["calls"] == []
    assert phases[1]["calls"][0]["service"] == "turn_on"
    assert source.list_active_targets() == ()
    assert source.export_lifecycle_records() == before
