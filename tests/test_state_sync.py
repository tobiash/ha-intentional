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
