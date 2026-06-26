"""Pure Home Assistant adapter helpers for the intent engine."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from intentional.adapter import FrozenValue as FrozenValue
from intentional.adapter import SceneActivationPlan as SceneActivationPlan
from intentional.adapter import ServiceCall as ServiceCall
from intentional.adapter import ServicePlanSignature as ServicePlanSignature
from intentional.adapter import ServiceSignature as ServiceSignature
from intentional.adapter.extractor import (
    manual_set_from_state_object as manual_set_from_state_object,
)
from intentional.adapter.matcher import service_plan_matches_state as service_plan_matches_state
from intentional.adapter.signer import _freeze_signature_value as _freeze_signature_value
from intentional.adapter.signer import service_plan_signature as service_plan_signature
from intentional.adapter.signer import service_signature as service_signature
from intentional.engine import Engine
from intentional.registry import ALARM_STATE_SERVICES as ALARM_STATE_SERVICES
from intentional.registry import LIGHT_COLOR_FIELDS

MANUAL_SET_FIELDS = (
    "state",
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
    "flash",
    "volume_level",
    "is_volume_muted",
    "tone",
    "source",
    "sound_mode",
    "media_action",
    "media_content_id",
    "media_content_type",
    "enqueue",
    "announce",
    "extra",
    "shuffle",
    "repeat",
    "seek_position",
    "group_members",
    "position",
    "tilt_position",
    "percentage",
    "hvac_mode",
    "temperature",
    "target_temp_low",
    "target_temp_high",
    "preset_mode",
    "fan_mode",
    "direction",
    "oscillating",
    "humidity",
    "swing_mode",
    "swing_horizontal_mode",
    "aux_heat",
    "mode",
    "operation_mode",
    "away_mode",
    "fan_speed",
    "camera_action",
    "filename",
    "media_player",
    "format",
    "lookback",
    "command",
    "params",
    "cleaning_area_id",
    "activity",
    "device",
    "num_repeats",
    "delay_secs",
    "hold_secs",
    "value",
    "option",
    "cycle",
    "code",
    "message",
    "name",
    "title",
    "data",
    "service",
    "service_data",
    "media_player_entity_id",
    "cache",
    "language",
    "options",
    "browser_id",
    "user_id",
    "path",
    "action_text",
    "action",
    "parse_mode",
    "disable_notification",
    "disable_web_page_preview",
    "keyboard",
    "inline_keyboard",
    "message_tag",
    "chat_id",
    "todo_action",
    "item",
    "rename",
    "status",
    "due_date",
    "due_datetime",
    "description",
    "variables",
    "skip_condition",
    "datetime",
    "date",
    "time",
    "timestamp",
    "duration",
    "reverse",
    "update_action",
    "version",
    "backup",
    "mac",
    "dev_id",
    "host_name",
    "location_name",
    "gps",
    "gps_accuracy",
    "battery",
    "update_entity",
)
COVER_STATE_SERVICES = {
    "open": "open_cover",
    "opening": "open_cover",
    "closed": "close_cover",
    "closing": "close_cover",
    "stop": "stop_cover",
    "stopped": "stop_cover",
    "toggle": "toggle",
    "tilt_open": "open_cover_tilt",
    "tilt_closed": "close_cover_tilt",
    "tilt_close": "close_cover_tilt",
    "tilt_stop": "stop_cover_tilt",
    "tilt_stopped": "stop_cover_tilt",
    "tilt_toggle": "toggle_tilt",
}
COUNTER_STATE_SERVICES = {
    "increment": "increment",
    "decrement": "decrement",
    "reset": "reset",
}
INPUT_NUMBER_STATE_SERVICES = {
    "increment": "increment",
    "decrement": "decrement",
}
ALERT_STATE_SERVICES = {
    "on": "turn_on",
    "off": "turn_off",
    "toggle": "toggle",
}
SELECT_STATE_SERVICES = {
    "next": "select_next",
    "previous": "select_previous",
    "prev": "select_previous",
}
INPUT_SELECT_STATE_SERVICES = {
    **SELECT_STATE_SERVICES,
    "first": "select_first",
    "last": "select_last",
}
LOCK_STATE_SERVICES = {
    "locked": "lock",
    "lock": "lock",
    "unlocked": "unlock",
    "unlock": "unlock",
}
VACUUM_STATE_SERVICES = {
    "on": "turn_on",
    "off": "turn_off",
    "start": "start",
    "cleaning": "start",
    "pause": "pause",
    "paused": "pause",
    "stop": "stop",
    "stopped": "stop",
    "idle": "stop",
    "return_to_base": "return_to_base",
    "returning": "return_to_base",
    "docked": "return_to_base",
    "locate": "locate",
    "clean_spot": "clean_spot",
    "start_pause": "start_pause",
    "toggle": "toggle",
}
VALVE_STATE_SERVICES = {
    "open": "open_valve",
    "opening": "open_valve",
    "closed": "close_valve",
    "closing": "close_valve",
    "stop": "stop_valve",
    "stopped": "stop_valve",
    "toggle": "toggle",
}
LAWN_MOWER_STATE_SERVICES = {
    "start": "start_mowing",
    "mowing": "start_mowing",
    "pause": "pause",
    "paused": "pause",
    "dock": "dock",
    "docked": "dock",
    "returning": "dock",
    "return_to_base": "dock",
}
REMOTE_STATE_SERVICES = {
    "on": "turn_on",
    "off": "turn_off",
    "toggle": "toggle",
}
TIMER_STATE_SERVICES = {
    "active": "start",
    "start": "start",
    "on": "start",
    "idle": "cancel",
    "cancel": "cancel",
    "off": "cancel",
    "paused": "pause",
    "pause": "pause",
    "finish": "finish",
    "finished": "finish",
}
MEDIA_PLAYER_STATE_SERVICES = {
    "toggle": "toggle",
    "play": "media_play",
    "playing": "media_play",
    "pause": "media_pause",
    "paused": "media_pause",
    "stop": "media_stop",
    "stopped": "media_stop",
    "idle": "media_stop",
    "play_pause": "media_play_pause",
    "next": "media_next_track",
    "next_track": "media_next_track",
    "previous": "media_previous_track",
    "previous_track": "media_previous_track",
    "clear_playlist": "clear_playlist",
    "volume_up": "volume_up",
    "volume_down": "volume_down",
    "unjoin": "unjoin",
}
MEDIA_PLAYER_ACTION_SERVICES = {
    **MEDIA_PLAYER_STATE_SERVICES,
    "mute": "volume_mute",
    "unmute": "volume_mute",
    "play_media": "play_media",
    "select_source": "select_source",
    "select_sound_mode": "select_sound_mode",
    "shuffle_set": "shuffle_set",
    "repeat_set": "repeat_set",
    "seek": "media_seek",
    "media_seek": "media_seek",
    "join": "join",
}
ACTION_SERVICE_FIELDS = frozenset({
    "message",
    "name",
    "title",
    "data",
    "media_player_entity_id",
    "cache",
    "language",
    "options",
    "browser_id",
    "user_id",
    "path",
    "action_text",
    "action",
    "parse_mode",
    "disable_notification",
    "disable_web_page_preview",
    "keyboard",
    "inline_keyboard",
    "message_tag",
    "chat_id",
    "duration",
    "reverse",
    "mac",
    "dev_id",
    "host_name",
    "location_name",
    "gps",
    "gps_accuracy",
    "battery",
})
TTS_SERVICE_TARGETS = frozenset({"speak", "cloud_say", "clear_cache"})
LIGHT_BRIGHTNESS_FIELDS = ("brightness", "brightness_pct")
LIGHT_FIELD_ALIASES = {
    "color_temp_k": "color_temp_kelvin",
    "color_temp_mired": "color_temp",
}


def manual_set_from_service_data(data: dict[str, Any]) -> dict[str, Any]:
    """Extract supported set fields from intentional.fire service data."""
    return {
        field: data[field]
        for field in MANUAL_SET_FIELDS
        if field in data
    }


def _add_light_turn_fields(service_data: dict[str, Any], value: dict[str, Any]) -> None:
    for key, val in value.items():
        if val is None:
            continue
        if key in {"state", "update_entity"}:
            continue
        if key in LIGHT_BRIGHTNESS_FIELDS or key in LIGHT_COLOR_FIELDS:
            continue
        service_data[key] = val

    for key in LIGHT_BRIGHTNESS_FIELDS:
        if key in value and value[key] is not None:
            service_data[key] = value[key]
            break

    for key in LIGHT_COLOR_FIELDS:
        if key in value and value[key] is not None:
            service_data[LIGHT_FIELD_ALIASES.get(key, key)] = value[key]
            break


def time_of_day_bucket(hour: int) -> str:
    """Return the Intentional time bucket for a local hour."""
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 17:
        return "afternoon"
    if 17 <= hour < 22:
        return "evening"
    return "night"


def sync_time_context_into_engine(engine: Engine, now: datetime | None = None) -> None:
    """Sync the local time helper used by `time_of_day` rule expressions."""
    current = now or datetime.now().astimezone()
    engine.set_time_of_day(
        time_of_day_bucket(current.hour),
        clock=f"{current.hour:02d}:{current.minute:02d}",
    )


def service_call_for_resolved_target(
    target: str,
    value: dict[str, Any],
    *,
    transition_ms: int = 0,
) -> tuple[str, str, dict[str, Any]] | None:
    """Translate a resolved target value into the first Home Assistant service call."""
    calls = service_calls_for_resolved_target(
        target,
        value,
        transition_ms=transition_ms,
    )
    return calls[0] if calls else None


def service_calls_for_resolved_target(
    target: str,
    value: dict[str, Any],
    *,
    transition_ms: int = 0,
) -> tuple[ServiceCall, ...]:
    """Translate a resolved target value into Home Assistant service calls."""
    calls = _service_calls_without_update_entity(
        target,
        value,
        transition_ms=transition_ms,
    )
    if value.get("update_entity") is True:
        calls = (
            *calls,
            ("homeassistant", "update_entity", {"entity_id": target}),
        )
    return calls


def _service_calls_without_update_entity(
    target: str,
    value: dict[str, Any],
    *,
    transition_ms: int = 0,
) -> tuple[ServiceCall, ...]:
    """Translate a resolved target value, excluding generic update_entity."""
    domain, sep, _object_id = target.partition(".")
    if not domain or not sep:
        return ()

    state = value.get("state")
    service_data = {"entity_id": target}

    if domain == "light":
        if state == "toggle":
            service = "toggle"
            _add_light_turn_fields(service_data, value)
        elif state == "off":
            service = "turn_off"
        else:
            service = "turn_on"
            _add_light_turn_fields(service_data, value)
        if transition_ms:
            service_data["transition"] = transition_ms / 1000.0
        return ((domain, service, service_data),)

    if domain in {"switch", "input_boolean"}:
        service = "toggle" if state == "toggle" else "turn_off" if state == "off" else "turn_on"
        return ((domain, service, service_data),)

    if domain == "media_player":
        if state == "off":
            return ((domain, "turn_off", service_data),)

        calls: list[ServiceCall] = []
        if state == "on":
            calls.append((domain, "turn_on", dict(service_data)))
        elif state in MEDIA_PLAYER_STATE_SERVICES:
            calls.append((domain, MEDIA_PLAYER_STATE_SERVICES[state], dict(service_data)))
        media_action = value.get("media_action")
        media_action_service = MEDIA_PLAYER_ACTION_SERVICES.get(media_action)
        if media_action in MEDIA_PLAYER_ACTION_SERVICES:
            service = MEDIA_PLAYER_ACTION_SERVICES[media_action]
            data = dict(service_data)
            if service == "volume_mute":
                data["is_volume_muted"] = media_action == "mute"
                if "is_volume_muted" in value:
                    data["is_volume_muted"] = value["is_volume_muted"]
            elif service == "play_media":
                if "media_content_id" not in value or "media_content_type" not in value:
                    data = {}
                else:
                    data["media_content_id"] = value["media_content_id"]
                    data["media_content_type"] = value["media_content_type"]
                    for field in ("enqueue", "announce", "extra"):
                        if field in value:
                            data[field] = value[field]
            elif service == "select_source":
                if "source" not in value:
                    data = {}
                else:
                    data["source"] = value["source"]
            elif service == "select_sound_mode":
                if "sound_mode" not in value:
                    data = {}
                else:
                    data["sound_mode"] = value["sound_mode"]
            elif service == "shuffle_set":
                if "shuffle" not in value:
                    data = {}
                else:
                    data["shuffle"] = value["shuffle"]
            elif service == "repeat_set":
                if "repeat" not in value:
                    data = {}
                else:
                    data["repeat"] = value["repeat"]
            elif service == "media_seek":
                if "seek_position" not in value:
                    data = {}
                else:
                    data["seek_position"] = value["seek_position"]
            elif service == "join":
                if "group_members" not in value:
                    data = {}
                else:
                    data["group_members"] = value["group_members"]
            if data:
                calls.append((domain, service, data))
        if "volume_level" in value:
            calls.append((
                domain,
                "volume_set",
                {"entity_id": target, "volume_level": value["volume_level"]},
            ))
        if "is_volume_muted" in value and media_action not in {"mute", "unmute"}:
            calls.append((
                domain,
                "volume_mute",
                {
                    "entity_id": target,
                    "is_volume_muted": value["is_volume_muted"],
                },
            ))
        if "source" in value and media_action_service != "select_source":
            calls.append((
                domain,
                "select_source",
                {"entity_id": target, "source": value["source"]},
            ))
        if "sound_mode" in value and media_action_service != "select_sound_mode":
            calls.append((
                domain,
                "select_sound_mode",
                {"entity_id": target, "sound_mode": value["sound_mode"]},
            ))
        if "shuffle" in value and media_action_service != "shuffle_set":
            calls.append((
                domain,
                "shuffle_set",
                {"entity_id": target, "shuffle": value["shuffle"]},
            ))
        if "repeat" in value and media_action_service != "repeat_set":
            calls.append((
                domain,
                "repeat_set",
                {"entity_id": target, "repeat": value["repeat"]},
            ))
        if "seek_position" in value and media_action_service != "media_seek":
            calls.append((
                domain,
                "media_seek",
                {"entity_id": target, "seek_position": value["seek_position"]},
            ))
        if "group_members" in value and media_action_service != "join":
            calls.append((
                domain,
                "join",
                {"entity_id": target, "group_members": value["group_members"]},
            ))
        if (
            "media_content_id" in value
            and "media_content_type" in value
            and media_action_service != "play_media"
        ):
            data = {
                "entity_id": target,
                "media_content_id": value["media_content_id"],
                "media_content_type": value["media_content_type"],
            }
            for field in ("enqueue", "announce", "extra"):
                if field in value:
                    data[field] = value[field]
            calls.append((domain, "play_media", data))
        return tuple(calls)

    if domain == "cover":
        if "tilt_position" in value:
            return ((
                domain,
                "set_cover_tilt_position",
                {"entity_id": target, "tilt_position": value["tilt_position"]},
            ),)
        if "position" in value:
            return ((
                domain,
                "set_cover_position",
                {"entity_id": target, "position": value["position"]},
            ),)
        service = COVER_STATE_SERVICES.get(state)
        return ((domain, service, service_data),) if service else ()

    if domain == "fan":
        if state == "toggle":
            return ((domain, "toggle", service_data),)
        if state == "off":
            return ((domain, "turn_off", service_data),)

        calls = []
        if state == "on":
            calls.append((domain, "turn_on", dict(service_data)))
        if "percentage" in value:
            calls.append((
                domain,
                "set_percentage",
                {"entity_id": target, "percentage": value["percentage"]},
            ))
        if "preset_mode" in value:
            calls.append((
                domain,
                "set_preset_mode",
                {"entity_id": target, "preset_mode": value["preset_mode"]},
            ))
        if "direction" in value:
            calls.append((
                domain,
                "set_direction",
                {"entity_id": target, "direction": value["direction"]},
            ))
        if "oscillating" in value:
            calls.append((
                domain,
                "oscillate",
                {"entity_id": target, "oscillating": value["oscillating"]},
            ))
        return tuple(calls)

    if domain == "climate":
        if state == "off":
            return ((domain, "turn_off", service_data),)
        if state == "on":
            return ((domain, "turn_on", service_data),)
        if state == "toggle":
            return ((domain, "toggle", service_data),)
        hvac_mode = value.get("hvac_mode", state)
        calls = []
        if hvac_mode is not None:
            calls.append((
                domain,
                "set_hvac_mode",
                {"entity_id": target, "hvac_mode": hvac_mode},
            ))

        temperature_fields = {
            key: value[key]
            for key in ("temperature", "target_temp_low", "target_temp_high")
            if key in value
        }
        if temperature_fields:
            calls.append((
                domain,
                "set_temperature",
                {"entity_id": target, **temperature_fields},
            ))
        if "preset_mode" in value:
            calls.append((
                domain,
                "set_preset_mode",
                {"entity_id": target, "preset_mode": value["preset_mode"]},
            ))
        if "fan_mode" in value:
            calls.append((
                domain,
                "set_fan_mode",
                {"entity_id": target, "fan_mode": value["fan_mode"]},
            ))
        if "humidity" in value:
            calls.append((
                domain,
                "set_humidity",
                {"entity_id": target, "humidity": value["humidity"]},
            ))
        if "swing_mode" in value:
            calls.append((
                domain,
                "set_swing_mode",
                {"entity_id": target, "swing_mode": value["swing_mode"]},
            ))
        if "swing_horizontal_mode" in value:
            calls.append((
                domain,
                "set_swing_horizontal_mode",
                {
                    "entity_id": target,
                    "swing_horizontal_mode": value["swing_horizontal_mode"],
                },
            ))
        if "aux_heat" in value:
            calls.append((
                domain,
                "set_aux_heat",
                {"entity_id": target, "aux_heat": value["aux_heat"]},
            ))
        return tuple(calls)

    if domain == "number":
        if "value" not in value:
            return ()
        return ((
            domain,
            "set_value",
            {"entity_id": target, "value": value["value"]},
        ),)

    if domain == "input_number":
        if "value" in value:
            return ((
                domain,
                "set_value",
                {"entity_id": target, "value": value["value"]},
            ),)
        service = INPUT_NUMBER_STATE_SERVICES.get(state)
        return ((domain, service, service_data),) if service else ()

    if domain == "counter":
        if "value" in value:
            return ((
                domain,
                "set_value",
                {"entity_id": target, "value": value["value"]},
            ),)
        if isinstance(state, int | float):
            return ((
                domain,
                "set_value",
                {"entity_id": target, "value": state},
            ),)
        if isinstance(state, str):
            try:
                numeric_state = float(state)
            except ValueError:
                numeric_state = None
            if numeric_state is not None:
                return ((
                    domain,
                    "set_value",
                    {"entity_id": target, "value": numeric_state},
                ),)
        service = COUNTER_STATE_SERVICES.get(state)
        return ((domain, service, service_data),) if service else ()

    if domain in {"select", "input_select"}:
        state_service_map = (
            INPUT_SELECT_STATE_SERVICES
            if domain == "input_select"
            else SELECT_STATE_SERVICES
        )
        service = state_service_map.get(state)
        if service is not None:
            data = dict(service_data)
            if service in {"select_next", "select_previous"} and "cycle" in value:
                data["cycle"] = value["cycle"]
            return ((domain, service, data),)
        option = value.get("option", state)
        if option is None:
            return ()
        return ((
            domain,
            "select_option",
            {"entity_id": target, "option": option},
        ),)

    if domain in {"text", "input_text"}:
        text_value = value.get("value", state)
        if text_value is None:
            return ()
        return ((
            domain,
            "set_value",
            {"entity_id": target, "value": text_value},
        ),)

    if domain == "todo":
        todo_action = value.get("todo_action", state)
        if todo_action is None and "item" in value:
            todo_action = "add_item"
        if todo_action is None:
            return ()
        service = str(todo_action)
        if service in {"add", "add_item"}:
            item = value.get("item")
            if item is None:
                return ()
            data = {"entity_id": target, "item": item}
            for field in ("due_date", "due_datetime", "description"):
                if field in value:
                    data[field] = value[field]
            return ((domain, "add_item", data),)
        if service in {"update", "update_item", "complete", "completed", "needs_action"}:
            item = value.get("item")
            if item is None:
                return ()
            data = {"entity_id": target, "item": item}
            if service in {"complete", "completed"}:
                data["status"] = "completed"
            elif service == "needs_action":
                data["status"] = "needs_action"
            for field in (
                "rename",
                "status",
                "due_date",
                "due_datetime",
                "description",
            ):
                if field in value:
                    data[field] = value[field]
            return ((domain, "update_item", data),)
        if service in {"remove", "remove_item"}:
            item = value.get("item")
            if item is None:
                return ()
            return ((domain, "remove_item", {"entity_id": target, "item": item}),)
        if service in {"clear_completed", "remove_completed", "remove_completed_items"}:
            return ((domain, "remove_completed_items", {"entity_id": target}),)
        if service in {"get", "get_items"}:
            data = dict(service_data)
            if "status" in value:
                data["status"] = value["status"]
            return ((domain, "get_items", data),)
        return ()

    if domain == "input_datetime":
        datetime_fields = {
            key: value[key]
            for key in ("datetime", "date", "time", "timestamp")
            if key in value
        }
        if not datetime_fields and state is not None:
            datetime_fields["datetime"] = state
        if not datetime_fields:
            return ()
        return ((
            domain,
            "set_datetime",
            {"entity_id": target, **datetime_fields},
        ),)

    if domain == "lock":
        service = LOCK_STATE_SERVICES.get(state)
        return ((domain, service, service_data),) if service else ()

    if domain == "alarm_control_panel":
        service = ALARM_STATE_SERVICES.get(state)
        if service is None:
            return ()
        data = dict(service_data)
        if "code" in value:
            data["code"] = value["code"]
        return ((domain, service, data),)

    if domain == "humidifier":
        if state == "off":
            return ((domain, "turn_off", service_data),)

        calls = []
        if state == "on":
            calls.append((domain, "turn_on", dict(service_data)))
        if "humidity" in value:
            calls.append((
                domain,
                "set_humidity",
                {"entity_id": target, "humidity": value["humidity"]},
            ))
        if "mode" in value:
            calls.append((
                domain,
                "set_mode",
                {"entity_id": target, "mode": value["mode"]},
            ))
        return tuple(calls)

    if domain == "water_heater":
        if state == "off":
            return ((domain, "turn_off", service_data),)

        calls = []
        if state == "on":
            calls.append((domain, "turn_on", dict(service_data)))
        operation_mode = value.get("operation_mode")
        if "temperature" in value:
            data = {"entity_id": target, "temperature": value["temperature"]}
            if operation_mode is not None:
                data["operation_mode"] = operation_mode
            calls.append((domain, "set_temperature", data))
        elif operation_mode is not None:
            calls.append((
                domain,
                "set_operation_mode",
                {"entity_id": target, "operation_mode": operation_mode},
            ))
        if "away_mode" in value:
            calls.append((
                domain,
                "set_away_mode",
                {"entity_id": target, "away_mode": value["away_mode"]},
            ))
        return tuple(calls)

    if domain == "vacuum":
        calls = []
        service = VACUUM_STATE_SERVICES.get(state)
        if service is not None:
            calls.append((domain, service, dict(service_data)))
        if "cleaning_area_id" in value:
            calls.append((
                domain,
                "clean_area",
                {
                    "entity_id": target,
                    "cleaning_area_id": value["cleaning_area_id"],
                },
            ))
        if "fan_speed" in value:
            calls.append((
                domain,
                "set_fan_speed",
                {"entity_id": target, "fan_speed": value["fan_speed"]},
            ))
        if "command" in value:
            data = {"entity_id": target, "command": value["command"]}
            if "params" in value:
                data["params"] = value["params"]
            calls.append((domain, "send_command", data))
        return tuple(calls)

    if domain == "siren":
        if state == "off":
            return ((domain, "turn_off", service_data),)
        if state == "toggle":
            return ((domain, "toggle", service_data),)
        if state is None and not any(
            field in value for field in ("tone", "duration", "volume_level")
        ):
            return ()
        data = dict(service_data)
        for field in ("tone", "duration", "volume_level"):
            if field in value:
                data[field] = value[field]
        return ((domain, "turn_on", data),)

    if domain == "valve":
        if "position" in value:
            return ((
                domain,
                "set_valve_position",
                {"entity_id": target, "position": value["position"]},
            ),)
        service = VALVE_STATE_SERVICES.get(state)
        return ((domain, service, service_data),) if service else ()

    if domain == "lawn_mower":
        service = LAWN_MOWER_STATE_SERVICES.get(state)
        return ((domain, service, service_data),) if service else ()

    if domain == "remote":
        calls = []
        if state in {"on", "off", "toggle"}:
            data = dict(service_data)
            if "activity" in value and state in {"on", "toggle"}:
                data["activity"] = value["activity"]
            calls.append((domain, REMOTE_STATE_SERVICES[state], data))
        if "command" in value:
            data = {"entity_id": target, "command": value["command"]}
            for field in (
                "device",
                "num_repeats",
                "delay_secs",
                "hold_secs",
            ):
                if field in value:
                    data[field] = value[field]
            calls.append((domain, "send_command", data))
        return tuple(calls)

    if domain == "camera":
        if state == "off":
            return ((domain, "turn_off", service_data),)
        if state == "on":
            return ((domain, "turn_on", service_data),)
        camera_action = value.get("camera_action", state)
        if camera_action in {"enable_motion_detection", "enable_motion", "motion_on"}:
            return ((domain, "enable_motion_detection", service_data),)
        if camera_action in {"disable_motion_detection", "disable_motion", "motion_off"}:
            return ((domain, "disable_motion_detection", service_data),)
        if camera_action == "snapshot":
            if "filename" not in value:
                return ()
            return ((
                domain,
                "snapshot",
                {"entity_id": target, "filename": value["filename"]},
            ),)
        if camera_action == "record":
            if "filename" not in value:
                return ()
            data = {"entity_id": target, "filename": value["filename"]}
            for field in ("duration", "lookback"):
                if field in value:
                    data[field] = value[field]
            return ((domain, "record", data),)
        if camera_action == "play_stream":
            media_player = value.get("media_player", value.get("media_player_entity_id"))
            if media_player is None:
                return ()
            data = {"entity_id": target, "media_player": media_player}
            if "format" in value:
                data["format"] = value["format"]
            return ((domain, "play_stream", data),)
        return ()

    if domain == "notify":
        message = value.get("message", state)
        if message is None:
            return ()
        notify_data = {"message": message}
        if "title" in value:
            notify_data["title"] = value["title"]
        if "data" in value:
            notify_data["data"] = value["data"]
        return ((domain, _object_id, notify_data),)

    if domain == "alert":
        service = ALERT_STATE_SERVICES.get(state)
        return ((domain, service, service_data),) if service else ()

    if domain == "browser_mod":
        service = str(value.get("service", state or _object_id))
        data = _action_service_data(value)
        return ((domain, service, data),)

    if domain == "telegram_bot":
        service = str(value.get("service", state or _object_id))
        data = _action_service_data(value)
        return ((domain, service, data),)

    if domain in {
        "rest_command",
        "persistent_notification",
        "logbook",
        "system_log",
        "scheduler",
        "cast",
    }:
        service = str(value.get("service", state or _object_id))
        data = _action_service_data(value)
        return ((domain, service, data),)

    if domain == "intentional":
        service = str(value.get("service", state or _object_id))
        if service != "clear":
            return ()
        data = _action_service_data(value)
        return ((domain, service, data),)

    if domain == "homeassistant":
        service = str(value.get("service", state or _object_id))
        if service != "update_entity":
            return ()
        data = _action_service_data(value)
        return ((domain, service, data),)

    if domain == "mqtt":
        service = str(value.get("service", state or _object_id))
        if service != "publish":
            return ()
        data = _action_service_data(value)
        return ((domain, service, data),)

    if domain == "google_assistant":
        service = str(value.get("service", state or _object_id))
        if service != "request_sync":
            return ()
        data = _action_service_data(value)
        return ((domain, service, data),)

    if domain == "assist_satellite":
        service = str(value.get("service", state or _object_id))
        if service not in {"announce", "start_conversation"}:
            return ()
        data = _action_service_data(value)
        return ((domain, service, data),)

    if domain == "alarmo":
        service = str(value.get("service", state or _object_id))
        if service not in {"arm", "disarm", "skip_delay"}:
            return ()
        data = _action_service_data(value)
        return ((domain, service, data),)

    if domain == "device_tracker":
        service = str(value.get("service", _object_id if _object_id == "see" else state))
        if service != "see":
            return ()
        data = _action_service_data(value)
        if state is not None and "location_name" not in data:
            data["location_name"] = state
        if not any(field in data for field in ("mac", "dev_id")):
            return ()
        return ((domain, service, data),)

    if domain == "shopping_list":
        service = str(value.get("service", _object_id or state))
        data = _action_service_data(value)
        if service in {
            "add_item",
            "remove_item",
            "complete_item",
            "incomplete_item",
        } and "name" not in data and state is not None:
            data["name"] = state
        return ((domain, service, data),)

    if domain == "tts":
        service = str(value.get("service", state or "speak"))
        if _object_id in TTS_SERVICE_TARGETS and "service" not in value and state is None:
            service = _object_id
        data = _action_service_data(value)
        if service == "speak":
            data["entity_id"] = target
        elif service == "cloud_say" and "media_player_entity_id" in data:
            data["entity_id"] = data.pop("media_player_entity_id")
        return ((domain, service, data),)

    if domain == "button":
        return ((domain, "press", service_data),)

    if domain == "input_button":
        return ((domain, "press", service_data),)

    if domain == "scene":
        data = dict(service_data)
        if transition_ms:
            data["transition"] = transition_ms / 1000.0
        return ((domain, "turn_on", data),)

    if domain == "script":
        if state == "off":
            return ((domain, "turn_off", service_data),)
        data = dict(service_data)
        if "variables" in value:
            data["variables"] = value["variables"]
        return ((domain, "turn_on", data),)

    if domain == "automation":
        if state == "off":
            return ((domain, "turn_off", service_data),)
        if state == "on":
            return ((domain, "turn_on", service_data),)
        data = dict(service_data)
        if "skip_condition" in value:
            data["skip_condition"] = value["skip_condition"]
        return ((domain, "trigger", data),)

    if domain == "timer":
        service = TIMER_STATE_SERVICES.get(state)
        if service is None:
            return ()
        data = dict(service_data)
        if service == "start" and "duration" in value:
            data["duration"] = value["duration"]
        return ((domain, service, data),)

    if domain == "update":
        update_action = value.get("update_action", state)
        if update_action == "install":
            data = dict(service_data)
            if "version" in value:
                data["version"] = value["version"]
            if "backup" in value:
                data["backup"] = value["backup"]
            return ((domain, "install", data),)
        if update_action == "skip":
            return ((domain, "skip", service_data),)
        if update_action in {"clear_skipped", "clear_skip", "clear"}:
            return ((domain, "clear_skipped", service_data),)
        return ()

    return ()


def _action_service_data(value: dict[str, Any]) -> dict[str, Any]:
    """Return data for service-style action targets."""
    data = dict(value.get("service_data") or {})
    for field in ACTION_SERVICE_FIELDS:
        if field in value:
            data[field] = value[field]
    return data


def scene_activation_plan(
    engine: Engine,
    already_activated: set[str],
) -> SceneActivationPlan:
    """Return scene.turn_on calls needed for newly active scene intents."""
    active = set(engine.list_active_scene_intents())
    new_or_changed = active - already_activated
    no_longer_active = already_activated - active
    if not new_or_changed and not no_longer_active:
        return (), already_activated, set()

    scene_intent_map = {
        scene: intent
        for intent, scene in engine.list_active_scene_intents(return_intents=True)
    }
    calls: list[ServiceCall] = []
    for scene_id in sorted(new_or_changed):
        intent = scene_intent_map.get(scene_id)
        if intent is None:
            continue
        service_data: dict[str, Any] = {"entity_id": scene_id}
        if intent.transition_ms:
            service_data["transition"] = intent.transition_ms / 1000.0
        calls.append(("scene", "turn_on", service_data))
    return tuple(calls), active, no_longer_active


def invalidate_service_plan_for_state_change(
    last_applied: dict[str, ServicePlanSignature],
    state: Any,
) -> bool:
    """Forget a cached service plan when HA reports conflicting state."""
    entity_id = state.entity_id
    plan = last_applied.get(entity_id)
    if plan is None:
        return False
    if service_plan_matches_state(plan, state):
        return False
    last_applied.pop(entity_id, None)
    return True


def classify_state_drift(
    engine: Engine,
    last_applied: dict[str, ServicePlanSignature],
    state: Any,
    *,
    ttl_ms: int,
    now_ms: int | None = None,
    drift_suppressed_until: dict[str, int] | None = None,
    drift_candidates: dict[str, tuple[int, FrozenValue]] | None = None,
    confirmation_ms: int = 0,
    reason: str = "Manual HA state change",
) -> dict[str, Any] | None:
    """Classify a state change and return the override payload, or None.

    Does NOT emit the intent; the caller applies the returned payload.
    """
    entity_id = state.entity_id
    if drift_suppressed_until is not None:
        suppress_until = drift_suppressed_until.get(entity_id)
        if suppress_until is not None:
            if now_ms is None:
                raise ValueError("now_ms is required with drift_suppressed_until")
            if now_ms < suppress_until:
                if drift_candidates is not None:
                    drift_candidates.pop(entity_id, None)
                return None
            drift_suppressed_until.pop(entity_id, None)
    plan = last_applied.get(entity_id)
    if plan is None:
        if drift_candidates is not None:
            drift_candidates.pop(entity_id, None)
        return None
    if service_plan_matches_state(plan, state):
        if drift_candidates is not None:
            drift_candidates.pop(entity_id, None)
        return None
    if not engine.has_active_target(entity_id):
        if drift_candidates is not None:
            drift_candidates.pop(entity_id, None)
        return None
    if _state_change_looks_like_ignored_activation(plan, state):
        if drift_candidates is not None:
            drift_candidates.pop(entity_id, None)
        last_applied.pop(entity_id, None)
        return None
    set_dict = manual_set_from_state_object(state)
    if not set_dict:
        if drift_candidates is not None:
            drift_candidates.pop(entity_id, None)
        return None
    if drift_candidates is not None and confirmation_ms > 0:
        if now_ms is None:
            raise ValueError("now_ms is required with drift_candidates")
        candidate_signature = _freeze_signature_value(set_dict)
        candidate = drift_candidates.get(entity_id)
        if candidate is None or candidate[1] != candidate_signature:
            drift_candidates[entity_id] = (now_ms, candidate_signature)
            return None
        first_seen_ms, _signature = candidate
        if now_ms - first_seen_ms < confirmation_ms:
            return None
        drift_candidates.pop(entity_id, None)
    last_applied.pop(entity_id, None)
    return {"target": entity_id, "set": set_dict, "ttl_ms": ttl_ms, "reason": reason}


def emit_manual_override_for_state_drift(
    engine: Engine,
    last_applied: dict[str, ServicePlanSignature],
    state: Any,
    *,
    ttl_ms: int,
    now_ms: int | None = None,
    drift_suppressed_until: dict[str, int] | None = None,
    drift_candidates: dict[str, tuple[int, FrozenValue]] | None = None,
    confirmation_ms: int = 0,
    reason: str = "Manual HA state change",
) -> bool:
    """Emit a USER intent when a managed target drifts from the applied plan.

    Legacy wrapper around classify_state_drift that applies the override
    directly to the engine. Prefer classify_state_drift for new callers.
    """
    result = classify_state_drift(
        engine,
        last_applied,
        state,
        ttl_ms=ttl_ms,
        now_ms=now_ms,
        drift_suppressed_until=drift_suppressed_until,
        drift_candidates=drift_candidates,
        confirmation_ms=confirmation_ms,
        reason=reason,
    )
    if result is None:
        return False
    engine.emit_user_intent(
        target=result["target"],
        set=result["set"],
        ttl_ms=result["ttl_ms"],
        reason=result["reason"],
    )
    return True


def _state_change_looks_like_ignored_activation(
    plan: ServicePlanSignature,
    state: Any,
) -> bool:
    """True when HA still reports off after an Intentional turn_on call.

    Some light integrations accept ``light.turn_on`` but the device never reaches
    ``on``. Without this guard that stale off state is promoted to a manual
    override, blocking retries for the drift TTL.
    """
    if str(getattr(state, "state", "")) != "off":
        return False
    context = getattr(state, "context", None)
    if getattr(context, "user_id", None) is not None:
        return False
    return any(domain == "light" and service == "turn_on" for domain, service, _data in plan)


def pending_drift_targets(drift_candidates: dict[str, tuple[int, FrozenValue]]) -> tuple[str, ...]:
    """Return targets with unconfirmed drift observations."""
    return tuple(sorted(drift_candidates))


def clear_pending_state_drift(
    drift_candidates: dict[str, tuple[int, FrozenValue]],
    target: str,
) -> None:
    """Forget any unconfirmed drift observation for a target."""
    drift_candidates.pop(target, None)


def sync_state_object_into_engine(engine: Engine, state: Any) -> None:
    """Sync one HA-style State object into the engine, including attributes."""
    entity_id = state.entity_id
    engine.update_state(entity_id, state.state)

    current_fields = {"state"}
    for synthetic_field in ("changed", "triggered"):
        if f"{entity_id}.{synthetic_field}" in engine.state:
            current_fields.add(synthetic_field)
    for field, value in state.attributes.items():
        current_fields.add(field)
        engine.update_state(entity_id, value, field=field)

    prefix = f"{entity_id}."
    for key in list(engine.state):
        if not key.startswith(prefix):
            continue
        field = key[len(prefix):]
        if field not in current_fields:
            del engine.state[key]


def pulse_state_change(
    engine: Engine,
    old_state: Any | None,
    new_state: Any,
) -> bool:
    """Expose a real HA entity state change as one-cycle trigger pulses.

    Rules often need edge semantics instead of level semantics: "this value
    just changed." Home Assistant event entities also keep their latest event
    as state and attributes, so they retain the older `triggered` pulse as a
    more domain-specific alias. The integration clears pulses after one apply
    cycle.
    """
    entity_id = new_state.entity_id
    if old_state is None:
        return False
    old_attributes = getattr(old_state, "attributes", {})
    new_attributes = getattr(new_state, "attributes", {})
    event_type_changed = old_attributes.get("event_type") != new_attributes.get(
        "event_type"
    )
    if old_state.state == new_state.state and not event_type_changed:
        return False
    engine.update_state(entity_id, True, field="changed")
    if entity_id.startswith("event."):
        engine.update_state(entity_id, True, field="triggered")
    return True


def pulse_event_state_change(
    engine: Engine,
    old_state: Any | None,
    new_state: Any,
) -> bool:
    """Backward-compatible alias for event/entity state-change pulses."""
    return pulse_state_change(engine, old_state, new_state)


def clear_state_change_pulses(engine: Engine, entity_ids: set[str]) -> None:
    """Clear one-cycle state-change pulses after the integration applies them."""
    for entity_id in entity_ids:
        engine.update_state(entity_id, False, field="changed")
        if entity_id.startswith("event."):
            engine.update_state(entity_id, False, field="triggered")


def clear_event_trigger_pulses(engine: Engine, entity_ids: set[str]) -> None:
    """Backward-compatible alias for clearing state-change pulses."""
    clear_state_change_pulses(engine, entity_ids)
