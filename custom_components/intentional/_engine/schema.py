"""Machine-readable DSL capability schema."""

from __future__ import annotations

from typing import Any


def dsl_schema() -> dict[str, Any]:
    """Return Intentional's agent-facing DSL capabilities."""
    return {
        "dsl_version": "vnext-draft",
        "top_level_rule_fields": [
            "id", "enabled", "labels", "notes", "while", "after", "hold", "observe", "intent",
            "effect", "authority", "confidence", "reason", "group", "profile",
        ],
        "observe_operators": [
            "is", "is_not", "lt", "lte", "gt", "gte", "all", "any",
            "not", "none", "changed", "happened", "for",
        ],
        "intent_field_operators": [
            "value", "min", "max", "offset", "multiply", "animate", "generate",
        ],
        "generator_kinds": [
            "sample", "walk", "weighted_sample", "gradient", "noise",
        ],
        "target_metadata": ["ttl", "linger", "transition", "easing"],
        "lifecycle_fields": [
            "while", "after", "hold.while", "hold.until", "hold.until.for",
            "hold.after", "hold.after_when_stops",
        ],
        "simulation_endpoint": "/api/intentional/simulate",
        "effect_service_policy": "any_home_assistant_domain_service",
        "selector_filters": ["domain", "area", "label", "exclude"],
    }
