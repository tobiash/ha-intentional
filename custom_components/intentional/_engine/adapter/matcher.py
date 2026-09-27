"""Service-plan matching: compare a cached plan against actual HA state.

Given a frozen service-plan signature and an HA-style State object, determine
whether the actual state is consistent with what the plan would produce. Used
by Reconciliation to decide whether to skip a redundant call or promote drift.
"""

from __future__ import annotations

import math
from enum import Enum
from typing import Any

from ..registry import (
    ALARM_STATE_SERVICES,
    HA_SERVICE_FIELD_STATE_ATTRIBUTES,
    LIGHT_COLOR_FIELDS,
)
from . import ServicePlanSignature

_MISSING = object()
_FLOAT_EPSILON = 1e-9
_ACTION_ONLY_DOMAINS = frozenset({
    "alert", "assist_satellite", "browser_mod", "button", "cast",
    "google_assistant", "homeassistant", "input_button", "intentional",
    "logbook", "mqtt", "notify", "persistent_notification", "rest_command",
    "scene", "scheduler", "shopping_list", "system_log", "telegram_bot", "tts",
})


class ServicePlanMatch(Enum):
    """How conclusively actual HA state agrees with a service plan."""

    MATCH = "match"
    MISMATCH = "mismatch"
    UNKNOWN = "unknown"


def service_plan_matches_state(plan: ServicePlanSignature, state: Any) -> bool:
    """Return True if an HA state object is consistent with a cached plan."""
    return service_plan_match(plan, state) is ServicePlanMatch.MATCH


def service_plan_has_observable_postcondition(plan: ServicePlanSignature) -> bool:
    """Return whether at least one plan call has durable observable state."""
    return any(
        domain not in _ACTION_ONLY_DOMAINS
        and not (domain == "script" and service == "turn_on")
        and not (domain == "automation" and service == "trigger")
        for domain, service, _data_items in plan
    )


def service_plan_match(plan: ServicePlanSignature, state: Any) -> ServicePlanMatch:
    """Compare a plan with state without treating unreported fields as conflicts."""
    if str(state.state) in {"unknown", "unavailable"}:
        return ServicePlanMatch.UNKNOWN
    unknown = False
    for domain, service, data_items in plan:
        data = dict(data_items)
        if data.get("entity_id") != state.entity_id:
            return ServicePlanMatch.MISMATCH
        state_match = _service_state_match(domain, service, state)
        if state_match is ServicePlanMatch.MISMATCH:
            return ServicePlanMatch.MISMATCH
        if state_match is ServicePlanMatch.UNKNOWN:
            unknown = True
        for field, expected in data.items():
            if field in {"entity_id", "transition"} or (
                domain == "alarm_control_panel" and field == "code"
            ):
                continue
            if domain == "light" and not _light_color_mode_matches(state, field):
                return ServicePlanMatch.MISMATCH
            if (
                domain in {"cover", "valve"}
                and field == "position"
                and str(state.state) in {"closing", "opening"}
            ):
                actual = _state_field_value(state, field)
                if actual is _MISSING or actual is None:
                    unknown = True
                    continue
                try:
                    moving_toward = (
                        str(state.state) == "opening" and float(expected) >= float(actual)
                    ) or (
                        str(state.state) == "closing" and float(expected) <= float(actual)
                    )
                except (TypeError, ValueError):
                    moving_toward = False
                if not moving_toward:
                    return ServicePlanMatch.MISMATCH
                unknown = True
                continue
            actual = _state_field_value(state, field)
            if actual is _MISSING or actual is None:
                unknown = True
            elif not _values_match(actual, expected, field=field):
                return ServicePlanMatch.MISMATCH
    return ServicePlanMatch.UNKNOWN if unknown else ServicePlanMatch.MATCH


def service_plan_mismatched_fields(
    plan: ServicePlanSignature, state: Any
) -> frozenset[str]:
    """Return durable desired fields conclusively contradicted by actual state."""
    if str(state.state) in {"unknown", "unavailable"}:
        return frozenset()
    mismatched: set[str] = set()

    def add(field: str) -> None:
        canonical = {
            "color_temp": "color_temp_mired",
            "color_temp_kelvin": "color_temp_k",
        }.get(field, field)
        if canonical in LIGHT_COLOR_FIELDS:
            mismatched.update(LIGHT_COLOR_FIELDS)
        else:
            mismatched.add(canonical)
    for domain, service, data_items in plan:
        data = dict(data_items)
        if data.get("entity_id") != state.entity_id:
            return frozenset({"state"})
        if _service_state_match(domain, service, state) is ServicePlanMatch.MISMATCH:
            add("state")
        for field, expected in data.items():
            if field in {"entity_id", "transition"} or (
                domain == "alarm_control_panel" and field == "code"
            ):
                continue
            if domain == "light" and not _light_color_mode_matches(state, field):
                add(field)
                continue
            actual = _state_field_value(state, field)
            if actual is _MISSING or actual is None:
                continue
            if (
                domain in {"cover", "valve"}
                and field == "position"
                and str(state.state) in {"closing", "opening"}
            ):
                try:
                    moving_toward = (
                        str(state.state) == "opening" and float(expected) >= float(actual)
                    ) or (
                        str(state.state) == "closing" and float(expected) <= float(actual)
                    )
                except (TypeError, ValueError):
                    moving_toward = False
                if not moving_toward:
                    add(field)
                continue
            if not _values_match(actual, expected, field=field):
                add(field)
    return frozenset(mismatched)


def _light_color_mode_matches(state: Any, field: str) -> bool:
    expected_modes = {
        "color_temp": {"color_temp"},
        "color_temp_kelvin": {"color_temp"},
        "rgb_color": {"rgb"},
        "rgbw_color": {"rgbw"},
        "rgbww_color": {"rgbww"},
        "hs_color": {"hs"},
        "xy_color": {"xy"},
    }.get(field)
    if expected_modes is None:
        return True
    color_mode = getattr(state, "attributes", {}).get("color_mode")
    return color_mode is None or str(color_mode) in expected_modes


def _service_state_match(domain: str, service: str, state: Any) -> ServicePlanMatch:
    actual = str(state.state)
    attributes = getattr(state, "attributes", {})
    if actual in {"unknown", "unavailable"}:
        return ServicePlanMatch.UNKNOWN
    if domain == "camera" and service in {"turn_on", "turn_off"}:
        if actual in {"streaming", "recording"}:
            return (
                ServicePlanMatch.MATCH
                if service == "turn_on"
                else ServicePlanMatch.MISMATCH
            )
        return ServicePlanMatch.UNKNOWN
    if domain == "media_player" and service in {"turn_on", "turn_off"}:
        operational = {"on", "idle", "playing", "paused", "buffering"}
        if actual in operational:
            return (
                ServicePlanMatch.MATCH
                if service == "turn_on"
                else ServicePlanMatch.MISMATCH
            )
        if actual == "off":
            return (
                ServicePlanMatch.MATCH
                if service == "turn_off"
                else ServicePlanMatch.MISMATCH
            )
        return ServicePlanMatch.UNKNOWN
    if domain == "climate" and service == "turn_on":
        valid_modes = {"heat", "cool", "heat_cool", "auto", "dry", "fan_only"}
        if actual in valid_modes:
            return ServicePlanMatch.MATCH
        return ServicePlanMatch.MISMATCH if actual == "off" else ServicePlanMatch.UNKNOWN
    if domain == "water_heater" and service == "turn_on":
        return ServicePlanMatch.MISMATCH if actual == "off" else ServicePlanMatch.MATCH
    if domain in {"cover", "valve"}:
        open_service = service in {"open_cover", "open_valve"}
        close_service = service in {"close_cover", "close_valve"}
        if open_service:
            if actual == "open":
                return ServicePlanMatch.MATCH
            return ServicePlanMatch.UNKNOWN if actual == "opening" else ServicePlanMatch.MISMATCH
        if close_service:
            if actual == "closed":
                return ServicePlanMatch.MATCH
            return ServicePlanMatch.UNKNOWN if actual == "closing" else ServicePlanMatch.MISMATCH
    if domain == "alarm_control_panel":
        if service == "alarm_disarm":
            if actual == "disarmed":
                return ServicePlanMatch.MATCH
            return ServicePlanMatch.UNKNOWN if actual == "disarming" else ServicePlanMatch.MISMATCH
        desired_by_service = {
            service_name: alarm_state
            for alarm_state, service_name in ALARM_STATE_SERVICES.items()
            if alarm_state.startswith("armed_")
        }
        if service in desired_by_service:
            desired = desired_by_service[service]
            if actual == desired:
                return ServicePlanMatch.MATCH
            if actual == "arming":
                next_state = attributes.get("next_state")
                if next_state is None or next_state == desired:
                    return ServicePlanMatch.UNKNOWN
                return ServicePlanMatch.MISMATCH
            if actual in {"pending", "triggered"}:
                return ServicePlanMatch.UNKNOWN
            return ServicePlanMatch.MISMATCH
    expected_states = _expected_states_for_service(domain, service)
    if expected_states is not None and actual not in expected_states:
        return ServicePlanMatch.MISMATCH
    return ServicePlanMatch.MATCH


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
        "update",
        "scene",
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
        if domain in {"climate", "media_player", "water_heater"}:
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
    if domain == "water_heater" and service == "turn_off":
        return {"off"}
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
    for attribute in HA_SERVICE_FIELD_STATE_ATTRIBUTES.get(field, ()):
        if attribute in attributes:
            actual = attributes[attribute]
            if field == "brightness_pct":
                return round(float(actual) * 100 / 255)
            return actual
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
    if field == "operation_mode":
        return attributes.get("operation_mode", state.state)
    if field == "away_mode" and field in attributes:
        return attributes[field]
    if field == "fan_speed" and field in attributes:
        return attributes[field]
    return _MISSING


def _values_match(actual: Any, expected: Any, *, field: str | None = None) -> bool:
    """Compare HA state attributes with service data, allowing small rounding gaps."""
    if isinstance(actual, list | tuple) and isinstance(expected, list | tuple):
        if len(actual) != len(expected):
            return False
        return all(
            _values_match(actual_item, expected_item, field=field)
            for actual_item, expected_item in zip(actual, expected, strict=True)
        )
    if isinstance(actual, bool) or isinstance(expected, bool):
        return type(actual) is type(expected) and actual == expected
    tolerance = _numeric_tolerance(field)
    if isinstance(actual, int | float) and isinstance(expected, int | float):
        if not math.isfinite(float(actual)) or not math.isfinite(float(expected)):
            return False
        return math.isclose(
            float(actual),
            float(expected),
            rel_tol=0,
            abs_tol=tolerance + _FLOAT_EPSILON,
        )
    if isinstance(expected, int | float):
        try:
            if not math.isfinite(float(actual)) or not math.isfinite(float(expected)):
                return False
            return math.isclose(
                float(actual),
                float(expected),
                rel_tol=0,
                abs_tol=tolerance + _FLOAT_EPSILON,
            )
        except (TypeError, ValueError):
            pass
    return actual == expected


def _numeric_tolerance(field: str | None) -> float:
    if field in {"color_temp_kelvin", "color_temp_k"}:
        return 25
    if field == "brightness_pct":
        return 2
    if field in {"volume_level", "xy_color"}:
        return 0.01
    if field in {"temperature", "target_temp_low", "target_temp_high"}:
        return 0.1
    return 1
