"""Storage-backed authored rule store for Intentional.

Rules are persisted as one YAML document in Home Assistant storage. Keeping the
YAML text as the stored representation preserves the existing editor/API shape
while detaching the source of truth from files on disk.
"""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

import yaml
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from ._engine import RuleLoadError
from ._engine.yaml_loader import Rule, load_rules_from_string
from .rule_files import (
    _raw_rule_items_from_yaml,
    _read_rule_dir_as_yaml,
    _replace_authored_rule_in_yaml,
    _set_rule_enabled_in_yaml,
    _yaml_contains_authored_rule_id,
)

RULE_STORE_VERSION = 1
RULE_STORE_FILENAME = "stored-rules.yaml"
RULE_HISTORY_LIMIT = 25


class StorageRuleStore:
    """Home Assistant storage-backed rule document."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._hass = hass
        self._store: Store = Store(
            hass,
            RULE_STORE_VERSION,
            f"intentional_rules_{entry_id}_v1",
        )
        self._contents = "[]\n"
        self._history: list[dict[str, Any]] = []

    async def async_load_or_import(self, rule_dir: str) -> list[Rule]:
        """Load stored rules, importing YAML files once if storage is empty."""
        data = await self._store.async_load()
        if isinstance(data, dict) and isinstance(data.get("contents"), str):
            self._contents = data["contents"]
            self._history = _valid_history(data.get("history"))
            return load_rules_from_string(self._contents)

        imported = await self._hass.async_add_executor_job(
            _read_rule_dir_as_yaml,
            rule_dir,
        )
        self._contents = imported or "[]\n"
        load_rules_from_string(self._contents)
        await self.async_save()
        return load_rules_from_string(self._contents)

    async def async_save(self) -> None:
        """Persist current YAML contents."""
        await self._store.async_save({
            "contents": self._contents,
            "generation": self.generation,
            "history": self._history,
        })

    @property
    def contents(self) -> str:
        """Return stored YAML contents."""
        return self._contents

    @property
    def generation(self) -> str:
        """Return generation hash for optimistic updates."""
        return sha256(self._contents.encode("utf-8")).hexdigest()

    def list_files(self) -> list[dict[str, Any]]:
        """Return a synthetic YAML file descriptor for compatibility."""
        return [{
            "filename": RULE_STORE_FILENAME,
            "size": str(len(self._contents.encode("utf-8"))),
            "generation": self.generation,
            "source": "storage",
        }]

    def read(self, filename: str) -> str | None:
        """Read the synthetic rule file."""
        if filename != RULE_STORE_FILENAME:
            return None
        return self._contents

    async def async_write(self, filename: str, contents: str) -> str | None:
        """Validate and persist the synthetic rule file."""
        if filename != RULE_STORE_FILENAME:
            return f"Storage-backed rules must be written as {RULE_STORE_FILENAME}"
        try:
            load_rules_from_string(contents)
        except RuleLoadError as err:
            return f"Rule validation failed: {err}"
        self._record_history("write")
        self._contents = contents
        await self.async_save()
        return None

    async def async_delete(self, filename: str) -> str | None:
        """Clear all stored rules through the synthetic file API."""
        if filename != RULE_STORE_FILENAME:
            return f"Storage-backed rules must be deleted as {RULE_STORE_FILENAME}"
        self._record_history("delete")
        self._contents = "[]\n"
        await self.async_save()
        return None

    async def async_patch_rule_by_id(
        self,
        rule_id: str,
        contents: str,
        *,
        expected_generation: str,
    ) -> dict[str, Any]:
        """Replace stored YAML if it still contains rule_id and generation matches."""
        if self.generation != expected_generation:
            return {"error": "generation_mismatch"}
        try:
            replacement_rules = load_rules_from_string(contents)
        except RuleLoadError as err:
            return {"error": "validation_failed", "message": str(err)}
        try:
            replacement_has_rule = _yaml_contains_authored_rule_id(contents, rule_id)
        except yaml.YAMLError as err:
            return {"error": "validation_failed", "message": str(err)}
        if not replacement_rules or not replacement_has_rule:
            return {"error": "rule_id_missing"}
        updated = _replace_authored_rule_in_yaml(self._contents, rule_id, contents)
        if "error" in updated:
            return updated
        self._record_history(f"patch:{rule_id}")
        self._contents = updated["contents"]
        await self.async_save()
        return {"filename": RULE_STORE_FILENAME, "generation": self.generation}

    def list_history(self, *, include_contents: bool = False) -> list[dict[str, Any]]:
        """Return stored rule document history, newest first."""
        return [
            _history_public_record(record, include_contents=include_contents)
            for record in self._history
        ]

    def read_history(self, generation: str) -> dict[str, Any] | None:
        """Return one history snapshot by generation."""
        for record in self._history:
            if record.get("generation") == generation:
                return _history_public_record(record, include_contents=True)
        return None

    async def async_rollback(
        self,
        generation: str,
        *,
        expected_generation: str,
    ) -> dict[str, Any]:
        """Restore a previous rule document generation."""
        if self.generation != expected_generation:
            return {"error": "generation_mismatch"}
        record = self.read_history(generation)
        if record is None:
            return {"error": "history_not_found"}
        contents = record["contents"]
        try:
            load_rules_from_string(contents)
        except RuleLoadError as err:
            return {"error": "validation_failed", "message": str(err)}
        self._record_history(f"rollback:{generation}")
        self._contents = contents
        await self.async_save()
        return {
            "filename": RULE_STORE_FILENAME,
            "generation": self.generation,
            "restored_generation": generation,
        }

    def list_rules(self) -> list[dict[str, Any]]:
        """Return authored rule IDs with enabled metadata."""
        try:
            load_rules_from_string(self._contents)
            raw_rules = _raw_rule_items_from_yaml(self._contents)
        except (RuleLoadError, yaml.YAMLError):
            return []
        rules: list[dict[str, Any]] = []
        for rule in raw_rules:
            rule_id = rule.get("id")
            if not isinstance(rule_id, str):
                continue
            rules.append({
                "id": rule_id,
                "filename": RULE_STORE_FILENAME,
                "enabled": rule.get("enabled", True) is not False,
                "generation": self.generation,
                "source": "storage",
            })
        return rules

    async def async_set_rule_enabled(self, rule_id: str, enabled: bool) -> dict[str, Any]:
        """Persist one rule's enabled flag."""
        updated = _set_rule_enabled_in_yaml(self._contents, rule_id, enabled)
        if updated is None:
            return {"error": "not_found"}
        try:
            load_rules_from_string(updated)
        except RuleLoadError as err:
            return {"error": "validation_failed", "message": str(err)}
        self._record_history(f"enabled:{rule_id}:{enabled}")
        self._contents = updated
        await self.async_save()
        return {
            "filename": RULE_STORE_FILENAME,
            "generation": self.generation,
            "enabled": enabled,
        }

    async def async_rules(self) -> list[Rule]:
        """Return parsed stored rules."""
        return load_rules_from_string(self._contents)

    def _record_history(self, reason: str) -> None:
        """Record the current document before replacing it."""
        if self._history and self._history[0].get("generation") == self.generation:
            return
        self._history.insert(0, _history_record(self._contents, reason=reason))
        self._history = self._history[:RULE_HISTORY_LIMIT]


def rule_store_key(entry_id: str) -> str:
    """Return hass.data key for a rule store."""
    return f"{entry_id}_rule_store"


def _history_record(contents: str, *, reason: str) -> dict[str, Any]:
    return {
        "generation": _generation_for(contents),
        "contents": contents,
        "created_at": datetime.now(UTC).isoformat(),
        "reason": reason,
        "size": len(contents.encode("utf-8")),
        "rule_count": _rule_count(contents),
    }


def _history_public_record(record: dict[str, Any], *, include_contents: bool) -> dict[str, Any]:
    result = {
        "generation": record["generation"],
        "created_at": record.get("created_at", ""),
        "reason": record.get("reason", "unknown"),
        "size": record.get("size", len(str(record.get("contents", "")).encode("utf-8"))),
        "rule_count": record.get("rule_count", _rule_count(str(record.get("contents", "")))),
    }
    if include_contents:
        result["contents"] = record.get("contents", "")
    return result


def _valid_history(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    history: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        contents = item.get("contents")
        generation = item.get("generation")
        if not isinstance(contents, str) or not isinstance(generation, str):
            continue
        history.append({
            **item,
            "generation": generation,
            "contents": contents,
        })
    return history[:RULE_HISTORY_LIMIT]


def _generation_for(contents: str) -> str:
    return sha256(contents.encode("utf-8")).hexdigest()


def _rule_count(contents: str) -> int:
    try:
        return len(load_rules_from_string(contents))
    except RuleLoadError:
        return 0
