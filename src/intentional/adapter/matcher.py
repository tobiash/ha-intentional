"""Service-plan matching: compare a cached plan against actual HA state.

Given a frozen service-plan signature and an HA-style State object, determine
whether the actual state is consistent with what the plan would produce. Used
by Reconciliation to decide whether to skip a redundant call or promote drift.
"""

from __future__ import annotations

from typing import Any

from ..registry import ALARM_STATE_SERVICES
from . import ServicePlanSignature


def service_plan_matches_state(plan: ServicePlanSignature, state: Any) -> bool:
    """Return True if an HA state object is consistent with a cached plan."""
    for domain, service, data_items in plan:
        data = dict(data_items)
        if data.get("entity_id") != state.entity_id:
            return False
        expected_states = _expected_states_for_service(domain, service)
        if expected_states is not None and str(state.state) not in expected_states:
            return False
        for field, expected in data.items():
            if field in {"entity_id", "transition"}:
                continue
            actual = _state_field_value(state, field)
            if actual is not None and not _values_match(actual, expected, field=field):
                return False
    return True


def _expected_states_for_service(domain: str, service: str) -> set[str] | None:
    """Return the HA states implied by a service call, when there are any."""
    if domain in {
        "button",
        "input_button",
        "notify",
        "browser_mod",
        "telegram_bot",
        "tts",
        "alert",
        "rest_command",
        "persistent_notification",
        "logbook",
        "system_log",
        "scheduler",
        "cast",
        "shopping_list",
        "intentional",
        "homeassistant",
        "mqtt",
        "google_assistant",
        "assist_satellite",
        "alarmo",
        "device_tracker",
        "camera",
        "update",
        "scene",
        "siren",
    }:
        return None
    if domain == "script" and service == "turn_on":
        return None
    if domain == "automation" and service == "trigger":
        return None
    if domain == "timer":
        if service == "start":
            return {"active"}
        if service == "pause":
            return {"paused"}
        if service in {"cancel", "finish"}:
            return {"idle"}
    if service == "turn_off":
        return {"off"}
    if service == "turn_on":
        if domain == "media_player":
            return None
        return {"on"}
    if service == "toggle":
        return None
    if domain == "media_player":
        if service == "media_play":
            return {"playing"}
        if service == "media_pause":
            return {"paused"}
        if service == "media_stop":
            return {"idle", "stopped"}
    if domain == "cover":
        if service == "open_cover":
            return {"open", "opening"}
        if service == "close_cover":
            return {"closed", "closing"}
    if domain == "valve":
        if service == "open_valve":
            return {"open", "opening"}
        if service == "close_valve":
            return {"closed", "closing"}
    if domain == "lock":
        if service == "lock":
            return {"locked", "locking"}
        if service == "unlock":
            return {"unlocked", "unlocking"}
    if domain == "alarm_control_panel":
        if service == "alarm_disarm":
            return {"disarmed"}
        state_by_service = {
            service: state
            for state, service in ALARM_STATE_SERVICES.items()
            if state.startswith("armed_")
        }
        if service in state_by_service:
            return {state_by_service[service]}
    if domain == "humidifier":
        if service == "turn_on":
            return {"on"}
        if service == "turn_off":
            return {"off"}
    if domain == "water_heater":
        if service == "turn_off":
            return {"off"}
        if service == "turn_on":
            return {"on"}
    if domain == "vacuum":
        if service == "start":
            return {"cleaning"}
        if service == "pause":
            return {"paused"}
        if service == "return_to_base":
            return {"returning", "docked"}
        if service == "stop":
            return {"idle"}
        if service == "turn_off":
            return {"off"}
    if domain == "lawn_mower":
        if service == "start_mowing":
            return {"mowing"}
        if service == "pause":
            return {"paused"}
        if service == "dock":
            return {"returning", "docked"}
    return None


def _state_field_value(state: Any, field: str) -> Any:
    """Return a comparable value for a service-data field from an HA state."""
    attributes = getattr(state, "attributes", {})
    if field in attributes:
        return attributes[field]
    if field == "brightness_pct" and "brightness" in attributes:
        return round(float(attributes["brightness"]) * 100 / 255)
    if field == "color_temp_kelvin" and "color_temp_k" in attributes:
        return attributes["color_temp_k"]
    if field == "color_temp" and "color_temp_mired" in attributes:
        return attributes["color_temp_mired"]
    if field == "position" and "current_position" in attributes:
        return attributes["current_position"]
    if field == "position" and field in attributes:
        return attributes[field]
    if field == "tilt_position" and "current_tilt_position" in attributes:
        return attributes["current_tilt_position"]
    if field == "tilt_position" and field in attributes:
        return attributes[field]
    if field == "hvac_mode":
        return state.state
    if field in {"option", "value"}:
        return state.state
    if field in {"datetime", "date", "time"}:
        return state.state
    if field == "timestamp" and field in attributes:
        return attributes[field]
    if field == "duration" and field in attributes:
        return attributes[field]
    if field == "humidity" and "target_humidity" in attributes:
        return attributes["target_humidity"]
    if field == "operation_mode":
        return attributes.get("operation_mode", state.state)
    if field == "temperature":
        if "target_temperature" in attributes:
            return attributes["target_temperature"]
        if field in attributes:
            return attributes[field]
    if field == "away_mode" and field in attributes:
        return attributes[field]
    if field == "fan_speed" and field in attributes:
        return attributes[field]
    if field == "activity":
        if "current_activity" in attributes:
            return attributes["current_activity"]
        if field in attributes:
            return attributes[field]
    return None


def _values_match(actual: Any, expected: Any, *, field: str | None = None) -> bool:
    """Compare HA state attributes with service data, allowing small rounding gaps."""
    if isinstance(actual, list | tuple) and isinstance(expected, list | tuple):
        if len(actual) != len(expected):
            return False
        return all(
            _values_match(actual_item, expected_item, field=field)
            for actual_item, expected_item in zip(actual, expected, strict=True)
        )
    tolerance = _numeric_tolerance(field)
    if isinstance(actual, int | float) and isinstance(expected, int | float):
        return abs(float(actual) - float(expected)) <= tolerance
    if isinstance(expected, int | float):
        try:
            return abs(float(actual) - float(expected)) <= tolerance
        except (TypeError, ValueError):
            pass
    return actual == expected


def _numeric_tolerance(field: str | None) -> int:
    if field in {"color_temp_kelvin", "color_temp_k"}:
        return 25
    if field == "brightness_pct":
        return 2
    return 1
