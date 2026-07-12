"""Pure end-to-end scenarios for automation replacement flows."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo


def _state(entity_id: str, state: str, **attributes: Any) -> SimpleNamespace:
    return SimpleNamespace(entity_id=entity_id, state=state, attributes=attributes)


def _service_calls_for_yaml(
    yaml_text: str,
    states: list[SimpleNamespace],
    *,
    now: datetime | None = None,
) -> dict[str, tuple[tuple[str, str, dict[str, Any]], ...]]:
    from intentional.engine import Engine
    from intentional.ha_adapter import (
        service_calls_for_resolved_target,
        sync_state_object_into_engine,
        sync_time_context_into_engine,
    )
    from intentional.yaml_loader import load_rules_from_string

    engine = Engine(clock_fn=lambda: 1000)
    engine.load_rules(load_rules_from_string(yaml_text))
    if now is not None:
        sync_time_context_into_engine(engine, now)
    for state in states:
        sync_state_object_into_engine(engine, state)
    engine.evaluate_all()

    calls_by_target = {}
    for target in engine.list_active_targets():
        resolved = engine.resolve(target)
        assert resolved is not None
        calls_by_target[target] = service_calls_for_resolved_target(
            target,
            dict(resolved.value),
            transition_ms=resolved.transition_ms,
        )
    return calls_by_target


def test_vnext_always_active_intent_resolves_to_service_call() -> None:
    calls = _service_calls_for_yaml(
        """
        - id: hallway-base
          intent:
            light.hallway:
              state: "on"
              brightness_pct: 3
        """,
        [],
    )

    assert calls == {
        "light.hallway": (
            (
                "light",
                "turn_on",
                {
                    "entity_id": "light.hallway",
                    "brightness_pct": 3,
                },
            ),
        ),
    }


def test_vnext_disabled_rule_does_not_resolve_to_service_call() -> None:
    calls = _service_calls_for_yaml(
        """
        - id: disabled-light
          enabled: false
          intent:
            light.hallway:
              state: "on"
        """,
        [],
    )

    assert calls == {}


def test_room_automation_scenario_resolves_to_ha_service_calls() -> None:
    calls = _service_calls_for_yaml(
        """
        - id: ambient-when-dark
          when: sensor.outdoor_light.illuminance < 50
          emit:
            target: light.living_room
            set:
              state: "on"
              brightness_pct: 80
              color_temp_k: 3000
            transition: 3s
          confidence: 0.7

        - id: tv-viewing-cap
          when: media_player.tv == "on"
          emit:
            target: light.living_room
            cap:
              brightness_pct: 40
            set:
              color_temp_k: 2700
            transition: 1.5s
          confidence: 0.9
        """,
        [
            _state("sensor.outdoor_light", "42", illuminance=42),
            _state("media_player.tv", "on"),
        ],
    )

    assert calls == {
        "light.living_room": (
            (
                "light",
                "turn_on",
                {
                    "entity_id": "light.living_room",
                    "brightness_pct": 40,
                    "color_temp_kelvin": 2700,
                    "transition": 1.5,
                },
            ),
        ),
    }


def test_action_and_notification_scenario_resolves_to_service_calls() -> None:
    calls = _service_calls_for_yaml(
        """
        - id: notify-front-door
          when: binary_sensor.front_door == "on"
          emit:
            target: notify.mobile_app_phone
            set:
              title: Security
              message: Front door opened
              data:
                tag: front-door
            ttl: 30s

        - id: run-night-script
          when: time_of_day >= "22:00" and time_of_day < "23:30"
          emit:
            target: script.night_mode
            set:
              variables:
                brightness: 20
            ttl: 10m
        """,
        [_state("binary_sensor.front_door", "on")],
        now=datetime(2026, 6, 5, 22, 15, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    assert calls == {
        "notify.mobile_app_phone": (
            (
                "notify",
                "mobile_app_phone",
                {
                    "title": "Security",
                    "message": "Front door opened",
                    "data": {"tag": "front-door"},
                },
            ),
        ),
        "script.night_mode": (
            (
                "script",
                "turn_on",
                {
                    "entity_id": "script.night_mode",
                    "variables": {"brightness": 20},
                },
            ),
        ),
    }


def test_vacuum_meeting_scenario_resolves_pause_and_notification() -> None:
    calls = _service_calls_for_yaml(
        """
        - id: pause-vacuum-during-meeting
          when: vacuum.office == "cleaning" and binary_sensor.meeting == "on"
          emit:
            target: vacuum.office
            set:
              state: paused
            ttl: 15m

        - id: notify-vacuum-paused
          when: vacuum.office == "cleaning" and binary_sensor.meeting == "on"
          emit:
            target: notify.mobile_app_phone
            set:
              title: Vacuum paused
              message: Office vacuum paused for the meeting
            ttl: 15m
        """,
        [
            _state("vacuum.office", "cleaning", fan_speed="balanced"),
            _state("binary_sensor.meeting", "on"),
        ],
    )

    assert calls == {
        "notify.mobile_app_phone": (
            (
                "notify",
                "mobile_app_phone",
                {
                    "title": "Vacuum paused",
                    "message": "Office vacuum paused for the meeting",
                },
            ),
        ),
        "vacuum.office": (
            ("vacuum", "pause", {"entity_id": "vacuum.office"}),
        ),
    }


def test_remote_movie_mode_scenario_resolves_activity_and_commands() -> None:
    calls = _service_calls_for_yaml(
        """
        - id: start-movie-activity
          when: input_boolean.movie_mode == "on"
          emit:
            target: remote.living_room
            set:
              state: "on"
              activity: Watch TV
            ttl: 30m

        - id: set-tv-home-screen
          when: input_boolean.movie_mode == "on"
          emit:
            target: remote.android_tv
            set:
              command: [HOME, DPAD_RIGHT, DPAD_CENTER]
              device: Android TV
              num_repeats: 1
              delay_secs: 0.4
            ttl: 30s
        """,
        [_state("input_boolean.movie_mode", "on")],
    )

    assert calls == {
        "remote.android_tv": (
            (
                "remote",
                "send_command",
                {
                    "entity_id": "remote.android_tv",
                    "command": ["HOME", "DPAD_RIGHT", "DPAD_CENTER"],
                    "device": "Android TV",
                    "num_repeats": 1,
                    "delay_secs": 0.4,
                },
            ),
        ),
        "remote.living_room": (
            (
                "remote",
                "turn_on",
                {"entity_id": "remote.living_room", "activity": "Watch TV"},
            ),
        ),
    }


def test_security_leak_scenario_resolves_valve_siren_and_notification() -> None:
    calls = _service_calls_for_yaml(
        """
        - id: close-water-main-on-leak
          when: binary_sensor.water_leak == "on"
          emit:
            target: valve.water_main
            set:
              state: closed
            ttl: 30m

        - id: sound-leak-siren
          when: binary_sensor.water_leak == "on"
          emit:
            target: siren.utility_room
            set:
              state: "on"
              tone: alarm
              duration: 30
              volume_level: 0.8
            ttl: 30s

        - id: notify-water-leak
          when: binary_sensor.water_leak == "on"
          emit:
            target: notify.mobile_app_phone
            set:
              title: Water leak
              message: Main valve closed and utility siren started
            ttl: 30s
        """,
        [_state("binary_sensor.water_leak", "on")],
    )

    assert calls == {
        "notify.mobile_app_phone": (
            (
                "notify",
                "mobile_app_phone",
                {
                    "title": "Water leak",
                    "message": "Main valve closed and utility siren started",
                },
            ),
        ),
        "siren.utility_room": (
            (
                "siren",
                "turn_on",
                {
                    "entity_id": "siren.utility_room",
                    "tone": "alarm",
                    "duration": 30,
                    "volume_level": 0.8,
                },
            ),
        ),
        "valve.water_main": (
            ("valve", "close_valve", {"entity_id": "valve.water_main"}),
        ),
    }


def test_lawn_mower_rain_scenario_resolves_dock_and_notification() -> None:
    calls = _service_calls_for_yaml(
        """
        - id: dock-mower-when-raining
          when: lawn_mower.backyard == "mowing" and binary_sensor.rain == "on"
          emit:
            target: lawn_mower.backyard
            set:
              state: returning
            ttl: 30m

        - id: notify-mower-docked-for-rain
          when: lawn_mower.backyard == "mowing" and binary_sensor.rain == "on"
          emit:
            target: notify.mobile_app_phone
            set:
              title: Mower returning
              message: Rain detected, backyard mower sent to dock
            ttl: 30m
        """,
        [
            _state("lawn_mower.backyard", "mowing"),
            _state("binary_sensor.rain", "on"),
        ],
    )

    assert calls == {
        "lawn_mower.backyard": (
            ("lawn_mower", "dock", {"entity_id": "lawn_mower.backyard"}),
        ),
        "notify.mobile_app_phone": (
            (
                "notify",
                "mobile_app_phone",
                {
                    "title": "Mower returning",
                    "message": "Rain detected, backyard mower sent to dock",
                },
            ),
        ),
    }


def test_helper_schedule_scenario_resolves_input_datetime_service_call() -> None:
    calls = _service_calls_for_yaml(
        """
        - id: set-quiet-hours-cutoff
          when: input_boolean.guest_mode == "on"
          emit:
            target: input_datetime.quiet_hours_until
            set:
              datetime: "2026-06-05 22:30:00"
        """,
        [_state("input_boolean.guest_mode", "on")],
    )

    assert calls == {
        "input_datetime.quiet_hours_until": (
            (
                "input_datetime",
                "set_datetime",
                {
                    "entity_id": "input_datetime.quiet_hours_until",
                    "datetime": "2026-06-05 22:30:00",
                },
            ),
        ),
    }


def test_timer_grace_period_scenario_resolves_timer_service_call() -> None:
    calls = _service_calls_for_yaml(
        """
        - id: start-hallway-grace-timer
          when: binary_sensor.hallway_motion == "off"
          emit:
            target: timer.hallway_grace
            set:
              state: active
              duration: "00:05:00"
            ttl: 10s
        """,
        [_state("binary_sensor.hallway_motion", "off")],
    )

    assert calls == {
        "timer.hallway_grace": (
            (
                "timer",
                "start",
                {
                    "entity_id": "timer.hallway_grace",
                    "duration": "00:05:00",
                },
            ),
        ),
    }


def test_update_available_scenario_resolves_install_service_call() -> None:
    calls = _service_calls_for_yaml(
        """
        - id: install-router-update-at-night
          when: update.router_firmware == "on" and time_of_day == "night"
          emit:
            target: update.router_firmware
            set:
              update_action: install
              backup: true
            ttl: 5m
        """,
        [_state("update.router_firmware", "on")],
        now=datetime(2026, 6, 5, 22, 15, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    assert calls == {
        "update.router_firmware": (
            (
                "update",
                "install",
                {"entity_id": "update.router_firmware", "backup": True},
            ),
        ),
    }


def test_counter_scenario_resolves_counter_service_call() -> None:
    calls = _service_calls_for_yaml(
        """
        - id: count-doorbell-rings
          when: binary_sensor.doorbell == "on"
          emit:
            target: counter.doorbell_rings
            set:
              state: increment
            ttl: 5s
        """,
        [_state("binary_sensor.doorbell", "on")],
    )

    assert calls == {
        "counter.doorbell_rings": (
            (
                "counter",
                "increment",
                {"entity_id": "counter.doorbell_rings"},
            ),
        ),
    }


def test_doorbell_action_scenario_resolves_stateless_service_calls() -> None:
    calls = _service_calls_for_yaml(
        """
        - id: ring-phones
          when: binary_sensor.doorbell == "on"
          emit:
            target: rest_command.ring_fritzbox_phones
            set:
              service_data:
                phone: "**9"
            ttl: 5s

        - id: doorbell-persistent-notification
          when: binary_sensor.doorbell == "on"
          emit:
            target: persistent_notification.create
            set:
              title: "Doorbell"
              message: "Someone rang the doorbell"
              service_data:
                notification_id: doorbell
            ttl: 5s

        - id: doorbell-logbook
          when: binary_sensor.doorbell == "on"
          emit:
            target: logbook.log
            set:
              message: "Doorbell rang"
              service_data:
                name: "Intentional"
                entity_id: binary_sensor.doorbell
                domain: binary_sensor
            ttl: 5s

        - id: doorbell-camera-snapshot
          when: binary_sensor.doorbell == "on"
          emit:
            target: camera.front_door
            set:
              camera_action: snapshot
              filename: /tmp/doorbell.jpg
            ttl: 5s
        """,
        [_state("binary_sensor.doorbell", "on")],
    )

    assert calls == {
        "camera.front_door": (
            (
                "camera",
                "snapshot",
                {"entity_id": "camera.front_door", "filename": "/tmp/doorbell.jpg"},
            ),
        ),
        "logbook.log": (
            (
                "logbook",
                "log",
                {
                    "name": "Intentional",
                    "entity_id": "binary_sensor.doorbell",
                    "domain": "binary_sensor",
                    "message": "Doorbell rang",
                },
            ),
        ),
        "persistent_notification.create": (
            (
                "persistent_notification",
                "create",
                {
                    "notification_id": "doorbell",
                    "message": "Someone rang the doorbell",
                    "title": "Doorbell",
                },
            ),
        ),
        "rest_command.ring_fritzbox_phones": (
            ("rest_command", "ring_fritzbox_phones", {"phone": "**9"}),
        ),
    }


def test_doorbell_cast_monitor_scenario_resolves_cast_service_call() -> None:
    calls = _service_calls_for_yaml(
        """
        - id: show-front-door-on-office-display
          when: event.espnow_recv_doorbell.triggered == true and event.espnow_recv_doorbell.event_type == "ringer"
          emit:
            target: cast.show_lovelace_view
            set:
              service_data:
                entity_id: media_player.office_display
                dashboard_path: lovelace
                view_path: front-door
            ttl: 5s
        """,
        [
            _state(
                "event.espnow_recv_doorbell",
                "2026-06-07T08:00:00+00:00",
                triggered=True,
                event_type="ringer",
            ),
        ],
    )

    assert calls == {
        "cast.show_lovelace_view": (
            (
                "cast",
                "show_lovelace_view",
                {
                    "entity_id": "media_player.office_display",
                    "dashboard_path": "lovelace",
                    "view_path": "front-door",
                },
            ),
        ),
    }


def test_doorbell_mqtt_publish_scenario_resolves_publish_service_call() -> None:
    calls = _service_calls_for_yaml(
        """
        - id: publish-doorbell-event
          when: event.espnow_recv_doorbell.triggered == true and event.espnow_recv_doorbell.event_type == "ringer"
          emit:
            target: mqtt.publish
            set:
              service_data:
                topic: intentional/doorbell
                payload: ringer
                qos: 1
                retain: false
            ttl: 5s
        """,
        [
            _state(
                "event.espnow_recv_doorbell",
                "2026-06-07T08:05:00+00:00",
                triggered=True,
                event_type="ringer",
            ),
        ],
    )

    assert calls == {
        "mqtt.publish": (
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
        ),
    }


def test_google_assistant_sync_scenario_resolves_request_sync_service_call() -> None:
    calls = _service_calls_for_yaml(
        """
        - id: sync-google-after-default-dashboard-change
          when: input_boolean.default_dashboard.changed == true
          emit:
            target: google_assistant.request_sync
            set:
              service_data:
                agent_user_id: home
            ttl: 5s
        """,
        [_state("input_boolean.default_dashboard", "on", changed=True)],
    )

    assert calls == {
        "google_assistant.request_sync": (
            ("google_assistant", "request_sync", {"agent_user_id": "home"}),
        ),
    }


def test_assist_satellite_announce_scenario_resolves_announce_service_call() -> None:
    calls = _service_calls_for_yaml(
        """
        - id: announce-air-quality-alert
          when: binary_sensor.lqi_hobbyraum.changed == true and binary_sensor.lqi_hobbyraum == "on"
          emit:
            target: assist_satellite.announce
            set:
              service_data:
                entity_id: assist_satellite.hobbyraum
                message: Air quality alert
                preannounce: false
            ttl: 5s
        """,
        [_state("binary_sensor.lqi_hobbyraum", "on", changed=True)],
    )

    assert calls == {
        "assist_satellite.announce": (
            (
                "assist_satellite",
                "announce",
                {
                    "entity_id": "assist_satellite.hobbyraum",
                    "message": "Air quality alert",
                    "preannounce": False,
                },
            ),
        ),
    }


def test_alarmo_night_arm_scenario_resolves_alarmo_service_call() -> None:
    calls = _service_calls_for_yaml(
        """
        - id: arm-alarmo-night-when-bedtime-starts
          when: schedule.bedtime.changed == true and schedule.bedtime == "on"
          emit:
            target: alarmo.arm
            set:
              service_data:
                entity_id: alarm_control_panel.alarmo
                mode: night
                skip_delay: true
                force: true
            ttl: 5s
        """,
        [_state("schedule.bedtime", "on", changed=True)],
    )

    assert calls == {
        "alarmo.arm": (
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
        ),
    }


def test_device_tracker_presence_scenario_resolves_see_service_call() -> None:
    calls = _service_calls_for_yaml(
        """
        - id: mark-phone-home-when-presence-helper-turns-on
          when: input_boolean.phone_presence_hint.changed == true and input_boolean.phone_presence_hint == "on"
          emit:
            target: device_tracker.see
            set:
              dev_id: phone_tobias
              host_name: Tobias Phone
              location_name: home
              gps: [52.52, 13.405]
              gps_accuracy: 12
              battery: 88
            ttl: 5s
        """,
        [_state("input_boolean.phone_presence_hint", "on", changed=True)],
    )

    assert calls == {
        "device_tracker.see": (
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
        ),
    }


def test_changed_pulse_scenario_resolves_only_for_one_cycle() -> None:
    from intentional.engine import Engine
    from intentional.ha_adapter import (
        clear_state_change_pulses,
        pulse_state_change,
        service_calls_for_resolved_target,
        sync_state_object_into_engine,
    )
    from intentional.yaml_loader import load_rules_from_string

    engine = Engine(clock_fn=lambda: 1000)
    engine.load_rules(load_rules_from_string("""
        - id: notify-door-opened
          when: binary_sensor.front_door.changed == true and binary_sensor.front_door == "on"
          emit:
            target: notify.pixel_8_pro
            set:
              title: Door
              message: Front door opened
            ttl: 5s
    """))
    old_state = _state("binary_sensor.front_door", "off", device_class="door")
    new_state = _state("binary_sensor.front_door", "on", device_class="door")

    sync_state_object_into_engine(engine, new_state)
    assert pulse_state_change(engine, old_state, new_state)
    sync_state_object_into_engine(engine, new_state)
    engine.evaluate_all()

    resolved = engine.resolve("notify.pixel_8_pro")
    assert resolved is not None
    assert service_calls_for_resolved_target(
        "notify.pixel_8_pro",
        dict(resolved.value),
    ) == (
        (
            "notify",
            "pixel_8_pro",
            {"message": "Front door opened", "title": "Door"},
        ),
    )

    clear_state_change_pulses(engine, {"binary_sensor.front_door"})
    engine.evaluate_all()

    assert engine.resolve("notify.pixel_8_pro") is None


def test_update_entity_action_scenario_resolves_homeassistant_service_call() -> None:
    calls = _service_calls_for_yaml(
        """
        - id: refresh-update-sensor-after-update-event
          when: event.update_event.triggered == true
          emit:
            target: homeassistant.update_entity
            set:
              service_data:
                entity_id: update.intentional_update
            ttl: 5s
        """,
        [
            _state(
                "event.update_event",
                "2026-06-06T19:00:00+00:00",
                triggered=True,
            ),
        ],
    )

    assert calls == {
        "homeassistant.update_entity": (
            (
                "homeassistant",
                "update_entity",
                {"entity_id": "update.intentional_update"},
            ),
        ),
    }


def test_shopping_list_scenario_resolves_stateless_service_calls() -> None:
    calls = _service_calls_for_yaml(
        """
        - id: add-humidifier-filter-when-empty
          when: input_boolean.buro_luftbefeuchter_leer == "on"
          emit:
            target: shopping_list.add_item
            set:
              name: Humidifier filter
            ttl: 5s

        - id: sort-shopping-list-at-night
          when: time_of_day == "night"
          emit:
            target: shopping_list.sort
            set:
              reverse: false
            ttl: 5s
        """,
        [_state("input_boolean.buro_luftbefeuchter_leer", "on")],
        now=datetime(2026, 6, 5, 22, 15, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    assert calls == {
        "shopping_list.add_item": (
            ("shopping_list", "add_item", {"name": "Humidifier filter"}),
        ),
        "shopping_list.sort": (
            ("shopping_list", "sort", {"reverse": False}),
        ),
    }


def test_manual_override_clear_scenario_resolves_intentional_service_call() -> None:
    calls = _service_calls_for_yaml(
        """
        - id: clear-office-light-override-when-room-empty
          when: binary_sensor.office_presence == "off" and input_boolean.office_manual_override == "on"
          emit:
            target: intentional.clear
            set:
              service_data:
                target: light.office
            ttl: 5s
        """,
        [
            _state("binary_sensor.office_presence", "off"),
            _state("input_boolean.office_manual_override", "on"),
        ],
    )

    assert calls == {
        "intentional.clear": (
            ("intentional", "clear", {"target": "light.office"}),
        ),
    }


def test_scheduler_action_scenario_resolves_stateless_service_call() -> None:
    calls = _service_calls_for_yaml(
        """
        - id: run-office-schedule-on-workday
          when: input_boolean.workday == "on" and time_of_day == "morning"
          emit:
            target: scheduler.run_action
            set:
              service_data:
                entity_id: switch.schedule_office_lights
                skip_conditions: true
            ttl: 5s
        """,
        [_state("input_boolean.workday", "on")],
        now=datetime(2026, 6, 5, 8, 15, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    assert calls == {
        "scheduler.run_action": (
            (
                "scheduler",
                "run_action",
                {
                    "entity_id": "switch.schedule_office_lights",
                    "skip_conditions": True,
                },
            ),
        ),
    }


def test_input_button_scenario_resolves_helper_press_service_call() -> None:
    calls = _service_calls_for_yaml(
        """
        - id: mark-arrival
          when: binary_sensor.driveway_motion == "on"
          emit:
            target: input_button.mark_arrival
            ttl: 5s
        """,
        [_state("binary_sensor.driveway_motion", "on")],
    )

    assert calls == {
        "input_button.mark_arrival": (
            (
                "input_button",
                "press",
                {"entity_id": "input_button.mark_arrival"},
            ),
        ),
    }


def test_humidifier_comfort_scenario_resolves_service_calls() -> None:
    calls = _service_calls_for_yaml(
        """
        - id: bedroom-dry-air
          when: sensor.bedroom_humidity.humidity < 40 and time_of_day == "night"
          emit:
            target: humidifier.bedroom
            set:
              state: "on"
              humidity: 55
              mode: sleep
            ttl: 30m
        """,
        [_state("sensor.bedroom_humidity", "36", humidity=36)],
        now=datetime(2026, 6, 5, 23, 0, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    assert calls == {
        "humidifier.bedroom": (
            ("humidifier", "turn_on", {"entity_id": "humidifier.bedroom"}),
            (
                "humidifier",
                "set_humidity",
                {"entity_id": "humidifier.bedroom", "humidity": 55},
            ),
            (
                "humidifier",
                "set_mode",
                {"entity_id": "humidifier.bedroom", "mode": "sleep"},
            ),
        ),
    }


def test_water_heater_energy_scenario_resolves_service_calls() -> None:
    calls = _service_calls_for_yaml(
        """
        - id: water-heater-away
          when: input_boolean.away_mode == "on"
          emit:
            target: water_heater.utility
            set:
              temperature: 50
              operation_mode: eco
              away_mode: true
            ttl: 2h
        """,
        [_state("input_boolean.away_mode", "on")],
    )

    assert calls == {
        "water_heater.utility": (
            (
                "water_heater",
                "set_temperature",
                {
                    "entity_id": "water_heater.utility",
                    "temperature": 50,
                    "operation_mode": "eco",
                },
            ),
            (
                "water_heater",
                "set_away_mode",
                {"entity_id": "water_heater.utility", "away_mode": True},
            ),
        ),
    }


def test_scene_and_target_modifier_scenario_resolves_to_scene_and_target_calls() -> None:
    from intentional.engine import Engine
    from intentional.ha_adapter import (
        scene_activation_plan,
        service_calls_for_resolved_target,
        sync_state_object_into_engine,
    )
    from intentional.yaml_loader import load_rules_from_string

    engine = Engine(clock_fn=lambda: 1000)
    engine.load_rules(load_rules_from_string("""
        - id: movie-scene
          when: input_boolean.movie == "on"
          emit:
            scene: scene.movie
            transition: 3s
          authority: user

        - id: movie-energy-cap
          when: input_boolean.movie == "on"
          emit:
            target: light.living_room
            cap:
              brightness_pct: 50
          confidence: 0.5
    """))
    sync_state_object_into_engine(engine, _state("input_boolean.movie", "on"))
    engine.evaluate_all()

    scene_calls, active_scenes, no_longer_active = scene_activation_plan(
        engine,
        set(),
    )
    resolved = engine.resolve("light.living_room")

    assert scene_calls == (
        (
            "scene",
            "turn_on",
            {"entity_id": "scene.movie", "transition": 3.0},
        ),
    )
    assert active_scenes == {"scene.movie"}
    assert no_longer_active == set()
    assert resolved is not None
    assert service_calls_for_resolved_target(
        "light.living_room",
        dict(resolved.value),
    ) == (
        (
            "light",
            "turn_on",
            {"entity_id": "light.living_room"},
        ),
    )

    scene_calls, active_scenes, no_longer_active = scene_activation_plan(
        engine,
        active_scenes,
    )

    assert scene_calls == ()
    assert active_scenes == {"scene.movie"}
    assert no_longer_active == set()


def test_dwell_and_time_scenario_does_not_fire_until_condition_is_stable() -> None:
    from intentional.engine import Engine
    from intentional.ha_adapter import (
        service_calls_for_resolved_target,
        sync_state_object_into_engine,
        sync_time_context_into_engine,
    )
    from intentional.yaml_loader import load_rules_from_string

    engine = Engine(clock_fn=lambda: 1000)
    engine.load_rules(load_rules_from_string("""
        - id: hallway-night-motion
          when: time_of_day == "night" and binary_sensor.hallway_motion == "on"
          for: 5m
          emit:
            target: light.hallway
            set:
              state: "on"
              brightness_pct: 15
    """))
    sync_time_context_into_engine(
        engine,
        datetime(2026, 6, 5, 23, 0, tzinfo=ZoneInfo("Europe/Berlin")),
    )
    sync_state_object_into_engine(
        engine,
        _state("binary_sensor.hallway_motion", "on"),
    )

    engine.evaluate_all()
    assert engine.list_active_targets() == ()

    engine.advance_clock(299_999)
    engine.evaluate_all()
    assert engine.list_active_targets() == ()

    engine.advance_clock(1)
    engine.evaluate_all()
    assert engine.list_active_targets() == ("light.hallway",)
    resolved = engine.resolve("light.hallway")
    assert resolved is not None
    assert service_calls_for_resolved_target(
        "light.hallway",
        dict(resolved.value),
    ) == (
        (
            "light",
            "turn_on",
            {
                "entity_id": "light.hallway",
                "brightness_pct": 15,
            },
        ),
    )
