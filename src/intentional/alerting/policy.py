"""Pure Alert routing policy and simulation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from intentional.durations import parse_duration


class PolicyStore(Protocol):
    """Minimal durable Store boundary used by alerting policy publication."""

    async def async_load(self) -> Any: ...

    async def async_save(self, data: dict[str, Any]) -> None: ...


class AlertingPolicyRepository:
    """Generation-controlled durable Alert routing policy document."""

    def __init__(self, store: PolicyStore, *, default_timezone: str = "UTC") -> None:
        self._store = store
        self._default_timezone = default_timezone
        self._contents: str | None = None
        self._history: list[dict[str, str]] = []
        self._lock = asyncio.Lock()
        self._current_error: str | None = None

    @property
    def contents(self) -> str | None:
        return self._contents

    @property
    def generation(self) -> str:
        return _policy_generation(self._contents)

    async def async_load(self) -> None:
        try:
            stored = await self._store.async_load()
            if stored is None:
                return
            if not isinstance(stored, dict) or not isinstance(
                stored.get("contents"), str
            ):
                raise ValueError("invalid stored alerting policy")
            contents = stored["contents"]
            _load_policy(contents, default_timezone=self._default_timezone)
            history = stored.get("history", [])
            if not isinstance(history, list):
                raise ValueError("invalid stored alerting policy history")
            self._contents = contents
            self._history = [
                record
                for record in history
                if isinstance(record, dict)
                and isinstance(record.get("generation"), str)
                and isinstance(record.get("contents"), str)
            ][-25:]
            self._current_error = None
        except Exception:  # Store or validation failure leaves routing disabled.
            self._contents = None
            self._history = []
            self._current_error = "policy_store_load_failed"

    def health(self) -> dict[str, str | None]:
        return {
            "status": "degraded" if self._current_error is not None else "ok",
            "current_error": self._current_error,
        }

    def list_history(self) -> list[dict[str, str]]:
        return [
            {"generation": record["generation"]}
            for record in reversed(self._history)
        ]

    def read_history(self, generation: str) -> dict[str, str] | None:
        for record in self._history:
            if record["generation"] == generation:
                return dict(record)
        return None

    def preview(
        self,
        contents: str,
        alerts: list[dict[str, str]],
        *,
        at: datetime | None = None,
    ) -> dict[str, Any]:
        return {
            "current_generation": self.generation,
            "candidate_generation": _policy_generation(contents),
            "alerts": simulate_alerting_policy(
                contents,
                alerts,
                at=at,
                default_timezone=self._default_timezone,
            ),
        }

    async def async_publish(
        self,
        contents: str,
        *,
        expected_generation: str,
        alerts: list[dict[str, str]] | None = None,
        confirm_spike: bool = False,
        at: datetime | None = None,
    ) -> dict[str, Any]:
        _load_policy(contents, default_timezone=self._default_timezone)
        async with self._lock:
            if self.generation != expected_generation:
                return {"error": "generation_mismatch"}
            if alerts:
                instant = at or datetime.now(UTC)
                candidate = _preview_cardinality(
                    simulate_alerting_policy(
                        contents,
                        alerts,
                        at=instant,
                        default_timezone=self._default_timezone,
                    )
                )
                if candidate["groups"] > 1_024 or candidate["fanout"] > 2_048:
                    raise ValueError("policy preview exceeds runtime cardinality caps")
                current = (
                    _preview_cardinality(
                        simulate_alerting_policy(
                            self._contents,
                            alerts,
                            at=instant,
                            default_timezone=self._default_timezone,
                        )
                    )
                    if self._contents is not None
                    else {"groups": 0, "fanout": 0}
                )
                spike = (
                    candidate["groups"] > 4 * max(1, current["groups"])
                    or candidate["fanout"] > 4 * max(1, current["fanout"])
                )
                if spike and not confirm_spike:
                    return {
                        "error": "confirmation_required",
                        "preview": {**candidate, "current": current},
                    }
            if contents == self._contents:
                return {"generation": self.generation}
            history = list(self._history)
            if self._contents is not None:
                history.append(
                    {"generation": self.generation, "contents": self._contents}
                )
                history = history[-25:]
            generation = _policy_generation(contents)
            try:
                await self._store.async_save(
                    {
                        "contents": contents,
                        "generation": generation,
                        "history": history,
                    }
                )
            except Exception:
                self._current_error = "policy_store_save_failed"
                raise
            self._contents = contents
            self._history = history
            self._current_error = None
            return {"generation": generation}

    async def async_rollback(
        self, generation: str, *, expected_generation: str
    ) -> dict[str, str]:
        async with self._lock:
            if self.generation != expected_generation:
                return {"error": "generation_mismatch"}
            if generation == self.generation:
                return {"generation": generation, "restored_generation": generation}
            record = self.read_history(generation)
            if record is None:
                return {"error": "history_not_found"}
            contents = record["contents"]
            _load_policy(contents, default_timezone=self._default_timezone)
            history = [
                *self._history,
                {"generation": self.generation, "contents": self._contents or ""},
            ][-25:]
            try:
                await self._store.async_save(
                    {"contents": contents, "generation": generation, "history": history}
                )
            except Exception:
                self._current_error = "policy_store_save_failed"
                raise
            self._contents = contents
            self._history = history
            self._current_error = None
            return {"generation": generation, "restored_generation": generation}


@dataclass(frozen=True)
class Route:
    """One inherited Alert routing branch."""

    id: str
    receiver: str
    group_by: tuple[str, ...]
    group_wait_ms: int
    group_interval_ms: int
    repeat_interval_ms: int
    send_resolved: bool
    matchers: tuple[str, ...] = ()
    active_intervals: tuple[str, ...] = ()
    mute_intervals: tuple[str, ...] = ()
    continue_: bool = False
    children: tuple[Route, ...] = ()


@dataclass(frozen=True)
class Interval:
    """One named local-time eligibility window."""

    name: str
    timezone: ZoneInfo
    weekdays: frozenset[int]
    times: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class InhibitRule:
    """One causal source-to-target Notification suppression rule."""

    source_matchers: tuple[str, ...]
    target_matchers: tuple[str, ...]
    equal: tuple[str, ...]


@dataclass(frozen=True)
class ReceiverDestination:
    data: dict[str, Any]
    identity: str
    allow_duplicate: bool


@dataclass(frozen=True)
class Receiver:
    name: str
    revision: str
    destinations: tuple[ReceiverDestination, ...]


@dataclass(frozen=True)
class _RegexToken:
    kind: str
    value: Any
    quantifier: str


def simulate_alerting_policy(
    contents: str,
    alerts: list[dict[str, str]],
    *,
    at: datetime | None = None,
    default_timezone: str = "UTC",
    inhibition_sources: set[int] | None = None,
) -> list[dict[str, Any]]:
    """Route synthetic Alert labels without dispatching Notifications."""
    root, receivers, intervals, inhibit_rules = _load_policy(
        contents, default_timezone=default_timezone
    )
    if not all(
        isinstance(labels, dict)
        and all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in labels.items()
        )
        for labels in alerts
    ):
        raise ValueError("alerts must contain string label mappings")
    result = []
    for alert_index, labels in enumerate(alerts):
        routes = _route_alert(root, labels)
        inhibition = _inhibition_suppression(
            alert_index,
            labels,
            alerts,
            inhibit_rules,
            inhibition_sources=inhibition_sources,
        )
        projected_routes = [
            _project_route(route, labels, receivers, intervals, at, inhibition)
            for route in routes
            if route.receiver in receivers
        ]
        fanout, warnings = _project_fanout(routes, projected_routes, receivers)
        projection = {
            "labels": dict(labels),
            "routes": projected_routes,
            "fanout": fanout,
        }
        if warnings:
            projection["warnings"] = warnings
        result.append(projection)
    return result


def match_alert_labels(matchers: list[str] | tuple[str, ...], labels: dict[str, str]) -> bool:
    """Evaluate shared safe Alert matchers for routing and suppression."""
    return _matches(tuple(matchers), labels)


def _load_policy(
    contents: str,
    *,
    default_timezone: str = "UTC",
) -> tuple[Route, dict[str, Receiver], dict[str, Interval], tuple[InhibitRule, ...]]:
    try:
        raw = yaml.safe_load(contents)
    except yaml.YAMLError as err:
        raise ValueError(f"invalid alerting policy YAML: {err}") from err
    allowed = {
        "route",
        "receivers",
        "active_intervals",
        "mute_intervals",
        "inhibit_rules",
    }
    if not isinstance(raw, dict) or set(raw) - allowed:
        raise ValueError("alerting policy contains unsupported fields")
    raw_receivers = raw.get("receivers")
    if not isinstance(raw_receivers, list):
        raise ValueError("receivers must be a list")
    if not 1 <= len(raw_receivers) <= 64:
        raise ValueError("policy requires between 1 and 64 Receivers")
    receivers: dict[str, Receiver] = {}
    for receiver in raw_receivers:
        if not isinstance(receiver, dict) or not isinstance(receiver.get("name"), str):
            raise ValueError("each Receiver must have a unique name")
        name = receiver["name"]
        if not name or name in receivers:
            raise ValueError("each Receiver must have a unique name")
        destinations = receiver.get("destinations")
        if not isinstance(destinations, list):
            raise ValueError("each Receiver requires a destinations list")
        if not 1 <= len(destinations) <= 8:
            raise ValueError("each Receiver requires at most 8 destinations")
        parsed_destinations = []
        for destination in destinations:
            if not isinstance(destination, dict) or not isinstance(
                destination.get("type"), str
            ):
                raise ValueError("Receiver destinations require a type")
            allow_duplicate = destination.get("allow_duplicate", False)
            if not isinstance(allow_duplicate, bool):
                raise ValueError("allow_duplicate must be a boolean")
            data = {
                key: value
                for key, value in destination.items()
                if key != "allow_duplicate"
            }
            destination_type = data["type"]
            if destination_type == "notify_entity":
                entity_id = data.get("entity_id")
                if not isinstance(entity_id, str) or not entity_id.startswith("notify."):
                    raise ValueError("notify_entity destinations require a notify entity_id")
            elif destination_type == "legacy_action":
                action = data.get("action")
                if not isinstance(action, str) or not action.startswith("notify."):
                    raise ValueError("legacy_action destinations require a notify action")
            elif destination_type != "persistent_notification":
                raise ValueError("unsupported Receiver destination type")
            identity = json.dumps(data, sort_keys=True, separators=(",", ":"))
            parsed_destinations.append(
                ReceiverDestination(data, identity, allow_duplicate)
            )
        encoded = json.dumps(destinations, sort_keys=True, separators=(",", ":"))
        receivers[name] = Receiver(
            name=name,
            revision=hashlib.sha256(encoded.encode()).hexdigest(),
            destinations=tuple(parsed_destinations),
        )
    intervals = _parse_intervals(
        raw.get("active_intervals", []), default_timezone=default_timezone
    )
    mute_intervals = _parse_intervals(
        raw.get("mute_intervals", []), default_timezone=default_timezone
    )
    if set(intervals) & set(mute_intervals):
        raise ValueError("interval names must be unique")
    intervals.update(mute_intervals)
    root = _parse_route(raw.get("route"), parent=None)
    if root.receiver not in receivers or any(
        route.receiver not in receivers for route in _walk_routes(root)
    ):
        raise ValueError("every route must reference a configured Receiver")
    routes = _walk_routes(root)
    if len(routes) > 256:
        raise ValueError("policy may contain at most 256 routes")
    if len({route.id for route in routes}) != len(routes):
        raise ValueError("route IDs must be unique")
    for route in routes:
        referenced_intervals = set(route.active_intervals) | set(route.mute_intervals)
        if referenced_intervals - intervals.keys():
            raise ValueError("every route interval must reference a configured interval")
    return root, receivers, intervals, _parse_inhibit_rules(raw.get("inhibit_rules", []))


def _parse_route(raw: Any, *, parent: Route | None) -> Route:
    if not isinstance(raw, dict):
        raise ValueError("route must be a mapping")
    allowed = {
        "id",
        "receiver",
        "group_by",
        "group_wait",
        "group_interval",
        "repeat_interval",
        "send_resolved",
        "matchers",
        "active_intervals",
        "mute_intervals",
        "continue",
        "routes",
    }
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(f"unsupported route fields: {sorted(unknown)}")
    route_id = raw.get("id")
    if not isinstance(route_id, str) or not route_id:
        raise ValueError("every route requires an id")
    if parent is None:
        receiver = raw.get("receiver")
        if not isinstance(receiver, str) or not receiver:
            raise ValueError("root route requires a receiver")
        route = Route(
            id=route_id,
            receiver=receiver,
            group_by=_string_tuple(raw.get("group_by", ["alertname", "area"])),
            group_wait_ms=_duration(raw.get("group_wait", "30s")),
            group_interval_ms=_duration(raw.get("group_interval", "5m")),
            repeat_interval_ms=_duration(raw.get("repeat_interval", "4h")),
            send_resolved=_boolean(raw.get("send_resolved", True), "send_resolved"),
        )
    else:
        route = replace(
            parent,
            id=route_id,
            receiver=str(raw.get("receiver", parent.receiver)),
            group_by=_string_tuple(raw.get("group_by", parent.group_by)),
            group_wait_ms=_duration(raw.get("group_wait", parent.group_wait_ms)),
            group_interval_ms=_duration(
                raw.get("group_interval", parent.group_interval_ms)
            ),
            repeat_interval_ms=_duration(
                raw.get("repeat_interval", parent.repeat_interval_ms)
            ),
            send_resolved=_boolean(
                raw.get("send_resolved", parent.send_resolved), "send_resolved"
            ),
            children=(),
        )
    _validate_route_timing(route)
    matchers = _string_tuple(raw.get("matchers", []))
    if len(matchers) > 16:
        raise ValueError("each route may contain at most 16 matchers")
    for matcher in matchers:
        _parse_matcher(matcher)
    children_raw = raw.get("routes", [])
    if not isinstance(children_raw, list):
        raise ValueError("routes must be a list")
    route = replace(
        route,
        matchers=matchers,
        active_intervals=_string_tuple(
            raw.get("active_intervals", route.active_intervals)
        ),
        mute_intervals=_string_tuple(raw.get("mute_intervals", route.mute_intervals)),
        continue_=_boolean(
            raw.get("continue", parent.continue_ if parent is not None else False),
            "continue",
        ),
    )
    return replace(
        route,
        children=tuple(_parse_route(child, parent=route) for child in children_raw),
    )


def _route_alert(route: Route, labels: dict[str, str]) -> list[Route]:
    matched: list[Route] = []
    for child in route.children:
        if not _matches(child.matchers, labels):
            continue
        matched.extend(_route_alert(child, labels))
        if not child.continue_:
            break
    return matched or [route]


def _matches(matchers: tuple[str, ...], labels: dict[str, str]) -> bool:
    for matcher in matchers:
        name, operator, expected = _parse_matcher(matcher)
        actual = labels.get(name)
        if operator == "=" and actual != expected:
            return False
        if operator == "!=" and actual == expected:
            return False
        if operator in {"=~", "!~"}:
            pattern = _safe_regex(expected)
            matches = actual is not None and _safe_regex_fullmatch(pattern, actual)
            if matches != (operator == "=~"):
                return False
    return True


def _parse_matcher(matcher: str) -> tuple[str, str, str]:
    parsed = re.fullmatch(
        r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*(!=|=~|!~|=)\s*\"([^\"]*)\"\s*",
        matcher,
    )
    if parsed is None:
        raise ValueError(f"invalid matcher {matcher!r}")
    name, operator, expected = parsed.groups()
    if operator in {"=~", "!~"}:
        _safe_regex(expected)
    return name, operator, expected


def _project_route(
    route: Route,
    labels: dict[str, str],
    receivers: dict[str, Receiver],
    intervals: dict[str, Interval],
    at: datetime | None,
    inhibition: dict[str, Any] | None,
) -> dict[str, Any]:
    projection: dict[str, Any] = {
        "route_id": route.id,
        "receiver": route.receiver,
        "group_by": list(route.group_by),
        "group_wait_ms": route.group_wait_ms,
        "group_interval_ms": route.group_interval_ms,
        "repeat_interval_ms": route.repeat_interval_ms,
        "send_resolved": route.send_resolved,
        "group_key": {
            "route_id": route.id,
            "receiver_revision": receivers[route.receiver].revision,
            "labels": {name: labels.get(name, "") for name in route.group_by},
        },
        "destinations": [
            dict(destination.data)
            for destination in receivers[route.receiver].destinations
        ],
    }
    suppression = _interval_suppression(route, intervals, at) or inhibition
    if suppression is not None:
        projection["suppression"] = suppression
    return projection


def _project_fanout(
    routes: list[Route],
    projections: list[dict[str, Any]],
    receivers: dict[str, Receiver],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    fanout = []
    warnings = []
    seen: set[str] = set()
    for route, projection in zip(routes, projections, strict=True):
        if "suppression" in projection:
            continue
        receiver = receivers[route.receiver]
        for destination in receiver.destinations:
            if destination.identity in seen and not destination.allow_duplicate:
                continue
            if destination.identity in seen:
                warnings.append(
                    {
                        "code": "duplicate_fanout",
                        "route_id": route.id,
                        "receiver": receiver.name,
                    }
                )
            seen.add(destination.identity)
            fanout.append(
                {
                    "route_id": route.id,
                    "receiver": receiver.name,
                    "destination": dict(destination.data),
                }
            )
    return fanout, warnings


def _interval_suppression(
    route: Route, intervals: dict[str, Interval], at: datetime | None
) -> dict[str, Any] | None:
    if not route.active_intervals and not route.mute_intervals:
        return None
    if at is None or at.tzinfo is None:
        raise ValueError("interval simulation requires an aware at timestamp")
    if route.active_intervals and not any(
        _interval_open(intervals[name], at) for name in route.active_intervals
    ):
        return {"reason": "inactive_interval", "intervals": list(route.active_intervals)}
    for name in route.mute_intervals:
        if _interval_open(intervals[name], at):
            return {"reason": "mute_interval", "interval": name}
    return None


def _inhibition_suppression(
    alert_index: int,
    labels: dict[str, str],
    alerts: list[dict[str, str]],
    rules: tuple[InhibitRule, ...],
    *,
    inhibition_sources: set[int] | None,
) -> dict[str, Any] | None:
    for rule_index, rule in enumerate(rules):
        if not _matches(rule.target_matchers, labels):
            continue
        for source_index, source_labels in enumerate(alerts):
            if (
                source_index == alert_index
                or (
                    inhibition_sources is not None
                    and source_index not in inhibition_sources
                )
                or not _matches(
                rule.source_matchers, source_labels
                )
            ):
                continue
            if not all(
                name in source_labels
                and name in labels
                and source_labels[name] == labels[name]
                for name in rule.equal
            ):
                continue
            return {
                "reason": "inhibition",
                "rule": rule_index,
                "source_alert": source_index,
                "equal": {name: labels.get(name, "") for name in rule.equal},
            }
    return None


def _interval_open(interval: Interval, at: datetime) -> bool:
    local = at.astimezone(interval.timezone)
    minute = local.hour * 60 + local.minute + local.second / 60
    for start, end in interval.times:
        if start < end:
            if local.weekday() in interval.weekdays and start <= minute < end:
                return True
            continue
        if local.weekday() in interval.weekdays and minute >= start:
            return True
        previous_weekday = (local.weekday() - 1) % 7
        if previous_weekday in interval.weekdays and minute < end:
            return True
    return False


def _parse_intervals(
    value: Any, *, default_timezone: str
) -> dict[str, Interval]:
    if not isinstance(value, list):
        raise ValueError("intervals must be a list")
    if len(value) > 64:
        raise ValueError("policy may contain at most 64 intervals of each kind")
    weekday_numbers = {
        name: number
        for number, name in enumerate(("mon", "tue", "wed", "thu", "fri", "sat", "sun"))
    }
    result: dict[str, Interval] = {}
    for raw in value:
        if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
            raise ValueError("every interval requires a name")
        name = raw["name"]
        if not name or name in result:
            raise ValueError("interval names must be unique")
        timezone_name = raw.get("timezone", default_timezone)
        if not isinstance(timezone_name, str):
            raise ValueError("interval timezone must be an IANA name")
        try:
            timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as err:
            raise ValueError(f"unknown interval timezone {timezone_name!r}") from err
        weekdays = _string_tuple(raw.get("weekdays"))
        if any(day not in weekday_numbers for day in weekdays):
            raise ValueError("interval weekdays must use mon through sun")
        raw_times = raw.get("times")
        if not isinstance(raw_times, list) or not raw_times:
            raise ValueError("every interval requires times")
        times: list[tuple[int, int]] = []
        for raw_time in raw_times:
            if not isinstance(raw_time, dict):
                raise ValueError("interval times must be mappings")
            times.append(
                (_clock_minutes(raw_time.get("start")), _clock_minutes(raw_time.get("end")))
            )
        result[name] = Interval(
            name=name,
            timezone=timezone,
            weekdays=frozenset(weekday_numbers[day] for day in weekdays),
            times=tuple(times),
        )
    return result


def _parse_inhibit_rules(value: Any) -> tuple[InhibitRule, ...]:
    if not isinstance(value, list):
        raise ValueError("inhibit_rules must be a list")
    if len(value) > 64:
        raise ValueError("policy may contain at most 64 inhibition rules")
    result = []
    for raw in value:
        if not isinstance(raw, dict) or set(raw) != {
            "source_matchers",
            "target_matchers",
            "equal",
        }:
            raise ValueError("inhibition rules require source_matchers, target_matchers, and equal")
        source_matchers = _string_tuple(raw["source_matchers"])
        target_matchers = _string_tuple(raw["target_matchers"])
        if not source_matchers or not target_matchers:
            raise ValueError("inhibition matcher lists must not be empty")
        if len(source_matchers) > 16 or len(target_matchers) > 16:
            raise ValueError("inhibition sides may contain at most 16 matchers")
        for matcher in (*source_matchers, *target_matchers):
            _parse_matcher(matcher)
        result.append(
            InhibitRule(
                source_matchers=source_matchers,
                target_matchers=target_matchers,
                equal=_string_tuple(raw["equal"]),
            )
        )
    return tuple(result)


def _clock_minutes(value: Any) -> int:
    if not isinstance(value, str):
        raise ValueError("interval times must use HH:MM")
    parsed = re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value)
    if parsed is None:
        raise ValueError("interval times must use HH:MM")
    hour, minute = value.split(":")
    return int(hour) * 60 + int(minute)


def _safe_regex(pattern: str) -> tuple[_RegexToken, ...]:
    if len(pattern.encode()) > 256 or not pattern.startswith("^") or not pattern.endswith("$"):
        raise ValueError("unsafe regex: patterns must be anchored and at most 256 bytes")

    tokens = []
    index = 1
    end = len(pattern) - 1
    while index < end:
        character = pattern[index]
        if character in "()|{}^$*+?":
            raise ValueError("unsafe regex: unsupported construct")
        if character == "\\":
            if index + 1 >= end:
                raise ValueError("unsafe regex: incomplete escape")
            escaped = pattern[index + 1]
            if escaped in "dDwWsS":
                kind, value = "category", escaped
            else:
                kind, value = "literal", escaped
            index += 2
        elif character == "[":
            closing = _class_end(pattern, index + 1, end)
            kind, value = "class", _parse_character_class(pattern[index + 1 : closing])
            index = closing + 1
        else:
            kind, value = ("any", None) if character == "." else ("literal", character)
            index += 1
        quantifier = ""
        if index < end and pattern[index] in "*+?":
            quantifier = pattern[index]
            index += 1
        tokens.append(_RegexToken(kind, value, quantifier))

    return tuple(tokens)


def _safe_regex_fullmatch(pattern: tuple[_RegexToken, ...], value: str) -> bool:
    positions = _regex_epsilon_closure(pattern, {0})
    for character in value:
        next_positions = set()
        for position in positions:
            if position >= len(pattern) or not _regex_atom_matches(
                pattern[position], character
            ):
                continue
            token = pattern[position]
            if token.quantifier in {"*", "+"}:
                next_positions.add(position)
            if token.quantifier in {"", "?", "+"}:
                next_positions.add(position + 1)
        positions = _regex_epsilon_closure(pattern, next_positions)
        if not positions:
            return False
    return len(pattern) in _regex_epsilon_closure(pattern, positions)


def _regex_epsilon_closure(
    pattern: tuple[_RegexToken, ...], positions: set[int]
) -> set[int]:
    closed = set(positions)
    pending = list(positions)
    while pending:
        position = pending.pop()
        if (
            position < len(pattern)
            and pattern[position].quantifier in {"*", "?"}
            and position + 1 not in closed
        ):
            closed.add(position + 1)
            pending.append(position + 1)
    return closed


def _regex_atom_matches(token: _RegexToken, character: str) -> bool:
    if token.kind == "any":
        return True
    if token.kind == "literal":
        return character == token.value
    if token.kind == "category":
        category = token.value.lower()
        if category == "d":
            matches = character.isdigit()
        elif category == "w":
            matches = character.isalnum() or character == "_"
        else:
            matches = character.isspace()
        return not matches if token.value.isupper() else matches
    negated, ranges = token.value
    matches = any(start <= character <= finish for start, finish in ranges)
    return not matches if negated else matches


def _class_end(pattern: str, start: int, end: int) -> int:
    escaped = False
    for index in range(start, end):
        if not escaped and pattern[index] == "]":
            return index
        escaped = not escaped and pattern[index] == "\\"
    raise ValueError("unsafe regex: unterminated character class")


def _parse_character_class(value: str) -> tuple[bool, tuple[tuple[str, str], ...]]:
    negated = value.startswith("^")
    if negated:
        value = value[1:]
    if not value:
        raise ValueError("unsafe regex: empty character class")
    characters = []
    index = 0
    while index < len(value):
        if value[index] == "\\":
            if index + 1 >= len(value) or value[index + 1] in "dDwWsS":
                raise ValueError("unsafe regex: unsupported character class escape")
            characters.append(value[index + 1])
            index += 2
        else:
            characters.append(value[index])
            index += 1
    ranges = []
    index = 0
    while index < len(characters):
        if index + 2 < len(characters) and characters[index + 1] == "-":
            if characters[index] > characters[index + 2]:
                raise ValueError("unsafe regex: reversed character class range")
            ranges.append((characters[index], characters[index + 2]))
            index += 3
        else:
            ranges.append((characters[index], characters[index]))
            index += 1
    return negated, tuple(ranges)


def _duration(value: Any) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        return parse_duration(value)
    raise ValueError("route timing must be a duration")


def _boolean(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a boolean")
    return value


def _validate_route_timing(route: Route) -> None:
    if route.group_wait_ms < 0 or 0 < route.group_wait_ms < 1_000:
        raise ValueError("group_wait must be zero or at least 1s")
    if route.group_interval_ms < 60_000:
        raise ValueError("group_interval must be at least 1m")
    if route.repeat_interval_ms < 60_000:
        raise ValueError("repeat_interval must be at least 1m")


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise ValueError("route lists must contain non-empty strings")
    return tuple(value)


def _walk_routes(route: Route) -> list[Route]:
    return [route, *(descendant for child in route.children for descendant in _walk_routes(child))]


def _policy_generation(contents: str | None) -> str:
    return hashlib.sha256((contents or "").encode()).hexdigest()


def _preview_cardinality(results: list[dict[str, Any]]) -> dict[str, int]:
    groups = {
        json.dumps(route["group_key"], sort_keys=True, separators=(",", ":"))
        for result in results
        for route in result["routes"]
        if "suppression" not in route
    }
    return {
        "groups": len(groups),
        "fanout": sum(len(result["fanout"]) for result in results),
    }
