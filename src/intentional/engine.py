"""The engine: orchestrates rules, state, intents, animations.

The Engine ties together:
- The YAML-loaded Rule set
- A state dict (entity_id.field → value)
- The compositor (turning intents into resolved values)
- Animation timing (via the tick loop)

It is intentionally HA-agnostic. The HA integration layer (in
`custom_components/intentional/`) wires this engine to HA's state
machine and service calls. The engine itself is fully testable in
isolation.

The engine's public API is small:

- update_state(entity_id, value, field='state'): inject a state change
- load_rules(rules): replace the rule set
- emit_user_intent(target, set, ...): inject a manual intent
- evaluate_all(): re-evaluate `when` clauses, emit/drop intents
- resolve(target): get the ResolvedIntent for a target
- list_active_intents(target): get the raw Intent list
- tick(t_ms): advance animation timing
- explain(target): human-readable explanation of why a target has its value
- on_state_change(callback): subscribe to state changes
- set_time_of_day(bucket): for `time_of_day` in expressions
- advance_clock(ms): for tests, advance the engine's internal clock

Time and clocks
---------------
The engine tracks a virtual clock (in ms) that can be advanced via
`advance_clock()`. This makes TTL expiry and animation timing testable
without real wall-clock waits. The clock starts at 0 by default; HA's
integration layer sets it to the real current time on startup.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from intentional.compositor import ResolvedIntent, resolve_intents
from intentional.intent import Authority, Intent
from intentional.when_parser import WhenAST, evaluate_when, parse_when
from intentional.yaml_loader import Rule

StateChangeCallback = Callable[[str, Any], None]


@dataclass
class _ParsedRule:
    """Internal: a Rule plus its parsed when-AST."""

    rule: Rule
    when_ast: WhenAST


class Engine:
    """The intent engine.

    Holds rules, state, and the set of currently-active intents. Drives
    animation timing via tick(). Exposes resolve() to get the final value
    for any target.
    """

    def __init__(self, *, clock_fn: Callable[[], int] | None = None) -> None:
        self._rules: dict[str, _ParsedRule] = {}
        self.state: dict[str, Any] = {}
        self._active_intents: list[Intent] = []
        self._time_of_day: str | None = None
        self._clock_fn = clock_fn or (lambda: int(time.time() * 1000))
        self._clock_offset_ms: int = 0  # for tests: advance_clock adds to this
        self._animation_started_at: dict[str, int] = {}  # rule_id → ms
        self._state_change_callbacks: list[StateChangeCallback] = []
        self._log: list[str] = []  # last N log lines for diagnostics

    # ── Time ────────────────────────────────────────────────────────

    def now_ms(self) -> int:
        """Return the current engine time in milliseconds."""
        return self._clock_fn() + self._clock_offset_ms

    def advance_clock(self, delta_ms: int) -> None:
        """Advance the engine's clock by delta_ms. For tests only."""
        self._clock_offset_ms += delta_ms

    def set_time_of_day(self, bucket: str) -> None:
        """Set the current time-of-day bucket for `time_of_day` references."""
        self._time_of_day = bucket

    # ── State ───────────────────────────────────────────────────────

    def update_state(
        self,
        entity_id: str,
        value: Any,
        *,
        field: str = "state",
    ) -> None:
        """Inject a state change. Notifies subscribers."""
        key = f"{entity_id}.{field}"
        old = self.state.get(key)
        self.state[key] = value
        if old != value:
            for cb in self._state_change_callbacks:
                cb(entity_id, value)

    def on_state_change(self, callback: StateChangeCallback) -> None:
        """Register a callback for state changes. Returns the callback for chaining."""
        self._state_change_callbacks.append(callback)
        return callback

    # ── Rules ───────────────────────────────────────────────────────

    def load_rules(self, rules: list[Rule]) -> None:
        """Replace the rule set. Parses each rule's `when` clause."""
        self._rules = {}
        for rule in rules:
            try:
                when_ast = parse_when(rule.when)
            except Exception as e:
                self._log.append(f"Failed to parse when for {rule.id!r}: {e}")
                continue
            self._rules[rule.id] = _ParsedRule(rule=rule, when_ast=when_ast)
        # Drop intents for rules that no longer exist
        self._active_intents = [
            i for i in self._active_intents
            if not i.rule_id or i.rule_id in self._rules
        ]

    def add_rule(self, rule: Rule) -> None:
        """Add or replace a single rule."""
        self.load_rules([r for r in (pr.rule for pr in self._rules.values()) if r.id != rule.id] + [rule])

    # ── Manual intent injection ────────────────────────────────────

    def emit_user_intent(
        self,
        target: str,
        set: dict[str, Any] | None = None,
        *,
        cap: dict[str, Any] | None = None,
        floor: dict[str, Any] | None = None,
        ttl_ms: int | None = None,
        reason: str = "Manual user action",
    ) -> Intent:
        """Inject a user-authority intent. Used for manual overrides."""
        intent = Intent(
            target=target,
            set=set or {},
            cap=cap or {},
            floor=floor or {},
            ttl_ms=ttl_ms,
            authority=Authority.USER,
            reason=reason,
            created_at_ms=self.now_ms(),
        )
        self._active_intents.append(intent)
        return intent

    # ── Evaluation ─────────────────────────────────────────────────

    def evaluate_all(self) -> None:
        """Re-evaluate all rules' `when` clauses against current state.

        Emits intents for triggers that just fired. Drops intents for
        triggers that no longer fire (UNLESS the rule has a TTL, in which
        case the intent persists until it expires).

        Also drops intents whose rules no longer exist.
        """
        now = self.now_ms()
        # Collect currently-firing rule IDs (and which target they affect)
        firing: dict[str, str] = {}  # rule_id → target
        for rule_id, parsed in self._rules.items():
            if self._eval_when(parsed.when_ast):
                firing[rule_id] = parsed.rule.target

        # Filter active intents:
        # - Drop rule-bound intents whose rule is no longer firing
        #   (TTL doesn't keep an intent alive after its trigger stops —
        #   TTL is "for how long this intent may be active", not
        #   "how long to keep it around after the rule withdraws")
        # - Keep user/manual intents (no rule_id) until their TTL expires
        new_active: list[Intent] = []
        for intent in self._active_intents:
            if not intent.rule_id:
                # Manual user intent — keep until TTL expires
                if not intent.is_expired(into_the_future_ms=now):
                    new_active.append(intent)
                continue
            if intent.rule_id in firing:
                # Rule still fires — keep
                new_active.append(intent)
                continue
            # Rule no longer fires — drop, regardless of TTL
            continue

        # Add new intents for rules that just started firing
        existing_rule_ids = {i.rule_id for i in new_active if i.rule_id}
        for rule_id, _target in firing.items():
            if rule_id not in existing_rule_ids:
                parsed = self._rules[rule_id]
                intent = self._spawn_intent_from_rule(parsed.rule, now)
                new_active.append(intent)
                self._animation_started_at[rule_id] = now

        self._active_intents = new_active

    def _eval_when(self, ast: WhenAST) -> bool:
        return evaluate_when(ast, self.state, time_of_day=self._time_of_day)

    def _spawn_intent_from_rule(self, rule: Rule, now: int) -> Intent:
        # Scene rules: target is empty, intent carries scene reference.
        # The integration layer discovers these via list_active_scene_intents()
        # and fires scene.turn_on instead of resolving a value.
        return Intent(
            target=rule.target or "",  # "" for scene rules, entity_id otherwise
            set=dict(rule.set),
            cap=dict(rule.cap),
            floor=dict(rule.floor),
            offset=dict(rule.offset),
            multiply=dict(rule.multiply),
            transition_ms=rule.transition_ms,
            easing=rule.easing,
            authority=rule.authority,
            confidence=rule.confidence,
            ttl_ms=rule.ttl_ms,
            reason=rule.reason,
            rule_id=rule.id,
            created_at_ms=now,
            animation=rule.animation,
        )

    # ── Resolution ─────────────────────────────────────────────────

    def list_active_intents(self, target: str) -> list[Intent]:
        """Return a copy of the active intents for a target.

        Excludes scene-rule intents (those have no target).
        """
        now = self.now_ms()
        return [
            i for i in self._active_intents
            if i.target == target and not i.is_expired(into_the_future_ms=now)
        ]

    def list_active_scene_intents(
        self, return_intents: bool = False
    ):
        """Return the scene IDs of currently-active scene rules.

        By default returns a list of scene entity_id strings (the
        convenient form for the integration layer). If
        `return_intents=True`, returns a list of (Intent, scene_id) tuples
        for diagnostics.

        Scene rules have no `target`; they're identified by the rule's
        `scene` attribute. The intent is created with `target=""` to
        distinguish it from target rules in `_active_intents`.
        """
        now = self.now_ms()
        active_scene_intents = [
            i for i in self._active_intents
            if not i.target  # scene rules have empty target
            and not i.is_expired(into_the_future_ms=now)
        ]
        if return_intents:
            return [
                (i, self._rules[i.rule_id].rule.scene)
                for i in active_scene_intents
                if i.rule_id in self._rules
            ]
        return [
            self._rules[i.rule_id].rule.scene
            for i in active_scene_intents
            if i.rule_id in self._rules
        ]

    def resolve(self, target: str) -> ResolvedIntent | None:
        """Resolve the active intents for a target into a final value."""
        intents = self.list_active_intents(target)
        if not intents:
            return None
        return resolve_intents(target, intents, into_the_future_ms=self.now_ms())

    # ── Animation ticking ─────────────────────────────────────────

    def tick(self, t_offset_ms: int) -> None:
        """Advance animation timing by t_offset_ms from the animation's start.

        This is a thin wrapper for the integration layer's tick loop.
        Currently the engine doesn't need to do anything per-tick besides
        expose the current animation frame via resolve(); the integration
        layer reads the ResolvedIntent and applies it.
        """
        # Future: maintain a list of "active animation frames" and
        # expire them when finished. For now, this method exists for
        # API symmetry and testability.
        pass

    # ── Diagnostics ────────────────────────────────────────────────

    def explain(self, target: str) -> str:
        """Return a human-readable explanation of a target's resolved value."""
        intents = self.list_active_intents(target)
        if not intents:
            return f"{target}: no active intents"
        resolved = self.resolve(target)
        if resolved is None:
            return f"{target}: no resolved value"
        lines = [f"{target}: {resolved.value}"]
        lines.append("  Active intents:")
        for i in intents:
            ttl_info = ""
            if i.ttl_ms is not None:
                ttl_info = f" (ttl: {i.ttl_ms}ms)"
            lines.append(f"    - {i.rule_id or '<manual>'}: {i.authority.value}{ttl_info} — {i.reason}")
        return "\n".join(lines)

    @property
    def log(self) -> list[str]:
        """Last few log lines for diagnostics."""
        return list(self._log)

    def explain_scenes(self) -> str:
        """Return a human-readable explanation of currently-active scene rules.

        Used by the integration layer's diagnostics surface. Mirrors the
        shape of `explain()` but for the scene-rule path.
        """
        scenes = self.list_active_scene_intents(return_intents=True)
        if not scenes:
            return "no active scene rules"
        lines = [f"{len(scenes)} active scene(s):"]
        for intent, scene_id in scenes:
            ttl_info = ""
            if intent.ttl_ms is not None:
                ttl_info = f" (ttl: {intent.ttl_ms}ms)"
            lines.append(
                f"  - {scene_id}: rule={intent.rule_id!r} "
                f"authority={intent.authority.value}{ttl_info} — {intent.reason}"
            )
        return "\n".join(lines)
