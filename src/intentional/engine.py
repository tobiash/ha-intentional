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
- activate_scene_rule(rule_id, ...): force a scene rule intent
- evaluate_all(): re-evaluate `when` clauses, emit/drop intents
- resolve(target): get the ResolvedIntent for a target
- list_active_intents(target): get the raw Intent list
- tick(t_ms): advance animation timing
- explain(target): human-readable explanation of why a target has its value
- explain_target(target): JSON-friendly explanation for APIs and tooling
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
from intentional.when_parser import TimeOfDay, WhenAST, evaluate_when, parse_when
from intentional.yaml_loader import Rule

StateChangeCallback = Callable[[str, Any], None]
_FOR_UNIT_MULTIPLIERS = {
    "ms": 1,
    "s": 1_000,
    "m": 60_000,
    "h": 3_600_000,
}


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
        self._time_of_day: str | TimeOfDay | None = None
        self._clock_fn = clock_fn or (lambda: int(time.time() * 1000))
        self._clock_offset_ms: int = 0  # for tests: advance_clock adds to this
        self._animation_started_at: dict[str, int] = {}  # rule_id → ms
        self._condition_true_since: dict[str, int] = {}  # rule_id → ms
        self._state_change_callbacks: list[StateChangeCallback] = []
        self._log: list[str] = []  # last N log lines for diagnostics

    # ── Time ────────────────────────────────────────────────────────

    def now_ms(self) -> int:
        """Return the current engine time in milliseconds."""
        return self._clock_fn() + self._clock_offset_ms

    def advance_clock(self, delta_ms: int) -> None:
        """Advance the engine's clock by delta_ms. For tests only."""
        self._clock_offset_ms += delta_ms

    def set_time_of_day(self, bucket: str, *, clock: str | None = None) -> None:
        """Set the current time helper for `time_of_day` references."""
        self._time_of_day = TimeOfDay(bucket=bucket, clock=clock)

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
        self._condition_true_since = {
            rule_id: since
            for rule_id, since in self._condition_true_since.items()
            if rule_id in self._rules
        }

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

    def clear_user_intents(self, target: str | None = None) -> int:
        """Remove active manual/user intents, optionally for one target."""
        before = len(self._active_intents)
        self._active_intents = [
            intent for intent in self._active_intents
            if not (
                intent.authority is Authority.USER
                and not intent.rule_id
                and (target is None or intent.target == target)
            )
        ]
        return before - len(self._active_intents)

    def activate_scene_rule(
        self,
        rule_id: str,
        *,
        ttl_ms: int | None = None,
        reason: str = "Manual activate_scene service",
    ) -> Intent | None:
        """Force a scene rule to fire, ignoring its when condition until TTL expiry."""
        parsed = self._rules.get(rule_id)
        if parsed is None or parsed.rule.scene is None:
            return None
        intent = Intent(
            target="",
            ttl_ms=parsed.rule.ttl_ms if ttl_ms is None else ttl_ms,
            authority=Authority.USER,
            rule_id=rule_id,
            ignore_when=True,
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
        firing, _condition_firing, _blocked_by, _for_remaining = (
            self._firing_rule_diagnostics(update_timers=True)
        )

        # Filter active intents:
        # - Drop rule-bound intents whose rule is no longer firing
        #   (TTL doesn't keep an intent alive after its trigger stops —
        #   TTL is "for how long this intent may be active", not
        #   "how long to keep it around after the rule withdraws")
        # - Keep user/manual intents (no rule_id) until their TTL expires
        new_active: list[Intent] = []
        for intent in self._active_intents:
            if intent.ignore_when:
                if not intent.is_expired(into_the_future_ms=now):
                    new_active.append(intent)
                continue
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

    def _firing_rule_diagnostics(
        self,
        *,
        update_timers: bool = False,
    ) -> tuple[dict[str, str], set[str], dict[str, list[str]], dict[str, int]]:
        """Return effective firing rules plus raw condition and blocking details."""
        now = self.now_ms()
        firing: dict[str, str] = {}
        for_remaining: dict[str, int] = {}
        for rule_id, parsed in self._rules.items():
            if self._eval_when(parsed.when_ast):
                since = self._condition_true_since.get(rule_id)
                if since is None:
                    since = now
                    if update_timers:
                        self._condition_true_since[rule_id] = since
                elapsed_ms = now - since
                required_for_ms = self._rule_for_ms(parsed.rule)
                remaining_ms = max(0, required_for_ms - elapsed_ms)
                if remaining_ms:
                    for_remaining[rule_id] = remaining_ms
                else:
                    firing[rule_id] = parsed.rule.target
            elif update_timers:
                self._condition_true_since.pop(rule_id, None)
        condition_firing = set(firing)
        condition_firing.update(for_remaining)

        blocked_by: dict[str, list[str]] = {}
        for rule_id in firing:
            for blocked_rule_id in self._rules[rule_id].rule.blocks:
                blocked_by.setdefault(blocked_rule_id, []).append(rule_id)
        for rule_id in blocked_by:
            firing.pop(rule_id, None)

        return firing, condition_firing, blocked_by, for_remaining

    def _rule_for_ms(self, rule: Rule) -> int:
        if rule.for_entity is None:
            return rule.for_ms
        raw_value = self.state.get(f"{rule.for_entity}.state")
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            return rule.for_ms
        multiplier = _FOR_UNIT_MULTIPLIERS.get(rule.for_entity_unit, 1_000)
        return max(0, int(value * multiplier))

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

    def rule_count(self) -> int:
        """Return the number of loaded, parsed rules."""
        return len(self._rules)

    def list_known_targets(self) -> tuple[str, ...]:
        """Return sorted target entity IDs referenced by loaded target rules."""
        return tuple(sorted({
            parsed.rule.target
            for parsed in self._rules.values()
            if parsed.rule.target
        }))

    def active_intent_count(self) -> int:
        """Return the number of currently active, non-expired intents."""
        now = self.now_ms()
        return sum(
            1
            for intent in self._active_intents
            if not intent.is_expired(into_the_future_ms=now)
        )

    def list_active_intents(self, target: str) -> list[Intent]:
        """Return a copy of the active intents for a target.

        Excludes scene-rule intents (those have no target).
        """
        now = self.now_ms()
        return [
            i for i in self._active_intents
            if i.target == target and not i.is_expired(into_the_future_ms=now)
        ]

    def list_active_targets(self) -> tuple[str, ...]:
        """Return sorted target entity IDs with at least one active intent."""
        now = self.now_ms()
        return tuple(sorted({
            intent.target
            for intent in self._active_intents
            if intent.target and not intent.is_expired(into_the_future_ms=now)
        }))

    def has_active_target(self, target: str) -> bool:
        """Return whether a target has at least one active, non-expired intent."""
        now = self.now_ms()
        return any(
            intent.target == target and not intent.is_expired(into_the_future_ms=now)
            for intent in self._active_intents
        )

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
        resolved = resolve_intents(target, intents, into_the_future_ms=self.now_ms())
        if resolved is None:
            return None
        return self._with_animation_frame(resolved)

    def _with_animation_frame(self, resolved: ResolvedIntent) -> ResolvedIntent:
        """Overlay the winning intent's current animation frame, if any."""
        intent = resolved.winning_intent
        if intent is None or intent.animation is None:
            return resolved

        started_at = self._animation_started_at.get(intent.rule_id)
        if started_at is None:
            started_at = intent.created_at_ms
        frame = intent.animation.evaluate(max(0, self.now_ms() - started_at))
        value = dict(resolved.value)
        value[intent.animation.parameter] = frame.value
        return ResolvedIntent(
            target=resolved.target,
            value=value,
            winning_intent=resolved.winning_intent,
            transition_ms=resolved.transition_ms,
            easing=resolved.easing,
            animation=resolved.animation,
            ttl_remaining_ms=resolved.ttl_remaining_ms,
            all_active_intents=resolved.all_active_intents,
        )

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

    def explain_target(self, target: str) -> dict[str, Any]:
        """Return a JSON-friendly explanation of a target's resolved state."""
        resolved_obj = self.resolve(target)
        resolved = None
        winning_intent = None
        if resolved_obj is not None:
            resolved = {
                "value": dict(resolved_obj.value),
                "ttl_remaining_ms": resolved_obj.ttl_remaining_ms,
            }
            winning_intent = _intent_to_diagnostic_dict(resolved_obj.winning_intent)

        active = sorted(
            self.list_active_intents(target),
            key=lambda intent: intent.priority,
            reverse=True,
        )
        firing, condition_firing, blocked_by, for_remaining = self._firing_rule_diagnostics()

        rules_for_target = []
        for rule_id, parsed in self._rules.items():
            if parsed.rule.target != target:
                continue
            rules_for_target.append({
                "rule_id": rule_id,
                "firing": rule_id in firing,
                "condition_firing": rule_id in condition_firing,
                "blocked_by": sorted(blocked_by.get(rule_id, [])),
                "for_remaining_ms": for_remaining.get(rule_id),
            })

        return {
            "target": target,
            "resolved": resolved,
            "active_intents": [_intent_to_diagnostic_dict(intent) for intent in active],
            "winning_intent": winning_intent,
            "rules_for_target": rules_for_target,
        }

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

    @property
    def log(self) -> list[str]:
        """Last few log lines for diagnostics."""
        return list(self._log)


def _intent_to_diagnostic_dict(intent: Intent | None) -> dict[str, Any] | None:
    """Serialize an intent for pure-engine diagnostics."""
    if intent is None:
        return None
    return {
        "rule_id": intent.rule_id,
        "target": intent.target,
        "set": dict(intent.set) if intent.set else {},
        "cap": dict(intent.cap) if intent.cap else {},
        "floor": dict(intent.floor) if intent.floor else {},
        "offset": dict(intent.offset) if intent.offset else {},
        "multiply": dict(intent.multiply) if intent.multiply else {},
        "authority": intent.authority.value,
        "authority_name": intent.authority.name,
        "confidence": intent.confidence,
        "ttl_ms": intent.ttl_ms,
        "reason": intent.reason,
        "created_at_ms": intent.created_at_ms,
        "ignore_when": intent.ignore_when,
    }
