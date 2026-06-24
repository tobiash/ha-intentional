"""Tests for the referenced_entities AST collector (ADR-0003 phase 1)."""

from intentional.when_parser import referenced_entities
from intentional.yaml_loader import Rule


def test_single_entity_reference() -> None:
    rules = [Rule(id="r1", when="input_boolean.work == 'on'", target="light.desk", set={"state": "on"})]
    assert referenced_entities(rules) == frozenset({"input_boolean.work"})


def test_compound_expression_collects_all_entities() -> None:
    rules = [Rule(
        id="r1",
        when="binary_sensor.presence == 'on' and sensor.lux < 50",
        target="light.desk",
        set={"state": "on"},
    )]
    assert referenced_entities(rules) == frozenset({"binary_sensor.presence", "sensor.lux"})


def test_hold_when_and_hold_until_when_collected() -> None:
    rules = [Rule(
        id="r1",
        when="binary_sensor.presence == 'on'",
        hold_when="input_boolean.guest == 'off'",
        hold_until_when="binary_sensor.presence == 'off'",
        target="light.desk",
        set={"state": "on"},
    )]
    assert referenced_entities(rules) == frozenset({
        "binary_sensor.presence", "input_boolean.guest",
    })


def test_for_entity_collected() -> None:
    rules = [Rule(
        id="r1",
        when="binary_sensor.motion == 'on'",
        for_entity="input_number.delay",
        target="light.hall",
        set={"state": "on"},
    )]
    assert referenced_entities(rules) == frozenset({"binary_sensor.motion", "input_number.delay"})


def test_time_helper_excluded() -> None:
    rules = [Rule(
        id="r1",
        when="time_of_day >= '22:00'",
        target="light.bedroom",
        set={"state": "on"},
    )]
    assert referenced_entities(rules) == frozenset()


def test_always_true_rule_has_no_entities() -> None:
    rules = [Rule(id="r1", when="true", target="light.desk", set={"state": "on"})]
    assert referenced_entities(rules) == frozenset()


def test_multiple_rules_union() -> None:
    rules = [
        Rule(id="r1", when="sensor.a > 10", target="light.x", set={"state": "on"}),
        Rule(id="r2", when="sensor.b == 'on'", target="light.y", set={"state": "on"}),
    ]
    assert referenced_entities(rules) == frozenset({"sensor.a", "sensor.b"})


def test_entity_field_reference() -> None:
    """References like light.desk.brightness should yield entity_id light.desk."""
    rules = [Rule(
        id="r1",
        when="light.desk.brightness > 100",
        target="light.desk",
        set={"state": "on"},
    )]
    assert referenced_entities(rules) == frozenset({"light.desk"})
