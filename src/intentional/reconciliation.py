"""Reconciliation: decide, apply, and promote drift.

Per ADR-0001, this module owns the decide + apply + drift-promotion policy
and the five mutable dicts it reads (``last_applied``, ``last_resolved``,
``drift_suppressed_until``, ``drift_candidates``, ``service_failure_backoff``).
It deliberately does NOT own HA state ingest or rule evaluation; those stay in
the integration and run before this module is called.

The module is pure-read on everything except its own dicts: HA I/O goes through
an injected :class:`HAAdapter` and context attribution through a
:class:`ContextTracker`. Decisions and observations are returned as
:class:`ReconciliationEvent` values so the integration can translate them to
diagnostics without this module knowing about HA's diagnostic sink.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from intentional.ha_adapter import (
    ServicePlanSignature,
    clear_pending_state_drift,
    emit_manual_override_for_state_drift,
    pending_drift_targets,
    service_calls_for_resolved_target,
    service_plan_matches_state,
    service_plan_signature,
)

# --------------------------------------------------------------------------- #
# Collaborators injected by the integration
# --------------------------------------------------------------------------- #


@runtime_checkable
class HAAdapter(Protocol):
    """The HA-facing operations Reconciliation needs."""

    def get_state(self, entity_id: str) -> Any: ...

    async def async_call(
        self, domain: str, service: str, data: dict[str, Any], *, context: Any
    ) -> None: ...

    def new_context(self) -> Any: ...


@runtime_checkable
class ContextTracker(Protocol):
    """Attribution for HA state changes owned by Intentional."""

    def owns_state(self, state: Any) -> bool: ...


# --------------------------------------------------------------------------- #
# Events
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ReconciliationEvent:
    """A single reconciliation observation the integration translates to diagnostics."""

    kind: str
    target: str = ""
    details: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Resolved target state (moved from the integration)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ResolvedTargetState:
    value: dict[str, Any]
    winner_key: tuple[str, int] | None
    winner_confidence: float | None
    transition_ms: int
    transition_withdraw_ms: int | None
    withdraw_value: dict[str, Any] | None = None


WITHDRAW_TO_OFF_DOMAINS = frozenset({"light", "switch", "input_boolean", "fan", "siren"})


# --------------------------------------------------------------------------- #
# Pure helpers (moved from the integration)
# --------------------------------------------------------------------------- #


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _transition_ms_for_resolved_change(
    previous: ResolvedTargetState | None,
    resolved: Any,
) -> int:
    winner = resolved.winning_intent
    if winner is None:
        return resolved.transition_ms
    if previous is None or previous.winner_key != (winner.rule_id, winner.created_at_ms):
        if (
            previous is not None
            and previous.winner_confidence is not None
            and winner.confidence < previous.winner_confidence
            and previous.transition_withdraw_ms is not None
        ):
            return previous.transition_withdraw_ms
        return (
            winner.transition_assert_ms
            if winner.transition_assert_ms is not None
            else resolved.transition_ms
        )
    return (
        winner.transition_change_ms
        if winner.transition_change_ms is not None
        else resolved.transition_ms
    )


def _default_withdraw_value(target: str, previous: ResolvedTargetState) -> dict[str, Any] | None:
    if previous.withdraw_value is not None:
        return dict(previous.withdraw_value)
    domain, sep, _object_id = target.partition(".")
    if not sep or domain not in WITHDRAW_TO_OFF_DOMAINS:
        return None
    if previous.value.get("state") != "on":
        return None
    return {"state": "off"}


def _withdraw_value_for_service_plan(
    target: str,
    calls: tuple[tuple[str, str, dict[str, Any]], ...] | None,
) -> dict[str, Any] | None:
    domain, sep, _object_id = target.partition(".")
    if not sep or domain not in WITHDRAW_TO_OFF_DOMAINS or calls is None:
        return None
    for call_domain, service, service_data in calls:
        if call_domain != domain:
            continue
        if service == "turn_on" and service_data.get("entity_id") == target:
            return {"state": "off"}
    return None


def _last_applied_is_withdraw_signature(
    target: str,
    previous: ResolvedTargetState | None,
    last_applied: dict[str, ServicePlanSignature],
) -> bool:
    if previous is None:
        return False
    withdraw_value = _default_withdraw_value(target, previous)
    if withdraw_value is None:
        return False
    transition_ms = (
        previous.transition_withdraw_ms
        if previous.transition_withdraw_ms is not None
        else previous.transition_ms
    )
    calls = service_calls_for_resolved_target(
        target,
        withdraw_value,
        transition_ms=transition_ms,
    )
    if not calls:
        return False
    return last_applied.get(target) == service_plan_signature(calls)


def _state_has_user_context(state: Any) -> bool:
    context = getattr(state, "context", None)
    return getattr(context, "user_id", None) is not None


def _build_resolved_target_state(
    resolved: Any,
    calls: tuple[tuple[str, str, dict[str, Any]], ...] | None = None,
) -> ResolvedTargetState:
    winner = resolved.winning_intent
    return ResolvedTargetState(
        value=dict(resolved.value),
        winner_key=(winner.rule_id, winner.created_at_ms) if winner is not None else None,
        winner_confidence=winner.confidence if winner is not None else None,
        transition_ms=resolved.transition_ms,
        transition_withdraw_ms=winner.transition_withdraw_ms if winner is not None else None,
        withdraw_value=_withdraw_value_for_service_plan(resolved.target, calls),
    )


def _resolved_target_state_from_record(
    raw: dict[str, Any],
    *,
    value_key: str,
) -> tuple[str, ResolvedTargetState] | None:
    target = raw.get("target")
    value = raw.get(value_key)
    if not isinstance(target, str) or not isinstance(value, dict):
        return None
    winner_key_raw = raw.get("winner_key")
    winner_key = None
    if (
        isinstance(winner_key_raw, list | tuple)
        and len(winner_key_raw) == 2
        and isinstance(winner_key_raw[0], str)
    ):
        try:
            winner_key = (winner_key_raw[0], int(winner_key_raw[1]))
        except (TypeError, ValueError):
            winner_key = None
    elif value_key == "set":
        rule_id = raw.get("rule_id")
        created_at_ms = raw.get("created_at_ms")
        if isinstance(rule_id, str):
            try:
                winner_key = (rule_id, int(created_at_ms or 0))
            except (TypeError, ValueError):
                winner_key = None
    try:
        return target, ResolvedTargetState(
            value=dict(value),
            winner_key=winner_key,
            winner_confidence=_optional_float(
                raw.get("winner_confidence") if "winner_confidence" in raw else raw.get("confidence")
            ),
            transition_ms=int(raw.get("transition_ms") or 0),
            transition_withdraw_ms=_optional_int(raw.get("transition_withdraw_ms")),
            withdraw_value=dict(raw["withdraw_value"])
            if isinstance(raw.get("withdraw_value"), dict)
            else None,
        )
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Reconciliation
# --------------------------------------------------------------------------- #


class Reconciliation:
    """Decide which service plans to call, what to suppress, and what drift to promote."""

    def __init__(
        self,
        *,
        drift_override_ttl_ms: int,
        drift_confirmation_ms: int,
        service_failure_backoff_ms: int,
        drift_transition_grace_ms: int = 2_000,
    ) -> None:
        self._drift_override_ttl_ms = drift_override_ttl_ms
        self._drift_confirmation_ms = drift_confirmation_ms
        self._service_failure_backoff_ms = service_failure_backoff_ms
        self._drift_transition_grace_ms = drift_transition_grace_ms

        self._last_applied: dict[str, ServicePlanSignature] = {}
        self._last_resolved: dict[str, ResolvedTargetState] = {}
        self._drift_suppressed_until: dict[str, int] = {}
        self._drift_candidates: dict[str, Any] = {}
        self._service_failure_backoff: dict[tuple[str, ServicePlanSignature], int] = {}

    # ----- drift classification (shared by on_state_delta and tick) -----

    def _classify_drift(
        self,
        engine: Any,
        state: Any,
        context_tracker: ContextTracker,
        now_ms: int,
    ) -> list[ReconciliationEvent]:
        """Classify one observed state as intentional, staged, or promoted drift."""
        entity_id = state.entity_id
        if context_tracker.owns_state(state):
            clear_pending_state_drift(self._drift_candidates, entity_id)
            return [ReconciliationEvent("context_ignored", entity_id, {"state": state})]
        promoted = emit_manual_override_for_state_drift(
            engine,
            self._last_applied,
            state,
            ttl_ms=self._drift_override_ttl_ms,
            now_ms=now_ms,
            drift_suppressed_until=self._drift_suppressed_until,
            drift_candidates=self._drift_candidates,
            confirmation_ms=self._drift_confirmation_ms,
        )
        if promoted:
            return [
                ReconciliationEvent(
                    "drift_promoted", entity_id, {"reason": "Manual HA state change"}
                )
            ]
        return []

    # ----- entry point 1: the listener path -----

    def on_state_delta(
        self,
        engine: Any,
        new_state: Any,
        context_tracker: ContextTracker,
        now_ms: int,
    ) -> list[ReconciliationEvent]:
        """Classify a single observed state change for drift.

        Does not apply service plans; that is :meth:`tick`'s job.
        """
        return self._classify_drift(engine, new_state, context_tracker, now_ms)

    # ----- entry point 2: the periodic path -----

    async def tick(
        self,
        engine: Any,
        adapter: HAAdapter,
        context_tracker: ContextTracker,
        now_ms: int,
    ) -> list[ReconciliationEvent]:
        """Confirm pending drift, apply resolved targets, and withdraw stale ones."""
        events: list[ReconciliationEvent] = []
        events.extend(self._confirm_pending_drift(engine, adapter, context_tracker, now_ms))
        events.extend(await self.apply(engine, adapter, now_ms))
        return events

    async def apply(
        self,
        engine: Any,
        adapter: HAAdapter,
        now_ms: int,
    ) -> list[ReconciliationEvent]:
        """Apply resolved targets and withdraw stale ones (no drift confirmation)."""
        events: list[ReconciliationEvent] = []
        events.extend(await self._apply_active_targets(engine, adapter, now_ms))
        events.extend(await self._withdraw_stale_targets(engine, adapter, now_ms))
        return events

    def _confirm_pending_drift(
        self,
        engine: Any,
        adapter: HAAdapter,
        context_tracker: ContextTracker,
        now_ms: int,
    ) -> list[ReconciliationEvent]:
        events: list[ReconciliationEvent] = []
        for target in pending_drift_targets(self._drift_candidates):
            state = adapter.get_state(target)
            if state is None:
                clear_pending_state_drift(self._drift_candidates, target)
                continue
            events.extend(self._classify_drift(engine, state, context_tracker, now_ms))
        return events

    async def _apply_active_targets(
        self,
        engine: Any,
        adapter: HAAdapter,
        now_ms: int,
    ) -> list[ReconciliationEvent]:
        events: list[ReconciliationEvent] = []
        active_targets = set(engine.list_active_targets())
        for target in sorted(active_targets):
            resolved = engine.resolve(target)
            if resolved is None:
                self._last_resolved.pop(target, None)
                continue
            resolved_value = dict(resolved.value)
            previous = self._last_resolved.get(target)
            transition_ms = _transition_ms_for_resolved_change(previous, resolved)
            calls = service_calls_for_resolved_target(
                target,
                resolved_value,
                transition_ms=transition_ms,
            )
            if not calls:
                self._last_resolved[target] = _build_resolved_target_state(resolved, calls)
                continue
            signature = service_plan_signature(calls)
            backoff_key = (target, signature)
            retry_after = self._service_failure_backoff.get(backoff_key)
            if retry_after is not None:
                if now_ms < retry_after:
                    continue
                self._service_failure_backoff.pop(backoff_key, None)
            has_pending_withdraw = _last_applied_is_withdraw_signature(
                target, previous, self._last_applied
            )
            current_state = adapter.get_state(target)
            suppress_until = self._drift_suppressed_until.get(target)
            if (
                previous is not None
                and previous.value == resolved_value
                and self._last_applied.get(target) is not None
                and not has_pending_withdraw
                and suppress_until is not None
                and now_ms < suppress_until
            ):
                self._last_resolved[target] = _build_resolved_target_state(resolved, calls)
                events.append(
                    ReconciliationEvent("service_skipped_pending_transition", target)
                )
                continue
            if self._last_applied.get(target) == signature:
                if current_state is None or service_plan_matches_state(signature, current_state):
                    self._last_resolved[target] = _build_resolved_target_state(resolved, calls)
                    continue
                if target in pending_drift_targets(self._drift_candidates):
                    self._last_resolved[target] = _build_resolved_target_state(resolved, calls)
                    continue
                if suppress_until is not None and now_ms < suppress_until:
                    self._last_resolved[target] = _build_resolved_target_state(resolved, calls)
                    continue
            if (
                not has_pending_withdraw
                and current_state is not None
                and service_plan_matches_state(signature, current_state)
            ):
                self._last_applied[target] = signature
                self._last_resolved[target] = _build_resolved_target_state(resolved, calls)
                events.append(ReconciliationEvent("service_skipped_matching_state", target))
                continue
            applied = await self._invoke_service_plan(
                adapter, target, calls
            )
            events.extend(applied.events)
            if applied.failed:
                self._service_failure_backoff[backoff_key] = now_ms + self._service_failure_backoff_ms
            else:
                self._service_failure_backoff.pop(backoff_key, None)
                self._last_applied[target] = signature
                clear_pending_state_drift(self._drift_candidates, target)
                self._suppress_drift_during_transition(target, transition_ms, now_ms)
                self._last_resolved[target] = _build_resolved_target_state(resolved, calls)
        return events

    async def _withdraw_stale_targets(
        self,
        engine: Any,
        adapter: HAAdapter,
        now_ms: int,
    ) -> list[ReconciliationEvent]:
        events: list[ReconciliationEvent] = []
        active_targets = set(engine.list_active_targets())
        for stale_target in sorted(set(self._last_resolved) - active_targets):
            previous = self._last_resolved[stale_target]
            withdraw_value = _default_withdraw_value(stale_target, previous)
            if withdraw_value is None:
                self._last_resolved.pop(stale_target, None)
                continue
            transition_ms = (
                previous.transition_withdraw_ms
                if previous.transition_withdraw_ms is not None
                else previous.transition_ms
            )
            calls = service_calls_for_resolved_target(
                stale_target,
                withdraw_value,
                transition_ms=transition_ms,
            )
            if not calls:
                continue
            signature = service_plan_signature(calls)
            backoff_key = (stale_target, signature)
            retry_after = self._service_failure_backoff.get(backoff_key)
            if retry_after is not None:
                if now_ms < retry_after:
                    continue
                self._service_failure_backoff.pop(backoff_key, None)
            current_state = adapter.get_state(stale_target)
            if current_state is not None and service_plan_matches_state(signature, current_state):
                self._last_resolved.pop(stale_target, None)
                self._last_applied[stale_target] = signature
                events.append(
                    ReconciliationEvent("service_skipped_matching_state", stale_target)
                )
                continue
            if self._last_applied.get(stale_target) == signature:
                if current_state is not None and _state_has_user_context(current_state):
                    self._last_resolved.pop(stale_target, None)
                    self._last_applied.pop(stale_target, None)
                    clear_pending_state_drift(self._drift_candidates, stale_target)
                    events.append(
                        ReconciliationEvent("withdraw_cancelled_user_change", stale_target)
                    )
                    continue
                suppress_until = self._drift_suppressed_until.get(stale_target)
                if suppress_until is not None and now_ms < suppress_until:
                    continue
            applied = await self._invoke_service_plan(
                adapter, stale_target, calls, withdraw=True
            )
            events.extend(applied.events)
            if applied.failed:
                self._service_failure_backoff[backoff_key] = now_ms + self._service_failure_backoff_ms
            else:
                self._service_failure_backoff.pop(backoff_key, None)
                self._last_applied[stale_target] = signature
                clear_pending_state_drift(self._drift_candidates, stale_target)
                self._suppress_drift_during_transition(stale_target, transition_ms, now_ms)
        return events

    async def _invoke_service_plan(
        self,
        adapter: HAAdapter,
        target: str,
        calls: tuple[tuple[str, str, dict[str, Any]], ...],
        *,
        withdraw: bool = False,
    ) -> _ServicePlanResult:
        """Call each service in the plan; stop on first failure."""
        fired: list[ReconciliationEvent] = []
        context = adapter.new_context()
        for domain, service, service_data in calls:
            try:
                await adapter.async_call(
                    domain, service, service_data, context=context
                )
            except Exception as err:  # noqa: BLE001 — record and back off
                fired.append(
                    ReconciliationEvent(
                        "service_failed",
                        target,
                        {
                            "domain": domain,
                            "service": service,
                            "service_data": service_data,
                            "error": str(err),
                            "retry_after_ms": self._service_failure_backoff_ms,
                            "withdraw": withdraw,
                        },
                    )
                )
                return _ServicePlanResult(fired, failed=True)
            fired.append(
                ReconciliationEvent(
                    "service_applied",
                    target,
                    {
                        "domain": domain,
                        "service": service,
                        "service_data": service_data,
                        "withdraw": withdraw,
                    },
                )
            )
        return _ServicePlanResult(fired, failed=False)

    def _suppress_drift_during_transition(
        self, target: str, transition_ms: int, now_ms: int
    ) -> None:
        self._drift_suppressed_until[target] = (
            now_ms + max(0, transition_ms) + self._drift_transition_grace_ms
        )

    # ----- lifecycle / restart safety -----

    def export_pending_withdraws(self, engine: Any) -> list[dict[str, Any]]:
        active_targets = set(engine.list_active_targets())
        records: list[dict[str, Any]] = []
        for target, state in sorted(self._last_resolved.items()):
            if target in active_targets:
                continue
            records.append(
                {
                    "target": target,
                    "value": dict(state.value),
                    "winner_key": list(state.winner_key) if state.winner_key is not None else None,
                    "winner_confidence": state.winner_confidence,
                    "transition_ms": state.transition_ms,
                    "transition_withdraw_ms": state.transition_withdraw_ms,
                    "withdraw_value": dict(state.withdraw_value)
                    if state.withdraw_value is not None
                    else None,
                }
            )
        return records

    def restore_pending_withdraws(
        self,
        records: dict[str, Any] | None,
        *,
        linger_rule_ids: set[str] | None = None,
        now_ms: int | None = None,
    ) -> None:
        if not records:
            return
        for raw in records.get("pending_withdraws", []):
            if not isinstance(raw, dict):
                continue
            restored = _resolved_target_state_from_record(raw, value_key="value")
            if restored is not None:
                target, state = restored
                self._last_resolved[target] = state
        if linger_rule_ids is None or now_ms is None:
            return
        for raw in records.get("intents", []):
            if not isinstance(raw, dict):
                continue
            rule_id = str(raw.get("rule_id") or "")
            if rule_id not in linger_rule_ids or not raw.get("ignore_when"):
                continue
            ttl_ms = raw.get("ttl_ms")
            if ttl_ms is None:
                continue
            try:
                expires_at_ms = int(raw.get("created_at_ms") or 0) + int(ttl_ms)
            except (TypeError, ValueError):
                continue
            if expires_at_ms > now_ms:
                continue
            restored = _resolved_target_state_from_record(raw, value_key="set")
            if restored is not None:
                target, state = restored
                self._last_resolved[target] = state

    def drop_inactive_applied(self, active_targets: set[str]) -> None:
        """Forget applied plans for targets no longer active (after pulse clearing)."""
        for stale_target in set(self._last_applied) - active_targets:
            self._last_applied.pop(stale_target, None)


@dataclass
class _ServicePlanResult:
    events: list[ReconciliationEvent]
    failed: bool


# --------------------------------------------------------------------------- #
# API helpers (kept for the HTTP API; see ADR-0001 flagged ambiguity)
# --------------------------------------------------------------------------- #


def actual_conditions_for_desired_record(
    record: dict[str, Any],
    actual_state: Any | None,
) -> list[dict[str, str]]:
    """Return reconciliation conditions for one desired record and actual state."""
    if actual_state is None:
        return [{"type": "ActualObserved", "status": "false"}]
    target = record["target"]
    calls = service_calls_for_resolved_target(target, dict(record["desired"]))
    matches = (
        service_plan_matches_state(service_plan_signature(calls), actual_state) if calls else False
    )
    return [{"type": "ActualMatchesDesired", "status": "true" if matches else "false"}]


def actual_snapshot(actual_state: Any) -> dict[str, Any]:
    """Return the compact actual-state shape exposed to agents."""
    return {
        "state": actual_state.state,
        "attributes": dict(actual_state.attributes),
    }
