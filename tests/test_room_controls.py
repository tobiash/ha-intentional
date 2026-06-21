"""Tests for area-derived room controls."""

from __future__ import annotations

import pytest


def test_room_controls_group_rules_by_target_area() -> None:
    pytest.importorskip("homeassistant", reason="homeassistant not installed")

    from custom_components.intentional.room_controls import AreaInfo, room_controls_for_engine
    from intentional.engine import Engine
    from intentional.yaml_loader import load_rules_from_string

    engine = Engine(clock_fn=lambda: 1000)
    engine.load_rules(load_rules_from_string('''
- id: living-room-evening
  observe:
    binary_sensor.living_room_presence: on
  intent:
    light.sofa:
      state: on
- id: kitchen-evening
  observe:
    binary_sensor.kitchen_presence: on
  intent:
    light.counter:
      state: on
'''))
    engine.update_state("binary_sensor.living_room_presence", "on")
    engine.evaluate_all()
    engine.emit_user_intent("light.sofa", {"brightness_pct": 20})

    areas = {
        "light.sofa": AreaInfo(id="living_room", name="Living Room"),
        "light.counter": AreaInfo(id="kitchen", name="Kitchen"),
    }
    controls = room_controls_for_engine(engine, areas.get)

    assert sorted(controls) == ["kitchen", "living_room"]
    assert controls["living_room"].rule_ids == {"living-room-evening"}
    assert controls["living_room"].active_rule_ids == {"living-room-evening"}
    assert controls["living_room"].manual_override_targets == {"light.sofa"}
    assert controls["kitchen"].rule_ids == {"kitchen-evening"}


def test_room_controls_expose_paused_rules() -> None:
    pytest.importorskip("homeassistant", reason="homeassistant not installed")

    from custom_components.intentional.room_controls import AreaInfo, room_controls_for_engine
    from intentional.engine import Engine
    from intentional.yaml_loader import load_rules_from_string

    engine = Engine(clock_fn=lambda: 1000)
    engine.load_rules(load_rules_from_string('''
- id: living-room-evening
  observe:
    binary_sensor.living_room_presence: on
  intent:
    light.sofa:
      state: on
'''))
    engine.set_rule_paused("living-room-evening", True)

    controls = room_controls_for_engine(
        engine,
        lambda target: AreaInfo(id="living_room", name="Living Room") if target == "light.sofa" else None,
    )

    assert controls["living_room"].paused is True
    assert controls["living_room"].paused_rule_ids == {"living-room-evening"}


def test_dashboard_cards_use_room_name_slugs() -> None:
    from types import SimpleNamespace

    from intentional.projection import dashboard_cards

    cards = dashboard_cards({
        "living_room": SimpleNamespace(area_id="living_room", name="Wohnzimmer"),
        "office": SimpleNamespace(area_id="office", name="Büro Tobias"),
    })

    entities = [entity for card in cards["cards"] for entity in card["entities"]]

    assert "sensor.intentional_wohnzimmer_status" in entities
    assert "switch.intentional_pause_wohnzimmer_rules" in entities
    assert "button.intentional_clear_wohnzimmer_manual_overrides" in entities
    assert "sensor.intentional_buro_tobias_status" in entities
