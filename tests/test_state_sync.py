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


def test_state_sync_removes_stale_attributes() -> None:
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

    assert engine.state["sensor.office_light.state"] == "unknown"
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
