from __future__ import annotations

import pytest

from intentional.engine import Engine
from intentional.rule_model import RuleLoadError
from intentional.yaml_loader import load_rules_from_string


def _document(window: str = "{from: '22:00', until: '06:00'}") -> str:
    return f"""
time_windows:
  night: {window}
retention_profiles:
  occupied: {{after: 5m, until: {{binary_sensor.door: {{is: 'off'}}}}}}
rules:
  - id: example
    while:
      time_window: {{in: night}}
      power:
        above: {{value: 10, area: office}}
    hold:
      use: occupied
      after: 10m
    intent:
      light.office: {{state: 'on'}}
"""


def test_profiles_windows_and_power_expand_to_effective_rule() -> None:
    rule = load_rules_from_string(_document())[0]
    assert rule.when == '(time_of_day >= "22:00" or time_of_day < "06:00")'
    assert rule.linger_ms == 600_000
    assert rule.hold_until_when == 'binary_sensor.door == "off"'
    selector = rule.observation_groups[0].selector
    assert (selector.domain, selector.purpose, selector.operator, selector.value) == ("sensor", "power", "gt", 10)


@pytest.mark.parametrize("fragment, message", [
    ("retention_profiles: {bad.name: {after: 1m}}\nrules: []", "Invalid retention profile"),
    ("retention_profiles: {x: {use: y}}\nrules: []", "cannot contain `use`"),
    ("time_windows: {x: {from: '1:00', until: '02:00'}}\nrules: []", "strict HH:MM"),
    ("rules: [{id: x, while: {time_window: {in: missing}}, intent: {light.x: {state: on}}}]", "Unknown time window"),
])
def test_invalid_document_references_are_rejected(fragment: str, message: str) -> None:
    with pytest.raises(RuleLoadError, match=message):
        load_rules_from_string(fragment)


def test_named_window_boundaries_overnight_and_equal_all_day() -> None:
    for clock, expected in (("21:59", False), ("22:00", True), ("05:59", True), ("06:00", False)):
        engine = Engine(selector_resolver=lambda _selector: ["sensor.office_power"])
        engine.load_rules(load_rules_from_string(_document()))
        engine.update_state("sensor.office_power", 20)
        engine.set_time_of_day("test", clock=clock)
        engine.evaluate_all()
        assert bool(engine.list_active_targets()) is expected

    engine = Engine(selector_resolver=lambda _selector: ["sensor.office_power"])
    engine.load_rules(load_rules_from_string(_document("{from: '08:00', until: '08:00'}")))
    engine.update_state("sensor.office_power", 20)
    engine.set_time_of_day("test", clock="08:00")
    engine.evaluate_all()
    assert engine.list_active_targets()


def test_duplicate_yaml_mapping_keys_are_rejected_before_collapse() -> None:
    with pytest.raises(RuleLoadError, match=r"duplicate key 'night'"):
        load_rules_from_string("time_windows:\n  night: {from: '08:00', until: '09:00'}\n  night: {from: '10:00', until: '11:00'}\nrules: []\n")


@pytest.mark.parametrize("document", [
    "retention_profiles:\n  occupied: {after: 1m}\n  occupied: {after: 2m}\nrules: []\n",
    "retention_profiles:\n  occupied: {after: 1m, after: 2m}\nrules: []\n",
    "time_windows:\n  night: {from: '20:00', from: '21:00', until: '06:00'}\nrules: []\n",
    "time_windows: {}\ntime_windows: {}\nrules: []\n",
])
def test_new_declarations_reject_duplicate_names_fields_and_blocks(document: str) -> None:
    with pytest.raises(RuleLoadError, match="duplicate key"):
        load_rules_from_string(document)


def test_legacy_rule_mapping_duplicate_preserves_last_value() -> None:
    rule = load_rules_from_string("""
- id: legacy
  while: {input_boolean.ready: off}
  while: {input_boolean.ready: on}
  intent:
    light.desk: {state: off, state: on}
""")[0]

    assert rule.when == 'input_boolean.ready == "on"'
    assert rule.set == {"state": "on"}


def test_named_window_alias_does_not_change_semantic_fingerprint() -> None:
    named = load_rules_from_string("""
time_windows: {night: {from: '22:00', until: '06:00'}}
rules:
  - id: adaptive
    while: {binary_sensor.office: on}
    hold: {after: {tiers: [{active_for: 0s, duration: 30s}], adjustments: [{window: night, add: 5s}], max: 5m}}
    intent: {light.office: {state: on}}
""")[0]
    literal = load_rules_from_string("""
- id: adaptive
  while: {binary_sensor.office: on}
  hold: {after: {tiers: [{active_for: 0s, duration: 30s}], adjustments: [{from: '22:00', until: '06:00', add: 5s}], max: 5m}}
  intent: {light.office: {state: on}}
""")[0]
    assert Engine.rule_fingerprint(named) == Engine.rule_fingerprint(literal)
