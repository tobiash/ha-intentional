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
- Numeric literals: 42, 3.14, -61
- Boolean literals: true, false
- Time helper: time_of_day (bucket names or exact HH:MM clock values)
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

import re
from dataclasses import dataclass
from typing import Any

# ── AST nodes ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EntityRef:
    """Reference to an entity field, e.g. sensor.x.state or light.y.brightness."""

    entity_id: str
    field: str = "state"

    def __str__(self) -> str:
        return f"{self.entity_id}.{self.field}"


@dataclass(frozen=True)
class TimeOfDay:
    """Current time helper value for bucket and exact clock comparisons."""

    bucket: str
    clock: str | None = None


@dataclass(frozen=True)
class Literal:
    """A literal value: string, number, or boolean."""

    value: str | int | float | bool

    def __str__(self) -> str:
        return repr(self.value)


@dataclass(frozen=True)
class Comparison:
    """A comparison between two values, e.g. x == 'on'."""

    left: EntityRef | Literal
    op: str
    right: EntityRef | Literal | None = None

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


WhenAST = Comparison | LogicalOp | EntityRef | Literal


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
    ("NUMBER", r"-?\d+(?:\.\d+)?"),
    ("DOT", r"\."),
    ("LPAREN", r"\("),
    ("RPAREN", r"\)"),
    ("IDENT", r"[a-zA-Z_][a-zA-Z0-9_]*"),
    ("UNKNOWN", r"."),
]

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

    def peek(self) -> tuple[str, str, int] | None:
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

    def _parse_value(self) -> EntityRef | Literal:
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
        kind, value, _pos = self.expect("IDENT")
        parts = [value]
        # Consume .field.field.field — the last component is the field,
        # everything before is the entity_id.
        while self.peek() and self.peek()[0] == "DOT":
            self.advance()
            _f_kind, f_value, _ = self.expect("IDENT")
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
    time_of_day: str | TimeOfDay | None = None,
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
        Current time context. A plain string is treated as a bucket name.
        TimeOfDay values support both bucket and exact HH:MM comparisons.
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
    time_of_day: str | TimeOfDay | None,
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
    time_of_day: str | TimeOfDay | None,
) -> bool:
    left = _resolve(comp.left, state, time_of_day) if isinstance(comp.left, EntityRef) else comp.left.value
    if comp.right is None:
        return bool(left)
    right = _resolve(comp.right, state, time_of_day) if isinstance(comp.right, EntityRef) else comp.right.value

    time_comparison = _compare_time_of_day(left, right, comp.op)
    if time_comparison is not None:
        return time_comparison

    numeric_comparison = _compare_numeric_values(left, right, comp.op)
    if numeric_comparison is not None:
        return numeric_comparison

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
    time_of_day: str | TimeOfDay | None,
) -> Any:
    """Resolve an entity reference to its current value."""
    if ref.entity_id == "__time__":
        return time_of_day
    key = f"{ref.entity_id}.{ref.field}"
    return state.get(key)


_CLOCK_RE = re.compile(r"^(?P<hour>[0-1]?\d|2[0-3]):(?P<minute>[0-5]\d)$")


def _compare_time_of_day(left: Any, right: Any, op: str) -> bool | None:
    """Compare TimeOfDay values with bucket names or HH:MM strings."""
    if isinstance(left, TimeOfDay):
        return _compare_time_value(left, right, op)
    if isinstance(right, TimeOfDay):
        reversed_op = {
            "==": "==",
            "!=": "!=",
            "<": ">",
            "<=": ">=",
            ">": "<",
            ">=": "<=",
        }[op]
        return _compare_time_value(right, left, reversed_op)
    return None


def _compare_time_value(value: TimeOfDay, other: Any, op: str) -> bool:
    if not isinstance(other, str):
        return False

    other_minutes = _clock_minutes(other)
    if other_minutes is not None:
        if value.clock is None:
            return op == "!="
        value_minutes = _clock_minutes(value.clock)
        if value_minutes is None:
            return op == "!="
        return _compare_ordered(value_minutes, other_minutes, op)

    if op == "==":
        return value.bucket == other
    if op == "!=":
        return value.bucket != other
    return False


def _compare_ordered(left: int, right: int, op: str) -> bool:
    if op == "==":
        return left == right
    if op == "!=":
        return left != right
    if op == "<":
        return left < right
    if op == "<=":
        return left <= right
    if op == ">":
        return left > right
    if op == ">=":
        return left >= right
    raise WhenSyntaxError(f"unknown comparison op: {op}")


def _compare_numeric_values(left: Any, right: Any, op: str) -> bool | None:
    """Compare numbers even when Home Assistant provides numeric state as text."""
    if op not in {"<", "<=", ">", ">="}:
        return None
    left_number = _numeric_value(left)
    right_number = _numeric_value(right)
    if left_number is None or right_number is None:
        return None
    if op == "<":
        return left_number < right_number
    if op == "<=":
        return left_number <= right_number
    if op == ">":
        return left_number > right_number
    if op == ">=":
        return left_number >= right_number
    raise WhenSyntaxError(f"unknown comparison op: {op}")


def _numeric_value(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped:
        return None
    try:
        return float(stripped)
    except ValueError:
        return None


def _clock_minutes(value: str) -> int | None:
    match = _CLOCK_RE.match(value)
    if match is None:
        return None
    return int(match.group("hour")) * 60 + int(match.group("minute"))


# ── Entity-reference collection ──────────────────────────────────────


def referenced_entities(rules: Any) -> frozenset[str]:
    """Return entity_ids statically referenced by rule expressions.

    Walks the parsed AST of each rule's ``when``, ``hold_when``, and
    ``hold_until_when`` expressions, collecting ``EntityRef`` values.
    Excludes the ``__time__`` helper. Selector-based references are not
    included (they resolve dynamically during evaluation); see ADR-0003.
    """
    entity_ids: set[str] = set()
    for rule in rules:
        for expr in (rule.when, rule.hold_when, rule.hold_until_when):
            if not expr:
                continue
            try:
                ast = parse_when(expr)
            except WhenSyntaxError:
                continue
            entity_ids.update(_collect_entity_ids(ast))
        if rule.for_entity:
            entity_ids.add(rule.for_entity)
    entity_ids.discard("__time__")
    return frozenset(entity_ids)


def _collect_entity_ids(node: WhenAST) -> set[str]:
    """Recursively collect entity_ids from a parsed when-AST."""
    ids: set[str] = set()
    if isinstance(node, EntityRef):
        ids.add(node.entity_id)
    elif isinstance(node, (Comparison, LogicalOp)):
        ids.update(_collect_entity_ids(node.left))
        if node.right is not None:
            ids.update(_collect_entity_ids(node.right))
    return ids
