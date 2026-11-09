"""Document-level safety policy for durable Targets."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TargetPolicy:
    """Canonical dispatch and ownership constraints for one Target."""

    ownership: str = "managed"
    dispatch: str = "apply"
    allowed_fields: frozenset[str] | None = None
    forbidden_automatic_states: frozenset[str] = frozenset()
    unavailable: str = "allow"
    max_retries: int | None = None
    user_authority_fields: frozenset[str] = frozenset()
    user_authority_states: frozenset[str] = frozenset()
    manual_resume: str = "timed"
    manual_detection: str = "all"
    manual_duration_ms: int | None = None
    vacancy_entity: str | None = None
    vacancy_for_ms: int = 30_000

    def as_dict(self) -> dict[str, Any]:
        """Return a stable JSON-friendly representation."""
        return {
            "ownership": self.ownership,
            "dispatch": self.dispatch,
            "allowed_fields": None if self.allowed_fields is None else sorted(self.allowed_fields),
            "forbidden_automatic_states": sorted(self.forbidden_automatic_states),
            "unavailable": self.unavailable,
            "max_retries": self.max_retries,
            "user_authority_fields": sorted(self.user_authority_fields),
            "user_authority_states": sorted(self.user_authority_states),
            "manual_override": {
                "resume": self.manual_resume,
                "unattributed": self.manual_detection,
                "duration_ms": self.manual_duration_ms,
                "vacancy_entity": self.vacancy_entity,
                "vacancy_for_ms": self.vacancy_for_ms,
            },
        }
