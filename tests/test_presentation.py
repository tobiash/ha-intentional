"""Tests for Home Assistant-facing presentation helpers."""

from __future__ import annotations

from types import SimpleNamespace

from intentional.intent import Authority
from intentional.presentation import intent_sensor_state, value_summary


def test_value_summary_formats_light_intent_readably() -> None:
    assert value_summary({
        "color_temp_k": 2700,
        "state": "on",
        "effect": "off",
        "brightness_pct": 40,
    }) == "on · 40% · 2700 K · effect off"


def test_intent_sensor_state_is_active_for_automation_intent() -> None:
    resolved = SimpleNamespace(
        winning_intent=SimpleNamespace(authority=Authority.AUTOMATION),
    )

    assert intent_sensor_state(resolved) == "active"


def test_intent_sensor_state_is_manual_override_for_user_intent() -> None:
    resolved = SimpleNamespace(
        winning_intent=SimpleNamespace(authority=Authority.USER),
    )

    assert intent_sensor_state(resolved) == "manual_override"


def test_intent_sensor_state_is_idle_without_resolved_intent() -> None:
    assert intent_sensor_state(None) == "idle"
