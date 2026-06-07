"""Presentation helpers for Home Assistant-facing intent entities."""

from __future__ import annotations

from typing import Any


def intent_sensor_state(resolved: Any | None) -> str:
    """Return a compact sensor state for a resolved target intent."""
    if resolved is None:
        return "idle"
    winner = getattr(resolved, "winning_intent", None)
    authority = getattr(getattr(winner, "authority", None), "value", None)
    if authority == "user":
        return "manual_override"
    return "active"


def value_summary(value: dict[str, Any]) -> str:
    """Return a human-readable summary of a desired state mapping."""
    if not value:
        return "no desired state"

    parts: list[str] = []
    state = value.get("state")
    if state is not None:
        parts.append(str(state))

    if "brightness_pct" in value:
        parts.append(f"{value['brightness_pct']}%")
    elif "brightness" in value:
        parts.append(f"brightness {value['brightness']}")

    if "color_temp_k" in value:
        parts.append(f"{value['color_temp_k']} K")
    elif "color_temp_mired" in value:
        parts.append(f"{value['color_temp_mired']} mired")

    if "effect" in value:
        parts.append(f"effect {value['effect']}")

    remaining = [
        key for key in sorted(value)
        if key not in {"state", "brightness_pct", "brightness", "color_temp_k", "color_temp_mired", "effect"}
    ]
    parts.extend(f"{key}={value[key]}" for key in remaining)
    return " · ".join(parts)
