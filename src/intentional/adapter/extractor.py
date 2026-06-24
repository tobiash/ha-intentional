"""HA State → intent set extraction.

Reverse of the translator: given an HA-style State object, extract the
fields Intentional would treat as durable desired state. Used by the
drift classifier to capture what the user actually set.
"""

from __future__ import annotations

from typing import Any

_STATE_DOMAINS = frozenset({
    "light",
    "switch",
    "input_boolean",
    "media_player",
    "fan",
    "lock",
    "siren",
    "lawn_mower",
    "remote",
})

_ATTRIBUTE_FIELDS = frozenset({
    "brightness_pct",
    "brightness",
    "color_temp_k",
    "color_temp_mired",
    "rgb_color",
    "rgbw_color",
    "rgbww_color",
    "hs_color",
    "xy_color",
    "effect",
    "volume_level",
    "is_volume_muted",
    "source",
    "sound_mode",
    "shuffle",
    "repeat",
    "percentage",
    "preset_mode",
    "direction",
    "oscillating",
})


def manual_set_from_state_object(state: Any) -> dict[str, Any]:
    """Extract a user-intent set payload from an HA-style State object."""
    domain, sep, _object_id = state.entity_id.partition(".")
    if not sep:
        return {}

    attributes = getattr(state, "attributes", {})
    state_value = state.state
    result: dict[str, Any] = {}

    if domain in _STATE_DOMAINS:
        result["state"] = state_value
    if domain == "alarm_control_panel":
        result["state"] = state_value
    if domain == "cover":
        result["state"] = state_value
        if "current_position" in attributes:
            result["position"] = attributes["current_position"]
        if "current_tilt_position" in attributes:
            result["tilt_position"] = attributes["current_tilt_position"]
    if domain == "valve":
        result["state"] = state_value
        if "current_position" in attributes:
            result["position"] = attributes["current_position"]
        elif "position" in attributes:
            result["position"] = attributes["position"]
    if domain == "climate":
        result["hvac_mode"] = state_value
        for field in (
            "temperature",
            "target_temp_low",
            "target_temp_high",
            "preset_mode",
            "fan_mode",
            "target_humidity",
            "humidity",
            "swing_mode",
            "swing_horizontal_mode",
            "aux_heat",
        ):
            if field in attributes:
                if field == "target_humidity":
                    result["humidity"] = attributes[field]
                else:
                    result[field] = attributes[field]
    if domain in {"number", "input_number", "counter"}:
        result["value"] = attributes.get("value", state_value)
    if domain in {"select", "input_select"}:
        result["option"] = state_value
    if domain in {"text", "input_text"}:
        result["value"] = state_value
    if domain == "input_datetime":
        has_date = attributes.get("has_date")
        has_time = attributes.get("has_time")
        if "timestamp" in attributes:
            result["timestamp"] = attributes["timestamp"]
        if has_date is True and has_time is False:
            result["date"] = state_value
        elif has_time is True and has_date is False:
            result["time"] = state_value
        else:
            result["datetime"] = state_value
    if domain == "timer":
        result["state"] = state_value
        if "duration" in attributes:
            result["duration"] = attributes["duration"]
    if domain == "humidifier":
        result["state"] = state_value
        if "target_humidity" in attributes:
            result["humidity"] = attributes["target_humidity"]
        if "mode" in attributes:
            result["mode"] = attributes["mode"]
    if domain == "water_heater":
        result["state"] = state_value
        if state_value not in {"on", "off", "unknown", "unavailable"}:
            result["operation_mode"] = state_value
        if "target_temperature" in attributes:
            result["temperature"] = attributes["target_temperature"]
        elif "temperature" in attributes:
            result["temperature"] = attributes["temperature"]
        if "operation_mode" in attributes:
            result["operation_mode"] = attributes["operation_mode"]
        if "away_mode" in attributes:
            result["away_mode"] = attributes["away_mode"]
    if domain == "vacuum":
        result["state"] = state_value
        if "fan_speed" in attributes:
            result["fan_speed"] = attributes["fan_speed"]
    if domain == "fan":
        for field in ("preset_mode", "direction", "oscillating"):
            if field in attributes:
                result[field] = attributes[field]
    if domain == "remote":
        if "current_activity" in attributes:
            result["activity"] = attributes["current_activity"]
        elif "activity" in attributes:
            result["activity"] = attributes["activity"]
    if domain == "camera":
        result["state"] = state_value

    for field in _ATTRIBUTE_FIELDS:
        if field in attributes:
            result[field] = attributes[field]
    if "color_temp_kelvin" in attributes:
        result["color_temp_k"] = attributes["color_temp_kelvin"]
    if "color_temp" in attributes:
        result["color_temp_mired"] = attributes["color_temp"]

    return result
