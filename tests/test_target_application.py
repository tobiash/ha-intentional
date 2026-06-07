"""Tests for applying resolved target intents to Home Assistant services."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest


def test_manual_set_from_service_data_keeps_supported_fields() -> None:
    from intentional.ha_adapter import manual_set_from_service_data

    assert manual_set_from_service_data({
        "target": "media_player.tv",
        "state": "on",
        "volume_level": 0.35,
        "is_volume_muted": False,
        "source": "HDMI 2",
        "sound_mode": "Movie",
        "ttl": 7200,
        "ignored": "nope",
    }) == {
        "state": "on",
        "volume_level": 0.35,
        "is_volume_muted": False,
        "source": "HDMI 2",
        "sound_mode": "Movie",
    }


def test_manual_set_from_service_data_supports_light_cover_and_fan_fields() -> None:
    from intentional.ha_adapter import manual_set_from_service_data

    assert manual_set_from_service_data({
        "brightness_pct": 70,
        "brightness": 180,
        "color_temp_k": 2700,
        "color_temp_mired": 370,
        "rgb_color": [255, 80, 40],
        "rgbw_color": [255, 80, 40, 10],
        "rgbww_color": [255, 80, 40, 10, 5],
        "hs_color": [24.0, 90.0],
        "xy_color": [0.45, 0.36],
        "effect": "colorloop",
        "flash": "short",
        "position": 25,
        "tilt_position": 75,
        "percentage": 40,
    }) == {
        "brightness_pct": 70,
        "brightness": 180,
        "color_temp_k": 2700,
        "color_temp_mired": 370,
        "rgb_color": [255, 80, 40],
        "rgbw_color": [255, 80, 40, 10],
        "rgbww_color": [255, 80, 40, 10, 5],
        "hs_color": [24.0, 90.0],
        "xy_color": [0.45, 0.36],
        "effect": "colorloop",
        "flash": "short",
        "position": 25,
        "tilt_position": 75,
        "percentage": 40,
    }


def test_manual_set_from_service_data_supports_climate_fields() -> None:
    from intentional.ha_adapter import manual_set_from_service_data

    assert manual_set_from_service_data({
        "hvac_mode": "heat",
        "temperature": 21.5,
        "target_temp_low": 18,
        "target_temp_high": 23,
        "preset_mode": "eco",
        "fan_mode": "auto",
        "direction": "forward",
        "oscillating": True,
        "swing_mode": "vertical",
        "swing_horizontal_mode": "wide",
        "aux_heat": False,
    }) == {
        "hvac_mode": "heat",
        "temperature": 21.5,
        "target_temp_low": 18,
        "target_temp_high": 23,
        "preset_mode": "eco",
        "fan_mode": "auto",
        "direction": "forward",
        "oscillating": True,
        "swing_mode": "vertical",
        "swing_horizontal_mode": "wide",
        "aux_heat": False,
    }


def test_manual_set_from_service_data_supports_helper_fields() -> None:
    from intentional.ha_adapter import manual_set_from_service_data

    assert manual_set_from_service_data({
        "value": 42,
        "option": "Guest",
        "cycle": False,
        "code": "1234",
        "message": "Door opened",
        "title": "Front door",
        "data": {"tag": "door"},
        "service": "notification",
        "service_data": {"browser_id": ["office"]},
        "media_player_entity_id": "media_player.office",
        "cache": True,
        "language": "de",
        "options": {"voice": "default"},
        "browser_id": ["office"],
        "user_id": ["person.tobias"],
        "path": "/lovelace/office",
        "action_text": "Open",
        "action": {"action": "navigate", "navigation_path": "/lovelace/office"},
        "parse_mode": "html",
        "disable_notification": False,
        "disable_web_page_preview": True,
        "keyboard": ["/ack"],
        "inline_keyboard": [["Acknowledge:/ack"]],
        "message_tag": "front-door",
        "chat_id": "12345",
        "todo_action": "add_item",
        "item": "Buy filters",
        "rename": "Buy HVAC filters",
        "status": "completed",
        "due_date": "2026-06-06",
        "due_datetime": "2026-06-06 10:00:00",
        "description": "For the office purifier",
        "variables": {"mode": "movie"},
        "skip_condition": True,
        "datetime": "2026-06-05 22:30:00",
        "date": "2026-06-05",
        "time": "22:30:00",
        "timestamp": 1780691400,
        "duration": "00:10:00",
        "update_entity": True,
        "media_action": "play_media",
        "media_content_id": "media-source://album/1",
        "media_content_type": "music",
        "enqueue": "play",
        "announce": True,
        "extra": {"metadata": {"title": "Dinner"}},
        "shuffle": True,
        "repeat": "all",
        "seek_position": 42.5,
        "group_members": ["media_player.kitchen"],
        "tone": "alarm",
        "humidity": 55,
        "mode": "eco",
        "operation_mode": "performance",
        "away_mode": True,
        "fan_speed": "turbo",
        "command": "clean_segments",
        "params": {"segments": [1, 2]},
        "cleaning_area_id": ["kitchen"],
        "activity": "Watch TV",
        "device": "soundbar",
        "num_repeats": 2,
        "delay_secs": 0.4,
        "hold_secs": 0.1,
    }) == {
        "value": 42,
        "option": "Guest",
        "cycle": False,
        "code": "1234",
        "message": "Door opened",
        "title": "Front door",
        "data": {"tag": "door"},
        "service": "notification",
        "service_data": {"browser_id": ["office"]},
        "media_player_entity_id": "media_player.office",
        "cache": True,
        "language": "de",
        "options": {"voice": "default"},
        "browser_id": ["office"],
        "user_id": ["person.tobias"],
        "path": "/lovelace/office",
        "action_text": "Open",
        "action": {"action": "navigate", "navigation_path": "/lovelace/office"},
        "parse_mode": "html",
        "disable_notification": False,
        "disable_web_page_preview": True,
        "keyboard": ["/ack"],
        "inline_keyboard": [["Acknowledge:/ack"]],
        "message_tag": "front-door",
        "chat_id": "12345",
        "todo_action": "add_item",
        "item": "Buy filters",
        "rename": "Buy HVAC filters",
        "status": "completed",
        "due_date": "2026-06-06",
        "due_datetime": "2026-06-06 10:00:00",
        "description": "For the office purifier",
        "variables": {"mode": "movie"},
        "skip_condition": True,
        "datetime": "2026-06-05 22:30:00",
        "date": "2026-06-05",
        "time": "22:30:00",
        "timestamp": 1780691400,
        "duration": "00:10:00",
        "update_entity": True,
        "media_action": "play_media",
        "media_content_id": "media-source://album/1",
        "media_content_type": "music",
        "enqueue": "play",
        "announce": True,
        "extra": {"metadata": {"title": "Dinner"}},
        "shuffle": True,
        "repeat": "all",
        "seek_position": 42.5,
        "group_members": ["media_player.kitchen"],
        "tone": "alarm",
        "humidity": 55,
        "mode": "eco",
        "operation_mode": "performance",
        "away_mode": True,
        "fan_speed": "turbo",
        "command": "clean_segments",
        "params": {"segments": [1, 2]},
        "cleaning_area_id": ["kitchen"],
        "activity": "Watch TV",
        "device": "soundbar",
        "num_repeats": 2,
        "delay_secs": 0.4,
        "hold_secs": 0.1,
    }


def test_manual_set_from_light_state_object_uses_ha_attributes() -> None:
    from intentional.ha_adapter import manual_set_from_state_object

    assert manual_set_from_state_object(
        SimpleNamespace(
            entity_id="light.desk",
            state="on",
            attributes={
                "brightness": 153,
                "color_temp_kelvin": 2700,
                "rgb_color": (255, 80, 40),
                "effect": "colorloop",
            },
        )
    ) == {
        "state": "on",
        "brightness": 153,
        "color_temp_k": 2700,
        "rgb_color": (255, 80, 40),
        "effect": "colorloop",
    }


def test_manual_set_from_climate_and_helper_state_objects() -> None:
    from intentional.ha_adapter import manual_set_from_state_object

    assert manual_set_from_state_object(
        SimpleNamespace(
            entity_id="climate.office",
            state="heat",
            attributes={"temperature": 21.5, "preset_mode": "eco"},
        )
    ) == {
        "hvac_mode": "heat",
        "temperature": 21.5,
        "preset_mode": "eco",
    }
    assert manual_set_from_state_object(
        SimpleNamespace(
            entity_id="climate.bedroom",
            state="cool",
            attributes={
                "target_humidity": 45,
                "swing_mode": "vertical",
                "swing_horizontal_mode": "wide",
                "aux_heat": True,
            },
        )
    ) == {
        "hvac_mode": "cool",
        "humidity": 45,
        "swing_mode": "vertical",
        "swing_horizontal_mode": "wide",
        "aux_heat": True,
    }
    assert manual_set_from_state_object(
        SimpleNamespace(
            entity_id="fan.bedroom",
            state="on",
            attributes={
                "percentage": 40,
                "preset_mode": "sleep",
                "direction": "reverse",
                "oscillating": True,
            },
        )
    ) == {
        "state": "on",
        "percentage": 40,
        "preset_mode": "sleep",
        "direction": "reverse",
        "oscillating": True,
    }
    assert manual_set_from_state_object(
        SimpleNamespace(entity_id="input_select.mode", state="Guest", attributes={})
    ) == {"option": "Guest"}
    assert manual_set_from_state_object(
        SimpleNamespace(entity_id="counter.motion_events", state="3", attributes={})
    ) == {"value": "3"}
    assert manual_set_from_state_object(
        SimpleNamespace(
            entity_id="input_datetime.reminder",
            state="2026-06-05 22:30:00",
            attributes={"has_date": True, "has_time": True, "timestamp": 1780691400},
        )
    ) == {"timestamp": 1780691400, "datetime": "2026-06-05 22:30:00"}
    assert manual_set_from_state_object(
        SimpleNamespace(
            entity_id="input_datetime.quiet_time",
            state="22:30:00",
            attributes={"has_date": False, "has_time": True},
        )
    ) == {"time": "22:30:00"}
    assert manual_set_from_state_object(
        SimpleNamespace(
            entity_id="timer.hallway_grace",
            state="active",
            attributes={"duration": "00:10:00", "remaining": "00:07:30"},
        )
    ) == {"state": "active", "duration": "00:10:00"}
    assert manual_set_from_state_object(
        SimpleNamespace(
            entity_id="cover.venetian_blinds",
            state="open",
            attributes={"current_position": 40, "current_tilt_position": 75},
        )
    ) == {"state": "open", "position": 40, "tilt_position": 75}
    assert manual_set_from_state_object(
        SimpleNamespace(
            entity_id="humidifier.bedroom",
            state="on",
            attributes={"target_humidity": 55, "mode": "eco"},
        )
    ) == {"state": "on", "humidity": 55, "mode": "eco"}
    assert manual_set_from_state_object(
        SimpleNamespace(
            entity_id="water_heater.utility",
            state="eco",
            attributes={"target_temperature": 55, "away_mode": False},
        )
    ) == {
        "state": "eco",
        "operation_mode": "eco",
        "temperature": 55,
        "away_mode": False,
    }
    assert manual_set_from_state_object(
        SimpleNamespace(
            entity_id="vacuum.downstairs",
            state="cleaning",
            attributes={"fan_speed": "turbo", "battery_level": 80},
        )
    ) == {"state": "cleaning", "fan_speed": "turbo"}
    assert manual_set_from_state_object(
        SimpleNamespace(entity_id="siren.entry", state="on", attributes={})
    ) == {"state": "on"}
    assert manual_set_from_state_object(
        SimpleNamespace(entity_id="lawn_mower.backyard", state="mowing", attributes={})
    ) == {"state": "mowing"}
    assert manual_set_from_state_object(
        SimpleNamespace(
            entity_id="remote.living_room",
            state="on",
            attributes={"current_activity": "Watch TV"},
        )
    ) == {"state": "on", "activity": "Watch TV"}
    assert manual_set_from_state_object(
        SimpleNamespace(
            entity_id="valve.water_main",
            state="open",
            attributes={"current_position": 75},
        )
    ) == {"state": "open", "position": 75}


def test_state_change_invalidates_cached_service_plan_for_entity() -> None:
    from intentional.ha_adapter import invalidate_service_plan_for_state_change

    last_applied = {
        "light.desk": (
            (
                "light",
                "turn_on",
                (("brightness_pct", 60), ("entity_id", "light.desk")),
            ),
        ),
        "light.other": (
            (
                "light",
                "turn_on",
                (("brightness_pct", 20), ("entity_id", "light.other")),
            ),
        ),
    }

    invalidated = invalidate_service_plan_for_state_change(
        last_applied,
        SimpleNamespace(entity_id="light.desk", state="off", attributes={}),
    )

    assert invalidated is True
    assert "light.desk" not in last_applied
    assert "light.other" in last_applied


def test_state_change_keeps_cached_service_plan_for_matching_state() -> None:
    from intentional.ha_adapter import invalidate_service_plan_for_state_change

    last_applied = {
        "light.desk": (
            (
                "light",
                "turn_on",
                (("brightness_pct", 60), ("entity_id", "light.desk")),
            ),
        ),
    }

    invalidated = invalidate_service_plan_for_state_change(
        last_applied,
        SimpleNamespace(
            entity_id="light.desk",
            state="on",
            attributes={"brightness": 153},
        ),
    )

    assert invalidated is False
    assert "light.desk" in last_applied


def test_state_change_keeps_cached_light_color_plan_for_tuple_attributes() -> None:
    from intentional.ha_adapter import (
        invalidate_service_plan_for_state_change,
        service_calls_for_resolved_target,
        service_plan_signature,
    )

    last_applied = {
        "light.desk": service_plan_signature(
            service_calls_for_resolved_target(
                "light.desk",
                {
                    "state": "on",
                    "rgb_color": [255, 80, 40],
                },
            )
        )
    }

    invalidated = invalidate_service_plan_for_state_change(
        last_applied,
        SimpleNamespace(
            entity_id="light.desk",
            state="on",
            attributes={
                "rgb_color": (255, 80, 40),
            },
        ),
    )

    assert invalidated is False
    assert "light.desk" in last_applied


def test_state_change_keeps_cached_humidity_water_heater_vacuum_and_valve_plans() -> None:
    from intentional.ha_adapter import (
        invalidate_service_plan_for_state_change,
        service_calls_for_resolved_target,
        service_plan_signature,
    )

    last_applied = {
        "humidifier.bedroom": service_plan_signature(
            service_calls_for_resolved_target(
                "humidifier.bedroom",
                {"state": "on", "humidity": 55, "mode": "sleep"},
            )
        ),
        "water_heater.utility": service_plan_signature(
            service_calls_for_resolved_target(
                "water_heater.utility",
                {"temperature": 50, "operation_mode": "eco", "away_mode": True},
            )
        ),
        "vacuum.downstairs": service_plan_signature(
            service_calls_for_resolved_target(
                "vacuum.downstairs",
                {"state": "cleaning", "fan_speed": "turbo"},
            )
        ),
        "fan.bedroom": service_plan_signature(
            service_calls_for_resolved_target(
                "fan.bedroom",
                {
                    "state": "on",
                    "percentage": 40,
                    "preset_mode": "sleep",
                    "direction": "reverse",
                    "oscillating": True,
                },
            )
        ),
        "valve.water_main": service_plan_signature(
            service_calls_for_resolved_target(
                "valve.water_main",
                {"position": 25},
            )
        ),
        "cover.venetian_blinds": service_plan_signature(
            service_calls_for_resolved_target(
                "cover.venetian_blinds",
                {"tilt_position": 75},
            )
        ),
        "lawn_mower.backyard": service_plan_signature(
            service_calls_for_resolved_target(
                "lawn_mower.backyard",
                {"state": "mowing"},
            )
        ),
        "remote.living_room": service_plan_signature(
            service_calls_for_resolved_target(
                "remote.living_room",
                {"state": "on", "activity": "Watch TV"},
            )
        ),
    }

    assert not invalidate_service_plan_for_state_change(
        last_applied,
        SimpleNamespace(
            entity_id="humidifier.bedroom",
            state="on",
            attributes={"target_humidity": 55, "mode": "sleep"},
        ),
    )
    assert not invalidate_service_plan_for_state_change(
        last_applied,
        SimpleNamespace(
            entity_id="water_heater.utility",
            state="eco",
            attributes={"target_temperature": 50, "away_mode": True},
        ),
    )
    assert not invalidate_service_plan_for_state_change(
        last_applied,
        SimpleNamespace(
            entity_id="vacuum.downstairs",
            state="cleaning",
            attributes={"fan_speed": "turbo"},
        ),
    )
    assert not invalidate_service_plan_for_state_change(
        last_applied,
        SimpleNamespace(
            entity_id="fan.bedroom",
            state="on",
            attributes={
                "percentage": 40,
                "preset_mode": "sleep",
                "direction": "reverse",
                "oscillating": True,
            },
        ),
    )
    assert not invalidate_service_plan_for_state_change(
        last_applied,
        SimpleNamespace(
            entity_id="valve.water_main",
            state="open",
            attributes={"current_position": 25},
        ),
    )
    assert not invalidate_service_plan_for_state_change(
        last_applied,
        SimpleNamespace(
            entity_id="cover.venetian_blinds",
            state="open",
            attributes={"current_tilt_position": 75},
        ),
    )
    assert not invalidate_service_plan_for_state_change(
        last_applied,
        SimpleNamespace(
            entity_id="lawn_mower.backyard",
            state="mowing",
            attributes={},
        ),
    )
    assert not invalidate_service_plan_for_state_change(
        last_applied,
        SimpleNamespace(
            entity_id="remote.living_room",
            state="on",
            attributes={"current_activity": "Watch TV"},
        ),
    )
    assert "humidifier.bedroom" in last_applied
    assert "water_heater.utility" in last_applied
    assert "vacuum.downstairs" in last_applied
    assert "fan.bedroom" in last_applied
    assert "valve.water_main" in last_applied
    assert "cover.venetian_blinds" in last_applied
    assert "lawn_mower.backyard" in last_applied
    assert "remote.living_room" in last_applied


def test_state_change_keeps_cached_media_player_plans_for_matching_state() -> None:
    from intentional.ha_adapter import (
        invalidate_service_plan_for_state_change,
        service_calls_for_resolved_target,
        service_plan_signature,
    )

    last_applied = {
        "media_player.kitchen": service_plan_signature(
            service_calls_for_resolved_target(
                "media_player.kitchen",
                {
                    "state": "pause",
                    "volume_level": 0.35,
                    "is_volume_muted": False,
                    "sound_mode": "Movie",
                    "shuffle": True,
                    "repeat": "all",
                },
            )
        ),
    }

    invalidated = invalidate_service_plan_for_state_change(
        last_applied,
        SimpleNamespace(
            entity_id="media_player.kitchen",
            state="paused",
            attributes={
                "volume_level": 0.35,
                "is_volume_muted": False,
                "sound_mode": "Movie",
                "shuffle": True,
                "repeat": "all",
            },
        ),
    )

    assert invalidated is False
    assert "media_player.kitchen" in last_applied


def test_state_change_invalidates_cached_media_player_transport_plan_for_drift() -> None:
    from intentional.ha_adapter import (
        invalidate_service_plan_for_state_change,
        service_calls_for_resolved_target,
        service_plan_signature,
    )

    last_applied = {
        "media_player.kitchen": service_plan_signature(
            service_calls_for_resolved_target(
                "media_player.kitchen",
                {"state": "pause"},
            )
        ),
    }

    invalidated = invalidate_service_plan_for_state_change(
        last_applied,
        SimpleNamespace(
            entity_id="media_player.kitchen",
            state="playing",
            attributes={},
        ),
    )

    assert invalidated is True
    assert "media_player.kitchen" not in last_applied


def test_state_drift_emits_manual_override_for_managed_target() -> None:
    from intentional.engine import Engine
    from intentional.ha_adapter import (
        emit_manual_override_for_state_drift,
        service_calls_for_resolved_target,
        service_plan_signature,
    )
    from intentional.intent import Authority
    from intentional.yaml_loader import Rule

    engine = Engine(clock_fn=lambda: 1000)
    engine.load_rules([
        Rule(
            id="desk-on",
            when="input_boolean.work == 'on'",
            target="light.desk",
            set={"state": "on", "brightness_pct": 60},
        )
    ])
    engine.update_state("input_boolean.work", "on")
    engine.evaluate_all()
    calls = service_calls_for_resolved_target(
        "light.desk",
        {"state": "on", "brightness_pct": 60},
    )
    last_applied = {"light.desk": service_plan_signature(calls)}

    emitted = emit_manual_override_for_state_drift(
        engine,
        last_applied,
        SimpleNamespace(
            entity_id="light.desk",
            state="off",
            attributes={},
        ),
        ttl_ms=7_200_000,
    )

    assert emitted is True
    assert "light.desk" not in last_applied
    intents = engine.list_active_intents("light.desk")
    manual = [intent for intent in intents if intent.authority is Authority.USER]
    assert len(manual) == 1
    assert manual[0].set == {"state": "off"}
    assert manual[0].ttl_ms == 7_200_000


def test_state_drift_does_not_emit_manual_override_for_matching_state() -> None:
    from intentional.engine import Engine
    from intentional.ha_adapter import (
        emit_manual_override_for_state_drift,
        service_calls_for_resolved_target,
        service_plan_signature,
    )
    from intentional.yaml_loader import Rule

    engine = Engine(clock_fn=lambda: 1000)
    engine.load_rules([
        Rule(
            id="desk-on",
            when="input_boolean.work == 'on'",
            target="light.desk",
            set={"state": "on", "brightness_pct": 60},
        )
    ])
    engine.update_state("input_boolean.work", "on")
    engine.evaluate_all()
    calls = service_calls_for_resolved_target(
        "light.desk",
        {"state": "on", "brightness_pct": 60},
    )
    last_applied = {"light.desk": service_plan_signature(calls)}

    emitted = emit_manual_override_for_state_drift(
        engine,
        last_applied,
        SimpleNamespace(
            entity_id="light.desk",
            state="on",
            attributes={"brightness": 153},
        ),
        ttl_ms=7_200_000,
    )

    assert emitted is False
    assert "light.desk" in last_applied
    from intentional.intent import Authority

    assert all(
        intent.authority is not Authority.USER
        for intent in engine.list_active_intents("light.desk")
    )


def test_service_plan_matches_equivalent_actual_light_state() -> None:
    from intentional.ha_adapter import (
        service_calls_for_resolved_target,
        service_plan_matches_state,
        service_plan_signature,
    )

    calls = service_calls_for_resolved_target(
        "light.desk",
        {"state": "on", "brightness_pct": 60},
    )
    signature = service_plan_signature(calls)

    assert service_plan_matches_state(
        signature,
        SimpleNamespace(
            entity_id="light.desk",
            state="on",
            attributes={"brightness": 153},
        ),
    )


def test_service_plan_does_not_match_conflicting_actual_light_state() -> None:
    from intentional.ha_adapter import (
        service_calls_for_resolved_target,
        service_plan_matches_state,
        service_plan_signature,
    )

    calls = service_calls_for_resolved_target(
        "light.desk",
        {"state": "on", "brightness_pct": 60},
    )
    signature = service_plan_signature(calls)

    assert not service_plan_matches_state(
        signature,
        SimpleNamespace(
            entity_id="light.desk",
            state="off",
            attributes={"brightness": 153},
        ),
    )


def test_state_drift_does_not_emit_manual_override_for_unmanaged_target() -> None:
    from intentional.engine import Engine
    from intentional.ha_adapter import emit_manual_override_for_state_drift

    engine = Engine(clock_fn=lambda: 1000)
    last_applied = {
        "light.desk": (
            (
                "light",
                "turn_on",
                (("brightness_pct", 60), ("entity_id", "light.desk")),
            ),
        ),
    }

    emitted = emit_manual_override_for_state_drift(
        engine,
        last_applied,
        SimpleNamespace(entity_id="light.desk", state="off", attributes={}),
        ttl_ms=7_200_000,
    )

    assert emitted is False
    assert engine.list_active_intents("light.desk") == []


def test_state_change_keeps_cached_climate_service_plan_for_matching_state() -> None:
    from intentional.ha_adapter import invalidate_service_plan_for_state_change

    last_applied = {
        "climate.living_room": (
            (
                "climate",
                "set_hvac_mode",
                (("entity_id", "climate.living_room"), ("hvac_mode", "heat")),
            ),
            (
                "climate",
                "set_temperature",
                (("entity_id", "climate.living_room"), ("temperature", 21.5)),
            ),
            (
                "climate",
                "set_preset_mode",
                (("entity_id", "climate.living_room"), ("preset_mode", "eco")),
            ),
            (
                "climate",
                "set_humidity",
                (("entity_id", "climate.living_room"), ("humidity", 45)),
            ),
            (
                "climate",
                "set_swing_mode",
                (("entity_id", "climate.living_room"), ("swing_mode", "vertical")),
            ),
            (
                "climate",
                "set_swing_horizontal_mode",
                (
                    ("entity_id", "climate.living_room"),
                    ("swing_horizontal_mode", "wide"),
                ),
            ),
            (
                "climate",
                "set_aux_heat",
                (("aux_heat", True), ("entity_id", "climate.living_room")),
            ),
        ),
    }

    invalidate_service_plan_for_state_change(
        last_applied,
        SimpleNamespace(
            entity_id="climate.living_room",
            state="heat",
            attributes={
                "temperature": 21.5,
                "preset_mode": "eco",
                "target_humidity": 45,
                "swing_mode": "vertical",
                "swing_horizontal_mode": "wide",
                "aux_heat": True,
            },
        ),
    )

    assert "climate.living_room" in last_applied


def test_state_change_keeps_cached_helper_service_plan_for_matching_state() -> None:
    from intentional.ha_adapter import invalidate_service_plan_for_state_change

    last_applied = {
        "input_number.target": (
            (
                "input_number",
                "set_value",
                (("entity_id", "input_number.target"), ("value", 42)),
            ),
        ),
        "input_select.mode": (
            (
                "input_select",
                "select_option",
                (("entity_id", "input_select.mode"), ("option", "Guest")),
            ),
        ),
        "input_datetime.reminder": (
            (
                "input_datetime",
                "set_datetime",
                (
                    ("datetime", "2026-06-05 22:30:00"),
                    ("entity_id", "input_datetime.reminder"),
                ),
            ),
        ),
        "timer.hallway_grace": (
            (
                "timer",
                "start",
                (
                    ("duration", "00:10:00"),
                    ("entity_id", "timer.hallway_grace"),
                ),
            ),
        ),
    }

    invalidate_service_plan_for_state_change(
        last_applied,
        SimpleNamespace(
            entity_id="input_number.target",
            state="42",
            attributes={"value": 42},
        ),
    )
    invalidate_service_plan_for_state_change(
        last_applied,
        SimpleNamespace(entity_id="input_select.mode", state="Guest", attributes={}),
    )
    invalidate_service_plan_for_state_change(
        last_applied,
        SimpleNamespace(
            entity_id="input_datetime.reminder",
            state="2026-06-05 22:30:00",
            attributes={"has_date": True, "has_time": True},
        ),
    )
    invalidate_service_plan_for_state_change(
        last_applied,
        SimpleNamespace(
            entity_id="timer.hallway_grace",
            state="active",
            attributes={"duration": "00:10:00"},
        ),
    )

    assert "input_number.target" in last_applied
    assert "input_select.mode" in last_applied
    assert "input_datetime.reminder" in last_applied
    assert "timer.hallway_grace" in last_applied


def test_state_change_keeps_cached_security_service_plan_for_matching_state() -> None:
    from intentional.ha_adapter import invalidate_service_plan_for_state_change

    last_applied = {
        "lock.front_door": (
            (
                "lock",
                "lock",
                (("entity_id", "lock.front_door"),),
            ),
        ),
        "alarm_control_panel.home": (
            (
                "alarm_control_panel",
                "alarm_arm_home",
                (("code", "1234"), ("entity_id", "alarm_control_panel.home")),
            ),
        ),
    }

    invalidate_service_plan_for_state_change(
        last_applied,
        SimpleNamespace(entity_id="lock.front_door", state="locked", attributes={}),
    )
    invalidate_service_plan_for_state_change(
        last_applied,
        SimpleNamespace(
            entity_id="alarm_control_panel.home",
            state="armed_home",
            attributes={},
        ),
    )

    assert "lock.front_door" in last_applied
    assert "alarm_control_panel.home" in last_applied


def test_state_change_invalidation_ignores_unknown_entity() -> None:
    from intentional.ha_adapter import invalidate_service_plan_for_state_change

    last_applied = {
        "light.desk": (
            (
                "light",
                "turn_on",
                (("brightness_pct", 60), ("entity_id", "light.desk")),
            ),
        ),
    }

    invalidate_service_plan_for_state_change(
        last_applied,
        SimpleNamespace(entity_id="light.unknown", state="off", attributes={}),
    )

    assert "light.desk" in last_applied


def test_light_resolved_value_maps_to_turn_on_service() -> None:
    from intentional.ha_adapter import service_call_for_resolved_target

    call = service_call_for_resolved_target(
        "light.desk",
        {
            "state": "on",
            "brightness_pct": 40,
            "color_temp_k": 2700,
            "rgb_color": [255, 80, 40],
            "effect": "colorloop",
            "flash": "short",
        },
        transition_ms=1500,
    )

    assert call == (
        "light",
        "turn_on",
        {
            "entity_id": "light.desk",
            "brightness_pct": 40,
            "color_temp_kelvin": 2700,
            "effect": "colorloop",
            "flash": "short",
            "transition": 1.5,
        },
    )


def test_light_off_resolved_value_maps_to_turn_off_service() -> None:
    from intentional.ha_adapter import service_call_for_resolved_target

    call = service_call_for_resolved_target(
        "light.desk",
        {"state": "off", "brightness_pct": 40},
        transition_ms=2000,
    )

    assert call == (
        "light",
        "turn_off",
        {
            "entity_id": "light.desk",
            "transition": 2.0,
        },
    )


def test_light_toggle_resolved_value_maps_to_toggle_service() -> None:
    from intentional.ha_adapter import service_call_for_resolved_target

    call = service_call_for_resolved_target(
        "light.desk",
        {
            "state": "toggle",
            "brightness_pct": 40,
            "color_temp_k": 2700,
            "hs_color": [24.0, 90.0],
            "effect": "colorloop",
        },
        transition_ms=1500,
    )

    assert call == (
        "light",
        "toggle",
        {
            "entity_id": "light.desk",
            "brightness_pct": 40,
            "color_temp_kelvin": 2700,
            "effect": "colorloop",
            "transition": 1.5,
        },
    )


def test_light_service_payload_uses_single_brightness_and_color_descriptor() -> None:
    from intentional.ha_adapter import service_call_for_resolved_target

    call = service_call_for_resolved_target(
        "light.desk",
        {
            "state": "on",
            "brightness": 26,
            "brightness_pct": 40,
            "color_temp_k": 4524,
            "rgb_color": [255, 218, 188],
            "hs_color": [26.732, 26.125],
            "xy_color": [0.392, 0.357],
            "effect": "off",
        },
    )

    assert call == (
        "light",
        "turn_on",
        {
            "entity_id": "light.desk",
            "brightness": 26,
            "color_temp_kelvin": 4524,
            "effect": "off",
        },
    )


def test_update_entity_appends_homeassistant_update_after_target_service() -> None:
    from intentional.ha_adapter import service_calls_for_resolved_target

    assert service_calls_for_resolved_target(
        "light.desk",
        {"state": "on", "brightness_pct": 40, "update_entity": True},
    ) == (
        (
            "light",
            "turn_on",
            {"entity_id": "light.desk", "brightness_pct": 40},
        ),
        ("homeassistant", "update_entity", {"entity_id": "light.desk"}),
    )
    assert service_calls_for_resolved_target(
        "sensor.travel_time",
        {"update_entity": True},
    ) == (
        ("homeassistant", "update_entity", {"entity_id": "sensor.travel_time"}),
    )


def test_switch_resolved_value_maps_to_turn_off_service() -> None:
    from intentional.ha_adapter import service_call_for_resolved_target

    assert service_call_for_resolved_target(
        "switch.fan",
        {"state": "off"},
    ) == ("switch", "turn_off", {"entity_id": "switch.fan"})


def test_toggle_resolved_value_maps_to_toggle_services() -> None:
    from intentional.ha_adapter import service_call_for_resolved_target

    assert service_call_for_resolved_target(
        "switch.fan",
        {"state": "toggle"},
    ) == ("switch", "toggle", {"entity_id": "switch.fan"})
    assert service_call_for_resolved_target(
        "input_boolean.away",
        {"state": "toggle"},
    ) == ("input_boolean", "toggle", {"entity_id": "input_boolean.away"})
    assert service_call_for_resolved_target(
        "fan.office",
        {"state": "toggle", "percentage": 40},
    ) == ("fan", "toggle", {"entity_id": "fan.office"})


def test_media_player_resolved_value_maps_to_multiple_services() -> None:
    from intentional.ha_adapter import service_calls_for_resolved_target

    assert service_calls_for_resolved_target(
        "media_player.tv",
        {"state": "on", "volume_level": 0.35, "source": "HDMI 2"},
    ) == (
        ("media_player", "turn_on", {"entity_id": "media_player.tv"}),
        (
            "media_player",
            "volume_set",
            {"entity_id": "media_player.tv", "volume_level": 0.35},
        ),
        (
            "media_player",
            "select_source",
            {"entity_id": "media_player.tv", "source": "HDMI 2"},
        ),
    )


def test_media_player_off_ignores_other_fields() -> None:
    from intentional.ha_adapter import service_calls_for_resolved_target

    assert service_calls_for_resolved_target(
        "media_player.tv",
        {"state": "off", "volume_level": 0.0},
    ) == (
        ("media_player", "turn_off", {"entity_id": "media_player.tv"}),
    )


def test_media_player_state_actions_map_to_transport_services() -> None:
    from intentional.ha_adapter import service_calls_for_resolved_target

    assert service_calls_for_resolved_target(
        "media_player.tv",
        {"state": "pause"},
    ) == (("media_player", "media_pause", {"entity_id": "media_player.tv"}),)
    assert service_calls_for_resolved_target(
        "media_player.tv",
        {"state": "next"},
    ) == (("media_player", "media_next_track", {"entity_id": "media_player.tv"}),)
    assert service_calls_for_resolved_target(
        "media_player.tv",
        {"state": "toggle"},
    ) == (("media_player", "toggle", {"entity_id": "media_player.tv"}),)


def test_media_player_action_fields_map_to_media_services() -> None:
    from intentional.ha_adapter import service_calls_for_resolved_target

    assert service_calls_for_resolved_target(
        "media_player.kitchen",
        {
            "media_action": "play_media",
            "media_content_id": "media-source://album/1",
            "media_content_type": "music",
            "enqueue": "play",
            "announce": True,
            "extra": {"metadata": {"title": "Dinner"}},
        },
    ) == (
        (
            "media_player",
            "play_media",
            {
                "entity_id": "media_player.kitchen",
                "media_content_id": "media-source://album/1",
                "media_content_type": "music",
                "enqueue": "play",
                "announce": True,
                "extra": {"metadata": {"title": "Dinner"}},
            },
        ),
    )
    assert service_calls_for_resolved_target(
        "media_player.kitchen",
        {"media_action": "mute"},
    ) == (
        (
            "media_player",
            "volume_mute",
            {"entity_id": "media_player.kitchen", "is_volume_muted": True},
        ),
    )
    assert service_calls_for_resolved_target(
        "media_player.kitchen",
        {"media_action": "unmute"},
    ) == (
        (
            "media_player",
            "volume_mute",
            {"entity_id": "media_player.kitchen", "is_volume_muted": False},
        ),
    )


def test_media_player_data_fields_map_to_media_services_without_action() -> None:
    from intentional.ha_adapter import service_calls_for_resolved_target

    assert service_calls_for_resolved_target(
        "media_player.kitchen",
        {
            "is_volume_muted": True,
            "sound_mode": "Movie",
            "shuffle": True,
            "repeat": "all",
            "seek_position": 42.5,
            "group_members": ["media_player.living_room"],
        },
    ) == (
        (
            "media_player",
            "volume_mute",
            {"entity_id": "media_player.kitchen", "is_volume_muted": True},
        ),
        (
            "media_player",
            "select_sound_mode",
            {"entity_id": "media_player.kitchen", "sound_mode": "Movie"},
        ),
        (
            "media_player",
            "shuffle_set",
            {"entity_id": "media_player.kitchen", "shuffle": True},
        ),
        (
            "media_player",
            "repeat_set",
            {"entity_id": "media_player.kitchen", "repeat": "all"},
        ),
        (
            "media_player",
            "media_seek",
            {"entity_id": "media_player.kitchen", "seek_position": 42.5},
        ),
        (
            "media_player",
            "join",
            {
                "entity_id": "media_player.kitchen",
                "group_members": ["media_player.living_room"],
            },
        ),
    )


def test_media_player_action_does_not_duplicate_matching_data_service() -> None:
    from intentional.ha_adapter import service_calls_for_resolved_target

    assert service_calls_for_resolved_target(
        "media_player.kitchen",
        {"media_action": "select_source", "source": "HDMI 2"},
    ) == (
        (
            "media_player",
            "select_source",
            {"entity_id": "media_player.kitchen", "source": "HDMI 2"},
        ),
    )


def test_cover_position_maps_to_set_cover_position() -> None:
    from intentional.ha_adapter import service_calls_for_resolved_target

    assert service_calls_for_resolved_target(
        "cover.blinds",
        {"position": 25},
    ) == (
        (
            "cover",
            "set_cover_position",
            {"entity_id": "cover.blinds", "position": 25},
        ),
    )


def test_cover_state_maps_to_open_close_service() -> None:
    from intentional.ha_adapter import service_calls_for_resolved_target

    assert service_calls_for_resolved_target(
        "cover.blinds",
        {"state": "closed"},
    ) == (
        ("cover", "close_cover", {"entity_id": "cover.blinds"}),
    )
    assert service_calls_for_resolved_target(
        "cover.blinds",
        {"state": "toggle"},
    ) == (
        ("cover", "toggle", {"entity_id": "cover.blinds"}),
    )


def test_cover_tilt_maps_to_tilt_services() -> None:
    from intentional.ha_adapter import service_calls_for_resolved_target

    assert service_calls_for_resolved_target(
        "cover.blinds",
        {"tilt_position": 75},
    ) == (
        (
            "cover",
            "set_cover_tilt_position",
            {"entity_id": "cover.blinds", "tilt_position": 75},
        ),
    )
    assert service_calls_for_resolved_target(
        "cover.blinds",
        {"state": "tilt_open"},
    ) == (
        ("cover", "open_cover_tilt", {"entity_id": "cover.blinds"}),
    )
    assert service_calls_for_resolved_target(
        "cover.blinds",
        {"state": "tilt_closed"},
    ) == (
        ("cover", "close_cover_tilt", {"entity_id": "cover.blinds"}),
    )
    assert service_calls_for_resolved_target(
        "cover.blinds",
        {"state": "tilt_stop"},
    ) == (
        ("cover", "stop_cover_tilt", {"entity_id": "cover.blinds"}),
    )
    assert service_calls_for_resolved_target(
        "cover.blinds",
        {"state": "tilt_toggle"},
    ) == (
        ("cover", "toggle_tilt", {"entity_id": "cover.blinds"}),
    )


def test_fan_resolved_value_maps_to_percentage_service() -> None:
    from intentional.ha_adapter import service_calls_for_resolved_target

    assert service_calls_for_resolved_target(
        "fan.office",
        {
            "state": "on",
            "percentage": 40,
            "preset_mode": "sleep",
            "direction": "reverse",
            "oscillating": True,
        },
    ) == (
        ("fan", "turn_on", {"entity_id": "fan.office"}),
        ("fan", "set_percentage", {"entity_id": "fan.office", "percentage": 40}),
        ("fan", "set_preset_mode", {"entity_id": "fan.office", "preset_mode": "sleep"}),
        ("fan", "set_direction", {"entity_id": "fan.office", "direction": "reverse"}),
        ("fan", "oscillate", {"entity_id": "fan.office", "oscillating": True}),
    )


def test_climate_resolved_value_maps_to_multiple_services() -> None:
    from intentional.ha_adapter import service_calls_for_resolved_target

    assert service_calls_for_resolved_target(
        "climate.living_room",
        {
            "hvac_mode": "heat",
            "temperature": 21.5,
            "preset_mode": "eco",
            "fan_mode": "auto",
            "humidity": 45,
            "swing_mode": "vertical",
            "swing_horizontal_mode": "wide",
            "aux_heat": True,
        },
    ) == (
        (
            "climate",
            "set_hvac_mode",
            {"entity_id": "climate.living_room", "hvac_mode": "heat"},
        ),
        (
            "climate",
            "set_temperature",
            {"entity_id": "climate.living_room", "temperature": 21.5},
        ),
        (
            "climate",
            "set_preset_mode",
            {"entity_id": "climate.living_room", "preset_mode": "eco"},
        ),
        (
            "climate",
            "set_fan_mode",
            {"entity_id": "climate.living_room", "fan_mode": "auto"},
        ),
        (
            "climate",
            "set_humidity",
            {"entity_id": "climate.living_room", "humidity": 45},
        ),
        (
            "climate",
            "set_swing_mode",
            {"entity_id": "climate.living_room", "swing_mode": "vertical"},
        ),
        (
            "climate",
            "set_swing_horizontal_mode",
            {
                "entity_id": "climate.living_room",
                "swing_horizontal_mode": "wide",
            },
        ),
        (
            "climate",
            "set_aux_heat",
            {"entity_id": "climate.living_room", "aux_heat": True},
        ),
    )


def test_climate_state_alias_maps_to_hvac_mode() -> None:
    from intentional.ha_adapter import service_calls_for_resolved_target

    assert service_calls_for_resolved_target(
        "climate.living_room",
        {"state": "off"},
    ) == (
        ("climate", "turn_off", {"entity_id": "climate.living_room"}),
    )
    assert service_calls_for_resolved_target(
        "climate.living_room",
        {"state": "toggle"},
    ) == (
        ("climate", "toggle", {"entity_id": "climate.living_room"}),
    )


def test_humidifier_resolved_value_maps_to_services() -> None:
    from intentional.ha_adapter import service_calls_for_resolved_target

    assert service_calls_for_resolved_target(
        "humidifier.bedroom",
        {"state": "on", "humidity": 55, "mode": "eco"},
    ) == (
        ("humidifier", "turn_on", {"entity_id": "humidifier.bedroom"}),
        (
            "humidifier",
            "set_humidity",
            {"entity_id": "humidifier.bedroom", "humidity": 55},
        ),
        (
            "humidifier",
            "set_mode",
            {"entity_id": "humidifier.bedroom", "mode": "eco"},
        ),
    )
    assert service_calls_for_resolved_target(
        "humidifier.bedroom",
        {"state": "off"},
    ) == (
        ("humidifier", "turn_off", {"entity_id": "humidifier.bedroom"}),
    )


def test_water_heater_resolved_value_maps_to_services() -> None:
    from intentional.ha_adapter import service_calls_for_resolved_target

    assert service_calls_for_resolved_target(
        "water_heater.utility",
        {"state": "on", "temperature": 55, "operation_mode": "eco"},
    ) == (
        ("water_heater", "turn_on", {"entity_id": "water_heater.utility"}),
        (
            "water_heater",
            "set_temperature",
            {
                "entity_id": "water_heater.utility",
                "temperature": 55,
                "operation_mode": "eco",
            },
        ),
    )
    assert service_calls_for_resolved_target(
        "water_heater.utility",
        {"operation_mode": "performance", "away_mode": True},
    ) == (
        (
            "water_heater",
            "set_operation_mode",
            {
                "entity_id": "water_heater.utility",
                "operation_mode": "performance",
            },
        ),
        (
            "water_heater",
            "set_away_mode",
            {"entity_id": "water_heater.utility", "away_mode": True},
        ),
    )
    assert service_calls_for_resolved_target(
        "water_heater.utility",
        {"state": "off"},
    ) == (
        ("water_heater", "turn_off", {"entity_id": "water_heater.utility"}),
    )


def test_number_input_number_and_counter_map_to_set_value_service() -> None:
    from intentional.ha_adapter import service_calls_for_resolved_target

    assert service_calls_for_resolved_target(
        "number.amp_limit",
        {"value": 12.5},
    ) == (
        ("number", "set_value", {"entity_id": "number.amp_limit", "value": 12.5}),
    )
    assert service_calls_for_resolved_target(
        "input_number.scene_level",
        {"value": 42},
    ) == (
        (
            "input_number",
            "set_value",
            {"entity_id": "input_number.scene_level", "value": 42},
        ),
    )
    assert service_calls_for_resolved_target(
        "counter.motion_events",
        {"value": 3},
    ) == (
        ("counter", "set_value", {"entity_id": "counter.motion_events", "value": 3}),
    )
    assert service_calls_for_resolved_target(
        "counter.motion_events",
        {"state": "3"},
    ) == (
        ("counter", "set_value", {"entity_id": "counter.motion_events", "value": 3.0}),
    )


def test_input_number_maps_state_to_increment_and_decrement_services() -> None:
    from intentional.ha_adapter import service_calls_for_resolved_target

    assert service_calls_for_resolved_target(
        "input_number.buro_tobias_off_delay_day",
        {"state": "increment"},
    ) == (
        (
            "input_number",
            "increment",
            {"entity_id": "input_number.buro_tobias_off_delay_day"},
        ),
    )
    assert service_calls_for_resolved_target(
        "input_number.buro_tobias_off_delay_day",
        {"state": "decrement"},
    ) == (
        (
            "input_number",
            "decrement",
            {"entity_id": "input_number.buro_tobias_off_delay_day"},
        ),
    )


def test_counter_maps_state_to_counter_action_services() -> None:
    from intentional.ha_adapter import service_calls_for_resolved_target

    assert service_calls_for_resolved_target(
        "counter.motion_events",
        {"state": "increment"},
    ) == (
        ("counter", "increment", {"entity_id": "counter.motion_events"}),
    )
    assert service_calls_for_resolved_target(
        "counter.motion_events",
        {"state": "decrement"},
    ) == (
        ("counter", "decrement", {"entity_id": "counter.motion_events"}),
    )
    assert service_calls_for_resolved_target(
        "counter.motion_events",
        {"state": "reset"},
    ) == (
        ("counter", "reset", {"entity_id": "counter.motion_events"}),
    )


def test_select_and_input_select_map_to_select_option_service() -> None:
    from intentional.ha_adapter import service_calls_for_resolved_target

    assert service_calls_for_resolved_target(
        "select.ev_mode",
        {"option": "Solar"},
    ) == (
        (
            "select",
            "select_option",
            {"entity_id": "select.ev_mode", "option": "Solar"},
        ),
    )
    assert service_calls_for_resolved_target(
        "input_select.house_mode",
        {"state": "Guest"},
    ) == (
        (
            "input_select",
            "select_option",
            {"entity_id": "input_select.house_mode", "option": "Guest"},
        ),
    )


def test_select_and_input_select_state_actions_map_to_selection_services() -> None:
    from intentional.ha_adapter import service_calls_for_resolved_target

    assert service_calls_for_resolved_target(
        "select.ev_mode",
        {"state": "next", "cycle": False},
    ) == (
        (
            "select",
            "select_next",
            {"entity_id": "select.ev_mode", "cycle": False},
        ),
    )
    assert service_calls_for_resolved_target(
        "select.ev_mode",
        {"state": "previous"},
    ) == (
        ("select", "select_previous", {"entity_id": "select.ev_mode"}),
    )
    assert service_calls_for_resolved_target(
        "input_select.house_mode",
        {"state": "first"},
    ) == (
        ("input_select", "select_first", {"entity_id": "input_select.house_mode"}),
    )
    assert service_calls_for_resolved_target(
        "input_select.house_mode",
        {"state": "last"},
    ) == (
        ("input_select", "select_last", {"entity_id": "input_select.house_mode"}),
    )


def test_input_text_maps_to_set_value_service() -> None:
    from intentional.ha_adapter import service_calls_for_resolved_target

    assert service_calls_for_resolved_target(
        "input_text.status",
        {"state": "quiet"},
    ) == (
        (
            "input_text",
            "set_value",
            {"entity_id": "input_text.status", "value": "quiet"},
        ),
    )
    assert service_calls_for_resolved_target(
        "text.fridge_note",
        {"value": "Buy milk"},
    ) == (
        (
            "text",
            "set_value",
            {"entity_id": "text.fridge_note", "value": "Buy milk"},
        ),
    )


def test_todo_target_maps_to_todo_services() -> None:
    from intentional.ha_adapter import service_calls_for_resolved_target

    assert service_calls_for_resolved_target(
        "todo.shopping_list",
        {
            "item": "Buy filters",
            "due_date": "2026-06-06",
            "description": "For the office purifier",
        },
    ) == (
        (
            "todo",
            "add_item",
            {
                "entity_id": "todo.shopping_list",
                "item": "Buy filters",
                "due_date": "2026-06-06",
                "description": "For the office purifier",
            },
        ),
    )
    assert service_calls_for_resolved_target(
        "todo.shopping_list",
        {
            "todo_action": "update",
            "item": "Buy filters",
            "rename": "Buy HVAC filters",
            "status": "needs_action",
            "due_datetime": "2026-06-06 10:00:00",
        },
    ) == (
        (
            "todo",
            "update_item",
            {
                "entity_id": "todo.shopping_list",
                "item": "Buy filters",
                "rename": "Buy HVAC filters",
                "status": "needs_action",
                "due_datetime": "2026-06-06 10:00:00",
            },
        ),
    )
    assert service_calls_for_resolved_target(
        "todo.shopping_list",
        {"state": "completed", "item": "Buy filters"},
    ) == (
        (
            "todo",
            "update_item",
            {
                "entity_id": "todo.shopping_list",
                "item": "Buy filters",
                "status": "completed",
            },
        ),
    )
    assert service_calls_for_resolved_target(
        "todo.shopping_list",
        {"todo_action": "remove", "item": "Buy filters"},
    ) == (
        (
            "todo",
            "remove_item",
            {"entity_id": "todo.shopping_list", "item": "Buy filters"},
        ),
    )
    assert service_calls_for_resolved_target(
        "todo.shopping_list",
        {"todo_action": "clear_completed"},
    ) == (
        ("todo", "remove_completed_items", {"entity_id": "todo.shopping_list"}),
    )
    assert service_calls_for_resolved_target(
        "todo.shopping_list",
        {"todo_action": "get_items", "status": ["needs_action", "completed"]},
    ) == (
        (
            "todo",
            "get_items",
            {
                "entity_id": "todo.shopping_list",
                "status": ["needs_action", "completed"],
            },
        ),
    )


def test_input_datetime_maps_to_set_datetime_service() -> None:
    from intentional.ha_adapter import service_calls_for_resolved_target

    assert service_calls_for_resolved_target(
        "input_datetime.reminder",
        {"datetime": "2026-06-05 22:30:00"},
    ) == (
        (
            "input_datetime",
            "set_datetime",
            {
                "entity_id": "input_datetime.reminder",
                "datetime": "2026-06-05 22:30:00",
            },
        ),
    )
    assert service_calls_for_resolved_target(
        "input_datetime.quiet_time",
        {"time": "22:30:00"},
    ) == (
        (
            "input_datetime",
            "set_datetime",
            {
                "entity_id": "input_datetime.quiet_time",
                "time": "22:30:00",
            },
        ),
    )
    assert service_calls_for_resolved_target(
        "input_datetime.reminder",
        {"state": "2026-06-05 22:30:00"},
    ) == (
        (
            "input_datetime",
            "set_datetime",
            {
                "entity_id": "input_datetime.reminder",
                "datetime": "2026-06-05 22:30:00",
            },
        ),
    )


def test_timer_maps_state_to_timer_services() -> None:
    from intentional.ha_adapter import service_calls_for_resolved_target

    assert service_calls_for_resolved_target(
        "timer.hallway_grace",
        {"state": "active", "duration": "00:10:00"},
    ) == (
        (
            "timer",
            "start",
            {
                "entity_id": "timer.hallway_grace",
                "duration": "00:10:00",
            },
        ),
    )
    assert service_calls_for_resolved_target(
        "timer.hallway_grace",
        {"state": "paused"},
    ) == (
        ("timer", "pause", {"entity_id": "timer.hallway_grace"}),
    )
    assert service_calls_for_resolved_target(
        "timer.hallway_grace",
        {"state": "idle"},
    ) == (
        ("timer", "cancel", {"entity_id": "timer.hallway_grace"}),
    )
    assert service_calls_for_resolved_target(
        "timer.hallway_grace",
        {"state": "finish"},
    ) == (
        ("timer", "finish", {"entity_id": "timer.hallway_grace"}),
    )


def test_lock_maps_state_to_lock_services() -> None:
    from intentional.ha_adapter import service_calls_for_resolved_target

    assert service_calls_for_resolved_target(
        "lock.front_door",
        {"state": "locked"},
    ) == (
        ("lock", "lock", {"entity_id": "lock.front_door"}),
    )
    assert service_calls_for_resolved_target(
        "lock.front_door",
        {"state": "unlocked"},
    ) == (
        ("lock", "unlock", {"entity_id": "lock.front_door"}),
    )


def test_alarm_control_panel_maps_state_to_alarm_services() -> None:
    from intentional.ha_adapter import service_calls_for_resolved_target

    assert service_calls_for_resolved_target(
        "alarm_control_panel.home",
        {"state": "armed_home", "code": "1234"},
    ) == (
        (
            "alarm_control_panel",
            "alarm_arm_home",
            {"entity_id": "alarm_control_panel.home", "code": "1234"},
        ),
    )
    assert service_calls_for_resolved_target(
        "alarm_control_panel.home",
        {"state": "disarmed", "code": "1234"},
    ) == (
        (
            "alarm_control_panel",
            "alarm_disarm",
            {"entity_id": "alarm_control_panel.home", "code": "1234"},
        ),
    )


def test_siren_resolved_value_maps_to_siren_services() -> None:
    from intentional.ha_adapter import service_calls_for_resolved_target

    assert service_calls_for_resolved_target(
        "siren.entry",
        {
            "state": "on",
            "tone": "alarm",
            "duration": 30,
            "volume_level": 0.8,
        },
    ) == (
        (
            "siren",
            "turn_on",
            {
                "entity_id": "siren.entry",
                "tone": "alarm",
                "duration": 30,
                "volume_level": 0.8,
            },
        ),
    )
    assert service_calls_for_resolved_target(
        "siren.entry",
        {"state": "off"},
    ) == (
        ("siren", "turn_off", {"entity_id": "siren.entry"}),
    )
    assert service_calls_for_resolved_target(
        "siren.entry",
        {"state": "toggle"},
    ) == (
        ("siren", "toggle", {"entity_id": "siren.entry"}),
    )


def test_valve_resolved_value_maps_to_valve_services() -> None:
    from intentional.ha_adapter import service_calls_for_resolved_target

    assert service_calls_for_resolved_target(
        "valve.water_main",
        {"state": "closed"},
    ) == (
        ("valve", "close_valve", {"entity_id": "valve.water_main"}),
    )
    assert service_calls_for_resolved_target(
        "valve.water_main",
        {"state": "open"},
    ) == (
        ("valve", "open_valve", {"entity_id": "valve.water_main"}),
    )
    assert service_calls_for_resolved_target(
        "valve.water_main",
        {"state": "stop"},
    ) == (
        ("valve", "stop_valve", {"entity_id": "valve.water_main"}),
    )
    assert service_calls_for_resolved_target(
        "valve.water_main",
        {"position": 25},
    ) == (
        (
            "valve",
            "set_valve_position",
            {"entity_id": "valve.water_main", "position": 25},
        ),
    )


def test_lawn_mower_resolved_value_maps_to_lawn_mower_services() -> None:
    from intentional.ha_adapter import service_calls_for_resolved_target

    assert service_calls_for_resolved_target(
        "lawn_mower.backyard",
        {"state": "mowing"},
    ) == (
        ("lawn_mower", "start_mowing", {"entity_id": "lawn_mower.backyard"}),
    )
    assert service_calls_for_resolved_target(
        "lawn_mower.backyard",
        {"state": "paused"},
    ) == (
        ("lawn_mower", "pause", {"entity_id": "lawn_mower.backyard"}),
    )
    assert service_calls_for_resolved_target(
        "lawn_mower.backyard",
        {"state": "returning"},
    ) == (
        ("lawn_mower", "dock", {"entity_id": "lawn_mower.backyard"}),
    )
    assert service_calls_for_resolved_target(
        "lawn_mower.backyard",
        {"state": "dock"},
    ) == (
        ("lawn_mower", "dock", {"entity_id": "lawn_mower.backyard"}),
    )


def test_remote_resolved_value_maps_to_remote_services() -> None:
    from intentional.ha_adapter import service_calls_for_resolved_target

    assert service_calls_for_resolved_target(
        "remote.living_room",
        {"state": "on", "activity": "Watch TV"},
    ) == (
        (
            "remote",
            "turn_on",
            {"entity_id": "remote.living_room", "activity": "Watch TV"},
        ),
    )
    assert service_calls_for_resolved_target(
        "remote.living_room",
        {"state": "off"},
    ) == (
        ("remote", "turn_off", {"entity_id": "remote.living_room"}),
    )
    assert service_calls_for_resolved_target(
        "remote.living_room",
        {
            "command": ["KEY_HOME", "KEY_RIGHT", "KEY_ENTER"],
            "device": "Android TV",
            "num_repeats": 2,
            "delay_secs": 0.4,
            "hold_secs": 0.1,
        },
    ) == (
        (
            "remote",
            "send_command",
            {
                "entity_id": "remote.living_room",
                "command": ["KEY_HOME", "KEY_RIGHT", "KEY_ENTER"],
                "device": "Android TV",
                "num_repeats": 2,
                "delay_secs": 0.4,
                "hold_secs": 0.1,
            },
        ),
    )


def test_notify_target_maps_to_notify_service() -> None:
    from intentional.ha_adapter import service_calls_for_resolved_target

    assert service_calls_for_resolved_target(
        "notify.mobile_app_phone",
        {
            "message": "Front door opened",
            "title": "Security",
            "data": {"tag": "front-door"},
        },
    ) == (
        (
            "notify",
            "mobile_app_phone",
            {
                "message": "Front door opened",
                "title": "Security",
                "data": {"tag": "front-door"},
            },
        ),
    )


def test_alert_target_maps_to_alert_services() -> None:
    from intentional.ha_adapter import service_calls_for_resolved_target

    assert service_calls_for_resolved_target(
        "alert.lqi_hobbyraum",
        {"state": "on"},
    ) == (
        ("alert", "turn_on", {"entity_id": "alert.lqi_hobbyraum"}),
    )
    assert service_calls_for_resolved_target(
        "alert.lqi_hobbyraum",
        {"state": "off"},
    ) == (
        ("alert", "turn_off", {"entity_id": "alert.lqi_hobbyraum"}),
    )
    assert service_calls_for_resolved_target(
        "alert.lqi_hobbyraum",
        {"state": "toggle"},
    ) == (
        ("alert", "toggle", {"entity_id": "alert.lqi_hobbyraum"}),
    )


def test_browser_mod_target_maps_to_named_service() -> None:
    from intentional.ha_adapter import service_calls_for_resolved_target

    assert service_calls_for_resolved_target(
        "browser_mod.notification",
        {
            "message": "Doorbell",
            "browser_id": ["office-dashboard"],
            "duration": 5000,
            "action_text": "Open camera",
            "action": {"action": "navigate", "navigation_path": "/lovelace/cameras"},
        },
    ) == (
        (
            "browser_mod",
            "notification",
            {
                "message": "Doorbell",
                "browser_id": ["office-dashboard"],
                "duration": 5000,
                "action_text": "Open camera",
                "action": {
                    "action": "navigate",
                    "navigation_path": "/lovelace/cameras",
                },
            },
        ),
    )
    assert service_calls_for_resolved_target(
        "browser_mod.navigate",
        {"path": "/lovelace/office", "service_data": {"browser_id": ["office"]}},
    ) == (
        (
            "browser_mod",
            "navigate",
            {"browser_id": ["office"], "path": "/lovelace/office"},
        ),
    )


def test_telegram_bot_target_maps_to_named_service() -> None:
    from intentional.ha_adapter import service_calls_for_resolved_target

    assert service_calls_for_resolved_target(
        "telegram_bot.send_message",
        {
            "message": "Door opened",
            "title": "Security",
            "parse_mode": "html",
            "disable_notification": False,
            "disable_web_page_preview": True,
            "keyboard": ["/ack"],
            "inline_keyboard": [["Acknowledge:/ack"]],
            "message_tag": "front-door",
            "chat_id": "12345",
        },
    ) == (
        (
            "telegram_bot",
            "send_message",
            {
                "message": "Door opened",
                "title": "Security",
                "parse_mode": "html",
                "disable_notification": False,
                "disable_web_page_preview": True,
                "keyboard": ["/ack"],
                "inline_keyboard": [["Acknowledge:/ack"]],
                "message_tag": "front-door",
                "chat_id": "12345",
            },
        ),
    )


def test_stateless_service_targets_map_to_named_services() -> None:
    from intentional.ha_adapter import service_calls_for_resolved_target

    assert service_calls_for_resolved_target(
        "rest_command.ring_fritzbox_phones",
        {"service_data": {"phone": "**9"}},
    ) == (
        ("rest_command", "ring_fritzbox_phones", {"phone": "**9"}),
    )
    assert service_calls_for_resolved_target(
        "persistent_notification.create",
        {
            "message": "Doorbell rang",
            "title": "Doorbell",
            "service_data": {"notification_id": "doorbell"},
        },
    ) == (
        (
            "persistent_notification",
            "create",
            {
                "notification_id": "doorbell",
                "message": "Doorbell rang",
                "title": "Doorbell",
            },
        ),
    )
    assert service_calls_for_resolved_target(
        "logbook.log",
        {
            "message": "Doorbell rang",
            "service_data": {
                "name": "Intentional",
                "entity_id": "event.espnow_recv_doorbell",
                "domain": "event",
            },
        },
    ) == (
        (
            "logbook",
            "log",
            {
                "name": "Intentional",
                "entity_id": "event.espnow_recv_doorbell",
                "domain": "event",
                "message": "Doorbell rang",
            },
        ),
    )
    assert service_calls_for_resolved_target(
        "system_log.write",
        {"message": "Doorbell rang", "service_data": {"level": "info"}},
    ) == (
        ("system_log", "write", {"level": "info", "message": "Doorbell rang"}),
    )
    assert service_calls_for_resolved_target(
        "scheduler.run_action",
        {
            "service_data": {
                "entity_id": "switch.schedule_office_lights",
                "skip_conditions": True,
            },
        },
    ) == (
        (
            "scheduler",
            "run_action",
            {
                "entity_id": "switch.schedule_office_lights",
                "skip_conditions": True,
            },
        ),
    )
    assert service_calls_for_resolved_target(
        "scheduler.disable_all",
        {},
    ) == (
        ("scheduler", "disable_all", {}),
    )
    assert service_calls_for_resolved_target(
        "cast.show_lovelace_view",
        {
            "service_data": {
                "entity_id": "media_player.office_display",
                "dashboard_path": "lovelace",
                "view_path": "front-door",
            },
        },
    ) == (
        (
            "cast",
            "show_lovelace_view",
            {
                "entity_id": "media_player.office_display",
                "dashboard_path": "lovelace",
                "view_path": "front-door",
            },
        ),
    )
    assert service_calls_for_resolved_target(
        "intentional.clear",
        {"service_data": {"target": "light.office"}},
    ) == (
        ("intentional", "clear", {"target": "light.office"}),
    )
    assert service_calls_for_resolved_target(
        "intentional.fire",
        {"service_data": {"target": "light.office", "state": "on"}},
    ) == ()
    assert service_calls_for_resolved_target(
        "homeassistant.update_entity",
        {"service_data": {"entity_id": "sensor.travel_time"}},
    ) == (
        ("homeassistant", "update_entity", {"entity_id": "sensor.travel_time"}),
    )
    assert service_calls_for_resolved_target(
        "homeassistant.restart",
        {},
    ) == ()
    assert service_calls_for_resolved_target(
        "mqtt.publish",
        {
            "service_data": {
                "topic": "intentional/doorbell",
                "payload": "ringer",
                "qos": 1,
                "retain": False,
            },
        },
    ) == (
        (
            "mqtt",
            "publish",
            {
                "topic": "intentional/doorbell",
                "payload": "ringer",
                "qos": 1,
                "retain": False,
            },
        ),
    )
    assert service_calls_for_resolved_target(
        "mqtt.dump",
        {"service_data": {"topic": "#"}},
    ) == ()
    assert service_calls_for_resolved_target(
        "google_assistant.request_sync",
        {"service_data": {"agent_user_id": "home"}},
    ) == (
        ("google_assistant", "request_sync", {"agent_user_id": "home"}),
    )
    assert service_calls_for_resolved_target(
        "google_assistant.reload",
        {},
    ) == ()
    assert service_calls_for_resolved_target(
        "assist_satellite.announce",
        {
            "service_data": {
                "entity_id": "assist_satellite.kitchen",
                "message": "Doorbell",
                "preannounce": False,
            },
        },
    ) == (
        (
            "assist_satellite",
            "announce",
            {
                "entity_id": "assist_satellite.kitchen",
                "message": "Doorbell",
                "preannounce": False,
            },
        ),
    )
    assert service_calls_for_resolved_target(
        "assist_satellite.start_conversation",
        {
            "service_data": {
                "entity_id": "assist_satellite.kitchen",
                "start_message": "Front door is open.",
            },
        },
    ) == (
        (
            "assist_satellite",
            "start_conversation",
            {
                "entity_id": "assist_satellite.kitchen",
                "start_message": "Front door is open.",
            },
        ),
    )
    assert service_calls_for_resolved_target(
        "assist_satellite.ask_question",
        {
            "service_data": {
                "entity_id": "assist_satellite.kitchen",
                "question": "Close the door?",
            },
        },
    ) == ()
    assert service_calls_for_resolved_target(
        "alarmo.arm",
        {
            "service_data": {
                "entity_id": "alarm_control_panel.alarmo",
                "mode": "night",
                "skip_delay": True,
                "force": True,
            },
        },
    ) == (
        (
            "alarmo",
            "arm",
            {
                "entity_id": "alarm_control_panel.alarmo",
                "mode": "night",
                "skip_delay": True,
                "force": True,
            },
        ),
    )
    assert service_calls_for_resolved_target(
        "alarmo.disarm",
        {
            "service_data": {
                "entity_id": "alarm_control_panel.alarmo",
                "code": "1234",
            },
        },
    ) == (
        (
            "alarmo",
            "disarm",
            {"entity_id": "alarm_control_panel.alarmo", "code": "1234"},
        ),
    )
    assert service_calls_for_resolved_target(
        "alarmo.enable_user",
        {"service_data": {"name": "Guest"}},
    ) == ()
    assert service_calls_for_resolved_target(
        "device_tracker.see",
        {
            "dev_id": "phone_tobias",
            "host_name": "Tobias Phone",
            "location_name": "home",
            "gps": [52.52, 13.405],
            "gps_accuracy": 12,
            "battery": 88,
        },
    ) == (
        (
            "device_tracker",
            "see",
            {
                "dev_id": "phone_tobias",
                "host_name": "Tobias Phone",
                "location_name": "home",
                "gps": [52.52, 13.405],
                "gps_accuracy": 12,
                "battery": 88,
            },
        ),
    )
    assert service_calls_for_resolved_target(
        "device_tracker.see",
        {"service_data": {"mac": "AA:BB:CC:DD:EE:FF", "location_name": "not_home"}},
    ) == (
        (
            "device_tracker",
            "see",
            {"mac": "AA:BB:CC:DD:EE:FF", "location_name": "not_home"},
        ),
    )
    assert service_calls_for_resolved_target(
        "device_tracker.see",
        {"state": "home", "dev_id": "phone_tobias"},
    ) == (
        (
            "device_tracker",
            "see",
            {"dev_id": "phone_tobias", "location_name": "home"},
        ),
    )
    assert service_calls_for_resolved_target(
        "device_tracker.reload",
        {"service_data": {}},
    ) == ()
    assert service_calls_for_resolved_target(
        "device_tracker.see",
        {"location_name": "home"},
    ) == ()


def test_camera_target_maps_to_camera_services() -> None:
    from intentional.ha_adapter import service_calls_for_resolved_target

    assert service_calls_for_resolved_target(
        "camera.front_door",
        {"state": "on"},
    ) == (("camera", "turn_on", {"entity_id": "camera.front_door"}),)
    assert service_calls_for_resolved_target(
        "camera.front_door",
        {"state": "off"},
    ) == (("camera", "turn_off", {"entity_id": "camera.front_door"}),)
    assert service_calls_for_resolved_target(
        "camera.front_door",
        {"camera_action": "snapshot", "filename": "/tmp/doorbell.jpg"},
    ) == (
        (
            "camera",
            "snapshot",
            {"entity_id": "camera.front_door", "filename": "/tmp/doorbell.jpg"},
        ),
    )
    assert service_calls_for_resolved_target(
        "camera.front_door",
        {
            "camera_action": "record",
            "filename": "/tmp/doorbell.mp4",
            "duration": 20,
            "lookback": 5,
        },
    ) == (
        (
            "camera",
            "record",
            {
                "entity_id": "camera.front_door",
                "filename": "/tmp/doorbell.mp4",
                "duration": 20,
                "lookback": 5,
            },
        ),
    )
    assert service_calls_for_resolved_target(
        "camera.front_door",
        {
            "camera_action": "play_stream",
            "media_player": "media_player.office",
            "format": "hls",
        },
    ) == (
        (
            "camera",
            "play_stream",
            {
                "entity_id": "camera.front_door",
                "media_player": "media_player.office",
                "format": "hls",
            },
        ),
    )
    assert service_calls_for_resolved_target(
        "camera.front_door",
        {"camera_action": "disable_motion_detection"},
    ) == (
        (
            "camera",
            "disable_motion_detection",
            {"entity_id": "camera.front_door"},
        ),
    )
    assert service_calls_for_resolved_target(
        "camera.front_door",
        {"state": "enable_motion_detection"},
    ) == (
        (
            "camera",
            "enable_motion_detection",
            {"entity_id": "camera.front_door"},
        ),
    )


def test_tts_target_maps_to_speak_and_cloud_say_services() -> None:
    from intentional.ha_adapter import service_calls_for_resolved_target

    assert service_calls_for_resolved_target(
        "tts.google_ai_tts",
        {
            "message": "Front door",
            "media_player_entity_id": "media_player.office",
            "cache": True,
            "language": "de",
            "options": {"voice": "default"},
        },
    ) == (
        (
            "tts",
            "speak",
            {
                "entity_id": "tts.google_ai_tts",
                "message": "Front door",
                "media_player_entity_id": "media_player.office",
                "cache": True,
                "language": "de",
                "options": {"voice": "default"},
            },
        ),
    )
    assert service_calls_for_resolved_target(
        "tts.cloud_say",
        {
            "message": "Front door",
            "media_player_entity_id": "media_player.office",
            "cache": False,
        },
    ) == (
        (
            "tts",
            "cloud_say",
            {
                "entity_id": "media_player.office",
                "message": "Front door",
                "cache": False,
            },
        ),
    )


def test_vacuum_resolved_value_maps_to_action_services() -> None:
    from intentional.ha_adapter import service_calls_for_resolved_target

    assert service_calls_for_resolved_target(
        "vacuum.downstairs",
        {"state": "cleaning", "fan_speed": "turbo"},
    ) == (
        ("vacuum", "start", {"entity_id": "vacuum.downstairs"}),
        (
            "vacuum",
            "set_fan_speed",
            {"entity_id": "vacuum.downstairs", "fan_speed": "turbo"},
        ),
    )
    assert service_calls_for_resolved_target(
        "vacuum.downstairs",
        {"state": "paused"},
    ) == (
        ("vacuum", "pause", {"entity_id": "vacuum.downstairs"}),
    )
    assert service_calls_for_resolved_target(
        "vacuum.downstairs",
        {"state": "returning"},
    ) == (
        ("vacuum", "return_to_base", {"entity_id": "vacuum.downstairs"}),
    )
    assert service_calls_for_resolved_target(
        "vacuum.downstairs",
        {"state": "locate"},
    ) == (
        ("vacuum", "locate", {"entity_id": "vacuum.downstairs"}),
    )


def test_vacuum_resolved_value_maps_to_area_and_command_services() -> None:
    from intentional.ha_adapter import service_calls_for_resolved_target

    assert service_calls_for_resolved_target(
        "vacuum.downstairs",
        {"cleaning_area_id": ["kitchen", "hallway"]},
    ) == (
        (
            "vacuum",
            "clean_area",
            {
                "entity_id": "vacuum.downstairs",
                "cleaning_area_id": ["kitchen", "hallway"],
            },
        ),
    )
    assert service_calls_for_resolved_target(
        "vacuum.downstairs",
        {
            "command": "clean_segments",
            "params": {"segments": [1, 2]},
        },
    ) == (
        (
            "vacuum",
            "send_command",
            {
                "entity_id": "vacuum.downstairs",
                "command": "clean_segments",
                "params": {"segments": [1, 2]},
            },
        ),
    )


def test_notify_state_alias_maps_to_message() -> None:
    from intentional.ha_adapter import service_calls_for_resolved_target

    assert service_calls_for_resolved_target(
        "notify.persistent_notification",
        {"state": "Rule fired"},
    ) == (
        ("notify", "persistent_notification", {"message": "Rule fired"}),
    )


def test_shopping_list_target_maps_to_shopping_list_services() -> None:
    from intentional.ha_adapter import service_calls_for_resolved_target

    assert service_calls_for_resolved_target(
        "shopping_list.add_item",
        {"name": "Coffee"},
    ) == (
        ("shopping_list", "add_item", {"name": "Coffee"}),
    )
    assert service_calls_for_resolved_target(
        "shopping_list.complete_item",
        {"state": "Milk"},
    ) == (
        ("shopping_list", "complete_item", {"name": "Milk"}),
    )
    assert service_calls_for_resolved_target(
        "shopping_list.sort",
        {"reverse": True},
    ) == (
        ("shopping_list", "sort", {"reverse": True}),
    )


def test_service_plan_signature_freezes_nested_notify_data() -> None:
    from intentional.ha_adapter import (
        service_calls_for_resolved_target,
        service_plan_signature,
    )

    calls = service_calls_for_resolved_target(
        "notify.mobile_app_phone",
        {
            "message": "Front door opened",
            "data": {
                "actions": [
                    {"action": "URI", "uri": "/lovelace/security"},
                ],
                "tag": "front-door",
            },
        },
    )

    signature = service_plan_signature(calls)

    assert hash(signature)
    assert signature == service_plan_signature(calls)


def test_button_and_scene_targets_map_to_action_services() -> None:
    from intentional.ha_adapter import service_calls_for_resolved_target

    assert service_calls_for_resolved_target(
        "button.restart_router",
        {},
    ) == (
        ("button", "press", {"entity_id": "button.restart_router"}),
    )
    assert service_calls_for_resolved_target(
        "input_button.mark_arrival",
        {"state": "press"},
    ) == (
        ("input_button", "press", {"entity_id": "input_button.mark_arrival"}),
    )
    assert service_calls_for_resolved_target(
        "scene.movie",
        {},
        transition_ms=1500,
    ) == (
        ("scene", "turn_on", {"entity_id": "scene.movie", "transition": 1.5}),
    )


def test_script_target_maps_to_turn_on_with_variables() -> None:
    from intentional.ha_adapter import service_calls_for_resolved_target

    assert service_calls_for_resolved_target(
        "script.movie_mode",
        {"variables": {"brightness": 20}},
    ) == (
        (
            "script",
            "turn_on",
            {"entity_id": "script.movie_mode", "variables": {"brightness": 20}},
        ),
    )
    assert service_calls_for_resolved_target(
        "script.movie_mode",
        {"state": "off"},
    ) == (
        ("script", "turn_off", {"entity_id": "script.movie_mode"}),
    )


def test_automation_target_maps_to_trigger_or_enable_disable() -> None:
    from intentional.ha_adapter import service_calls_for_resolved_target

    assert service_calls_for_resolved_target(
        "automation.arrival",
        {"skip_condition": False},
    ) == (
        (
            "automation",
            "trigger",
            {"entity_id": "automation.arrival", "skip_condition": False},
        ),
    )
    assert service_calls_for_resolved_target(
        "automation.arrival",
        {"state": "off"},
    ) == (
        ("automation", "turn_off", {"entity_id": "automation.arrival"}),
    )


def test_update_target_maps_to_update_services() -> None:
    from intentional.ha_adapter import service_calls_for_resolved_target

    assert service_calls_for_resolved_target(
        "update.router_firmware",
        {"update_action": "install", "version": "1.2.3", "backup": True},
    ) == (
        (
            "update",
            "install",
            {
                "entity_id": "update.router_firmware",
                "version": "1.2.3",
                "backup": True,
            },
        ),
    )
    assert service_calls_for_resolved_target(
        "update.router_firmware",
        {"state": "skip"},
    ) == (
        ("update", "skip", {"entity_id": "update.router_firmware"}),
    )
    assert service_calls_for_resolved_target(
        "update.router_firmware",
        {"update_action": "clear_skipped"},
    ) == (
        ("update", "clear_skipped", {"entity_id": "update.router_firmware"}),
    )
    assert service_calls_for_resolved_target(
        "update.router_firmware",
        {"state": "on"},
    ) == ()


def test_fire_and_forget_targets_do_not_invalidate_when_state_settles() -> None:
    from intentional.ha_adapter import (
        invalidate_service_plan_for_state_change,
        service_calls_for_resolved_target,
        service_plan_signature,
    )

    script_calls = service_calls_for_resolved_target(
        "script.movie_mode",
        {"variables": {"brightness": 20}},
    )
    automation_calls = service_calls_for_resolved_target(
        "automation.arrival",
        {"skip_condition": False},
    )
    update_calls = service_calls_for_resolved_target(
        "update.router_firmware",
        {"update_action": "install"},
    )
    last_applied = {
        "script.movie_mode": service_plan_signature(script_calls),
        "automation.arrival": service_plan_signature(automation_calls),
        "update.router_firmware": service_plan_signature(update_calls),
        "input_button.mark_arrival": service_plan_signature(
            service_calls_for_resolved_target(
                "input_button.mark_arrival",
                {"state": "press"},
            )
        ),
    }

    assert not invalidate_service_plan_for_state_change(
        last_applied,
        SimpleNamespace(entity_id="script.movie_mode", state="off", attributes={}),
    )
    assert not invalidate_service_plan_for_state_change(
        last_applied,
        SimpleNamespace(entity_id="automation.arrival", state="on", attributes={}),
    )
    assert not invalidate_service_plan_for_state_change(
        last_applied,
        SimpleNamespace(entity_id="update.router_firmware", state="off", attributes={}),
    )
    assert not invalidate_service_plan_for_state_change(
        last_applied,
        SimpleNamespace(
            entity_id="input_button.mark_arrival",
            state="2026-06-05T22:30:00+02:00",
            attributes={},
        ),
    )
    assert "script.movie_mode" in last_applied
    assert "automation.arrival" in last_applied
    assert "update.router_firmware" in last_applied
    assert "input_button.mark_arrival" in last_applied


class _FakeServices:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any], bool]] = []

    async def async_call(
        self,
        domain: str,
        service: str,
        service_data: dict[str, Any],
        *,
        blocking: bool = False,
    ) -> None:
        self.calls.append((domain, service, dict(service_data), blocking))


@pytest.mark.asyncio
async def test_apply_resolved_targets_suppresses_duplicate_calls() -> None:
    pytest.importorskip("homeassistant", reason="homeassistant not installed")

    from custom_components.intentional import _apply_resolved_targets
    from custom_components.intentional._engine import Engine
    from custom_components.intentional._engine.yaml_loader import Rule

    engine = Engine(clock_fn=lambda: 1000)
    engine.load_rules([
        Rule(
            id="desk-on",
            when="input_boolean.work == 'on'",
            target="light.desk",
            set={"state": "on", "brightness_pct": 60},
        )
    ])
    engine.update_state("input_boolean.work", "on")
    engine.evaluate_all()

    services = _FakeServices()
    hass = SimpleNamespace(services=services)
    last_applied = {}

    await _apply_resolved_targets(hass, engine, last_applied)
    await _apply_resolved_targets(hass, engine, last_applied)

    assert services.calls == [
        (
            "light",
            "turn_on",
            {"entity_id": "light.desk", "brightness_pct": 60},
            False,
        )
    ]


@pytest.mark.asyncio
async def test_apply_resolved_targets_allows_empty_action_payloads() -> None:
    pytest.importorskip("homeassistant", reason="homeassistant not installed")

    from custom_components.intentional import _apply_resolved_targets
    from custom_components.intentional._engine import Engine
    from custom_components.intentional._engine.yaml_loader import Rule

    engine = Engine(clock_fn=lambda: 1000)
    engine.load_rules([
        Rule(
            id="mark-arrival",
            when="binary_sensor.driveway_motion == 'on'",
            target="input_button.mark_arrival",
            set={},
        )
    ])
    engine.update_state("binary_sensor.driveway_motion", "on")
    engine.evaluate_all()

    services = _FakeServices()
    hass = SimpleNamespace(services=services)
    last_applied = {}

    await _apply_resolved_targets(hass, engine, last_applied)
    await _apply_resolved_targets(hass, engine, last_applied)

    assert services.calls == [
        (
            "input_button",
            "press",
            {"entity_id": "input_button.mark_arrival"},
            False,
        )
    ]


@pytest.mark.asyncio
async def test_apply_resolved_targets_uses_assert_and_withdraw_transitions() -> None:
    pytest.importorskip("homeassistant", reason="homeassistant not installed")

    from custom_components.intentional import _apply_resolved_targets
    from custom_components.intentional._engine import Engine
    from custom_components.intentional._engine.yaml_loader import load_rules_from_string

    engine = Engine(clock_fn=lambda: 1000)
    engine.load_rules(load_rules_from_string('''
- id: desk-presence
  observe:
    binary_sensor.desk_presence: on
  intent:
    light.desk:
      state: on
      brightness_pct: 40
      apply:
        transition:
          assert: 2s
          withdraw: 6s
'''))
    services = _FakeServices()
    hass = SimpleNamespace(services=services)
    last_applied = {}
    last_resolved = {}

    engine.update_state("binary_sensor.desk_presence", "on")
    engine.evaluate_all()
    await _apply_resolved_targets(hass, engine, last_applied, last_resolved)

    engine.update_state("binary_sensor.desk_presence", "off")
    engine.evaluate_all()
    await _apply_resolved_targets(hass, engine, last_applied, last_resolved)

    assert services.calls == [
        (
            "light",
            "turn_on",
            {"entity_id": "light.desk", "brightness_pct": 40, "transition": 2.0},
            False,
        ),
        (
            "light",
            "turn_off",
            {"entity_id": "light.desk", "transition": 6.0},
            False,
        ),
    ]


@pytest.mark.asyncio
async def test_apply_resolved_targets_withdraw_reconciles_to_revealed_intent() -> None:
    pytest.importorskip("homeassistant", reason="homeassistant not installed")

    from custom_components.intentional import _apply_resolved_targets
    from custom_components.intentional._engine import Engine
    from custom_components.intentional._engine.yaml_loader import load_rules_from_string

    engine = Engine(clock_fn=lambda: 1000)
    engine.load_rules(load_rules_from_string('''
- id: ambient
  observe:
    input_boolean.ambient: on
  intent:
    light.desk:
      state: on
      brightness_pct: 10
      apply:
        transition:
          assert: 7s
- id: presence
  observe:
    binary_sensor.desk_presence: on
  confidence: 0.8
  intent:
    light.desk:
      state: on
      brightness_pct: 40
      apply:
        transition:
          assert: 2s
          withdraw: 6s
'''))
    services = _FakeServices()
    hass = SimpleNamespace(services=services)
    last_applied = {}
    last_resolved = {}

    engine.update_state("input_boolean.ambient", "on")
    engine.update_state("binary_sensor.desk_presence", "on")
    engine.evaluate_all()
    await _apply_resolved_targets(hass, engine, last_applied, last_resolved)

    engine.update_state("binary_sensor.desk_presence", "off")
    engine.evaluate_all()
    await _apply_resolved_targets(hass, engine, last_applied, last_resolved)

    assert services.calls == [
        (
            "light",
            "turn_on",
            {"entity_id": "light.desk", "brightness_pct": 40, "transition": 2.0},
            False,
        ),
        (
            "light",
            "turn_on",
            {"entity_id": "light.desk", "brightness_pct": 10, "transition": 7.0},
            False,
        ),
    ]
