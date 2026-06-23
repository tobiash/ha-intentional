"""Interface-level tests for the Reconciliation module.

These tests exercise Reconciliation.on_state_delta and Reconciliation.apply/tick
through the module's interface — a fake adapter and a real engine, no HA required.
They assert on returned events, not internal state. This is the test surface
ADR-0001 creates: the bug-prone decide+apply+promote policy is now reachable
without the async HA harness.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from intentional.engine import Engine
from intentional.reconciliation import Reconciliation
from intentional.yaml_loader import Rule


class _FakeAdapter:
    """Minimal HAAdapter fake: records service calls, serves canned state."""

    def __init__(self) -> None:
        self._states: dict[str, Any] = {}
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self._fail_next: bool = False

    def get_state(self, entity_id: str) -> Any:
        return self._states.get(entity_id)

    async def async_call(
        self, domain: str, service: str, data: dict[str, Any], *, context: Any
    ) -> None:
        if self._fail_next:
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


def _make_engine_with_light_rule() -> Engine:
    engine = Engine(clock_fn=lambda: 1000)
    engine.load_rules([
        Rule(
            id="desk-on",
            when="input_boolean.work == 'on'",
            target="light.desk",
            set={"state": "on", "brightness_pct": 60},
        )
    ])
    engine.update_state("input_boolean.work", "on")
    engine.evaluate_all()
    return engine


def _events_of(events: list, kind: str) -> list:
    return [e for e in events if e.kind == kind]


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
        "light.desk", "off", context=SimpleNamespace(id="intentional-ctx", parent_id=None, user_id=None)
    )
    events = reconciler.on_state_delta(engine, adapter.get_state("light.desk"), tracker, now_ms=2_000)

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
        engine, adapter.get_state("light.desk"), _NoContextTracker(), now_ms=2_000
    )

    promoted = _events_of(events, "drift_promoted")
    assert len(promoted) == 1
    assert promoted[0].details["target"] == "light.desk"
    assert promoted[0].details["set"] == {"state": "off"}
    assert promoted[0].details["ttl_ms"] == 300_000


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
    events = reconciler.on_state_delta(engine, state, _NoContextTracker(), now_ms=2_000)
    assert _events_of(events, "drift_promoted") == []

    # Same observation within the window: still no promotion.
    events = reconciler.on_state_delta(engine, state, _NoContextTracker(), now_ms=2_500)
    assert _events_of(events, "drift_promoted") == []

    # After the confirmation window: promoted.
    events = reconciler.on_state_delta(engine, state, _NoContextTracker(), now_ms=4_000)
    promoted = _events_of(events, "drift_promoted")
    assert len(promoted) == 1
    assert promoted[0].details["set"] == {"state": "off"}


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

    # Within backoff window: no retry.
    events = await reconciler.apply(engine, adapter, now_ms=10_000)
    assert _events_of(events, "service_applied") == []
    assert _events_of(events, "service_failed") == []

    # After backoff: retries.
    events = await reconciler.apply(engine, adapter, now_ms=35_000)
    assert len(_events_of(events, "service_applied")) == 1


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
    await reconciler.apply(engine, adapter, now_ms=1_000)

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
