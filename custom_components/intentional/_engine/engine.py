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
from dataclasses import dataclass, replace
from typing import Any

from .compositor import ResolvedIntent, resolve_intents
from .generation import GeneratedFieldState, sample_generated_field
from .intent import Authority, Intent
from .lifecycle import export_lifecycle_records, restore_lifecycle_intents
from .records import Effect, IntentSelector
from .rule_lifecycle import dominant_phase, min_optional, rule_phase
from .selectors import observe_selectors_fire, selector_diagnostics
from .templates import TemplateRenderer
from .when_parser import TimeOfDay, WhenAST, evaluate_when, parse_when
from .yaml_loader import Rule

StateChangeCallback = Callable[[str, Any], None]
SelectorResolver = Callable[[IntentSelector], list[str]]
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
    hold_when_ast: WhenAST | None = None
    hold_until_ast: WhenAST | None = None


class Engine:
    """The intent engine.

    Holds rules, state, and the set of currently-active intents. Drives
    animation timing via tick(). Exposes resolve() to get the final value
    for any target.
    """

    def __init__(
        self,
        *,
        clock_fn: Callable[[], int] | None = None,
        selector_resolver: SelectorResolver | None = None,
    ) -> None:
        self._rules: dict[str, _ParsedRule] = {}
        self.state: dict[str, Any] = {}
        self._active_intents: list[Intent] = []
        self._time_of_day: str | TimeOfDay | None = None
        self._clock_fn = clock_fn or (lambda: int(time.time() * 1000))
        self._selector_resolver = selector_resolver or (lambda _selector: [])
        self._clock_offset_ms: int = 0  # for tests: advance_clock adds to this
        self._animation_started_at: dict[str, int] = {}  # rule_id → ms
        self._condition_true_since: dict[str, int] = {}  # rule_id → ms
        self._hold_until_true_since: dict[str, int] = {}  # rule_id → ms
        self._rule_held_since: dict[str, int] = {}  # rule_id → ms
        self._state_change_callbacks: list[StateChangeCallback] = []
        self._log: list[str] = []  # last N log lines for diagnostics
        self._active_effect_rule_ids: set[str] = set()
        self._pending_effects: list[tuple[str, Effect]] = []
        self._template_renderer = TemplateRenderer()
        self._generated_fields: dict[tuple[str, str], GeneratedFieldState] = {}
        self._enabled = True
        self._paused_labels: set[str] = set()
        self._paused_rule_ids: set[str] = set()

    # ── Lifecycle Persistence ───────────────────────────────────────

    def export_lifecycle_records(self) -> dict[str, Any]:
        """Return persistent lifecycle state for restart/reload recovery."""
        records = export_lifecycle_records(
            self._active_intents,
            self._active_effect_rule_ids,
            self._generated_fields,
            now_ms=self.now_ms(),
        )
        records["enabled"] = self._enabled
        records["paused_labels"] = sorted(self._paused_labels)
        records["paused_rule_ids"] = sorted(self._paused_rule_ids)
        return records

    def import_lifecycle_records(self, records: dict[str, Any] | None) -> None:
        """Restore persisted lifecycle records produced by export_lifecycle_records()."""
        restored, active_effect_rule_ids, generated_fields = restore_lifecycle_intents(
            records,
            now_ms=self.now_ms(),
            known_rule_ids=set(self._rules),
        )
        self._active_intents.extend(restored)
        self._active_effect_rule_ids = active_effect_rule_ids
        self._generated_fields.update(generated_fields)
        if isinstance(records, dict) and records.get("enabled") is False:
            self.set_enabled(False)
        if isinstance(records, dict):
            paused_labels = records.get("paused_labels")
            if isinstance(paused_labels, list):
                self._paused_labels = {
                    label for label in paused_labels
                    if isinstance(label, str) and label
                }
            paused_rule_ids = records.get("paused_rule_ids")
            if isinstance(paused_rule_ids, list):
                self._paused_rule_ids = {
                    rule_id for rule_id in paused_rule_ids
                    if isinstance(rule_id, str) and rule_id
                }

    def is_enabled(self) -> bool:
        """Return whether automation rule evaluation is globally enabled."""
        return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        """Globally enable or disable Intentional automation."""
        self._enabled = enabled
        if not enabled:
            self._active_intents = []
            self._active_effect_rule_ids.clear()

    def set_label_paused(self, label: str, paused: bool) -> None:
        """Pause or resume all rules carrying a locality label."""
        if paused:
            self._paused_labels.add(label)
            self._active_intents = [
                intent for intent in self._active_intents
                if not self._intent_has_label(intent, label)
            ]
            self._active_effect_rule_ids = {
                rule_id for rule_id in self._active_effect_rule_ids
                if not self._rule_has_label(rule_id, label)
            }
            return
        self._paused_labels.discard(label)

    def set_rule_paused(self, rule_id: str, paused: bool) -> None:
        """Pause or resume one authored or expanded rule id."""
        rule_ids = {
            current_id for current_id in self._rules
            if current_id == rule_id or current_id.split(":", 1)[0] == rule_id
        }
        if not rule_ids:
            rule_ids = {rule_id}
        if paused:
            self._paused_rule_ids.update(rule_ids)
            self._active_intents = [
                intent for intent in self._active_intents
                if intent.rule_id not in rule_ids
            ]
            self._active_effect_rule_ids.difference_update(rule_ids)
            return
        self._paused_rule_ids.difference_update(rule_ids)

    def set_rules_paused(self, rule_ids: set[str], paused: bool) -> None:
        """Pause or resume multiple authored or expanded rule ids."""
        for rule_id in rule_ids:
            self.set_rule_paused(rule_id, paused)

    def is_rule_paused(self, rule_id: str) -> bool:
        """Return whether an authored or expanded rule id is paused."""
        return any(
            current_id == rule_id or current_id.split(":", 1)[0] == rule_id
            for current_id in self._paused_rule_ids
        )

    def list_paused_rule_ids(self) -> tuple[str, ...]:
        """Return paused rule ids."""
        return tuple(sorted(self._paused_rule_ids))

    def is_label_paused(self, label: str) -> bool:
        """Return whether rules with the given label are paused."""
        return label in self._paused_labels

    def list_paused_labels(self) -> tuple[str, ...]:
        """Return paused rule labels."""
        return tuple(sorted(self._paused_labels))

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
                hold_when_ast = parse_when(rule.hold_when) if rule.hold_when else None
                hold_until_ast = parse_when(rule.hold_until_when) if rule.hold_until_when else None
            except Exception as e:
                self._log.append(f"Failed to parse when for {rule.id!r}: {e}")
                continue
            self._rules[rule.id] = _ParsedRule(
                rule=rule,
                when_ast=when_ast,
                hold_when_ast=hold_when_ast,
                hold_until_ast=hold_until_ast,
            )
        # Drop level-rule intents on reload so active observations recreate
        # intents from the current rule definition (target/value/lifecycle).
        self._active_intents = [
            i for i in self._active_intents
            if not i.rule_id or (i.rule_id in self._rules and i.ignore_when)
        ]
        self._condition_true_since = {
            rule_id: since
            for rule_id, since in self._condition_true_since.items()
            if rule_id in self._rules
        }
        self._hold_until_true_since = {
            rule_id: since
            for rule_id, since in self._hold_until_true_since.items()
            if rule_id in self._rules
        }
        self._rule_held_since = {
            rule_id: since
            for rule_id, since in self._rule_held_since.items()
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
        self.clear_user_intents(target=target)
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
        if not self._enabled:
            self._active_intents = []
            self._active_effect_rule_ids.clear()
            self._pending_effects.clear()
            return
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
            if self._intent_is_paused(intent):
                continue
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
                self._hold_until_true_since.pop(intent.rule_id, None)
                self._rule_held_since.pop(intent.rule_id, None)
                continue
            if intent.rule_id in self._rules:
                parsed = self._rules[intent.rule_id]
                rule = parsed.rule
                if parsed.hold_until_ast is not None and not self._hold_until_released(parsed, now):
                    self._rule_held_since.setdefault(intent.rule_id, now)
                    new_active.append(intent)
                    continue
                if parsed.hold_when_ast is not None and self._eval_when(parsed.hold_when_ast):
                    self._hold_until_true_since.pop(intent.rule_id, None)
                    self._rule_held_since.setdefault(intent.rule_id, now)
                    new_active.append(intent)
                    continue
                if rule.linger_ms and not intent.ignore_when:
                    new_active.append(_linger_intent(intent, rule.linger_ms, now))
                    continue
            # Rule no longer fires — drop, regardless of TTL
            continue

        # Add new intents for rules that just started firing
        new_active = [self._refresh_generated_intent(intent, now) for intent in new_active]
        existing_rule_ids = {i.rule_id for i in new_active if i.rule_id}
        for rule_id, _target in firing.items():
            parsed = self._rules[rule_id]
            if parsed.rule.effects and rule_id not in self._active_effect_rule_ids:
                self._pending_effects.extend(
                    (rule_id, self._template_renderer.render_effect(effect, self.state))
                    for effect in parsed.rule.effects
                )
            if rule_id not in existing_rule_ids:
                if not parsed.rule.target and parsed.rule.scene is None:
                    if parsed.rule.intent_selectors:
                        new_active.extend(self._spawn_intents_from_selectors(parsed.rule, now))
                    continue
                intent = self._spawn_intent_from_rule(parsed.rule, now)
                new_active.append(intent)
                self._animation_started_at[rule_id] = now

        self._active_intents = new_active
        self._active_effect_rule_ids = {
            rule_id for rule_id in firing
            if self._rules[rule_id].rule.effects
        }

    def drain_pending_effects(self) -> list[tuple[str, Effect]]:
        """Return and clear effects that became active since the last drain."""
        effects = list(self._pending_effects)
        self._pending_effects.clear()
        return effects

    def _eval_when(self, ast: WhenAST) -> bool:
        return evaluate_when(ast, self.state, time_of_day=self._time_of_day)

    def _rule_has_label(self, rule_id: str, label: str) -> bool:
        parsed = self._rules.get(rule_id)
        return parsed is not None and label in parsed.rule.labels

    def _intent_has_label(self, intent: Intent, label: str) -> bool:
        return bool(intent.rule_id and self._rule_has_label(intent.rule_id, label))

    def _intent_is_paused(self, intent: Intent) -> bool:
        if not intent.rule_id:
            return False
        return self._rule_is_paused(intent.rule_id)

    def _rule_is_paused(self, rule_id: str) -> bool:
        parsed = self._rules.get(rule_id)
        if rule_id in self._paused_rule_ids:
            return True
        authored_id = rule_id.split(":", 1)[0]
        if authored_id in self._paused_rule_ids:
            return True
        if parsed is None:
            return False
        return any(label in self._paused_labels for label in parsed.rule.labels)

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
            if self._rule_is_paused(rule_id):
                if update_timers:
                    self._condition_true_since.pop(rule_id, None)
                continue
            if self._eval_when(parsed.when_ast) and observe_selectors_fire(
                parsed.rule,
                self.state,
                self._selector_resolver,
            ):
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

    def _hold_until_released(self, parsed: _ParsedRule, now: int) -> bool:
        if parsed.hold_until_ast is None:
            return True
        rule_id = parsed.rule.id
        if not self._eval_when(parsed.hold_until_ast):
            self._hold_until_true_since.pop(rule_id, None)
            return False
        since = self._hold_until_true_since.get(rule_id)
        if since is None:
            self._hold_until_true_since[rule_id] = now
            since = now
        return now - since >= parsed.rule.hold_until_for_ms

    def _spawn_intent_from_rule(self, rule: Rule, now: int) -> Intent:
        # Scene rules: target is empty, intent carries scene reference.
        # The integration layer discovers these via list_active_scene_intents()
        # and fires scene.turn_on instead of resolving a value.
        intent = Intent(
            target=rule.target or "",  # "" for scene rules, entity_id otherwise
            set=self._template_renderer.render_value(rule.set, self.state),
            cap=self._template_renderer.render_value(rule.cap, self.state),
            floor=self._template_renderer.render_value(rule.floor, self.state),
            offset=self._template_renderer.render_value(rule.offset, self.state),
            multiply=self._template_renderer.render_value(rule.multiply, self.state),
            transition_ms=rule.transition_ms,
            transition_assert_ms=rule.transition_assert_ms,
            transition_change_ms=rule.transition_change_ms,
            transition_withdraw_ms=rule.transition_withdraw_ms,
            easing=rule.easing,
            authority=rule.authority,
            confidence=rule.confidence,
            ttl_ms=rule.ttl_ms,
            reason=rule.reason,
            rule_id=rule.id,
            ignore_when=rule.edge_created,
            created_at_ms=now,
            animation=rule.animation,
            generators=rule.generators,
        )

        if rule.generators:
            intent = replace(
                intent,
                set=self._sample_generated_fields(rule, intent.set, now),
                transition_ms=self._generated_transition_ms(rule) or intent.transition_ms,
            )
        return intent

    def _refresh_generated_intent(self, intent: Intent, now: int) -> Intent:
        if not intent.generators or not intent.rule_id:
            return intent
        if intent.rule_id not in self._rules:
            return intent
        changed = False
        set_values = dict(intent.set)
        transition_ms = intent.transition_ms
        for field_name, spec in intent.generators.items():
            key = (intent.rule_id, field_name)
            state = self._generated_fields.get(key)
            if state is None or now >= state.next_due_ms:
                state = sample_generated_field(
                    spec,
                    now_ms=now,
                    seed=f"{intent.rule_id}:{field_name}:{now}",
                    previous_value=set_values.get(field_name),
                )
                self._generated_fields[key] = state
                set_values[field_name] = state.value
                if state.transition_ms is not None:
                    transition_ms = state.transition_ms
                changed = True
        if not changed:
            return intent
        return replace(intent, set=set_values, transition_ms=transition_ms)

    def _sample_generated_fields(self, rule: Rule, set_values: dict[str, Any], now: int) -> dict[str, Any]:
        sampled = dict(set_values)
        for field_name, spec in rule.generators.items():
            key = (rule.id, field_name)
            state = self._generated_fields.get(key)
            if state is None or now >= state.next_due_ms:
                state = sample_generated_field(
                    spec,
                    now_ms=now,
                    seed=f"{rule.id}:{field_name}:{now}",
                )
            self._generated_fields[(rule.id, field_name)] = state
            sampled[field_name] = state.value
        return sampled

    def _generated_transition_ms(self, rule: Rule) -> int | None:
        transitions = [
            state.transition_ms
            for (rule_id, _field_name), state in self._generated_fields.items()
            if rule_id == rule.id and state.transition_ms is not None
        ]
        return max(transitions) if transitions else None

    def _spawn_intents_from_selectors(self, rule: Rule, now: int) -> list[Intent]:
        intents: list[Intent] = []
        for selector in rule.intent_selectors:
            for target in self._selector_resolver(selector):
                if target in selector.exclude:
                    continue
                intents.append(Intent(
                    target=target,
                    set=self._template_renderer.render_value(selector.set, self.state),
                    cap=self._template_renderer.render_value(selector.cap, self.state),
                    floor=self._template_renderer.render_value(selector.floor, self.state),
                    offset=self._template_renderer.render_value(selector.offset, self.state),
                    multiply=self._template_renderer.render_value(selector.multiply, self.state),
                    transition_ms=selector.transition_ms,
                    easing=selector.easing,
                    authority=rule.authority,
                    confidence=rule.confidence,
                    ttl_ms=selector.ttl_ms,
                    reason=rule.reason,
                    rule_id=rule.id,
                    ignore_when=rule.edge_created,
                    created_at_ms=now,
                ))
        return intents

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

    def list_active_user_intents(self, target: str | None = None) -> list[Intent]:
        """Return active manual/user intents, optionally for one target."""
        now = self.now_ms()
        return [
            intent for intent in self._active_intents
            if intent.authority is Authority.USER
            and not intent.rule_id
            and (target is None or intent.target == target)
            and not intent.is_expired(into_the_future_ms=now)
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
            and i.rule_id in self._rules
            and self._rules[i.rule_id].rule.scene is not None
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
        statuses = self.list_rule_statuses()

        rules_for_target = []
        for rule_id, parsed in self._rules.items():
            if parsed.rule.target != target:
                continue
            status = statuses.get(rule_id, {})
            rules_for_target.append({
                "rule_id": rule_id,
                "firing": rule_id in firing,
                "condition_firing": rule_id in condition_firing,
                "blocked_by": sorted(blocked_by.get(rule_id, [])),
                "for_remaining_ms": for_remaining.get(rule_id),
                "phase": status.get("phase", "idle"),
                "active_for_ms": status.get("active_for_ms"),
                "condition_active_for_ms": status.get("condition_active_for_ms"),
                "held_for_ms": status.get("held_for_ms"),
                "group": status.get("group", ""),
                "profile": status.get("profile", ""),
            })

        return {
            "target": target,
            "resolved": resolved,
            "active_intents": [_intent_to_diagnostic_dict(intent) for intent in active],
            "winning_intent": winning_intent,
            "rules_for_target": rules_for_target,
        }

    def world_model(self) -> dict[str, Any]:
        """Return a compact desired/spec-status model for agents and APIs."""
        desired_records = []
        for target in self.list_active_targets():
            resolved = self.resolve(target)
            if resolved is None:
                continue
            winning = resolved.winning_intent
            desired_records.append({
                "target": target,
                "desired": dict(resolved.value),
                "rule_id": winning.rule_id if winning is not None else "",
                "reason": winning.reason if winning is not None else "",
                "conditions": [{"type": "DesiredResolved", "status": "true"}],
            })
        return {
            "dsl_version": "vnext-draft",
            "rule_count": self.rule_count(),
            "active_intent_count": self.active_intent_count(),
            "authored_rules": list(self.list_authored_rule_statuses().values()),
            "active_rules": [
                status for status in self.list_authored_rule_statuses().values()
                if status["active"] or status["active_intent_count"]
            ],
            "desired_records": desired_records,
            "selector_diagnostics": selector_diagnostics(
                {rule_id: parsed.rule for rule_id, parsed in self._rules.items()},
                self.state,
                self._selector_resolver,
            ),
            "lifecycle": self.export_lifecycle_records(),
            "paused_labels": self.list_paused_labels(),
            "paused_rule_ids": self.list_paused_rule_ids(),
            "errors": self.log,
        }

    def list_rule_statuses(self) -> dict[str, dict[str, Any]]:
        """Return authored-rule status for HA entities and agent UIs."""
        firing, condition_firing, blocked_by, for_remaining = self._firing_rule_diagnostics()
        active_counts: dict[str, int] = {}
        active_since: dict[str, int] = {}
        has_lingering_intent: set[str] = set()
        now = self.now_ms()
        for intent in self._active_intents:
            if intent.rule_id and not intent.is_expired(into_the_future_ms=now):
                active_counts[intent.rule_id] = active_counts.get(intent.rule_id, 0) + 1
                active_since[intent.rule_id] = min(
                    active_since.get(intent.rule_id, intent.created_at_ms),
                    intent.created_at_ms,
                )
                if intent.ignore_when:
                    has_lingering_intent.add(intent.rule_id)

        statuses: dict[str, dict[str, Any]] = {}
        for rule_id, parsed in self._rules.items():
            statuses[rule_id] = self._rule_status(
                rule_id,
                parsed.rule,
                firing,
                condition_firing,
                blocked_by,
                for_remaining,
                active_counts,
                active_since,
                has_lingering_intent,
            )
        return statuses

    def list_authored_rule_statuses(self) -> dict[str, dict[str, Any]]:
        """Return statuses grouped by authored rule id before target expansion."""
        grouped: dict[str, dict[str, Any]] = {}
        for rule_id, status in self.list_rule_statuses().items():
            authored_id = rule_id.split(":", 1)[0]
            current = grouped.get(authored_id)
            if current is None:
                grouped[authored_id] = {**status, "rule_id": authored_id}
                continue
            current["paused"] = current.get("paused", False) or status.get("paused", False)
            current["active"] = current["active"] or status["active"]
            current["phase"] = dominant_phase(str(current.get("phase", "idle")), str(status.get("phase", "idle")))
            current["condition_firing"] = current["condition_firing"] or status["condition_firing"]
            current["active_intent_count"] += status["active_intent_count"]
            current["active_for_ms"] = min_optional(current.get("active_for_ms"), status.get("active_for_ms"))
            current["condition_active_for_ms"] = min_optional(
                current.get("condition_active_for_ms"),
                status.get("condition_active_for_ms"),
            )
            current["held_for_ms"] = min_optional(current.get("held_for_ms"), status.get("held_for_ms"))
            current["targets"] = sorted(set(current["targets"]) | set(status["targets"]))
            current["blocked_by"] = sorted(set(current["blocked_by"]) | set(status["blocked_by"]))
            current["group"] = current.get("group") or status.get("group", "")
            current["profile"] = current.get("profile") or status.get("profile", "")
            remaining = [
                value for value in (current.get("for_remaining_ms"), status.get("for_remaining_ms"))
                if value is not None
            ]
            current["for_remaining_ms"] = min(remaining) if remaining else None
        return grouped

    def _rule_status(
        self,
        rule_id: str,
        rule: Rule,
        firing: set[str],
        condition_firing: set[str],
        blocked_by: dict[str, set[str]],
        for_remaining: dict[str, int],
        active_counts: dict[str, int],
        active_since: dict[str, int],
        has_lingering_intent: set[str],
    ) -> dict[str, Any]:
        targets = []
        if rule.target:
            targets.append(rule.target)
        if rule.scene:
            targets.append(rule.scene)
        targets.extend(
            _selector_summary(selector)
            for selector in rule.intent_selectors
        )
        desired: dict[str, Any] = {}
        if rule.set:
            desired["set"] = dict(rule.set)
        if rule.cap:
            desired["cap"] = dict(rule.cap)
        if rule.floor:
            desired["floor"] = dict(rule.floor)
        if rule.offset:
            desired["offset"] = dict(rule.offset)
        if rule.multiply:
            desired["multiply"] = dict(rule.multiply)
        if rule.effects:
            desired["effects"] = [
                {
                    "domain": effect.domain,
                    "service": effect.service,
                    "target": dict(effect.target),
                    "data": dict(effect.data),
                }
                for effect in rule.effects
            ]

        now = self.now_ms()
        phase = rule_phase(
            rule_id,
            firing=firing,
            for_remaining=for_remaining,
            active_counts=active_counts,
            lingering_rules=has_lingering_intent,
        )
        active_for_ms = None
        if rule_id in active_since:
            active_for_ms = max(0, now - active_since[rule_id])
        condition_active_for_ms = None
        if rule_id in condition_firing and rule_id in self._condition_true_since:
            condition_active_for_ms = max(0, now - self._condition_true_since[rule_id])
        held_for_ms = None
        if phase == "held" and rule_id in self._rule_held_since:
            held_for_ms = max(0, now - self._rule_held_since[rule_id])

        return {
            "rule_id": rule_id,
            "enabled": rule.enabled,
            "paused": self._rule_is_paused(rule_id),
            "active": rule_id in firing or active_counts.get(rule_id, 0) > 0,
            "phase": phase,
            "condition_firing": rule_id in condition_firing,
            "active_intent_count": active_counts.get(rule_id, 0),
            "active_for_ms": active_for_ms,
            "condition_active_for_ms": condition_active_for_ms,
            "held_for_ms": held_for_ms,
            "targets": targets,
            "desired": desired,
            "authority": rule.authority.value,
            "confidence": rule.confidence,
            "reason": rule.reason,
            "labels": list(rule.labels),
            "group": rule.group,
            "profile": rule.profile,
            "notes": rule.notes,
            "blocked_by": sorted(blocked_by.get(rule_id, [])),
            "for_remaining_ms": for_remaining.get(rule_id),
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


def _selector_summary(selector: Any) -> str:
    parts = []
    for field in ("domain", "area", "label"):
        value = getattr(selector, field, None)
        if value:
            parts.append(f"{field}={value}")
    return "select:" + (",".join(parts) if parts else "*")


def _linger_intent(intent: Intent, linger_ms: int, now: int) -> Intent:
    """Return a copy of an intent that survives after its level observation stops."""
    return Intent(
        target=intent.target,
        set=dict(intent.set),
        merge=intent.merge,
        cap=dict(intent.cap),
        floor=dict(intent.floor),
        offset=dict(intent.offset),
        multiply=dict(intent.multiply),
        transition_ms=intent.transition_ms,
        transition_assert_ms=intent.transition_assert_ms,
        transition_change_ms=intent.transition_change_ms,
        transition_withdraw_ms=intent.transition_withdraw_ms,
        easing=intent.easing,
        authority=intent.authority,
        confidence=intent.confidence,
        ttl_ms=linger_ms,
        reason=intent.reason,
        rule_id=intent.rule_id,
        ignore_when=True,
        created_at_ms=now,
        animation=intent.animation,
        generators=intent.generators,
    )
