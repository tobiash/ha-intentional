"""Durable Notification grouping and obligation state machine."""

from __future__ import annotations

import json
import random
from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from .alerting.policy import simulate_alerting_policy

MAX_GROUPS = 1_024
MAX_NONTERMINAL_OBLIGATIONS = 2_048
MAX_ATTEMPTS = 8
TERMINAL_STATUSES = {"accepted", "cancelled", "superseded", "dead_lettered"}


class NotificationRuntime:
    """Plan immutable per-destination Notification obligations."""

    def __init__(
        self,
        *,
        id_factory: Callable[[], str] | None = None,
        jitter: Callable[[], float] | None = None,
    ) -> None:
        self._id_factory = id_factory or (lambda: str(uuid4()))
        self._jitter = jitter or random.random
        self._groups: dict[str, dict[str, Any]] = {}
        self._obligations: list[dict[str, Any]] = []
        self._attempts: list[dict[str, Any]] = []
        self.degraded = False

    def reconcile(
        self,
        alerts: list[dict[str, object]],
        policy_contents: str,
        *,
        now_ms: int,
        at: datetime | None = None,
        default_timezone: str = "UTC",
    ) -> None:
        firing = [
            alert
            for alert in alerts
            if alert.get("state") == "firing"
            and isinstance(alert.get("instance_id"), str)
            and isinstance(alert.get("labels"), dict)
        ]
        labels = [dict(alert["labels"]) for alert in firing]
        routed = simulate_alerting_policy(
            policy_contents,
            labels,
            at=at or datetime.fromtimestamp(now_ms / 1_000, tz=UTC),
            default_timezone=default_timezone,
        )
        desired: dict[str, dict[str, Any]] = {}
        for alert, result in zip(firing, routed, strict=True):
            if alert.get("notification_suppressed") is True:
                continue
            route_by_id = {route["route_id"]: route for route in result["routes"]}
            for fanout in result["fanout"]:
                route = route_by_id[fanout["route_id"]]
                group_identity = json.dumps(
                    route["group_key"], sort_keys=True, separators=(",", ":")
                )
                group = desired.setdefault(
                    group_identity,
                    {
                        "identity": group_identity,
                        "route_id": route["route_id"],
                        "receiver": fanout["receiver"],
                        "receiver_revision": route["group_key"]["receiver_revision"],
                        "group_wait_ms": route["group_wait_ms"],
                        "group_interval_ms": route["group_interval_ms"],
                        "repeat_interval_ms": route["repeat_interval_ms"],
                        "send_resolved": route["send_resolved"],
                        "members": {},
                        "destinations": {},
                    },
                )
                instance_id = str(alert["instance_id"])
                group["members"][instance_id] = {
                    "instance_id": instance_id,
                    "severity": alert.get("severity", "info"),
                    "summary": alert.get("summary", "Alert firing"),
                    "annotations": deepcopy(alert.get("annotations", {})),
                }
                destination = deepcopy(fanout["destination"])
                destination_id = json.dumps(
                    destination, sort_keys=True, separators=(",", ":")
                )
                group["destinations"][destination_id] = destination

        for identity in list(self._groups):
            if identity in desired:
                continue
            group = self._groups[identity]
            if not group.get("accepted_destinations"):
                self._cancel_group_obligations(identity, "cancelled")
                del self._groups[identity]
                continue
            group["members"] = {}
            group["version"] += 1
            group["message_kind"] = "resolved"
            group["due_at_ms"] = now_ms + group["group_interval_ms"]

        for identity, candidate in desired.items():
            existing = self._groups.get(identity)
            if existing is None:
                if len(self._groups) >= MAX_GROUPS:
                    self.degraded = True
                    continue
                self._groups[identity] = {
                    **candidate,
                    "version": 1,
                    "message_kind": "initial",
                    "due_at_ms": now_ms + candidate["group_wait_ms"],
                    "last_accepted_at_ms": None,
                    "accepted_destinations": [],
                }
                continue
            changed = (
                existing["members"] != candidate["members"]
                or existing["destinations"] != candidate["destinations"]
                or existing["receiver_revision"] != candidate["receiver_revision"]
            )
            existing.update({key: value for key, value in candidate.items() if key != "identity"})
            if changed:
                self._supersede_group_obligations(identity)
                existing["version"] += 1
                existing["message_kind"] = (
                    "initial" if existing["last_accepted_at_ms"] is None else "update"
                )
                if existing["due_at_ms"] is None:
                    accepted = existing["last_accepted_at_ms"]
                    existing["due_at_ms"] = (
                        now_ms
                        if accepted is None
                        else max(now_ms, accepted + existing["group_interval_ms"])
                    )

    def advance(self, *, now_ms: int) -> list[dict[str, Any]]:
        for group in self._groups.values():
            due_at = group.get("due_at_ms")
            if due_at is None or now_ms < due_at:
                continue
            self._plan_group(group, now_ms)
            group["due_at_ms"] = None
        return [
            deepcopy(obligation)
            for obligation in self._obligations
            if obligation["status"] == "planned"
            and obligation.get("next_attempt_at_ms", 0) <= now_ms
        ]

    def mark_in_flight(self, obligation_id: str, *, now_ms: int) -> None:
        obligation = self._obligation(obligation_id)
        if obligation["status"] != "planned":
            return
        obligation["status"] = "in_flight"
        obligation["attempt"] += 1
        self._attempts.append(
            {
                "obligation_id": obligation_id,
                "attempt": obligation["attempt"],
                "at_ms": now_ms,
                "result": "in_flight",
            }
        )

    def accept(self, obligation_id: str, *, now_ms: int) -> None:
        obligation = self._obligation(obligation_id)
        if obligation["status"] != "in_flight":
            return
        obligation["status"] = "accepted"
        obligation["accepted_at_ms"] = now_ms
        self._attempts.append(
            {
                "obligation_id": obligation_id,
                "attempt": obligation["attempt"],
                "at_ms": now_ms,
                "result": "accepted",
            }
        )
        group = self._groups.get(obligation["group_identity"])
        if group is None:
            return
        destination_id = obligation["destination_id"]
        if destination_id not in group["accepted_destinations"]:
            group["accepted_destinations"].append(destination_id)
        if self._group_version_complete(group):
            group["last_accepted_at_ms"] = now_ms
            group["message_kind"] = "repeat"
            group["due_at_ms"] = now_ms + group["repeat_interval_ms"]

    def reject(
        self, obligation_id: str, *, now_ms: int, error_class: str
    ) -> None:
        obligation = self._obligation(obligation_id)
        if obligation["status"] != "in_flight":
            return
        self._attempts.append(
            {
                "obligation_id": obligation_id,
                "attempt": obligation["attempt"],
                "at_ms": now_ms,
                "result": "rejected",
                "error_class": error_class,
            }
        )
        if obligation["attempt"] >= MAX_ATTEMPTS:
            obligation["status"] = "dead_lettered"
            obligation["error_class"] = error_class
            return
        backoff = min(300_000, 1_000 * 2 ** (obligation["attempt"] - 1))
        obligation["status"] = "planned"
        obligation["next_attempt_at_ms"] = now_ms + int(
            backoff * (0.75 + self._jitter() * 0.5)
        )
        obligation["error_class"] = error_class

    def next_deadline_ms(self) -> int | None:
        deadlines = [
            int(group["due_at_ms"])
            for group in self._groups.values()
            if group.get("due_at_ms") is not None
        ]
        deadlines.extend(
            int(obligation["next_attempt_at_ms"])
            for obligation in self._obligations
            if obligation["status"] == "planned"
        )
        return min(deadlines) if deadlines else None

    def list_obligations(self) -> list[dict[str, Any]]:
        return [deepcopy(obligation) for obligation in self._obligations]

    def list_attempts(self) -> list[dict[str, Any]]:
        return [dict(attempt) for attempt in self._attempts]

    def export_state(self) -> dict[str, Any]:
        return {
            "groups": deepcopy(self._groups),
            "obligations": deepcopy(self._obligations),
            "attempts": deepcopy(self._attempts[-10_000:]),
            "degraded": self.degraded,
        }

    def import_state(self, state: dict[str, Any]) -> None:
        groups = state.get("groups", {})
        obligations = state.get("obligations", [])
        attempts = state.get("attempts", [])
        if (
            not isinstance(groups, dict)
            or len(groups) > MAX_GROUPS
            or not isinstance(obligations, list)
            or not isinstance(attempts, list)
        ):
            raise ValueError("invalid Notification runtime state")
        nonterminal = sum(
            isinstance(item, dict) and item.get("status") not in TERMINAL_STATUSES
            for item in obligations
        )
        if nonterminal > MAX_NONTERMINAL_OBLIGATIONS:
            raise ValueError("too many nonterminal Notification obligations")
        self._groups = deepcopy(groups)
        self._obligations = deepcopy(obligations)
        self._attempts = deepcopy(attempts[-10_000:])
        self.degraded = state.get("degraded") is True

    def _plan_group(self, group: dict[str, Any], now_ms: int) -> None:
        if group["message_kind"] == "resolved" and not group["send_resolved"]:
            return
        existing = {
            obligation["destination_id"]
            for obligation in self._obligations
            if obligation["group_identity"] == group["identity"]
            and obligation["group_version"] == group["version"]
            and obligation["status"] not in {"cancelled", "superseded"}
        }
        members = sorted(
            group["members"].values(),
            key=lambda member: (
                {"critical": 0, "warning": 1, "info": 2}.get(member["severity"], 3),
                member["instance_id"],
            ),
        )
        if not members and group["message_kind"] != "resolved":
            return
        payload = self._render_payload(group["message_kind"], members)
        for destination_id, destination in group["destinations"].items():
            if destination_id in existing:
                continue
            if group["message_kind"] == "resolved" and destination_id not in group["accepted_destinations"]:
                continue
            nonterminal = sum(
                item["status"] not in TERMINAL_STATUSES for item in self._obligations
            )
            if nonterminal >= MAX_NONTERMINAL_OBLIGATIONS:
                self.degraded = True
                return
            self._obligations.append(
                {
                    "obligation_id": self._id_factory(),
                    "group_identity": group["identity"],
                    "group_version": group["version"],
                    "receiver": group["receiver"],
                    "receiver_revision": group["receiver_revision"],
                    "destination_id": destination_id,
                    "destination": deepcopy(destination),
                    "message_kind": group["message_kind"],
                    "member_instance_ids": [member["instance_id"] for member in members],
                    "payload": deepcopy(payload),
                    "status": "planned",
                    "attempt": 0,
                    "planned_at_ms": now_ms,
                    "next_attempt_at_ms": now_ms,
                }
            )

    @staticmethod
    def _render_payload(kind: str, members: list[dict[str, Any]]) -> dict[str, str]:
        if kind == "resolved":
            return {"title": "Resolved", "message": "Alert resolved"}
        if len(members) == 1:
            message = str(members[0]["summary"])
        else:
            shown = members[:20]
            message = "\n".join(str(member["summary"]) for member in shown)
            if len(members) > len(shown):
                message += f"\n+{len(members) - len(shown)} more"
        encoded = message.encode()[:16_000]
        message = encoded.decode(errors="ignore")
        return {"title": "Intentional Alerts", "message": message}

    def _group_version_complete(self, group: dict[str, Any]) -> bool:
        matching = [
            obligation
            for obligation in self._obligations
            if obligation["group_identity"] == group["identity"]
            and obligation["group_version"] == group["version"]
        ]
        return bool(matching) and all(
            obligation["status"] in TERMINAL_STATUSES for obligation in matching
        )

    def _obligation(self, obligation_id: str) -> dict[str, Any]:
        for obligation in self._obligations:
            if obligation["obligation_id"] == obligation_id:
                return obligation
        raise KeyError(obligation_id)

    def _cancel_group_obligations(self, identity: str, status: str) -> None:
        for obligation in self._obligations:
            if (
                obligation["group_identity"] == identity
                and obligation["status"] not in TERMINAL_STATUSES
            ):
                obligation["status"] = status

    def _supersede_group_obligations(self, identity: str) -> None:
        self._cancel_group_obligations(identity, "superseded")
