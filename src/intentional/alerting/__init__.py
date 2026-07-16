"""Pure Alert lifecycle policy."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import uuid4


@dataclass(frozen=True)
class AlertObservation:
    """Current activity for one authored Alert definition."""

    rule_id: str
    name: str
    severity: str
    summary: str
    active: bool
    for_ms: int = 0
    quality: str = "known"
    stale_after_ms: int = 120_000
    resolve_after_ms: int | None = None
    inactive_reason: str = "condition_inactive"
    pulse_id: str | None = None
    duration_revision: str = ""

    @property
    def key(self) -> str:
        return alert_definition_key(self.rule_id, self.name)


def alert_definition_key(rule_id: str, name: str) -> str:
    """Return a collision-safe serialized Alert-definition identity."""
    return json.dumps([rule_id, name], ensure_ascii=True, separators=(",", ":"))


@dataclass
class _AlertInstance:
    instance_id: str
    active_at_ms: int
    state: str
    resolve_at_ms: int | None = None
    for_ms: int = 0
    last_pulse_id: str | None = None
    duration_revision: str = ""


class AlertLifecycle:
    """Advance Alert instances from authored observations."""

    def __init__(self, *, id_factory: Callable[[], str] | None = None) -> None:
        self._id_factory = id_factory or (lambda: str(uuid4()))
        self._active: dict[str, _AlertInstance] = {}
        self._unknown_since: dict[str, int] = {}
        self._consumed_pulses: dict[str, list[str]] = {}

    def advance(
        self, observations: Iterable[AlertObservation], *, now_ms: int
    ) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        projected: list[dict[str, object]] = []
        transitions: list[dict[str, object]] = []
        for observation in observations:
            instance = self._active.get(observation.key)
            if observation.quality == "unknown":
                unknown_since = self._unknown_since.setdefault(observation.key, now_ms)
                evaluation_status = (
                    "stale"
                    if now_ms - unknown_since >= observation.stale_after_ms
                    else "grace"
                )
            else:
                self._unknown_since.pop(observation.key, None)
                evaluation_status = "current"
            if observation.resolve_after_ms is not None:
                consumed = self._consumed_pulses.get(observation.key, [])
                new_pulse = (
                    observation.quality == "known"
                    and observation.active
                    and observation.pulse_id is not None
                    and observation.pulse_id not in consumed
                )
                if (
                    observation.quality == "known"
                    and not observation.active
                    and observation.inactive_reason != "condition_inactive"
                    and instance is not None
                ):
                    transitions.append(
                        self._transition(
                            observation, instance.instance_id, "resolved", now_ms
                        )
                    )
                    self._active.pop(observation.key)
                    instance = None
                elif (
                    new_pulse
                    and instance is None
                ):
                    instance = _AlertInstance(
                        self._id_factory(),
                        now_ms,
                        "firing",
                        now_ms + observation.resolve_after_ms,
                        last_pulse_id=observation.pulse_id,
                    )
                    self._active[observation.key] = instance
                    self._remember_pulse(observation.key, observation.pulse_id)
                    transitions.append(
                        self._transition(
                            observation, instance.instance_id, "firing", now_ms
                        )
                    )
                elif (
                    new_pulse
                    and instance is not None
                ):
                    instance.resolve_at_ms = now_ms + observation.resolve_after_ms
                    instance.last_pulse_id = observation.pulse_id
                    self._remember_pulse(observation.key, observation.pulse_id)
                elif (
                    instance is not None
                    and instance.resolve_at_ms is not None
                    and now_ms >= instance.resolve_at_ms
                ):
                    transitions.append(
                        self._transition(
                            observation, instance.instance_id, "resolved", now_ms
                        )
                    )
                    self._active.pop(observation.key)
                    instance = None
            elif observation.quality == "known" and observation.active and instance is None:
                state = "pending" if observation.for_ms > 0 else "firing"
                instance = _AlertInstance(
                    self._id_factory(),
                    now_ms,
                    state,
                    for_ms=observation.for_ms,
                    duration_revision=observation.duration_revision,
                )
                self._active[observation.key] = instance
                transitions.append(
                    self._transition(observation, instance.instance_id, state, now_ms)
                )
            elif (
                observation.quality == "known"
                and observation.active
                and instance is not None
                and instance.state == "pending"
            ):
                if instance.duration_revision != observation.duration_revision:
                    instance.for_ms = observation.for_ms
                    instance.duration_revision = observation.duration_revision
                if now_ms - instance.active_at_ms >= instance.for_ms:
                    instance.state = "firing"
                    transitions.append(
                        self._transition(observation, instance.instance_id, "firing", now_ms)
                    )
            elif observation.resolve_after_ms is None and (
                observation.quality == "known"
                and not observation.active
                and instance is not None
            ):
                transitions.append(
                    self._transition(observation, instance.instance_id, "resolved", now_ms)
                )
                self._active.pop(observation.key)
                instance = None
            projected.append(
                {
                    "rule_id": observation.rule_id,
                    "name": observation.name,
                    "severity": observation.severity,
                    "summary": observation.summary,
                    "state": instance.state if instance is not None else "inactive",
                    "instance_id": instance.instance_id if instance is not None else None,
                    "evaluation_status": evaluation_status,
                }
            )
        return projected, transitions

    def export_state(self) -> dict[str, object]:
        """Return restart-safe lifecycle state."""
        return {
            "active": {
                key: {
                    "instance_id": instance.instance_id,
                    "active_at_ms": instance.active_at_ms,
                    "state": instance.state,
                    "resolve_at_ms": instance.resolve_at_ms,
                    "for_ms": instance.for_ms,
                    "last_pulse_id": instance.last_pulse_id,
                    "duration_revision": instance.duration_revision,
                }
                for key, instance in self._active.items()
            },
            "unknown_since": dict(self._unknown_since),
            "consumed_pulses": {
                key: list(pulse_ids)
                for key, pulse_ids in self._consumed_pulses.items()
            },
        }

    def import_state(self, state: dict[str, object]) -> None:
        """Restore lifecycle state exported by ``export_state``."""
        active = state.get("active")
        self._active = {}
        if isinstance(active, dict):
            for key, value in active.items():
                if not isinstance(value, dict):
                    continue
                resolve_at = value.get("resolve_at_ms")
                self._active[str(key)] = _AlertInstance(
                    instance_id=str(value["instance_id"]),
                    active_at_ms=int(value["active_at_ms"]),
                    state=str(value["state"]),
                    resolve_at_ms=int(resolve_at) if resolve_at is not None else None,
                    for_ms=int(value.get("for_ms", 0)),
                    last_pulse_id=(
                        str(value["last_pulse_id"])
                        if value.get("last_pulse_id") is not None
                        else None
                    ),
                    duration_revision=str(value.get("duration_revision", "")),
                )
        unknown_since = state.get("unknown_since")
        self._unknown_since = (
            {str(key): int(value) for key, value in unknown_since.items()}
            if isinstance(unknown_since, dict)
            else {}
        )
        consumed_pulses = state.get("consumed_pulses", {})
        self._consumed_pulses = (
            {
                str(key): [str(pulse_id) for pulse_id in pulse_ids]
                for key, pulse_ids in consumed_pulses.items()
                if isinstance(pulse_ids, list)
            }
            if isinstance(consumed_pulses, dict)
            else {}
        )

    def _remember_pulse(self, key: str, pulse_id: str) -> None:
        consumed = self._consumed_pulses.setdefault(key, [])
        consumed.append(pulse_id)
        del consumed[:-64]

    @staticmethod
    def _transition(
        observation: AlertObservation, instance_id: str, state: str, now_ms: int
    ) -> dict[str, object]:
        reason = (
            observation.inactive_reason
            if state == "resolved" and observation.inactive_reason != "condition_inactive"
            else "resolve_after_elapsed"
            if state == "resolved" and observation.resolve_after_ms is not None
            else observation.inactive_reason
            if state == "resolved"
            else "condition_active"
        )
        return {
            "rule_id": observation.rule_id,
            "name": observation.name,
            "instance_id": instance_id,
            "to": state,
            "at_ms": now_ms,
            "reason": reason,
        }


class AlertStore(Protocol):
    """Persistence port for Alert runtime state."""

    async def async_load(self) -> dict[str, Any] | None: ...

    async def async_save(self, data: dict[str, Any]) -> None: ...


class AlertStateUnavailableError(RuntimeError):
    """Raised when persisted Alert state cannot be trusted."""


def _valid_stored_alert_state(stored: dict[str, Any]) -> bool:
    lifecycle = stored.get("lifecycle")
    alerts = stored.get("alerts")
    if not isinstance(lifecycle, dict) or not isinstance(alerts, list):
        return False
    active = lifecycle.get("active")
    unknown_since = lifecycle.get("unknown_since")
    consumed_pulses = lifecycle.get("consumed_pulses", {})
    if (
        not isinstance(active, dict)
        or not isinstance(unknown_since, dict)
        or not isinstance(consumed_pulses, dict)
    ):
        return False
    for key, value in active.items():
        if (
            not isinstance(key, str)
            or not isinstance(value, dict)
            or not isinstance(value.get("instance_id"), str)
            or not value["instance_id"]
            or value.get("state") not in {"pending", "firing"}
            or not _nonnegative_int(value.get("active_at_ms"))
            or not _optional_nonnegative_int(value.get("resolve_at_ms"))
            or not _nonnegative_int(value.get("for_ms", 0))
        ):
            return False
    if any(
        not isinstance(key, str) or not _nonnegative_int(value)
        for key, value in unknown_since.items()
    ):
        return False
    if any(
        not isinstance(key, str)
        or not isinstance(pulse_ids, list)
        or len(pulse_ids) > 64
        or any(not isinstance(pulse_id, str) or not pulse_id for pulse_id in pulse_ids)
        for key, pulse_ids in consumed_pulses.items()
    ):
        return False
    projected_active: set[str] = set()
    for alert in alerts:
        if not isinstance(alert, dict):
            return False
        rule_id = alert.get("rule_id")
        name = alert.get("name")
        state = alert.get("state")
        instance_id = alert.get("instance_id")
        if (
            not isinstance(rule_id, str)
            or not rule_id
            or not isinstance(name, str)
            or not name
            or alert.get("severity") not in {"info", "warning", "critical"}
            or not isinstance(alert.get("summary"), str)
            or state not in {"inactive", "pending", "firing"}
            or alert.get("evaluation_status") not in {"current", "grace", "stale"}
            or (state == "inactive" and instance_id is not None)
            or (state != "inactive" and (not isinstance(instance_id, str) or not instance_id))
        ):
            return False
        key = alert_definition_key(rule_id, name)
        if state != "inactive":
            persisted = active.get(key)
            if (
                not isinstance(persisted, dict)
                or persisted.get("instance_id") != instance_id
                or persisted.get("state") != state
            ):
                return False
            projected_active.add(key)
    return projected_active == set(active)


def _nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _optional_nonnegative_int(value: Any) -> bool:
    return value is None or _nonnegative_int(value)


class AlertCoordinator:
    """Serialize and durably expose Alert lifecycle state."""

    def __init__(
        self,
        store: AlertStore,
        *,
        id_factory: Callable[[], str] | None = None,
        publish: Callable[[], None] | None = None,
    ) -> None:
        self._store = store
        self._lifecycle = AlertLifecycle(id_factory=id_factory)
        self._alerts: list[dict[str, object]] = []
        self._generation = 0
        self._lock = asyncio.Lock()
        self._publish = publish
        self._desired_snapshot: dict[str, Any] | None = None
        self._dirty = False
        self._current_error: str | None = None
        self.available = True

    async def async_load(self) -> None:
        """Load the latest durable Alert state."""
        try:
            stored = await self._store.async_load()
        except Exception as err:  # noqa: BLE001 - isolate Alert Store failure
            self.available = False
            self._current_error = str(err)
            return
        if stored is None:
            return
        if (
            not isinstance(stored, dict)
            or stored.get("version") != 1
            or not isinstance(stored.get("generation"), int)
            or isinstance(stored.get("generation"), bool)
            or not isinstance(stored.get("lifecycle"), dict)
            or not isinstance(stored.get("alerts"), list)
            or not _valid_stored_alert_state(stored)
        ):
            self.available = False
            self._current_error = "corrupt_alert_store"
            return
        lifecycle = stored.get("lifecycle")
        alerts = stored.get("alerts")
        generation = stored.get("generation")
        try:
            self._lifecycle.import_state(lifecycle)
            self._alerts = [dict(alert) for alert in alerts if isinstance(alert, dict)]
            self._generation = generation
        except (KeyError, TypeError, ValueError, OverflowError):
            self.available = False
            self._current_error = "corrupt_alert_store"
            self._alerts = []
            return
        self._desired_snapshot = stored

    async def async_observe(
        self, observations: Iterable[AlertObservation], *, now_ms: int
    ) -> list[dict[str, object]]:
        """Advance and persist one authored observation generation."""
        async with self._lock:
            if not self.available:
                raise AlertStateUnavailableError("Alert state is unavailable")
            current = list(observations)
            keys = {observation.key for observation in current}
            for alert in self._alerts:
                key = alert_definition_key(str(alert.get("rule_id")), str(alert.get("name")))
                if alert.get("state") == "inactive" or key in keys:
                    continue
                current.append(
                    AlertObservation(
                        rule_id=str(alert["rule_id"]),
                        name=str(alert["name"]),
                        severity=str(alert["severity"]),
                        summary=str(alert["summary"]),
                        active=False,
                        inactive_reason="definition_removed",
                    )
                )
            alerts, transitions = self._lifecycle.advance(current, now_ms=now_ms)
            changed = alerts != self._alerts or bool(transitions)
            if not changed and not self._dirty:
                return []
            if changed:
                self._generation += 1
                self._alerts = alerts
                self._desired_snapshot = {
                    "version": 1,
                    "generation": self._generation,
                    "lifecycle": self._lifecycle.export_state(),
                    "alerts": alerts,
                }
            assert self._desired_snapshot is not None
            try:
                await self._store.async_save(self._desired_snapshot)
            except Exception as err:  # noqa: BLE001 - health exposes Store failure
                self._dirty = True
                self._current_error = str(err)
                if self._publish is not None:
                    self._publish()
                return transitions
            self._dirty = False
            self._current_error = None
            if self._publish is not None:
                self._publish()
            return transitions

    def list_alerts(self) -> list[dict[str, object]]:
        """Return the current Alert-definition projection."""
        return [dict(alert) for alert in self._alerts]

    def health(self) -> dict[str, object]:
        """Return Alert persistence health."""
        return {
            "status": (
                "unhealthy"
                if not self.available
                else "degraded"
                if self._current_error is not None
                else "ok"
            ),
            "dirty": self._dirty,
            "current_error": self._current_error,
        }
