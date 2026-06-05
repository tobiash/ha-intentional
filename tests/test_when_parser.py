"""Tests for the when-expression parser.

A `when:` clause is a string expression that's evaluated against the
current state of Home Assistant entities. Examples:

- 'sensor.x.state == "on"'
- 'binary_sensor.door.state == "on" and time_of_day == "night"'
- 'media_player.tv.state == "on" and brightness_pct < 50'
- 'input_boolean.focus == "off"'

The expression supports:
- Entity references: 'entity_id.field' or just 'entity_id' (defaults to .state)
- Comparison operators: ==, !=, <, <=, >, >=
- Logical operators: and, or, not (with parentheses)
- String literals: "on", 'off'
- Numeric literals: 42, 3.14
- Time helpers: time_of_day buckets and exact HH:MM clock values

The parser produces an AST that the engine evaluates. It does NOT do
attribute access via Python's `eval` — that would be a security risk.
"""

from __future__ import annotations

import pytest

from intentional.when_parser import (
    Comparison,
    LogicalOp,
    TimeOfDay,
    WhenSyntaxError,
    evaluate_when,
    parse_when,
)

# ── Parsing ──────────────────────────────────────────────────────────


class TestParsing:
    def test_simple_equality(self) -> None:
        ast = parse_when('sensor.x.state == "on"')
        assert isinstance(ast, Comparison)
        assert ast.op == "=="

    def test_inequality(self) -> None:
        ast = parse_when('sensor.x.state != "off"')
        assert isinstance(ast, Comparison)
        assert ast.op == "!="

    def test_numeric_comparison(self) -> None:
        ast = parse_when('sensor.temp.state > 20')
        assert isinstance(ast, Comparison)
        assert ast.op == ">"

    def test_and_expression(self) -> None:
        ast = parse_when('sensor.x.state == "on" and sensor.y.state == "off"')
        assert isinstance(ast, LogicalOp)
        assert ast.op == "and"

    def test_or_expression(self) -> None:
        ast = parse_when('sensor.x.state == "on" or sensor.y.state == "off"')
        assert isinstance(ast, LogicalOp)
        assert ast.op == "or"

    def test_not_expression(self) -> None:
        ast = parse_when('not sensor.x.state == "on"')
        assert isinstance(ast, LogicalOp)
        assert ast.op == "not"

    def test_parentheses(self) -> None:
        ast = parse_when('(sensor.x.state == "on" or sensor.y.state == "on") and sensor.z.state == "on"')
        assert isinstance(ast, LogicalOp)
        assert ast.op == "and"

    def test_bare_entity_reference(self) -> None:
        """Without .state, defaults to .state. So 'sensor.x' means 'sensor.x.state'."""
        ast = parse_when('sensor.x == "on"')
        assert isinstance(ast, Comparison)

    def test_time_of_day(self) -> None:
        ast = parse_when('time_of_day == "night"')
        assert isinstance(ast, Comparison)


# ── Syntax errors ────────────────────────────────────────────────────


class TestSyntaxErrors:
    def test_empty_string_raises(self) -> None:
        with pytest.raises(WhenSyntaxError):
            parse_when("")

    def test_unclosed_paren_raises(self) -> None:
        with pytest.raises(WhenSyntaxError):
            parse_when('(sensor.x.state == "on"')

    def test_unknown_function_raises(self) -> None:
        with pytest.raises(WhenSyntaxError):
            parse_when('hack() and sensor.x.state == "on"')

    def test_chained_comparison_raises(self) -> None:
        """We don't support Python-style chained comparisons."""
        with pytest.raises(WhenSyntaxError):
            parse_when('1 < sensor.x.state < 10')


# ── Evaluation ───────────────────────────────────────────────────────


class TestEvaluation:
    def test_simple_equality_true(self) -> None:
        ast = parse_when('sensor.x.state == "on"')
        assert evaluate_when(ast, {"sensor.x.state": "on"}) is True

    def test_simple_equality_false(self) -> None:
        ast = parse_when('sensor.x.state == "on"')
        assert evaluate_when(ast, {"sensor.x.state": "off"}) is False

    def test_and_both_true(self) -> None:
        ast = parse_when('sensor.x.state == "on" and sensor.y.state == "on"')
        result = evaluate_when(ast, {"sensor.x.state": "on", "sensor.y.state": "on"})
        assert result is True

    def test_and_one_false(self) -> None:
        ast = parse_when('sensor.x.state == "on" and sensor.y.state == "on"')
        result = evaluate_when(ast, {"sensor.x.state": "on", "sensor.y.state": "off"})
        assert result is False

    def test_or_one_true(self) -> None:
        ast = parse_when('sensor.x.state == "on" or sensor.y.state == "on"')
        result = evaluate_when(ast, {"sensor.x.state": "off", "sensor.y.state": "on"})
        assert result is True

    def test_not(self) -> None:
        ast = parse_when('not sensor.x.state == "on"')
        result = evaluate_when(ast, {"sensor.x.state": "off"})
        assert result is True

    def test_numeric_comparison(self) -> None:
        ast = parse_when('sensor.temp.state > 20')
        result = evaluate_when(ast, {"sensor.temp.state": 25})
        assert result is True

    def test_missing_entity_returns_none_state(self) -> None:
        """If an entity isn't in the state dict, treat it as None."""
        ast = parse_when('sensor.x.state == "on"')
        result = evaluate_when(ast, {})
        assert result is False  # None != "on"

    def test_time_of_day_helper(self) -> None:
        """time_of_day is supplied by the engine, not the state dict."""
        ast = parse_when('time_of_day == "night"')
        result = evaluate_when(ast, {}, time_of_day="night")
        assert result is True
        result = evaluate_when(ast, {}, time_of_day="day")
        assert result is False

    def test_time_of_day_matches_bucket_and_exact_clock(self) -> None:
        context = TimeOfDay(bucket="night", clock="23:00")

        assert evaluate_when(
            parse_when('time_of_day == "night"'),
            {},
            time_of_day=context,
        )
        assert evaluate_when(
            parse_when('time_of_day == "23:00"'),
            {},
            time_of_day=context,
        )
        assert not evaluate_when(
            parse_when('time_of_day == "22:59"'),
            {},
            time_of_day=context,
        )

    def test_time_of_day_supports_clock_ordering(self) -> None:
        context = TimeOfDay(bucket="evening", clock="21:30")

        assert evaluate_when(
            parse_when('time_of_day >= "21:00" and time_of_day < "22:00"'),
            {},
            time_of_day=context,
        )
        assert evaluate_when(
            parse_when('"21:00" <= time_of_day and "22:00" > time_of_day'),
            {},
            time_of_day=context,
        )
