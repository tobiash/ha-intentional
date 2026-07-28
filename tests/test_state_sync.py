"""Tests for syncing Home Assistant state into the intent engine."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo


def test_state_sync_exposes_attributes_to_when_expressions() -> None:
    from intentional.engine import Engine
    from intentional.ha_adapter import sync_state_object_into_engine
    from intentional.yaml_loader import Rule

    engine = Engine(clock_fn=lambda: 1000)
    engine.load_rules([
        Rule(
            id="dark-room",
            when="sensor.office_light.illuminance < 50",
            target="light.office",
            set={"state": "on", "brightness_pct": 80},
        )
    ])

    sync_state_object_into_engine(
        engine,
        SimpleNamespace(
            entity_id="sensor.office_light",
            state="42",
            attributes={"illuminance": 42},
        ),
    )
    engine.evaluate_all()

    resolved = engine.resolve("light.office")
    assert resolved is not None
    assert resolved.value == {"state": "on", "brightness_pct": 80}


def test_state_sync_retains_last_known_values_while_unavailable() -> None:
    from intentional.engine import Engine
    from intentional.ha_adapter import sync_state_object_into_engine

    engine = Engine(clock_fn=lambda: 1000)

    sync_state_object_into_engine(
        engine,
        SimpleNamespace(
            entity_id="sensor.office_light",
            state="42",
            attributes={"illuminance": 42},
        ),
    )
    assert engine.state["sensor.office_light.illuminance"] == 42

    sync_state_object_into_engine(
        engine,
        SimpleNamespace(
            entity_id="sensor.office_light",
            state="unknown",
            attributes={},
        ),
    )

    assert engine.state["sensor.office_light.state"] == "42"
    assert engine.state["sensor.office_light.illuminance"] == 42
    assert engine.state["sensor.office_light.availability"] == "unknown"


def test_state_sync_exposes_unavailable_without_changing_last_known_rule_value() -> None:
    from intentional.engine import Engine
    from intentional.ha_adapter import sync_state_object_into_engine
    from intentional.yaml_loader import Rule

    engine = Engine(clock_fn=lambda: 1000)
    engine.load_rules([
        Rule(
            id="production-high",
            when="sensor.solar_power > 50",
            target="switch.fountain",
            set={"state": "on"},
        ),
        Rule(
            id="source-unavailable",
            when='sensor.solar_power == "unavailable"',
            target="input_boolean.solar_fault",
            set={"state": "on"},
        ),
    ])
    sync_state_object_into_engine(
        engine,
        SimpleNamespace(entity_id="sensor.solar_power", state="60", attributes={}),
    )
    engine.evaluate_all()
    assert engine.resolve("switch.fountain") is not None
    assert engine.resolve("input_boolean.solar_fault") is None

    sync_state_object_into_engine(
        engine,
        SimpleNamespace(
            entity_id="sensor.solar_power", state="unavailable", attributes={}
        ),
    )
    engine.evaluate_all()

    assert engine.state["sensor.solar_power.state"] == "60"
    assert engine.resolve("switch.fountain") is not None
    assert engine.resolve("input_boolean.solar_fault") is not None


def test_state_sync_removes_stale_attributes_after_recovery() -> None:
    from intentional.engine import Engine
    from intentional.ha_adapter import sync_state_object_into_engine

    engine = Engine(clock_fn=lambda: 1000)
    sync_state_object_into_engine(
        engine,
        SimpleNamespace(
            entity_id="sensor.office_light",
            state="42",
            attributes={"illuminance": 42},
        ),
    )
    sync_state_object_into_engine(
        engine,
        SimpleNamespace(
            entity_id="sensor.office_light", state="unavailable", attributes={}
        ),
    )
    sync_state_object_into_engine(
        engine,
        SimpleNamespace(entity_id="sensor.office_light", state="41", attributes={}),
    )

    assert engine.state["sensor.office_light.state"] == "41"
    assert engine.state["sensor.office_light.availability"] == "available"
    assert "sensor.office_light.illuminance" not in engine.state


def test_event_state_change_exposes_one_cycle_trigger_pulse() -> None:
    from intentional.engine import Engine
    from intentional.ha_adapter import (
        clear_event_trigger_pulses,
        pulse_event_state_change,
        sync_state_object_into_engine,
    )
    from intentional.yaml_loader import Rule

    engine = Engine(clock_fn=lambda: 1000)
    engine.load_rules([
        Rule(
            id="doorbell-notify",
            when=(
                "event.espnow_recv_doorbell.triggered == true "
                'and event.espnow_recv_doorbell.event_type == "ringer"'
            ),
            target="telegram_bot.send_message",
            set={"message": "Doorbell"},
        )
    ])
    old_state = SimpleNamespace(
        entity_id="event.espnow_recv_doorbell",
        state="2026-06-05T18:00:00+00:00",
        attributes={"event_type": "ringer"},
    )
    new_state = SimpleNamespace(
        entity_id="event.espnow_recv_doorbell",
        state="2026-06-05T18:07:36+00:00",
        attributes={"event_type": "ringer"},
    )

    sync_state_object_into_engine(engine, new_state)
    assert pulse_event_state_change(engine, old_state, new_state)
    sync_state_object_into_engine(engine, new_state)
    engine.evaluate_all()

    resolved = engine.resolve("telegram_bot.send_message")
    assert resolved is not None
    assert resolved.value == {"message": "Doorbell"}

    clear_event_trigger_pulses(engine, {"event.espnow_recv_doorbell"})
    engine.evaluate_all()

    assert engine.resolve("telegram_bot.send_message") is None
    assert engine.state["event.espnow_recv_doorbell.changed"] is False
    assert engine.state["event.espnow_recv_doorbell.triggered"] is False


def test_event_state_change_does_not_pulse_initial_state() -> None:
    from intentional.engine import Engine
    from intentional.ha_adapter import pulse_event_state_change

    engine = Engine(clock_fn=lambda: 1000)
    new_state = SimpleNamespace(
        entity_id="event.espnow_recv_doorbell",
        state="2026-06-05T18:07:36+00:00",
        attributes={"event_type": "ringer"},
    )

    assert not pulse_event_state_change(engine, None, new_state)
    assert "event.espnow_recv_doorbell.triggered" not in engine.state


def test_regular_state_change_exposes_one_cycle_changed_pulse() -> None:
    from intentional.engine import Engine
    from intentional.ha_adapter import (
        clear_state_change_pulses,
        pulse_state_change,
        sync_state_object_into_engine,
    )
    from intentional.yaml_loader import Rule

    engine = Engine(clock_fn=lambda: 1000)
    engine.load_rules([
        Rule(
            id="door-open-notify",
            when='binary_sensor.front_door.changed == true and binary_sensor.front_door == "on"',
            target="notify.pixel_8_pro",
            set={"message": "Front door opened"},
        )
    ])
    old_state = SimpleNamespace(
        entity_id="binary_sensor.front_door",
        state="off",
        attributes={"device_class": "door"},
    )
    new_state = SimpleNamespace(
        entity_id="binary_sensor.front_door",
        state="on",
        attributes={"device_class": "door"},
    )

    sync_state_object_into_engine(engine, new_state)
    assert pulse_state_change(engine, old_state, new_state)
    sync_state_object_into_engine(engine, new_state)
    engine.evaluate_all()

    resolved = engine.resolve("notify.pixel_8_pro")
    assert resolved is not None
    assert resolved.value == {"message": "Front door opened"}

    clear_state_change_pulses(engine, {"binary_sensor.front_door"})
    engine.evaluate_all()

    assert engine.resolve("notify.pixel_8_pro") is None
    assert engine.state["binary_sensor.front_door.changed"] is False
    assert "binary_sensor.front_door.triggered" not in engine.state


def test_clearing_state_change_pulses_tolerates_reentrant_pulse_addition() -> None:
    from intentional.engine import Engine
    from intentional.ha_adapter import clear_state_change_pulses

    engine = Engine(clock_fn=lambda: 1000)
    pulses = {"binary_sensor.front_door"}
    engine.update_state("binary_sensor.front_door", True, field="changed")
    engine.update_state("binary_sensor.back_door", True, field="changed")

    def add_pulse(_entity_id: str, _value: object) -> None:
        pulses.add("binary_sensor.back_door")

    engine.on_state_change(add_pulse)

    clear_state_change_pulses(engine, pulses)

    assert engine.state["binary_sensor.front_door.changed"] is False
    assert engine.state["binary_sensor.back_door.changed"] is True
    assert pulses == {"binary_sensor.front_door", "binary_sensor.back_door"}


def test_vnext_edge_intent_persists_until_ttl_after_pulse_clears() -> None:
    from intentional.engine import Engine
    from intentional.ha_adapter import (
        clear_state_change_pulses,
        pulse_state_change,
        sync_state_object_into_engine,
    )
    from intentional.yaml_loader import load_rules_from_string

    engine = Engine(clock_fn=lambda: 1000)
    engine.load_rules(load_rules_from_string("""
- id: door-open-light
  observe:
    changed:
      binary_sensor.front_door:
        to: on
  intent:
    light.entry:
      ttl: 2m
      state: on
"""))
    old_state = SimpleNamespace(entity_id="binary_sensor.front_door", state="off", attributes={})
    new_state = SimpleNamespace(entity_id="binary_sensor.front_door", state="on", attributes={})

    sync_state_object_into_engine(engine, new_state)
    assert pulse_state_change(engine, old_state, new_state)
    engine.evaluate_all()
    assert engine.resolve("light.entry").value == {"state": "on"}

    clear_state_change_pulses(engine, {"binary_sensor.front_door"})
    engine.evaluate_all()
    assert engine.resolve("light.entry").value == {"state": "on"}

    engine.advance_clock(120_000)
    engine.evaluate_all()
    assert engine.resolve("light.entry") is None


def test_regular_state_change_does_not_pulse_initial_state() -> None:
    from intentional.engine import Engine
    from intentional.ha_adapter import pulse_state_change

    engine = Engine(clock_fn=lambda: 1000)
    new_state = SimpleNamespace(
        entity_id="binary_sensor.front_door",
        state="on",
        attributes={"device_class": "door"},
    )

    assert not pulse_state_change(engine, None, new_state)
    assert "binary_sensor.front_door.changed" not in engine.state


def test_availability_changes_do_not_emit_changed_pulses() -> None:
    from intentional.engine import Engine
    from intentional.ha_adapter import pulse_state_change

    engine = Engine(clock_fn=lambda: 1000)
    available = SimpleNamespace(
        entity_id="sensor.solar_power", state="42", attributes={}
    )
    unavailable = SimpleNamespace(
        entity_id="sensor.solar_power", state="unavailable", attributes={}
    )

    assert not pulse_state_change(engine, available, unavailable)
    assert not pulse_state_change(engine, unavailable, available)
    assert "sensor.solar_power.changed" not in engine.state


def test_time_sync_sets_bucket_and_exact_clock_context() -> None:
    from intentional.engine import Engine
    from intentional.ha_adapter import sync_time_context_into_engine
    from intentional.yaml_loader import Rule

    engine = Engine(clock_fn=lambda: 1000)
    engine.load_rules([
        Rule(
            id="bedroom-off-at-23",
            when='time_of_day == "night" and time_of_day == "23:00"',
            target="light.bedroom",
            set={"state": "off"},
        )
    ])

    sync_time_context_into_engine(
        engine,
        datetime(2026, 6, 5, 23, 0, tzinfo=ZoneInfo("Europe/Berlin")),
    )
    engine.evaluate_all()

    resolved = engine.resolve("light.bedroom")
    assert resolved is not None
    assert resolved.value == {"state": "off"}


def test_time_sync_bucket_boundaries() -> None:
    from intentional.ha_adapter import time_of_day_bucket

    assert time_of_day_bucket(4) == "night"
    assert time_of_day_bucket(5) == "morning"
    assert time_of_day_bucket(12) == "afternoon"
    assert time_of_day_bucket(17) == "evening"
    assert time_of_day_bucket(22) == "night"
