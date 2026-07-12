"""Validation warnings for authored Intentional rules."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant


def validation_warnings(hass: HomeAssistant, rules: list[Any]) -> list[dict[str, Any]]:
    """Return non-blocking warnings for syntactically valid rules."""
    warnings: list[dict[str, Any]] = []
    for rule in rules:
        if _looks_like_presence_light_rule_without_stability(rule):
            warnings.append({
                "code": "presence_light_without_stability",
                "rule_id": rule.id,
                "message": "Presence-driven light rule has no dwell (`after`/`for`) and no retention (`hold.until.for` or target `linger`); short sensor flaps can toggle lights.",
            })
        warnings.extend(_live_capability_warnings(hass, rule))
    return warnings


def _looks_like_presence_light_rule_without_stability(rule: Any) -> bool:
    target = getattr(rule, "target", "")
    if not isinstance(target, str) or not target.startswith("light."):
        return False
    if (
        getattr(rule, "for_ms", 0)
        or getattr(rule, "hold_until_for_ms", 0)
        or getattr(rule, "linger_ms", None) is not None
        or getattr(rule, "dynamic_hold_after", None) is not None
    ):
        return False
    set_payload = getattr(rule, "set", {}) or {}
    if set_payload.get("state") not in {True, "on", "true", "True"}:
        return False
    when = str(getattr(rule, "when", "")).lower()
    return "presence" in when or "occupancy" in when or "motion" in when


def _live_capability_warnings(hass: HomeAssistant, rule: Any) -> list[dict[str, Any]]:
    target = getattr(rule, "target", "")
    if not isinstance(target, str) or not target.startswith("light."):
        return []
    state = hass.states.get(target)
    if state is None:
        return []
    supported = set((state.attributes or {}).get("supported_color_modes") or [])
    if not supported:
        return []
    warnings: list[dict[str, Any]] = []
    set_payload = getattr(rule, "set", {}) or {}
    fields = set(set_payload) | set(getattr(rule, "cap", {}) or {}) | set(getattr(rule, "floor", {}) or {})
    if "color_temp_k" in fields and "color_temp" not in supported:
        warnings.append({
            "code": "unsupported_light_color_temp",
            "rule_id": rule.id,
            "target": target,
            "field": "color_temp_k",
            "message": f"{target} does not advertise color temperature support.",
        })
    color_fields = {"rgb_color", "rgbw_color", "rgbww_color", "hs_color", "xy_color"}
    if fields & color_fields and not (supported & {"rgb", "rgbw", "rgbww", "hs", "xy"}):
        warnings.append({
            "code": "unsupported_light_color",
            "rule_id": rule.id,
            "target": target,
            "fields": sorted(fields & color_fields),
            "message": f"{target} does not advertise color support for {sorted(fields & color_fields)}.",
        })
    return warnings
