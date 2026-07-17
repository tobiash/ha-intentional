"""Single-use mobile Alert action capabilities."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from collections.abc import Callable
from copy import deepcopy
from typing import Any


class CapabilityRuntime:
    """Issue and consume bound tokens without persisting raw token material."""

    def __init__(
        self, secret: bytes, *, id_factory: Callable[[], str] | None = None
    ) -> None:
        if len(secret) < 32:
            raise ValueError("capability HMAC secret must be at least 32 bytes")
        self._secret = secret
        self._id_factory = id_factory or (lambda: secrets.token_urlsafe(24))
        self._records: dict[str, dict[str, Any]] = {}

    def issue(
        self,
        *,
        entry_id: str,
        instance_id: str,
        operation: str,
        destination_id: str,
        now_ms: int,
        expires_at_ms: int,
    ) -> dict[str, str]:
        if expires_at_ms <= now_ms or expires_at_ms - now_ms > 86_400_000:
            raise ValueError("capability lifetime must be between 1ms and 24h")
        self._prune(now_ms)
        active = sum(
            not record["consumed"] and record["expires_at_ms"] >= now_ms
            for record in self._records.values()
        )
        if active >= 2_048:
            raise ValueError("too many active capability records")
        record_id = self._id_factory()
        record = {
            "record_id": record_id,
            "entry_id": entry_id,
            "instance_id": instance_id,
            "operation": operation,
            "destination_id": destination_id,
            "expires_at_ms": expires_at_ms,
            "consumed": False,
        }
        self._records[record_id] = record
        return {"record_id": record_id, "token": self._derive(record)}

    def consume(
        self,
        record_id: str,
        token: str,
        *,
        actor: str | None,
        now_ms: int,
        entry_id: str,
        instance_id: str,
        operation: str,
    ) -> dict[str, Any]:
        record = self._records.get(record_id)
        if actor is None:
            raise ValueError("authenticated actor required")
        if record is None or record["consumed"]:
            raise ValueError("capability unavailable")
        if now_ms >= record["expires_at_ms"]:
            raise ValueError("capability expired")
        if (
            record["entry_id"] != entry_id
            or record["instance_id"] != instance_id
            or record["operation"] != operation
        ):
            raise ValueError("capability binding mismatch")
        if not hmac.compare_digest(token, self._derive(record)):
            raise ValueError("invalid capability token")
        record["consumed"] = True
        record["consumed_by"] = actor
        record["consumed_at_ms"] = now_ms
        return deepcopy(record)

    def export_state(self) -> dict[str, Any]:
        active = [record for record in self._records.values() if not record["consumed"]]
        terminal = [record for record in self._records.values() if record["consumed"]]
        terminal_capacity = max(0, 2_048 - len(active))
        retained = [*active, *(terminal[-terminal_capacity:] if terminal_capacity else [])]
        return {"records": deepcopy(retained)}

    def token(self, record_id: str) -> str:
        record = self._records.get(record_id)
        if record is None:
            raise KeyError(record_id)
        return self._derive(record)

    def record(self, record_id: str) -> dict[str, Any]:
        if record_id not in self._records:
            raise KeyError(record_id)
        return deepcopy(self._records[record_id])

    def import_state(self, state: dict[str, Any]) -> None:
        records = state.get("records", [])
        if not isinstance(records, list):
            raise ValueError("invalid capability state")
        if len(records) > 2_048:
            raise ValueError("too many capability records")
        self._records = {
            str(record["record_id"]): deepcopy(record)
            for record in records
            if isinstance(record, dict) and isinstance(record.get("record_id"), str)
        }

    def _derive(self, record: dict[str, Any]) -> str:
        binding = json.dumps(
            {
                key: record[key]
                for key in (
                    "record_id",
                    "entry_id",
                    "instance_id",
                    "operation",
                    "destination_id",
                    "expires_at_ms",
                )
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hmac.new(self._secret, binding, hashlib.sha256).hexdigest()

    def _prune(self, now_ms: int) -> None:
        terminal = [
            record_id
            for record_id, record in self._records.items()
            if record["consumed"] or record["expires_at_ms"] < now_ms
        ]
        for record_id in terminal[:-1_024]:
            self._records.pop(record_id, None)
