"""Machine-readable DSL capability schema."""

from __future__ import annotations

from typing import Any


def dsl_schema() -> dict[str, Any]:
    """Return Intentional's agent-facing DSL capabilities."""
    return {
        "dsl_version": "vnext-draft",
        "top_level_rule_fields": [
            "id", "enabled", "labels", "notes", "while", "after", "stable_for", "hold", "observe", "intent",
            "effect", "authority", "confidence", "reason", "group", "profile",
        ],
        "top_level_document_fields": ["rules", "scenes", "targets"],
        "target_policy_fields": [
            "default", "ownership", "allowed_fields", "forbidden_automatic_states",
            "unavailable", "max_retries", "user_authority",
        ],
        "target_ownership": ["managed", "opportunistic", "observe_only"],
        "target_unavailable_policy": ["allow", "skip"],
        "observe_operators": [
            "is", "is_not", "lt", "lte", "gt", "gte", "all", "any",
            "not", "none", "changed", "happened", "for", "stable_for",
        ],
        "intent_field_operators": [
            "value", "min", "max", "offset", "multiply", "animate", "generate",
        ],
        "generator_kinds": [
            "sample", "walk", "weighted_sample", "gradient", "noise",
        ],
        "target_metadata": ["ttl", "linger", "transition", "easing"],
        "lifecycle_fields": [
            "while", "after", "stable_for", "hold.while", "hold.until", "hold.until.for",
            "hold.after", "hold.after_when_stops",
        ],
        "simulation_endpoint": "/api/intentional/simulate",
        "replay_endpoint": "/api/intentional/replay",
        "simulation_endpoints": ["/api/intentional/simulate", "/api/intentional/replay"],
        "effect_service_policy": "any_home_assistant_domain_service",
        "selector_filters": ["domain", "area", "label", "exclude"],
        "semantic_observations": {
            "purposes": {
                "motion": ["binary_sensor", "motion"],
                "occupancy": ["binary_sensor", "occupancy"],
                "door": ["binary_sensor", "door"],
                "window": ["binary_sensor", "window"],
                "moisture": ["binary_sensor", "moisture"],
                "temperature": ["sensor", "temperature"],
                "illuminance": ["sensor", "illuminance"],
            },
            "authored_filters": ["area", "entity", "device", "exclude"],
            "behavior": ["any", "all", "none"],
            "binary_states": {
                "motion": ["detected", "clear"],
                "occupancy": ["occupied", "clear"],
                "door": ["open", "closed"],
                "window": ["open", "closed"],
                "moisture": ["wet", "dry"],
            },
            "numeric_comparisons": ["above", "below", "is", "is_not", "lt", "lte", "gt", "gte"],
            "edge_field": "changed",
        },
        "simulation_selector_membership": {
            "field": "selectors",
            "selector_filters": ["domain", "area", "label", "device", "entity", "purpose"],
            "target_field": "targets",
        },
        "simulation_semantic_metadata": {
            "field": "semantic_metadata",
            "record_fields": [
                "entity_id", "area", "device", "device_class", "original_device_class",
            ],
            "required_fields": ["entity_id"],
            "device_class_precedence": ["device_class", "original_device_class"],
        },
    }
