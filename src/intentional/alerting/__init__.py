"""Pure Alert lifecycle policy."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterable
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol
from uuid import uuid4

from intentional.alerting.capabilities import CapabilityRuntime
from intentional.alerting.delivery import NotificationRuntime
from intentional.alerting.policy import match_alert_labels

ALERT_STATE_VERSION = 2
RETENTION_MS = 30 * 24 * 60 * 60 * 1_000


@dataclass(frozen=True)
class AlertObservation:
    """Current activity for one authored Alert definition."""

    rule_id: str
    name: str
    severity: str
    summary: str
    active: bool
    observed_at_ms: int = 0
    labels: dict[str, str] = field(default_factory=dict)
    annotations: dict[str, str] = field(default_factory=dict)
    definition_revision: str = ""
    escalations: tuple[tuple[int, str], ...] = ()
    for_ms: int = 0
    quality: str = "known"
    stale_after_ms: int = 120_000
    resolve_after_ms: int | None = None
    inactive_reason: str = "condition_inactive"
    pulse_id: str | None = None
    source_timestamp_ms: int | None = None
    duration_revision: str = ""
    presentation_degraded: bool = False

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
    firing_at_ms: int | None = None
    resolve_at_ms: int | None = None
    for_ms: int = 0
    last_pulse_id: str | None = None
    duration_revision: str = ""
    severity: str = ""


class AlertLifecycle:
    """Advance Alert instances from authored observations."""

    def __init__(self, *, id_factory: Callable[[], str] | None = None) -> None:
        self._id_factory = id_factory or (lambda: str(uuid4()))
        self._active: dict[str, _AlertInstance] = {}
        self._unknown_since: dict[str, int] = {}
        self._consumed_pulses: dict[str, list[str]] = {}
        self._pulse_watermarks: dict[str, dict[str, int]] = {}
        self._stale: set[str] = set()

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
                self._stale.discard(observation.key)
                evaluation_status = "current"
            stale_episode_started = evaluation_status == "stale" and observation.key not in self._stale
            if evaluation_status == "stale":
                self._stale.add(observation.key)
            if observation.resolve_after_ms is not None:
                new_pulse = (
                    observation.quality == "known"
                    and observation.active
                    and observation.pulse_id is not None
                    and self._is_new_pulse(observation.key, observation.pulse_id)
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
                        firing_at_ms=now_ms,
                        resolve_at_ms=(
                            observation.source_timestamp_ms
                            if observation.source_timestamp_ms is not None
                            else now_ms
                        )
                        + observation.resolve_after_ms,
                        last_pulse_id=observation.pulse_id,
                        severity=observation.severity,
                    )
                    self._active[observation.key] = instance
                    self._remember_pulse(observation.key, observation.pulse_id)
                    transitions.append(
                        self._transition(
                            observation, instance.instance_id, "firing", now_ms
                        )
                    )
                    if now_ms >= instance.resolve_at_ms:
                        transitions.append(
                            self._transition(
                                observation, instance.instance_id, "resolved", now_ms
                            )
                        )
                        self._active.pop(observation.key)
                        instance = None
                elif (
                    new_pulse
                    and instance is not None
                ):
                    instance.resolve_at_ms = (
                        observation.source_timestamp_ms
                        if observation.source_timestamp_ms is not None
                        else now_ms
                    ) + observation.resolve_after_ms
                    instance.last_pulse_id = observation.pulse_id
                    self._remember_pulse(observation.key, observation.pulse_id)
                    if now_ms >= instance.resolve_at_ms:
                        transitions.append(
                            self._transition(
                                observation, instance.instance_id, "resolved", now_ms
                            )
                        )
                        self._active.pop(observation.key)
                        instance = None
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
                    firing_at_ms=now_ms if state == "firing" else None,
                    for_ms=observation.for_ms,
                    duration_revision=observation.duration_revision,
                    severity=observation.severity,
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
                    instance.firing_at_ms = now_ms
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
            if instance is not None and instance.state == "firing":
                severity_rank = {"info": 0, "warning": 1, "critical": 2}
                effective_severity = observation.severity
                elapsed = now_ms - instance.active_at_ms
                for after_ms, severity in observation.escalations:
                    if elapsed >= after_ms:
                        effective_severity = severity
                if instance.severity and (
                    severity_rank[effective_severity] > severity_rank[instance.severity]
                ):
                    transitions.append(
                        {
                            **self._transition(
                                observation, instance.instance_id, "firing", now_ms
                            ),
                            "reason": "severity_escalation",
                            "from_severity": instance.severity,
                            "severity": effective_severity,
                        }
                    )
                instance.severity = effective_severity
            next_escalation = (
                min(
                    (
                        instance.active_at_ms + after_ms
                        for after_ms, _severity in observation.escalations
                        if instance is not None
                        and instance.state == "firing"
                        and instance.active_at_ms + after_ms > now_ms
                    ),
                    default=None,
                )
                if instance is not None
                else None
            )
            effective_labels = dict(observation.labels)
            if instance is not None and instance.severity:
                effective_labels["severity"] = instance.severity
            projected.append(
                {
                    "rule_id": observation.rule_id,
                    "name": observation.name,
                    "severity": (
                        instance.severity
                        if instance is not None and instance.severity
                        else observation.severity
                    ),
                    "summary": observation.summary,
                    "state": instance.state if instance is not None else "inactive",
                    "instance_id": instance.instance_id if instance is not None else None,
                    "evaluation_status": evaluation_status,
                    "observed_at_ms": observation.observed_at_ms,
                    "active_at_ms": (
                        instance.active_at_ms if instance is not None else None
                    ),
                    "firing_at_ms": (
                        instance.firing_at_ms if instance is not None else None
                    ),
                    "next_deadline_ms": _minimum_deadline(
                        instance.resolve_at_ms
                        if instance is not None and instance.resolve_at_ms is not None
                        else instance.active_at_ms + instance.for_ms
                        if instance is not None and instance.state == "pending"
                        else None,
                        unknown_since + observation.stale_after_ms
                        if observation.quality == "unknown"
                        and evaluation_status == "grace"
                        else None,
                        next_escalation,
                    ),
                    "labels": effective_labels,
                    "annotations": dict(observation.annotations),
                    "definition_revision": observation.definition_revision,
                    "presentation_degraded": observation.presentation_degraded,
                    "stale_episode_started": stale_episode_started,
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
                    "firing_at_ms": instance.firing_at_ms,
                    "resolve_at_ms": instance.resolve_at_ms,
                    "for_ms": instance.for_ms,
                    "last_pulse_id": instance.last_pulse_id,
                    "duration_revision": instance.duration_revision,
                    "severity": instance.severity,
                }
                for key, instance in self._active.items()
            },
            "unknown_since": dict(self._unknown_since),
            "consumed_pulses": {
                key: list(pulse_ids)
                for key, pulse_ids in self._consumed_pulses.items()
            },
            "pulse_watermarks": {
                key: dict(watermarks)
                for key, watermarks in self._pulse_watermarks.items()
            },
            "stale": sorted(self._stale),
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
                    firing_at_ms=(
                        int(value["firing_at_ms"])
                        if value.get("firing_at_ms") is not None
                        else None
                    ),
                    resolve_at_ms=int(resolve_at) if resolve_at is not None else None,
                    for_ms=int(value.get("for_ms", 0)),
                    last_pulse_id=(
                        str(value["last_pulse_id"])
                        if value.get("last_pulse_id") is not None
                        else None
                    ),
                    duration_revision=str(value.get("duration_revision", "")),
                    severity=str(value.get("severity", "")),
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
        pulse_watermarks = state.get("pulse_watermarks", {})
        self._pulse_watermarks = (
            {
                str(key): {str(stream): int(value) for stream, value in watermarks.items()}
                for key, watermarks in pulse_watermarks.items()
                if isinstance(watermarks, dict)
            }
            if isinstance(pulse_watermarks, dict)
            else {}
        )
        stale = state.get("stale", [])
        self._stale = {str(key) for key in stale} if isinstance(stale, list) else set()

    def _remember_pulse(self, key: str, pulse_id: str) -> None:
        parsed = _pulse_streams(pulse_id)
        if parsed is not None:
            watermarks = self._pulse_watermarks.setdefault(key, {})
            for stream, sequence in parsed:
                watermarks[stream] = max(sequence, watermarks.get(stream, -1))
            while len(watermarks) > 64:
                del watermarks[next(iter(watermarks))]
            return
        consumed = self._consumed_pulses.setdefault(key, [])
        consumed.append(pulse_id)
        del consumed[:-64]

    def _is_new_pulse(self, key: str, pulse_id: str) -> bool:
        parsed = _pulse_streams(pulse_id)
        if parsed is None:
            return pulse_id not in self._consumed_pulses.get(key, [])
        watermarks = self._pulse_watermarks.get(key, {})
        return any(sequence > watermarks.get(stream, -1) for stream, sequence in parsed)

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


def _pulse_streams(pulse_id: str) -> list[tuple[str, int]] | None:
    streams: list[tuple[str, int]] = []
    for token in pulse_id.split("|"):
        parts = token.rsplit(":", 2)
        if len(parts) != 3:
            return None
        entity_id, epoch, raw_sequence = parts
        try:
            sequence = int(raw_sequence)
        except ValueError:
            return None
        if not entity_id or not epoch or sequence < 0:
            return None
        streams.append((f"{entity_id}:{epoch}", sequence))
    return streams or None


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
    pulse_watermarks = lifecycle.get("pulse_watermarks", {})
    if (
        not isinstance(active, dict)
        or len(active) > 256
        or len(alerts) > 256
        or not isinstance(unknown_since, dict)
        or len(unknown_since) > 256
        or not isinstance(consumed_pulses, dict)
        or not isinstance(pulse_watermarks, dict)
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
    if len(pulse_watermarks) > 256 or any(
        not isinstance(key, str)
        or not isinstance(watermarks, dict)
        or len(watermarks) > 64
        or any(
            not isinstance(stream, str) or not _nonnegative_int(sequence)
            for stream, sequence in watermarks.items()
        )
        for key, watermarks in pulse_watermarks.items()
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
    observations = stored.get("observations", [])
    acknowledgments = stored.get("acknowledgments", {})
    silences = stored.get("silences", [])
    if (
        not isinstance(observations, list)
        or len(observations) > 256
        or not isinstance(acknowledgments, dict)
        or len(acknowledgments) > 1_000
        or not isinstance(silences, list)
        or len(silences) > 1_024
    ):
        return False
    if any(
        not isinstance(instance_id, str)
        or not isinstance(record, dict)
        or not isinstance(record.get("actor"), str)
        or not _nonnegative_int(record.get("at_ms"))
        or (
            record.get("comment") is not None
            and not isinstance(record.get("comment"), str)
        )
        or record.get("severity") not in {"info", "warning", "critical"}
        for instance_id, record in acknowledgments.items()
    ):
        return False
    if any(
        not isinstance(silence, dict)
        or not isinstance(silence.get("silence_id"), str)
        or not isinstance(silence.get("actor"), str)
        or not isinstance(silence.get("reason"), str)
        or not _nonnegative_int(silence.get("created_at_ms"))
        or not _nonnegative_int(silence.get("expires_at_ms"))
        or (
            not isinstance(silence.get("instance_id"), str)
            and not isinstance(silence.get("matchers"), list)
        )
        or (
            isinstance(silence.get("matchers"), list)
            and (
                len(silence["matchers"]) > 16
                or any(not isinstance(matcher, str) for matcher in silence["matchers"])
                or not isinstance(silence.get("match_all"), bool)
            )
        )
        or silence["expires_at_ms"] <= silence["created_at_ms"]
        for silence in silences
    ):
        return False
    for silence in silences:
        matchers = silence.get("matchers")
        if not isinstance(matchers, list):
            continue
        try:
            match_alert_labels(matchers, {})
        except ValueError:
            return False
    return projected_active == set(active)


def _nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _optional_nonnegative_int(value: Any) -> bool:
    return value is None or _nonnegative_int(value)


def _minimum_deadline(*values: int | None) -> int | None:
    present = [value for value in values if value is not None]
    return min(present) if present else None


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
        self._id_factory = id_factory
        self._lifecycle = AlertLifecycle(id_factory=id_factory)
        self._alerts: list[dict[str, object]] = []
        self._generation = 0
        self._lock = asyncio.Lock()
        self._publish = publish
        self._desired_snapshot: dict[str, Any] | None = None
        self._dirty = False
        self._current_error: str | None = None
        self._audit: list[dict[str, object]] = []
        self._instances: list[dict[str, object]] = []
        self._observations: dict[str, AlertObservation] = {}
        self._notifications = NotificationRuntime()
        self._policy_contents: str | None = None
        self._default_timezone = "UTC"
        self._acknowledgments: dict[str, dict[str, object]] = {}
        self._silences: list[dict[str, object]] = []
        self._failed_payload: dict[str, Any] | None = None
        self._capabilities: CapabilityRuntime | None = None
        self._entry_id: str | None = None
        self._startup_pending: set[str] = set()
        self.available = True

    async def async_load(self) -> None:
        """Load the latest durable Alert state."""
        try:
            stored = await self._store.async_load()
        except Exception as err:  # noqa: BLE001 - isolate Alert Store failure
            self.available = False
            self._current_error = type(err).__name__
            return
        if stored is None:
            return
        if (
            not isinstance(stored, dict)
            or stored.get("version") not in {1, ALERT_STATE_VERSION}
            or not isinstance(stored.get("generation"), int)
            or isinstance(stored.get("generation"), bool)
            or not isinstance(stored.get("lifecycle"), dict)
            or not isinstance(stored.get("alerts"), list)
            or not isinstance(stored.get("audit", []), list)
            or not isinstance(stored.get("instances", []), list)
            or not _valid_stored_alert_state(stored)
        ):
            self.available = False
            self._current_error = "corrupt_alert_store"
            self._failed_payload = deepcopy(stored) if isinstance(stored, dict) else None
            return
        lifecycle = stored.get("lifecycle")
        alerts = stored.get("alerts")
        generation = stored.get("generation")
        try:
            self._lifecycle.import_state(lifecycle)
            self._alerts = [dict(alert) for alert in alerts if isinstance(alert, dict)]
            self._generation = generation
            self._audit = [
                dict(event)
                for event in stored.get("audit", [])[-10_000:]
                if isinstance(event, dict)
            ]
            self._instances = [
                dict(instance)
                for instance in stored.get("instances", [])[-1_000:]
                if isinstance(instance, dict)
            ]
            self._observations = {}
            for raw_observation in stored.get("observations", []):
                if not isinstance(raw_observation, dict):
                    continue
                observation = AlertObservation(**raw_observation)
                self._observations[observation.key] = observation
            notifications = stored.get("notifications")
            if isinstance(notifications, dict):
                self._notifications.import_state(notifications)
            self._acknowledgments = {
                str(instance_id): dict(record)
                for instance_id, record in stored.get("acknowledgments", {}).items()
                if isinstance(record, dict)
            }
            self._silences = [
                dict(record)
                for record in stored.get("silences", [])
                if isinstance(record, dict)
            ]
            startup_pending = stored.get("startup_pending", [])
            self._startup_pending = (
                {str(key) for key in startup_pending}
                if isinstance(startup_pending, list)
                else set()
            )
            if self._capabilities is not None and isinstance(
                stored.get("capabilities"), dict
            ):
                try:
                    self._capabilities.import_state(stored["capabilities"])
                except (KeyError, TypeError, ValueError, OverflowError):
                    self._capabilities = None
                    self._entry_id = None
                    self._current_error = "capability_state_invalid"
        except (KeyError, TypeError, ValueError, OverflowError):
            self.available = False
            self._current_error = "corrupt_alert_store"
            self._failed_payload = deepcopy(stored)
            self._alerts = []
            return
        if stored.get("version") == 1:
            self._desired_snapshot = self._current_snapshot()
            await self._save_current_snapshot()
        else:
            self._desired_snapshot = stored

    async def async_observe(
        self, observations: Iterable[AlertObservation], *, now_ms: int
    ) -> list[dict[str, object]]:
        """Advance and persist one authored observation generation."""
        async with self._lock:
            if not self.available:
                raise AlertStateUnavailableError("Alert state is unavailable")
            self._silences = [
                silence
                for silence in self._silences
                if int(silence["expires_at_ms"]) > now_ms
            ]
            cutoff = now_ms - RETENTION_MS
            self._audit = [
                event for event in self._audit if int(event.get("at_ms", now_ms)) >= cutoff
            ][-10_000:]
            self._instances = [
                instance
                for instance in self._instances
                if int(instance.get("resolved_at_ms", now_ms)) >= cutoff
            ][-1_000:]
            current = list(observations)
            for observation in current:
                if observation.quality == "known":
                    self._startup_pending.discard(observation.key)
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
            self._observations = {
                observation.key: observation for observation in current
            }
            previous_by_instance = {
                str(alert["instance_id"]): alert
                for alert in self._alerts
                if alert.get("instance_id") is not None
            }
            alerts, transitions = self._lifecycle.advance(current, now_ms=now_ms)
            for transition in transitions:
                if transition.get("reason") == "severity_escalation":
                    self._acknowledgments.pop(str(transition["instance_id"]), None)
            active_instance_ids = {
                str(alert["instance_id"])
                for alert in alerts
                if alert.get("state") == "firing" and alert.get("instance_id") is not None
            }
            self._acknowledgments = {
                instance_id: record
                for instance_id, record in self._acknowledgments.items()
                if instance_id in active_instance_ids
            }
            self._apply_operational_state(alerts, now_ms)
            notifications_before = self._notifications.export_state()
            if self._policy_contents is not None:
                self._notifications.reconcile(
                    alerts,
                    self._policy_contents,
                    now_ms=now_ms,
                    default_timezone=self._default_timezone,
                )
            for alert in alerts:
                alert["stale_episode_started"] = False
            changed = (
                alerts != self._alerts
                or bool(transitions)
                or self._notifications.export_state() != notifications_before
            )
            if not changed and not self._dirty:
                return []
            if changed:
                self._generation += 1
                for transition in transitions:
                    self._audit.append(dict(transition))
                    if transition.get("to") != "resolved":
                        continue
                    instance_id = str(transition["instance_id"])
                    previous = previous_by_instance.get(instance_id, {})
                    self._instances.append(
                        {
                            "instance_id": instance_id,
                            "rule_id": transition["rule_id"],
                            "name": transition["name"],
                            "state": "resolved",
                            "active_at_ms": previous.get("active_at_ms"),
                            "firing_at_ms": previous.get("firing_at_ms"),
                            "resolved_at_ms": transition["at_ms"],
                            "reason": transition["reason"],
                        }
                    )
                del self._audit[:-10_000]
                del self._instances[:-1_000]
                self._alerts = alerts
                self._desired_snapshot = {
                    "version": ALERT_STATE_VERSION,
                    "generation": self._generation,
                    "lifecycle": self._lifecycle.export_state(),
                    "alerts": alerts,
                    "audit": self._audit,
                    "instances": self._instances,
                    "observations": [
                        asdict(observation)
                        for observation in self._observations.values()
                    ],
                    "notifications": self._notifications.export_state(),
                    "acknowledgments": self._acknowledgments,
                    "silences": self._silences,
                    "capabilities": (
                        self._capabilities.export_state()
                        if self._capabilities is not None
                        else {}
                    ),
                    "startup_pending": sorted(self._startup_pending),
                }
            assert self._desired_snapshot is not None
            try:
                await self._store.async_save(self._desired_snapshot)
            except Exception as err:  # noqa: BLE001 - health exposes Store failure
                self._dirty = True
                self._current_error = type(err).__name__
                if self._publish is not None:
                    self._publish()
                return transitions
            self._dirty = False
            self._current_error = None
            if self._publish is not None:
                self._publish()
            return transitions

    async def async_advance(self, *, now_ms: int) -> list[dict[str, object]]:
        """Advance durable Alert deadlines without requiring fresh Rule evaluation."""
        return await self.async_observe(self._observations.values(), now_ms=now_ms)

    async def async_acknowledge(
        self,
        instance_id: str,
        *,
        actor: str,
        now_ms: int,
        comment: str | None = None,
    ) -> dict[str, object]:
        if not actor or comment is not None and len(comment.encode()) > 1_024:
            raise ValueError("invalid acknowledgment actor or comment")
        async with self._lock:
            existing = self._acknowledgments.get(instance_id)
            if existing is not None:
                return dict(existing)
            snapshot = self._operational_snapshot()
            alert = self._firing_instance(instance_id)
            record: dict[str, object] = {
                "actor": actor,
                "at_ms": now_ms,
                "comment": comment,
                "severity": alert.get("severity"),
            }
            self._acknowledgments[instance_id] = record
            self._audit.append(
                {
                    "instance_id": instance_id,
                    "event": "acknowledged",
                    "at_ms": now_ms,
                    "actor": actor,
                }
            )
            self._apply_operational_state(self._alerts, now_ms)
            self._reconcile_notifications(now_ms)
            await self._persist_or_restore(snapshot)
            return dict(record)

    async def async_revoke_acknowledgment(
        self, instance_id: str, *, actor: str, now_ms: int
    ) -> bool:
        async with self._lock:
            snapshot = self._operational_snapshot()
            if self._acknowledgments.pop(instance_id, None) is None:
                return False
            self._audit.append(
                {
                    "instance_id": instance_id,
                    "event": "acknowledgment_revoked",
                    "at_ms": now_ms,
                    "actor": actor,
                }
            )
            self._apply_operational_state(self._alerts, now_ms)
            self._reconcile_notifications(now_ms)
            await self._persist_or_restore(snapshot)
            return True

    async def async_create_instance_silence(
        self,
        instance_id: str,
        *,
        actor: str,
        reason: str,
        now_ms: int,
        duration_ms: int,
    ) -> dict[str, object]:
        if duration_ms <= 0 or duration_ms > 86_400_000:
            raise ValueError("instance Silence duration must be between 1ms and 24h")
        if not reason or len(reason.encode()) > 256:
            raise ValueError("invalid Silence reason or capacity exhausted")
        async with self._lock:
            if len(self._silences) >= 1_024:
                raise ValueError("invalid Silence reason or capacity exhausted")
            snapshot = self._operational_snapshot()
            self._firing_instance(instance_id)
            record: dict[str, object] = {
                "silence_id": str(uuid4()),
                "instance_id": instance_id,
                "actor": actor,
                "reason": reason,
                "created_at_ms": now_ms,
                "expires_at_ms": now_ms + duration_ms,
            }
            self._silences.append(record)
            self._apply_operational_state(self._alerts, now_ms)
            self._reconcile_notifications(now_ms)
            await self._persist_or_restore(snapshot)
            return dict(record)

    async def async_create_matcher_silence(
        self,
        matchers: list[str],
        *,
        match_all: bool,
        actor: str,
        reason: str,
        now_ms: int,
        duration_ms: int,
    ) -> dict[str, object]:
        if duration_ms <= 0 or duration_ms > 31_536_000_000:
            raise ValueError("matcher Silence duration must be at most one year")
        if not matchers and not match_all:
            raise ValueError("match-all Silence requires match_all confirmation")
        if len(matchers) > 16:
            raise ValueError("Silence may contain at most 16 matchers")
        if not reason or len(reason.encode()) > 256:
            raise ValueError("invalid Silence reason or capacity exhausted")
        match_alert_labels(matchers, {})
        async with self._lock:
            if len(self._silences) >= 1_024:
                raise ValueError("invalid Silence reason or capacity exhausted")
            snapshot = self._operational_snapshot()
            record: dict[str, object] = {
                "silence_id": str(uuid4()),
                "matchers": list(matchers),
                "match_all": match_all,
                "actor": actor,
                "reason": reason,
                "created_at_ms": now_ms,
                "expires_at_ms": now_ms + duration_ms,
            }
            self._silences.append(record)
            self._apply_operational_state(self._alerts, now_ms)
            self._reconcile_notifications(now_ms)
            await self._persist_or_restore(snapshot)
            return dict(record)

    async def async_delete_silence(
        self, silence_id: str, *, actor: str, now_ms: int
    ) -> bool:
        async with self._lock:
            snapshot = self._operational_snapshot()
            retained = [
                silence
                for silence in self._silences
                if silence.get("silence_id") != silence_id
            ]
            if len(retained) == len(self._silences):
                return False
            self._silences = retained
            self._audit.append(
                {
                    "silence_id": silence_id,
                    "event": "silence_deleted",
                    "at_ms": now_ms,
                    "actor": actor,
                }
            )
            self._apply_operational_state(self._alerts, now_ms)
            self._reconcile_notifications(now_ms)
            await self._persist_or_restore(snapshot)
            return True

    async def async_consume_mobile_action(
        self,
        *,
        record_id: str,
        token: str,
        operation: str,
        instance_id: str,
        actor: str | None,
        now_ms: int,
    ) -> None:
        if self._capabilities is None or self._entry_id is None:
            raise ValueError("mobile capabilities are unavailable")
        async with self._lock:
            snapshot = self._operational_snapshot()
            self._firing_instance(instance_id)
            if operation == "silence" and len(self._silences) >= 1_024:
                raise ValueError("Silence capacity exhausted")
            self._capabilities.consume(
                record_id,
                token,
                actor=actor,
                now_ms=now_ms,
                entry_id=self._entry_id,
                instance_id=instance_id,
                operation=operation,
            )
            assert actor is not None
            if operation == "acknowledge":
                self._acknowledgments.setdefault(
                    instance_id,
                    {
                        "actor": actor,
                        "at_ms": now_ms,
                        "comment": "Mobile notification action",
                        "severity": self._firing_instance(instance_id).get("severity"),
                    },
                )
            elif operation == "silence":
                self._silences.append(
                    {
                        "silence_id": str(uuid4()),
                        "instance_id": instance_id,
                        "actor": actor,
                        "reason": "Mobile notification action",
                        "created_at_ms": now_ms,
                        "expires_at_ms": now_ms + 3_600_000,
                    }
                )
            else:
                raise ValueError("unsupported mobile Alert operation")
            self._apply_operational_state(self._alerts, now_ms)
            self._reconcile_notifications(now_ms)
            await self._persist_or_restore(snapshot)

    async def async_set_policy(
        self,
        contents: str,
        *,
        now_ms: int,
        default_timezone: str = "UTC",
    ) -> None:
        """Apply routing policy and durably reconcile current firing Alerts."""
        async with self._lock:
            if not self.available:
                raise AlertStateUnavailableError("Alert state is unavailable")
            previous_policy = self._policy_contents
            previous_timezone = self._default_timezone
            previous_notifications = self._notifications.export_state()
            previous_alerts = deepcopy(self._alerts)
            self._policy_contents = contents
            self._default_timezone = default_timezone
            self._apply_operational_state(self._alerts, now_ms)
            self._notifications.reconcile(
                self._alerts,
                contents,
                now_ms=now_ms,
                default_timezone=default_timezone,
            )
            self._generation += 1
            self._desired_snapshot = self._current_snapshot()
            if not await self._save_current_snapshot():
                self._policy_contents = previous_policy
                self._default_timezone = previous_timezone
                self._notifications.import_state(previous_notifications)
                self._alerts = previous_alerts
                raise AlertStateUnavailableError("Alert policy could not be persisted")

    async def async_notification_advance(
        self, *, now_ms: int
    ) -> list[dict[str, Any]]:
        """Plan due obligations and persist them before dispatch exposure."""
        async with self._lock:
            due = self._notifications.advance(now_ms=now_ms)
            if not due:
                return []
            self._generation += 1
            self._desired_snapshot = self._current_snapshot()
            if not await self._save_current_snapshot():
                return []
            return due

    async def async_begin_notification_dispatch(
        self, obligation_id: str, *, now_ms: int
    ) -> dict[str, Any] | None:
        """Persist an in-flight attempt before allowing an external call."""
        async with self._lock:
            notifications_snapshot = self._notifications.export_state()
            capabilities_snapshot = (
                self._capabilities.export_state()
                if self._capabilities is not None
                else None
            )
            obligation = next(
                (
                    item
                    for item in self._notifications.list_obligations()
                    if item["obligation_id"] == obligation_id
                ),
                None,
            )
            if obligation is None:
                return None
            if (
                self._capabilities is not None
                and self._entry_id is not None
                and obligation["destination"].get("type") == "notify_entity"
                and not obligation.get("capability_record_ids")
                and len(obligation.get("member_instance_ids", [])) == 1
            ):
                instance_id = str(obligation["member_instance_ids"][0])
                record_ids = []
                for operation in ("acknowledge", "silence"):
                    issued = self._capabilities.issue(
                        entry_id=self._entry_id,
                        instance_id=instance_id,
                        operation=operation,
                        destination_id=str(obligation["destination_id"]),
                        now_ms=now_ms,
                        expires_at_ms=now_ms + 86_400_000,
                    )
                    record_ids.append(issued["record_id"])
                self._notifications.attach_capabilities(obligation_id, record_ids)
            if not self._notifications.mark_in_flight(
                obligation_id, now_ms=now_ms
            ):
                return None
            self._generation += 1
            self._desired_snapshot = self._current_snapshot()
            if not await self._save_current_snapshot():
                self._notifications.import_state(notifications_snapshot)
                if self._capabilities is not None and capabilities_snapshot is not None:
                    self._capabilities.import_state(capabilities_snapshot)
                self._desired_snapshot = self._current_snapshot()
                return None
            persisted = next(
                (
                    item
                    for item in self._notifications.list_obligations()
                    if item["obligation_id"] == obligation_id
                ),
                None,
            )
            if persisted is not None and self._capabilities is not None:
                persisted["capabilities"] = [
                    {
                        **self._capabilities.record(record_id),
                        "token": self._capabilities.token(record_id),
                    }
                    for record_id in persisted.get("capability_record_ids", [])
                ]
            return persisted

    async def async_accept_notification(
        self, obligation_id: str, *, now_ms: int
    ) -> None:
        async with self._lock:
            self._notifications.accept(obligation_id, now_ms=now_ms)
            self._generation += 1
            self._desired_snapshot = self._current_snapshot()
            await self._save_current_snapshot()

    async def async_reject_notification(
        self,
        obligation_id: str,
        *,
        now_ms: int,
        error_class: str,
    ) -> None:
        async with self._lock:
            self._notifications.reject(
                obligation_id, now_ms=now_ms, error_class=error_class
            )
            self._generation += 1
            self._desired_snapshot = self._current_snapshot()
            await self._save_current_snapshot()

    def next_deadline_ms(self) -> int | None:
        """Return the earliest pending lifecycle or evidence deadline."""
        deadlines = [
            int(alert["next_deadline_ms"])
            for alert in self._alerts
            if alert.get("next_deadline_ms") is not None
        ]
        notification_deadline = self._notifications.next_deadline_ms()
        if notification_deadline is not None:
            deadlines.append(notification_deadline)
        deadlines.extend(int(silence["expires_at_ms"]) for silence in self._silences)
        return min(deadlines) if deadlines else None

    def list_alerts(self) -> list[dict[str, object]]:
        """Return the current Alert-definition projection."""
        return [dict(alert) for alert in self._alerts]

    def list_instances(self) -> list[dict[str, object]]:
        """Return retained resolved Alert instances, newest first."""
        return [dict(instance) for instance in reversed(self._instances)]

    def list_audit(self) -> list[dict[str, object]]:
        """Return bounded lifecycle transition audit in occurrence order."""
        return [dict(event) for event in self._audit]

    def list_notifications(self) -> list[dict[str, Any]]:
        """Return immutable Notification obligations and current statuses."""
        return self._notifications.list_obligations()

    def notification_dead_letter_totals(self) -> dict[str, int]:
        return self._notifications.dead_letter_totals()

    def list_silences(self) -> list[dict[str, object]]:
        return [dict(silence) for silence in self._silences]

    def failed_payload(self) -> dict[str, Any] | None:
        return deepcopy(self._failed_payload)

    def configure_capabilities(
        self, secret: bytes, *, entry_id: str
    ) -> None:
        state = (
            self._desired_snapshot.get("capabilities", {})
            if self._desired_snapshot is not None
            else {}
        )
        self._capabilities = CapabilityRuntime(secret)
        self._entry_id = entry_id
        if isinstance(state, dict):
            self._capabilities.import_state(state)

    def begin_startup_barrier(self) -> None:
        """Require one known post-sync observation for state-observed definitions."""
        self._startup_pending = {
            observation.key
            for observation in self._observations.values()
            if observation.resolve_after_ms is None
        }

    async def async_reset(self, *, confirmed: bool) -> None:
        """Explicitly replace unavailable/corrupt Alert state with a fresh generation."""
        if not confirmed:
            raise ValueError("Alert reset requires confirmation")
        async with self._lock:
            generation = self._generation + 1
            lifecycle = AlertLifecycle(id_factory=self._id_factory)
            notifications = NotificationRuntime()
            if self._capabilities is not None:
                self._capabilities.import_state({"records": []})
            snapshot = {
                "version": ALERT_STATE_VERSION,
                "generation": generation,
                "lifecycle": lifecycle.export_state(),
                "alerts": [],
                "audit": [],
                "instances": [],
                "observations": [],
                "notifications": notifications.export_state(),
                "acknowledgments": {},
                "silences": [],
                "capabilities": (
                    self._capabilities.export_state()
                    if self._capabilities is not None
                    else {}
                ),
            }
            await self._store.async_save(snapshot)
            self._lifecycle = lifecycle
            self._notifications = notifications
            self._alerts = []
            self._audit = []
            self._instances = []
            self._observations = {}
            self._acknowledgments = {}
            self._silences = []
            self._generation = generation
            self._desired_snapshot = snapshot
            self._dirty = False
            self._current_error = None
            self._failed_payload = None
            self.available = True

    def _current_snapshot(self) -> dict[str, Any]:
        return {
            "version": ALERT_STATE_VERSION,
            "generation": self._generation,
            "lifecycle": self._lifecycle.export_state(),
            "alerts": self._alerts,
            "audit": self._audit,
            "instances": self._instances,
            "observations": [
                asdict(observation) for observation in self._observations.values()
            ],
            "notifications": self._notifications.export_state(),
            "acknowledgments": self._acknowledgments,
            "silences": self._silences,
            "capabilities": (
                self._capabilities.export_state()
                if self._capabilities is not None
                else {}
            ),
            "startup_pending": sorted(self._startup_pending),
        }

    async def _save_current_snapshot(self) -> bool:
        assert self._desired_snapshot is not None
        try:
            await self._store.async_save(self._desired_snapshot)
        except Exception as err:  # noqa: BLE001 - health exposes Store failure
            self._dirty = True
            self._current_error = type(err).__name__
            if self._publish is not None:
                self._publish()
            return False
        self._dirty = False
        self._current_error = None
        if self._publish is not None:
            self._publish()
        return True

    async def _persist_operational_mutation(self) -> None:
        self._generation += 1
        self._desired_snapshot = self._current_snapshot()
        if not await self._save_current_snapshot():
            raise AlertStateUnavailableError("Alert mutation could not be persisted")

    async def _persist_or_restore(self, snapshot: dict[str, Any]) -> None:
        try:
            await self._persist_operational_mutation()
        except AlertStateUnavailableError:
            self._generation = snapshot["generation"]
            self._alerts = snapshot["alerts"]
            self._audit = snapshot["audit"]
            self._acknowledgments = snapshot["acknowledgments"]
            self._silences = snapshot["silences"]
            self._notifications.import_state(snapshot["notifications"])
            if self._capabilities is not None:
                self._capabilities.import_state(snapshot["capabilities"])
            self._desired_snapshot = self._current_snapshot()
            raise

    def _operational_snapshot(self) -> dict[str, Any]:
        return {
            "generation": self._generation,
            "alerts": deepcopy(self._alerts),
            "audit": deepcopy(self._audit),
            "acknowledgments": deepcopy(self._acknowledgments),
            "silences": deepcopy(self._silences),
            "notifications": self._notifications.export_state(),
            "capabilities": (
                self._capabilities.export_state()
                if self._capabilities is not None
                else {}
            ),
        }

    def _firing_instance(self, instance_id: str) -> dict[str, object]:
        alert = next(
            (
                item
                for item in self._alerts
                if item.get("instance_id") == instance_id
                and item.get("state") == "firing"
            ),
            None,
        )
        if alert is None:
            raise ValueError("Alert instance is not firing")
        return alert

    def _apply_operational_state(
        self, alerts: list[dict[str, object]], now_ms: int
    ) -> None:
        active_silences = {
            str(silence["instance_id"])
            for silence in self._silences
            if int(silence["expires_at_ms"]) > now_ms
            and silence.get("instance_id") is not None
        }
        for alert in alerts:
            instance_id = alert.get("instance_id")
            matcher_silenced = any(
                int(silence["expires_at_ms"]) > now_ms
                and silence.get("instance_id") is None
                and (
                    silence.get("match_all") is True
                    or match_alert_labels(
                        list(silence.get("matchers", [])),
                        dict(alert.get("labels", {})),
                    )
                )
                for silence in self._silences
            )
            alert["acknowledgment"] = (
                dict(self._acknowledgments[str(instance_id)])
                if instance_id is not None
                and str(instance_id) in self._acknowledgments
                else None
            )
            alert["suppression"] = (
                ["silence"]
                if (
                    instance_id is not None and str(instance_id) in active_silences
                )
                or matcher_silenced
                else []
            )
            alert["notification_suppressed"] = (
                (
                    alert["acknowledgment"] is not None
                    and alert.get("evaluation_status") != "stale"
                )
                or bool(alert["suppression"])
                or alert_definition_key(
                    str(alert.get("rule_id")), str(alert.get("name"))
                )
                in self._startup_pending
            )

    def _reconcile_notifications(self, now_ms: int) -> None:
        if self._policy_contents is None:
            return
        self._notifications.reconcile(
            self._alerts,
            self._policy_contents,
            now_ms=now_ms,
            default_timezone=self._default_timezone,
        )

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

    def record_runtime_error(self, error_class: str) -> None:
        """Expose a contained deadline/delivery failure without disabling lifecycle."""
        self._current_error = error_class
        if self._publish is not None:
            self._publish()
