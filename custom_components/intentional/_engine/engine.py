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

import hashlib
import json
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, fields, is_dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any

from .alerting import AlertObservation
from .compositor import ResolvedIntent, resolve_intents
from .generation import GeneratedFieldState, sample_generated_field
from .intent import Authority, Intent
from .lifecycle import (
    export_lifecycle_records,
    lifecycle_version,
    restore_effect_outbox,
    restore_lifecycle_intents,
)
from .records import EffectOutboxRecord, FrozenHoldAfter, IntentSelector
from .rule_lifecycle import dominant_phase, min_optional, rule_phase
from .selectors import (
    observation_groups_fire,
    observe_selectors_evidence,
    observe_selectors_fire,
    selector_diagnostics,
)
from .target_policy import TargetPolicy
from .templates import TemplateRenderer
from .when_parser import (
    TimeOfDay,
    WhenAST,
    evaluate_when,
    evaluate_when_evidence,
    parse_when,
    references_state_change_pulse,
    state_change_pulse_entities,
)
from .yaml_loader import Rule

StateChangeCallback = Callable[[str, Any], None]
SelectorResolver = Callable[[IntentSelector], list[str]]
_FOR_UNIT_MULTIPLIERS = {
    "ms": 1,
    "s": 1_000,
    "m": 60_000,
    "h": 3_600_000,
}
EFFECT_RETRY_BASE_MS = 1_000
EFFECT_RETRY_MAX_MS = 300_000
EFFECT_MAX_ATTEMPTS = 8
EFFECT_DEAD_LETTER_LIMIT = 100


@dataclass
class _ParsedRule:
    """Internal: a Rule plus its parsed when-AST."""

    rule: Rule
    when_ast: WhenAST
    hold_when_ast: WhenAST | None = None
    hold_until_ast: WhenAST | None = None


def _canonical_value(value: Any) -> Any:
    """Return a JSON-safe semantic value with deterministic mapping order."""
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _canonical_value(getattr(value, field.name))
            for field in fields(value)
            if field.name not in {"window_name", "provenance"}
        }
    if isinstance(value, dict):
        return {
            str(key): _canonical_value(item)
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
            if str(key) not in {"window_name", "provenance"}
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    return value


def _rule_fingerprint(rule: Rule) -> str:
    """Return the canonical semantic Rule identity, excluding source location."""
    semantic = {
        field.name: _canonical_value(getattr(rule, field.name))
        for field in fields(rule)
        if field.name not in {
            "source_file",
            "source_line",
            "window_name",
            "provenance",
            "alerts",
        }
    }
    encoded = json.dumps(semantic, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


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
        self._target_policies: dict[str, TargetPolicy] = {}
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
        self._rule_active_since: dict[str, int] = {}
        self._frozen_hold_after: dict[str, FrozenHoldAfter] = {}
        self._state_change_callbacks: list[StateChangeCallback] = []
        self._log: list[str] = []  # last N log lines for diagnostics
        self._active_effect_rule_ids: set[str] = set()
        self._effect_outbox: list[EffectOutboxRecord] = []
        self._template_renderer = TemplateRenderer()
        self._generated_fields: dict[tuple[str, str], GeneratedFieldState] = {}
        self._enabled = True
        self._paused_labels: set[str] = set()
        self._paused_rule_ids: set[str] = set()
        self._hysteresis_latches: dict[str, bool] = {}

    # ── Lifecycle Persistence ───────────────────────────────────────

    def export_lifecycle_records(self) -> dict[str, Any]:
        """Return persistent lifecycle state for restart/reload recovery."""
        records = export_lifecycle_records(
            self._active_intents,
            self._active_effect_rule_ids,
            self._generated_fields,
            self._effect_outbox,
            rule_fingerprints={
                rule_id: _rule_fingerprint(parsed.rule)
                for rule_id, parsed in self._rules.items()
            },
            now_ms=self.now_ms(),
        )
        records["enabled"] = self._enabled
        records["paused_labels"] = sorted(self._paused_labels)
        records["paused_rule_ids"] = sorted(self._paused_rule_ids)
        records["rule_activations"] = [
            {
                "rule_id": rule_id,
                "rule_fingerprint": _rule_fingerprint(self._rules[rule_id].rule),
                "active_since_ms": active_since,
                "frozen_hold_after": _frozen_hold_to_record(self._frozen_hold_after.get(rule_id)),
            }
            for rule_id, active_since in sorted(self._rule_active_since.items())
            if rule_id in self._rules
        ]
        records["hysteresis_latches"] = [
            {
                "rule_fingerprint": fingerprint,
                "latched": True,
                "condition_true_since_ms": self._condition_true_since.get(rule_id),
            }
            for rule_id, parsed in sorted(self._rules.items())
            for fingerprint in (_rule_fingerprint(parsed.rule),)
            for latched in (self._hysteresis_latches.get(fingerprint, False),)
            if latched
        ]
        return records

    def import_lifecycle_records(self, records: dict[str, Any] | None) -> None:
        """Restore persisted lifecycle records produced by export_lifecycle_records()."""
        if lifecycle_version(records) is None:
            return
        restored, active_effect_rule_ids, generated_fields = restore_lifecycle_intents(
            records,
            now_ms=self.now_ms(),
            known_rule_ids=set(self._rules),
            rule_fingerprints={
                rule_id: _rule_fingerprint(parsed.rule)
                for rule_id, parsed in self._rules.items()
            },
        )
        self._active_intents.extend(restored)
        self._active_effect_rule_ids = active_effect_rule_ids
        self._generated_fields.update(generated_fields)
        self._effect_outbox = restore_effect_outbox(records)
        assert records is not None
        paused_labels = records.get("paused_labels")
        if isinstance(paused_labels, list):
            self._paused_labels = {
                label for label in paused_labels if isinstance(label, str) and label
            }
        paused_rule_ids = records.get("paused_rule_ids")
        if isinstance(paused_rule_ids, list):
            self._paused_rule_ids = {
                rule_id for rule_id in paused_rule_ids if isinstance(rule_id, str) and rule_id
            }
        enabled = records.get("enabled", True)
        if not isinstance(enabled, bool):
            enabled = True
        if enabled:
            self._restore_rule_activations(records)
        valid_fingerprints = {
            _rule_fingerprint(parsed.rule)
            for parsed in self._rules.values()
            if parsed.rule.hysteresis is not None
        }
        latches = records.get("hysteresis_latches", [])
        if isinstance(latches, list):
            self._hysteresis_latches = {
                raw["rule_fingerprint"]: True
                for raw in latches
                if isinstance(raw, dict)
                and raw.get("latched") is True
                and raw.get("rule_fingerprint") in valid_fingerprints
            }
            by_fingerprint = {
                _rule_fingerprint(parsed.rule): rule_id
                for rule_id, parsed in self._rules.items()
                if parsed.rule.hysteresis is not None
            }
            for raw in latches:
                if not isinstance(raw, dict):
                    continue
                rule_id = by_fingerprint.get(raw.get("rule_fingerprint"))
                since = raw.get("condition_true_since_ms")
                if rule_id is not None and _is_nonnegative_int(since):
                    self._condition_true_since[rule_id] = since
        if not enabled:
            self.set_enabled(False)
            return
        self._terminate_rule_lifecycle(
            {rule_id for rule_id in self._rules if self._rule_is_paused(rule_id)}
        )

    def _restore_rule_activations(self, records: dict[str, Any]) -> None:
        activations = records.get("rule_activations")
        if not isinstance(activations, list):
            return
        for raw in activations:
            if not isinstance(raw, dict):
                continue
            rule_id = raw.get("rule_id")
            parsed = self._rules.get(rule_id) if isinstance(rule_id, str) else None
            if parsed is None or parsed.rule.dynamic_hold_after is None:
                continue
            if raw.get("rule_fingerprint") != _rule_fingerprint(parsed.rule):
                continue
            active_since = raw.get("active_since_ms")
            if not _is_nonnegative_int(active_since):
                continue
            self._rule_active_since[rule_id] = active_since
            decision = _frozen_hold_from_record(raw.get("frozen_hold_after"))
            if decision is not None and _frozen_hold_matches_policy(
                decision, parsed.rule.dynamic_hold_after, active_since
            ):
                self._frozen_hold_after[rule_id] = decision

    def is_enabled(self) -> bool:
        """Return whether automation rule evaluation is globally enabled."""
        return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        """Globally enable or disable Intentional automation."""
        self._enabled = enabled
        if not enabled:
            self._active_intents = []
            self._active_effect_rule_ids.clear()
            self._terminate_rule_lifecycle(set(self._rules))

    def set_label_paused(self, label: str, paused: bool) -> None:
        """Pause or resume all rules carrying a locality label."""
        if paused:
            self._paused_labels.add(label)
            self._active_intents = [
                intent
                for intent in self._active_intents
                if not self._intent_has_label(intent, label)
            ]
            self._active_effect_rule_ids = {
                rule_id
                for rule_id in self._active_effect_rule_ids
                if not self._rule_has_label(rule_id, label)
            }
            self._terminate_rule_lifecycle(
                {rule_id for rule_id in self._rules if self._rule_has_label(rule_id, label)}
            )
            return
        self._paused_labels.discard(label)

    def set_rule_paused(self, rule_id: str, paused: bool) -> None:
        """Pause or resume one authored or expanded rule id."""
        rule_ids = {
            current_id
            for current_id in self._rules
            if current_id == rule_id or current_id.split(":", 1)[0] == rule_id
        }
        if not rule_ids:
            rule_ids = {rule_id}
        if paused:
            self._paused_rule_ids.update(rule_ids)
            self._active_intents = [
                intent for intent in self._active_intents if intent.rule_id not in rule_ids
            ]
            self._active_effect_rule_ids.difference_update(rule_ids)
            self._terminate_rule_lifecycle(rule_ids)
            return
        self._paused_rule_ids.difference_update(rule_ids)

    def set_rules_paused(self, rule_ids: set[str], paused: bool) -> None:
        """Pause or resume multiple authored or expanded rule ids."""
        for rule_id in rule_ids:
            self.set_rule_paused(rule_id, paused)

    def _terminate_rule_lifecycle(self, rule_ids: set[str]) -> None:
        for rule_id in rule_ids:
            self._rule_active_since.pop(rule_id, None)
            self._frozen_hold_after.pop(rule_id, None)

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
        if clock is not None and re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", clock) is None:
            raise ValueError("time_of_day clock must be strict HH:MM")
        self._time_of_day = TimeOfDay(bucket=bucket, clock=clock)

    def _freeze_dynamic_hold(self, rule_id: str, rule: Rule, now: int) -> FrozenHoldAfter:
        existing = self._frozen_hold_after.get(rule_id)
        if existing is not None:
            return existing
        policy = rule.dynamic_hold_after
        assert policy is not None
        active_for = max(0, now - self._rule_active_since.get(rule_id, now))
        tier_index = max(index for index, tier in enumerate(policy.tiers) if tier.active_for_ms <= active_for)
        tier = policy.tiers[tier_index]
        adjustment_index = None
        adjustment = None
        clock = self._time_of_day.clock if isinstance(self._time_of_day, TimeOfDay) else None
        minute = None
        if clock is not None:
            hour, minute_part = clock.split(":")
            minute = int(hour) * 60 + int(minute_part)
        for index, candidate in enumerate(policy.adjustments):
            if minute is not None and _minute_in_window(minute, candidate.from_minute, candidate.until_minute):
                adjustment_index, adjustment = index, candidate
                break
        add_ms = adjustment.add_ms if adjustment is not None else 0
        unclamped = tier.duration_ms + add_ms
        duration = max(0, min(unclamped, policy.max_ms))
        expires = now + duration
        decision = FrozenHoldAfter(
            active_for_ms=active_for,
            tier_index=tier_index,
            tier_threshold_ms=tier.active_for_ms,
            base_duration_ms=tier.duration_ms,
            adjustment_index=adjustment_index,
            adjustment_from=adjustment.from_time if adjustment else None,
            adjustment_until=adjustment.until_time if adjustment else None,
            adjustment_add_ms=add_ms,
            max_ms=policy.max_ms,
            unclamped_duration_ms=unclamped,
            duration_ms=duration,
            started_at_ms=now,
            expires_at_ms=expires,
        )
        self._frozen_hold_after[rule_id] = decision
        return decision

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

    def remove_state(self, entity_id: str) -> None:
        """Remove all cached facts for an entity that no longer exists."""
        prefix = f"{entity_id}."
        keys = [key for key in self.state if key.startswith(prefix)]
        for key in keys:
            del self.state[key]
        if keys:
            for cb in self._state_change_callbacks:
                cb(entity_id, None)

    def on_state_change(self, callback: StateChangeCallback) -> None:
        """Register a callback for state changes. Returns the callback for chaining."""
        self._state_change_callbacks.append(callback)
        return callback

    def set_selector_resolver(self, resolver: SelectorResolver) -> None:
        """Replace selector membership resolution for isolated runtimes."""
        self._selector_resolver = resolver

    # ── Rules ───────────────────────────────────────────────────────

    def load_rules(
        self,
        rules: list[Rule],
        *,
        target_policies: dict[str, TargetPolicy] | None = None,
    ) -> None:
        """Transactionally replace rules and preserve only identical-rule memory."""
        policies = target_policies
        if policies is None:
            policies = getattr(rules, "target_policies", {})
        parsed_rules: dict[str, _ParsedRule] = {}
        for rule in rules:
            try:
                when_ast = parse_when(rule.when)
                hold_when_ast = parse_when(rule.hold_when) if rule.hold_when else None
                hold_until_ast = parse_when(rule.hold_until_when) if rule.hold_until_when else None
            except Exception as e:
                raise ValueError(f"Failed to parse when for {rule.id!r}: {e}") from e
            parsed_rules[rule.id] = _ParsedRule(
                rule=rule,
                when_ast=when_ast,
                hold_when_ast=hold_when_ast,
                hold_until_ast=hold_until_ast,
            )
        unchanged = {
            rule_id
            for rule_id, parsed in parsed_rules.items()
            if rule_id in self._rules
            and _rule_fingerprint(self._rules[rule_id].rule) == _rule_fingerprint(parsed.rule)
        }
        self._rules = parsed_rules
        self._target_policies = dict(policies)
        # Drop level-rule intents on reload so active observations recreate
        # intents from the current rule definition (target/value/lifecycle).
        self._active_intents = [
            i
            for i in self._active_intents
            if not i.rule_id
            or (
                i.rule_id in unchanged
                and (i.ignore_when or i.rule_id in self._rule_held_since)
            )
        ]
        self._condition_true_since = {
            rule_id: since
            for rule_id, since in self._condition_true_since.items()
            if rule_id in unchanged
        }
        self._hold_until_true_since = {
            rule_id: since
            for rule_id, since in self._hold_until_true_since.items()
            if rule_id in unchanged
        }
        self._rule_held_since = {
            rule_id: since
            for rule_id, since in self._rule_held_since.items()
            if rule_id in unchanged
        }
        self._rule_active_since = {
            rule_id: since for rule_id, since in self._rule_active_since.items() if rule_id in unchanged
        }
        self._frozen_hold_after = {
            rule_id: decision for rule_id, decision in self._frozen_hold_after.items() if rule_id in unchanged
        }
        self._animation_started_at = {
            rule_id: started
            for rule_id, started in self._animation_started_at.items()
            if rule_id in unchanged
        }
        self._active_effect_rule_ids.intersection_update(unchanged)
        # Rendered outbox entries are obligations and survive Rule changes.
        self._generated_fields = {
            key: value for key, value in self._generated_fields.items() if key[0] in unchanged
        }

    @staticmethod
    def rule_fingerprint(rule: Rule) -> str:
        """Expose reload identity semantics to the integration coordinator."""
        return _rule_fingerprint(rule)

    @staticmethod
    def validate_rules(rules: list[Rule]) -> None:
        """Validate expressions and templates without mutating engine state."""
        renderer = TemplateRenderer()
        for rule in rules:
            parse_when(rule.when)
            if rule.hold_when:
                parse_when(rule.hold_when)
            if rule.hold_until_when:
                parse_when(rule.hold_until_when)
            for value in (rule.set, rule.cap, rule.floor, rule.offset, rule.multiply):
                renderer.validate_value(value)
            for selector in rule.intent_selectors:
                for value in (
                    selector.set,
                    selector.cap,
                    selector.floor,
                    selector.offset,
                    selector.multiply,
                ):
                    renderer.validate_value(value)
            for effect in rule.effects:
                renderer.validate_value(effect.target)
                renderer.validate_value(effect.data)

    def rule_fingerprints(self) -> dict[str, tuple[Any, ...]]:
        """Return current semantic fingerprints keyed by expanded rule id."""
        return {rule_id: _rule_fingerprint(parsed.rule) for rule_id, parsed in self._rules.items()}

    def loaded_rules(self) -> list[Rule]:
        """Return the currently loaded expanded Rules for isolated tooling."""
        return [parsed.rule for parsed in self._rules.values()]

    def add_rule(self, rule: Rule) -> None:
        """Add or replace a single rule."""
        self.load_rules(
            [r for r in (pr.rule for pr in self._rules.values()) if r.id != rule.id] + [rule]
        )

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
            intent
            for intent in self._active_intents
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
            return
        firing, _condition_firing, _blocked_by, _for_remaining = self._firing_rule_diagnostics(
            update_timers=True
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
                parsed = self._rules.get(intent.rule_id)
                if (
                    intent.rule_id in firing
                    and parsed is not None
                    and parsed.rule.dynamic_hold_after is not None
                ):
                    if not intent.selector_generated:
                        new_active.append(self._spawn_intent_from_rule(parsed.rule, now))
                    self._rule_active_since[intent.rule_id] = now
                    self._frozen_hold_after.pop(intent.rule_id, None)
                    self._animation_started_at[intent.rule_id] = now
                    continue
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
                if parsed.hold_when_ast is not None and self._eval_when(parsed.hold_when_ast) and observation_groups_fire(rule.hold_observation_groups, self.state, self._selector_resolver):
                    self._hold_until_true_since.pop(intent.rule_id, None)
                    self._rule_held_since.setdefault(intent.rule_id, now)
                    new_active.append(intent)
                    continue
                if rule.dynamic_hold_after is not None and not intent.ignore_when:
                    decision = self._freeze_dynamic_hold(intent.rule_id, rule, now)
                    if decision.duration_ms > 0:
                        new_active.append(_linger_intent(intent, decision.duration_ms, now))
                    continue
                if rule.linger_ms and not intent.ignore_when:
                    new_active.append(_linger_intent(intent, rule.linger_ms, now))
                    continue
            # Rule no longer fires — drop, regardless of TTL
            continue

        # Add new intents for rules that just started firing
        new_active = [self._refresh_generated_intent(intent, now) for intent in new_active]
        for rule_id in firing:
            rule = self._rules[rule_id].rule
            if not rule.intent_selectors:
                continue
            existing = [
                intent
                for intent in new_active
                if intent.rule_id == rule_id and intent.selector_generated
            ]
            created_at_by_target = {intent.target: intent.created_at_ms for intent in existing}
            new_active = [
                intent
                for intent in new_active
                if intent.rule_id != rule_id or not intent.selector_generated
            ]
            new_active.extend(
                replace(
                    intent,
                    created_at_ms=created_at_by_target.get(intent.target, intent.created_at_ms),
                )
                for intent in self._spawn_intents_from_selectors(rule, now)
            )
        for rule_id, _target in firing.items():
            parsed = self._rules[rule_id]
            if parsed.rule.effects and rule_id not in self._active_effect_rule_ids:
                activation_id = str(uuid.uuid4())
                fingerprint = _rule_fingerprint(parsed.rule)
                for effect_index, effect in enumerate(parsed.rule.effects):
                    rendered = self._template_renderer.render_effect(effect, self.state)
                    self._effect_outbox.append(
                        EffectOutboxRecord(
                            activation_id=activation_id,
                            rule_id=rule_id,
                            rule_fingerprint=fingerprint,
                            effect_index=effect_index,
                            domain=rendered.domain,
                            service=rendered.service,
                            target=dict(rendered.target),
                            data=dict(rendered.data),
                            next_retry_ms=now,
                        )
                    )
            has_explicit_intent = any(
                intent.rule_id == rule_id and not intent.selector_generated for intent in new_active
            )
            if not has_explicit_intent:
                if not parsed.rule.target and parsed.rule.scene is None:
                    continue
                intent = self._spawn_intent_from_rule(parsed.rule, now)
                new_active.append(intent)
                if parsed.rule.dynamic_hold_after is not None:
                    self._rule_active_since.setdefault(rule_id, now)
                self._animation_started_at[rule_id] = now

        self._active_intents = new_active
        active_rule_ids = {intent.rule_id for intent in new_active if intent.rule_id}
        for rule_id in active_rule_ids:
            parsed = self._rules.get(rule_id)
            if parsed is not None and parsed.rule.dynamic_hold_after is not None:
                self._rule_active_since.setdefault(rule_id, now)
        for rule_id in list(self._rule_active_since):
            if rule_id not in active_rule_ids:
                self._rule_active_since.pop(rule_id, None)
                self._frozen_hold_after.pop(rule_id, None)
        self._active_effect_rule_ids = {
            rule_id for rule_id in firing if self._rules[rule_id].rule.effects
        }

    def list_effect_outbox(self, *, include_acknowledged: bool = True) -> list[EffectOutboxRecord]:
        """Return a snapshot of durable Effect delivery records."""
        return [
            record
            for record in self._effect_outbox
            if include_acknowledged or record.acknowledged_at_ms is None
        ]

    def due_effects(self) -> list[EffectOutboxRecord]:
        """Return unacknowledged Effects whose retry time has arrived."""
        now = self.now_ms()
        return [
            record
            for record in self._effect_outbox
            if record.acknowledged_at_ms is None
            and record.dead_lettered_at_ms is None
            and record.next_retry_ms <= now
        ]

    def begin_effect_attempt(
        self, activation_id: str, effect_index: int
    ) -> EffectOutboxRecord | None:
        """Record an attempt and its bounded exponential retry before dispatch."""
        now = self.now_ms()
        for index, record in enumerate(self._effect_outbox):
            if (record.activation_id, record.effect_index) != (activation_id, effect_index):
                continue
            if (
                record.acknowledged_at_ms is not None
                or record.dead_lettered_at_ms is not None
                or record.next_retry_ms > now
            ):
                return None
            attempts = record.attempts + 1
            delay = min(EFFECT_RETRY_MAX_MS, EFFECT_RETRY_BASE_MS * (2 ** min(attempts - 1, 20)))
            updated = replace(record, attempts=attempts, next_retry_ms=now + delay)
            self._effect_outbox[index] = updated
            return updated
        return None

    def acknowledge_effect(self, activation_id: str, effect_index: int) -> bool:
        """Mark an Effect accepted by Home Assistant's blocking service call."""
        for index, record in enumerate(self._effect_outbox):
            if (record.activation_id, record.effect_index) != (activation_id, effect_index):
                continue
            if record.acknowledged_at_ms is not None:
                return False
            # Active Effect Rule ids are the durable duplicate-suppression
            # tombstone, so acknowledged delivery records can be compacted.
            self._effect_outbox.pop(index)
            return True
        return False

    def fail_effect(self, activation_id: str, effect_index: int, error: str) -> bool:
        """Record a failed delivery and terminally bound retries/diagnostics."""
        for index, record in enumerate(self._effect_outbox):
            if (record.activation_id, record.effect_index) != (activation_id, effect_index):
                continue
            if record.acknowledged_at_ms is not None or record.dead_lettered_at_ms is not None:
                return False
            terminal = record.attempts >= EFFECT_MAX_ATTEMPTS
            self._effect_outbox[index] = replace(
                record,
                last_error=str(error)[:500],
                dead_lettered_at_ms=self.now_ms() if terminal else None,
            )
            if terminal:
                dead = [
                    item for item in self._effect_outbox if item.dead_lettered_at_ms is not None
                ]
                excess = len(dead) - EFFECT_DEAD_LETTER_LIMIT
                if excess > 0:
                    remove = {
                        (item.activation_id, item.effect_index)
                        for item in sorted(dead, key=lambda item: item.dead_lettered_at_ms or 0)[
                            :excess
                        ]
                    }
                    self._effect_outbox = [
                        item
                        for item in self._effect_outbox
                        if (item.activation_id, item.effect_index) not in remove
                    ]
            return terminal
        return False

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
            condition = self._hysteresis_fires(parsed, update=update_timers)
            if condition and observe_selectors_fire(
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
                for candidate_id in firing:
                    candidate = self._rules[candidate_id].rule
                    if (
                        candidate_id == blocked_rule_id
                        or candidate.authored_rule_id == blocked_rule_id
                    ):
                        blocked_by.setdefault(candidate_id, []).append(rule_id)
        for rule_id in blocked_by:
            firing.pop(rule_id, None)

        return firing, condition_firing, blocked_by, for_remaining

    def _hysteresis_fires(self, parsed: _ParsedRule, *, update: bool) -> bool:
        observation = parsed.rule.hysteresis
        if observation is None:
            return self._eval_when(parsed.when_ast)
        fingerprint = _rule_fingerprint(parsed.rule)
        latched = self._hysteresis_latches.get(fingerprint, False)
        raw = self.state.get(f"{observation.entity}.state")
        try:
            actual = float(raw)
        except (TypeError, ValueError):
            return latched
        operator = observation.exit_operator if latched else observation.enter_operator
        threshold = observation.exit_value if latched else observation.enter_value
        matched = _numeric_comparison(actual, operator, float(threshold))
        result = not matched if latched else matched
        if update:
            if result:
                self._hysteresis_latches[fingerprint] = True
            else:
                self._hysteresis_latches.pop(fingerprint, None)
        return result

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

    def rule_for_ms(self, rule_id: str) -> int:
        """Return the current effective pending duration for a loaded Rule."""
        parsed = self._rules.get(rule_id)
        return 0 if parsed is None else self._rule_for_ms(parsed.rule)

    def _hold_until_released(self, parsed: _ParsedRule, now: int) -> bool:
        if parsed.hold_until_ast is None:
            return True
        rule_id = parsed.rule.id
        if not self._eval_when(parsed.hold_until_ast) or not observation_groups_fire(parsed.rule.hold_until_observation_groups, self.state, self._selector_resolver):
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
            withdraw=dict(rule.withdraw),
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

    def _sample_generated_fields(
        self, rule: Rule, set_values: dict[str, Any], now: int
    ) -> dict[str, Any]:
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
        matched_targets: set[str] = set()
        for selector in rule.intent_selectors:
            for target in self._selector_resolver(selector):
                if target in selector.exclude or target in matched_targets:
                    continue
                matched_targets.add(target)
                intents.append(
                    Intent(
                        target=target,
                        set=self._template_renderer.render_value(selector.set, self.state),
                        withdraw=dict(selector.withdraw),
                        cap=self._template_renderer.render_value(selector.cap, self.state),
                        floor=self._template_renderer.render_value(selector.floor, self.state),
                        offset=self._template_renderer.render_value(selector.offset, self.state),
                        multiply=self._template_renderer.render_value(
                            selector.multiply, self.state
                        ),
                        transition_ms=selector.transition_ms,
                        easing=selector.easing,
                        authority=rule.authority,
                        confidence=rule.confidence,
                        ttl_ms=selector.ttl_ms,
                        reason=rule.reason,
                        rule_id=rule.id,
                        ignore_when=rule.edge_created,
                        selector_generated=True,
                        created_at_ms=now,
                    )
                )
        return intents

    # ── Resolution ─────────────────────────────────────────────────

    def rule_count(self) -> int:
        """Return the number of loaded, parsed rules."""
        return len(self._rules)

    def list_known_targets(self) -> tuple[str, ...]:
        """Return sorted target entity IDs referenced by loaded target rules."""
        return tuple(
            sorted({parsed.rule.target for parsed in self._rules.values() if parsed.rule.target})
        )

    def target_policy(self, target: str) -> Any | None:
        """Return the explicit document policy for a Target, if declared."""
        return self._target_policies.get(target)

    def target_policies(self) -> dict[str, TargetPolicy]:
        """Return an isolated snapshot of document-owned Target policies."""
        return dict(self._target_policies)

    def active_intent_count(self) -> int:
        """Return the number of currently active, non-expired intents."""
        now = self.now_ms()
        return sum(
            1 for intent in self._active_intents if not intent.is_expired(into_the_future_ms=now)
        )

    def list_active_intents(self, target: str) -> list[Intent]:
        """Return a copy of the active intents for a target.

        Excludes scene-rule intents (those have no target).
        """
        now = self.now_ms()
        return [
            i
            for i in self._active_intents
            if i.target == target and not i.is_expired(into_the_future_ms=now)
        ]

    def list_active_user_intents(self, target: str | None = None) -> list[Intent]:
        """Return active manual/user intents, optionally for one target."""
        now = self.now_ms()
        return [
            intent
            for intent in self._active_intents
            if intent.authority is Authority.USER
            and not intent.rule_id
            and (target is None or intent.target == target)
            and not intent.is_expired(into_the_future_ms=now)
        ]

    def list_active_targets(self) -> tuple[str, ...]:
        """Return sorted target entity IDs with at least one active intent."""
        now = self.now_ms()
        return tuple(
            sorted(
                {
                    intent.target
                    for intent in self._active_intents
                    if intent.target and not intent.is_expired(into_the_future_ms=now)
                }
            )
        )

    def has_active_target(self, target: str) -> bool:
        """Return whether a target has at least one active, non-expired intent."""
        now = self.now_ms()
        return any(
            intent.target == target and not intent.is_expired(into_the_future_ms=now)
            for intent in self._active_intents
        )

    def list_active_scene_intents(self, return_intents: bool = False):
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
            i
            for i in self._active_intents
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
            diagnostics=resolved.diagnostics,
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
                "diagnostics": list(resolved_obj.diagnostics),
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
            rules_for_target.append(
                {
                    "rule_id": rule_id,
                    "firing": rule_id in firing,
                    "condition_firing": rule_id in condition_firing,
                    "blocked_by": sorted(blocked_by.get(rule_id, [])),
                    "for_remaining_ms": for_remaining.get(rule_id),
                    "phase": status.get("phase", "idle"),
                    "active_for_ms": status.get("active_for_ms"),
                    "condition_active_for_ms": status.get("condition_active_for_ms"),
                    "held_for_ms": status.get("held_for_ms"),
                    **({"hold_after": status["hold_after"]} if "hold_after" in status else {}),
                    "group": status.get("group", ""),
                    "profile": status.get("profile", ""),
                }
            )

        from .reconciliation import target_policy_denial

        return {
            "target": target,
            "target_policy": None
            if self.target_policy(target) is None
            else self.target_policy(target).as_dict(),
            "resolved": resolved,
            "active_intents": [_intent_to_diagnostic_dict(intent) for intent in active],
            "winning_intent": winning_intent,
            "policy_denial": None
            if resolved_obj is None
            else target_policy_denial(self, target, dict(resolved_obj.value)),
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
            desired_records.append(
                {
                    "target": target,
                    "desired": dict(resolved.value),
                    "rule_id": winning.rule_id if winning is not None else "",
                    "reason": winning.reason if winning is not None else "",
                    "conditions": [{"type": "DesiredResolved", "status": "true"}],
                }
            )
        return {
            "dsl_version": "vnext-draft",
            "rule_count": self.rule_count(),
            "active_intent_count": self.active_intent_count(),
            "authored_rules": list(self.list_authored_rule_statuses().values()),
            "active_rules": [
                status
                for status in self.list_authored_rule_statuses().values()
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
            rule = self._rules[rule_id].rule
            authored_id = rule.authored_rule_id or rule_id
            current = grouped.get(authored_id)
            if current is None:
                grouped[authored_id] = {**status, "rule_id": authored_id}
                continue
            current["paused"] = current.get("paused", False) or status.get("paused", False)
            current["active"] = current["active"] or status["active"]
            current["phase"] = dominant_phase(
                str(current.get("phase", "idle")), str(status.get("phase", "idle"))
            )
            current["condition_firing"] = current["condition_firing"] or status["condition_firing"]
            current["active_intent_count"] += status["active_intent_count"]
            current["active_for_ms"] = min_optional(
                current.get("active_for_ms"), status.get("active_for_ms")
            )
            current["condition_active_for_ms"] = min_optional(
                current.get("condition_active_for_ms"),
                status.get("condition_active_for_ms"),
            )
            current["held_for_ms"] = min_optional(
                current.get("held_for_ms"), status.get("held_for_ms")
            )
            current["targets"] = sorted(set(current["targets"]) | set(status["targets"]))
            current["blocked_by"] = sorted(set(current["blocked_by"]) | set(status["blocked_by"]))
            current["group"] = current.get("group") or status.get("group", "")
            current["profile"] = current.get("profile") or status.get("profile", "")
            remaining = [
                value
                for value in (current.get("for_remaining_ms"), status.get("for_remaining_ms"))
                if value is not None
            ]
            current["for_remaining_ms"] = min(remaining) if remaining else None
        return grouped

    def alert_observations(
        self, pulse_tokens: dict[str, object] | None = None
    ) -> tuple[AlertObservation, ...]:
        """Return one current observation per authored Alert declaration."""
        statuses = self.list_authored_rule_statuses()
        observations: list[AlertObservation] = []
        seen: set[tuple[str, str]] = set()
        for parsed in self._rules.values():
            rule = parsed.rule
            rule_id = rule.authored_rule_id or rule.id
            status = statuses.get(rule_id, {})
            when_evidence = evaluate_when_evidence(
                parsed.when_ast, self.state, time_of_day=self._time_of_day
            )
            selector_evidence = observe_selectors_evidence(
                rule, self.state, self._selector_resolver
            )
            pulse_entities = state_change_pulse_entities(parsed.when_ast)
            pulse_id = None
            if pulse_tokens is not None:
                consumed = [
                    f"{entity_id}:{pulse_tokens[entity_id]}"
                    for entity_id in sorted(pulse_entities)
                    if entity_id in pulse_tokens
                ]
                if consumed:
                    pulse_id = "|".join(consumed)
            quality = (
                "known"
                if (when_evidence.quality == "known" and not when_evidence.value)
                or (selector_evidence.quality == "known" and not selector_evidence.value)
                or (
                    when_evidence.quality == "known"
                    and selector_evidence.quality == "known"
                )
                else "unknown"
            )
            for alert in rule.alerts:
                key = (rule_id, alert.name)
                if key in seen:
                    continue
                seen.add(key)
                inactive_reason = (
                    "evaluation_disabled"
                    if not self.is_enabled()
                    else "evaluation_paused"
                    if status.get("paused")
                    else "rule_blocked"
                    if status.get("blocked_by")
                    else "condition_inactive"
                )
                labels = {
                    "alertname": alert.name,
                    "rule_id": rule_id,
                    "severity": alert.severity,
                    "integration": "intentional",
                    **alert.labels,
                }
                definition_revision = hashlib.sha256(
                    json.dumps(
                        {
                            "condition": repr(parsed.when_ast),
                            "name": alert.name,
                            "severity": alert.severity,
                            "for_ms": alert.for_ms,
                            "resolve_after_ms": alert.resolve_after_ms,
                            "stale_after_ms": alert.stale_after_ms,
                            "labels": alert.labels,
                            "annotations": alert.annotations,
                            "escalations": [
                                [step.after_ms, step.severity]
                                for step in alert.escalations
                            ],
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest()
                observations.append(
                    AlertObservation(
                        rule_id=rule_id,
                        name=alert.name,
                        severity=alert.severity,
                        summary=str(alert.summary),
                        active=bool(
                            self.is_enabled()
                            and status.get("condition_firing")
                            and not status.get("blocked_by")
                            and not status.get("paused")
                            and status.get("enabled", True)
                        ),
                        observed_at_ms=self.now_ms(),
                        labels=labels,
                        annotations=dict(alert.annotations),
                        definition_revision=definition_revision,
                        escalations=tuple(
                            (step.after_ms, step.severity)
                            for step in alert.escalations
                        ),
                        for_ms=(
                            alert.for_ms
                            if alert.for_ms is not None
                            else self.rule_for_ms(rule.id)
                        ),
                        quality=(
                            "known" if inactive_reason != "condition_inactive" else quality
                        ),
                        stale_after_ms=alert.stale_after_ms,
                        resolve_after_ms=(
                            alert.resolve_after_ms
                            if references_state_change_pulse(parsed.when_ast)
                            else None
                        ),
                        inactive_reason=inactive_reason,
                        pulse_id=pulse_id,
                        duration_revision=(
                            f"alert:{alert.for_ms}"
                            if alert.for_ms is not None
                            else (
                                f"rule:{rule.for_ms}:{rule.for_entity}:{rule.for_entity_unit}"
                            )
                        ),
                    )
                )
        return tuple(observations)

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
        targets.extend(_selector_summary(selector) for selector in rule.intent_selectors)
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

        status = {
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
        if rule.dynamic_hold_after is not None:
            status["hold_after"] = self._hold_after_status(rule_id, now)
        return status

    def _hold_after_status(self, rule_id: str, now: int) -> dict[str, Any] | None:
        decision = self._frozen_hold_after.get(rule_id)
        if decision is None:
            return None
        rule = self._rules.get(rule_id)
        adjustment = None
        if decision.adjustment_index is not None:
            source = rule.rule.dynamic_hold_after.adjustments[decision.adjustment_index] if rule and rule.rule.dynamic_hold_after else None
            adjustment = {
                "index": decision.adjustment_index, "from": decision.adjustment_from,
                "until": decision.adjustment_until, "add_ms": decision.adjustment_add_ms,
            }
            if source and source.window_name is not None:
                adjustment["window"] = source.window_name
        return {
            "frozen": True,
            "active_for_ms": decision.active_for_ms,
            "tier": {"index": decision.tier_index, "threshold_ms": decision.tier_threshold_ms, "base_duration_ms": decision.base_duration_ms},
            "adjustment": adjustment,
            "max_ms": decision.max_ms,
            "unclamped_duration_ms": decision.unclamped_duration_ms,
            "duration_ms": decision.duration_ms,
            "started_at_ms": decision.started_at_ms,
            "expires_at_ms": decision.expires_at_ms,
            "remaining_ms": max(0, decision.expires_at_ms - now),
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
            lines.append(
                f"    - {i.rule_id or '<manual>'}: {i.authority.value}{ttl_info} — {i.reason}"
            )
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
        "withdraw": dict(intent.withdraw) if intent.withdraw else {},
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
        withdraw=dict(intent.withdraw),
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
        selector_generated=intent.selector_generated,
        created_at_ms=now,
        animation=intent.animation,
        generators=intent.generators,
    )


def _minute_in_window(minute: int, start: int, end: int) -> bool:
    if start == end:
        return True
    return start <= minute < end if start < end else minute >= start or minute < end


def _numeric_comparison(actual: float, operator: str, threshold: float) -> bool:
    if operator == "gt":
        return actual > threshold
    if operator == "gte":
        return actual >= threshold
    if operator == "lt":
        return actual < threshold
    if operator == "lte":
        return actual <= threshold
    return False


def _frozen_hold_to_record(decision: FrozenHoldAfter | None) -> dict[str, Any] | None:
    if decision is None:
        return None
    return {field.name: getattr(decision, field.name) for field in fields(FrozenHoldAfter)}


def _frozen_hold_from_record(raw: Any) -> FrozenHoldAfter | None:
    if not isinstance(raw, dict):
        return None
    try:
        decision = FrozenHoldAfter(**{field.name: raw[field.name] for field in fields(FrozenHoldAfter)})
    except (KeyError, TypeError, ValueError):
        return None
    integers = (
        decision.active_for_ms, decision.tier_index, decision.tier_threshold_ms,
        decision.base_duration_ms, decision.adjustment_add_ms, decision.max_ms,
        decision.unclamped_duration_ms, decision.duration_ms, decision.started_at_ms,
        decision.expires_at_ms,
    )
    if any(not isinstance(value, int) or isinstance(value, bool) for value in integers):
        return None
    nonnegative = (
        decision.active_for_ms, decision.tier_index, decision.tier_threshold_ms,
        decision.base_duration_ms, decision.max_ms, decision.duration_ms,
        decision.started_at_ms, decision.expires_at_ms,
    )
    if any(value < 0 for value in nonnegative):
        return None
    if decision.tier_threshold_ms > decision.active_for_ms:
        return None
    if decision.unclamped_duration_ms != (
        decision.base_duration_ms + decision.adjustment_add_ms
    ):
        return None
    if decision.duration_ms != max(
        0, min(decision.unclamped_duration_ms, decision.max_ms)
    ):
        return None
    if decision.expires_at_ms != decision.started_at_ms + decision.duration_ms:
        return None
    if decision.adjustment_index is None:
        if (decision.adjustment_from, decision.adjustment_until, decision.adjustment_add_ms) != (
            None, None, 0
        ):
            return None
    elif (
        not _is_nonnegative_int(decision.adjustment_index)
        or not _is_clock(decision.adjustment_from)
        or not _is_clock(decision.adjustment_until)
    ):
        return None
    return decision


def _frozen_hold_matches_policy(decision: FrozenHoldAfter, policy: Any, active_since: int) -> bool:
    if decision.active_for_ms != max(0, decision.started_at_ms - active_since):
        return False
    if decision.max_ms != policy.max_ms or decision.tier_index >= len(policy.tiers):
        return False
    tier = policy.tiers[decision.tier_index]
    if (decision.tier_threshold_ms, decision.base_duration_ms) != (
        tier.active_for_ms, tier.duration_ms
    ):
        return False
    if decision.adjustment_index is None:
        return True
    if decision.adjustment_index >= len(policy.adjustments):
        return False
    adjustment = policy.adjustments[decision.adjustment_index]
    return (
        decision.adjustment_from, decision.adjustment_until, decision.adjustment_add_ms
    ) == (adjustment.from_time, adjustment.until_time, adjustment.add_ms)


def _is_nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_clock(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value) is not None
