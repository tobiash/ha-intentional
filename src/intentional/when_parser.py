"""Parser and evaluator for `when:` expressions.

A `when:` clause is a small, safe expression language that's evaluated
against the current state of Home Assistant entities. The engine
provides a dict of {entity_field: value} and an optional time_of_day
context.

Supported syntax:
- Entity references: 'entity_id.field' or 'entity_id' (defaults to .state)
- Comparison operators: ==, !=, <, <=, >, >=
- Logical operators: and, or, not (with parentheses)
- String literals: "on", 'off'
- Numeric literals: 42, 3.14
- Boolean literals: true, false
- Time helper: time_of_day (one of: morning, afternoon, evening, night)
- Parentheses for grouping

This is intentionally NOT a Python expression evaluator. We parse to
an AST and evaluate that. This makes it safe to evaluate rule expressions
from user-supplied YAML without `eval()`-style injection risks.

Grammar (informal):
    expr        ::= or_expr
    or_expr     ::= and_expr ('or' and_expr)*
    and_expr    ::= not_expr ('and' not_expr)*
    not_expr    ::= 'not' not_expr | atom
    atom        ::= comparison | '(' expr ')'
    comparison  ::= value (comp_op value)?
    value       ::= entity_ref | literal
    entity_ref  ::= IDENT ('.' IDENT)?
    literal     ::= STRING | NUMBER | BOOLEAN
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Union


# ── AST nodes ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EntityRef:
    """Reference to an entity field, e.g. sensor.x.state or light.y.brightness."""

    entity_id: str
    field: str = "state"

    def __str__(self) -> str:
        return f"{self.entity_id}.{self.field}"


@dataclass(frozen=True)
class Literal:
    """A literal value: string, number, or boolean."""

    value: Union[str, int, float, bool]

    def __str__(self) -> str:
        return repr(self.value)


@dataclass(frozen=True)
class Comparison:
    """A comparison between two values, e.g. x == 'on'."""

    left: Union[EntityRef, Literal]
    op: str
    right: Optional[Union[EntityRef, Literal]] = None

    def __str__(self) -> str:
        if self.right is None:
            # Bare entity reference treated as truthy check
            return f"{self.left}"
        return f"{self.left} {self.op} {self.right}"


@dataclass(frozen=True)
class LogicalOp:
    """and, or, or not."""

    op: str
    left: Any
    right: Any = None  # Only used for binary ops

    def __str__(self) -> str:
        if self.op == "not":
            return f"not {self.left}"
        return f"({self.left} {self.op} {self.right})"


WhenAST = Union[Comparison, LogicalOp, EntityRef, Literal]


# ── Errors ───────────────────────────────────────────────────────────


class WhenSyntaxError(Exception):
    """Raised when a when-expression is malformed."""

    def __init__(self, message: str, *, position: int = 0) -> None:
        super().__init__(f"at position {position}: {message}")
        self.position = position


# ── Lexer ────────────────────────────────────────────────────────────


_TOKEN_SPEC = [
    ("WHITESPACE", r"\s+"),
    ("AND", r"\band\b"),
    ("OR", r"\bor\b"),
    ("NOT", r"\bnot\b"),
    ("TRUE", r"\btrue\b"),
    ("FALSE", r"\bfalse\b"),
    ("TIME_OF_DAY", r"\btime_of_day\b"),
    ("LE", r"<="),
    ("GE", r">="),
    ("LT", r"<"),
    ("GT", r">"),
    ("EQ", r"=="),
    ("NE", r"!="),
    ("STRING", r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\''),
    ("NUMBER", r"\d+(?:\.\d+)?"),
    ("DOT", r"\."),
    ("LPAREN", r"\("),
    ("RPAREN", r"\)"),
    ("IDENT", r"[a-zA-Z_][a-zA-Z0-9_]*"),
    ("UNKNOWN", r"."),
]

import re

_TOKEN_RE = re.compile("|".join(f"(?P<{name}>{pat})" for name, pat in _TOKEN_SPEC))


def _tokenize(text: str) -> list[tuple[str, str, int]]:
    """Return a list of (type, value, position) tuples."""
    tokens: list[tuple[str, str, int]] = []
    pos = 0
    while pos < len(text):
        m = _TOKEN_RE.match(text, pos)
        if not m:
            break
        kind = m.lastgroup or "UNKNOWN"
        value = m.group()
        position = m.start()
        if kind != "WHITESPACE":
            tokens.append((kind, value, position))
        pos = m.end()
    if pos < len(text):
        raise WhenSyntaxError(f"unexpected character {text[pos]!r}", position=pos)
    return tokens


# ── Parser ───────────────────────────────────────────────────────────


class _Parser:
    def __init__(self, tokens: list[tuple[str, str, int]]) -> None:
        self.tokens = tokens
        self.pos = 0

    def peek(self) -> Optional[tuple[str, str, int]]:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def advance(self) -> tuple[str, str, int]:
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def expect(self, kind: str) -> tuple[str, str, int]:
        tok = self.peek()
        if tok is None or tok[0] != kind:
            actual = tok[1] if tok else "end of expression"
            raise WhenSyntaxError(f"expected {kind}, got {actual!r}", position=tok[2] if tok else 0)
        return self.advance()

    def parse(self) -> WhenAST:
        if not self.tokens:
            raise WhenSyntaxError("empty expression")
        result = self._parse_or()
        if self.pos < len(self.tokens):
            tok = self.tokens[self.pos]
            raise WhenSyntaxError(f"unexpected token {tok[1]!r}", position=tok[2])
        return result

    def _parse_or(self) -> WhenAST:
        left = self._parse_and()
        while self._match("OR"):
            right = self._parse_and()
            left = LogicalOp(op="or", left=left, right=right)
        return left

    def _parse_and(self) -> WhenAST:
        left = self._parse_not()
        while self._match("AND"):
            right = self._parse_not()
            left = LogicalOp(op="and", left=left, right=right)
        return left

    def _parse_not(self) -> WhenAST:
        if self._match("NOT"):
            operand = self._parse_not()
            return LogicalOp(op="not", left=operand)
        return self._parse_atom()

    def _parse_atom(self) -> WhenAST:
        tok = self.peek()
        if tok is None:
            raise WhenSyntaxError("unexpected end of expression")
        if tok[0] == "LPAREN":
            self.advance()
            expr = self._parse_or()
            self.expect("RPAREN")
            return expr
        return self._parse_comparison()

    def _parse_comparison(self) -> WhenAST:
        left = self._parse_value()
        tok = self.peek()
        if tok and tok[0] in ("EQ", "NE", "LT", "LE", "GT", "GE"):
            op_kind, op_str, _ = self.advance()
            op_map = {"EQ": "==", "NE": "!=", "LT": "<", "LE": "<=", "GT": ">", "GE": ">="}
            right = self._parse_value()
            return Comparison(left=left, op=op_map[op_kind], right=right)
        # Bare entity reference — treat as a "is truthy" comparison
        if isinstance(left, EntityRef):
            return Comparison(left=left, op="==", right=Literal("on"))
        return Comparison(left=left, op="==", right=None)  # bare literal

    def _parse_value(self) -> Union[EntityRef, Literal]:
        tok = self.peek()
        if tok is None:
            raise WhenSyntaxError("expected a value")
        if tok[0] == "STRING":
            self.advance()
            return Literal(self._unquote(tok[1]))
        if tok[0] == "NUMBER":
            self.advance()
            value = float(tok[1]) if "." in tok[1] else int(tok[1])
            return Literal(value)
        if tok[0] == "TRUE":
            self.advance()
            return Literal(True)
        if tok[0] == "FALSE":
            self.advance()
            return Literal(False)
        if tok[0] == "TIME_OF_DAY":
            self.advance()
            return EntityRef("__time__", "time_of_day")
        if tok[0] == "IDENT":
            return self._parse_entity_ref()
        raise WhenSyntaxError(f"unexpected token {tok[1]!r}", position=tok[2])

    def _parse_entity_ref(self) -> EntityRef:
        """Parse an entity reference like 'sensor.x.state' or 'light.y'.

        For 'sensor.x.state': entity_id='sensor.x', field='state'.
        For 'light.y': entity_id='light.y', field='state' (default).
        For 'sensor': entity_id='sensor', field='state' (default).

        The 'state' default means the comparison in the test
        state dict uses key 'sensor.x.state' even when the rule says
        'sensor.x' — the engine injects the '.state' suffix.
        """
        kind, value, pos = self.expect("IDENT")
        parts = [value]
        # Consume .field.field.field — the last component is the field,
        # everything before is the entity_id.
        while self.peek() and self.peek()[0] == "DOT":
            self.advance()
            f_kind, f_value, _ = self.expect("IDENT")
            parts.append(f_value)

        if len(parts) == 1:
            # Just 'sensor' → state by default
            return EntityRef(entity_id=parts[0], field="state")
        if len(parts) == 2:
            # 'sensor.x' → assume .state
            return EntityRef(entity_id=parts[0] + "." + parts[1], field="state")
        # 'sensor.x.state' or longer: entity_id is the joined prefix,
        # field is the last component
        return EntityRef(entity_id=".".join(parts[:-1]), field=parts[-1])

    def _match(self, kind: str) -> bool:
        tok = self.peek()
        if tok and tok[0] == kind:
            self.advance()
            return True
        return False

    @staticmethod
    def _unquote(s: str) -> str:
        """Strip quotes and unescape basic sequences."""
        inner = s[1:-1]
        return inner.replace('\\"', '"').replace("\\'", "'")


def parse_when(text: str) -> WhenAST:
    """Parse a when-expression string into an AST.

    Raises WhenSyntaxError on any syntax error.
    """
    tokens = _tokenize(text)
    return _Parser(tokens).parse()


# ── Evaluator ────────────────────────────────────────────────────────


def evaluate_when(
    ast: WhenAST,
    state: dict[str, Any],
    *,
    time_of_day: Optional[str] = None,
) -> bool:
    """Evaluate a parsed when-AST against the given state.

    Parameters
    ----------
    ast
        The parsed expression tree.
    state
        Dict of {entity_field: value}, e.g. {"sensor.x.state": "on"}.
        Missing keys are treated as None.
    time_of_day
        Current time-of-day bucket: "morning", "afternoon", "evening",
        "night". Used when the expression references `time_of_day`.
    """
    if isinstance(ast, LogicalOp):
        return _eval_logical(ast, state, time_of_day)
    if isinstance(ast, Comparison):
        return _eval_comparison(ast, state, time_of_day)
    if isinstance(ast, EntityRef):
        # Bare entity reference in boolean context: truthy if non-None
        return bool(_resolve(ast, state, time_of_day))
    if isinstance(ast, Literal):
        return bool(ast.value)
    raise WhenSyntaxError(f"unknown AST node: {ast!r}")


def _eval_logical(
    op: LogicalOp,
    state: dict[str, Any],
    time_of_day: Optional[str],
) -> bool:
    if op.op == "not":
        return not evaluate_when(op.left, state, time_of_day=time_of_day)
    if op.op == "and":
        return (
            evaluate_when(op.left, state, time_of_day=time_of_day)
            and evaluate_when(op.right, state, time_of_day=time_of_day)
        )
    if op.op == "or":
        return (
            evaluate_when(op.left, state, time_of_day=time_of_day)
            or evaluate_when(op.right, state, time_of_day=time_of_day)
        )
    raise WhenSyntaxError(f"unknown logical op: {op.op}")


def _eval_comparison(
    comp: Comparison,
    state: dict[str, Any],
    time_of_day: Optional[str],
) -> bool:
    left = _resolve(comp.left, state, time_of_day) if isinstance(comp.left, EntityRef) else comp.left.value
    if comp.right is None:
        return bool(left)
    right = _resolve(comp.right, state, time_of_day) if isinstance(comp.right, EntityRef) else comp.right.value

    try:
        if comp.op == "==":
            return left == right
        if comp.op == "!=":
            return left != right
        if comp.op == "<":
            return left < right
        if comp.op == "<=":
            return left <= right
        if comp.op == ">":
            return left > right
        if comp.op == ">=":
            return left >= right
    except TypeError:
        # Comparing incompatible types (e.g. string < int) is False
        return False
    raise WhenSyntaxError(f"unknown comparison op: {comp.op}")


def _resolve(
    ref: EntityRef,
    state: dict[str, Any],
    time_of_day: Optional[str],
) -> Any:
    """Resolve an entity reference to its current value."""
    if ref.entity_id == "__time__":
        return time_of_day
    key = f"{ref.entity_id}.{ref.field}"
    return state.get(key)
