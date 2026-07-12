"""Pure reconciliation-aware timeline simulation."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from .engine import Engine
from .projection import simulation_step, target_projection
from .reconciliation import Reconciliation
from .records import IntentSelector, ObserveSelector


class FakeAdapter:
    """In-memory Adapter used by simulation; it never touches Home Assistant."""

    def __init__(self) -> None:
        self.states: dict[str, Any] = {}
        self.calls: list[dict[str, Any]] = []
        self.reject: bool | set[str] = False

    def get_state(self, entity_id: str) -> Any:
        return self.states.get(entity_id)

    def set_state(self, entity_id: str, value: Any) -> Any:
        if isinstance(value, dict):
            state = value.get("state", "unknown")
            attributes = value.get("attributes", {})
            user_id = value.get("user_id")
        else:
            state, attributes, user_id = value, {}, None
        result = SimpleNamespace(
            entity_id=entity_id,
            state=str(state),
            attributes=dict(attributes),
            context=SimpleNamespace(id=None, parent_id=None, user_id=user_id),
        )
        self.states[entity_id] = result
        return result

    async def async_call(
        self, domain: str, service: str, data: dict[str, Any], *, context: Any
    ) -> None:
        rejected = self.reject is True or isinstance(self.reject, set) and service in self.reject
        self.calls.append(
            {"domain": domain, "service": service, "data": dict(data), "rejected": rejected}
        )
        if rejected:
            raise RuntimeError("simulated rejected service call")

    def new_context(self) -> Any:
        return SimpleNamespace(id="simulation", parent_id=None, user_id=None)


class _NoOwnership:
    def owns_state(self, state: Any) -> bool:
        return False


MAX_TIMELINE_STEPS = 500
MAX_STATE_UPDATES_PER_STEP = 200
MAX_ACTUAL_STATES_PER_STEP = 100
MAX_TOTAL_STATE_UPDATES = 5_000
MAX_PROJECTED_TARGETS = 200
MAX_SELECTOR_MEMBERSHIPS = 200
MAX_PREVIEW_HORIZONS = 32
MAX_PREVIEW_HORIZON_MS = 86_400_000
_RECONCILIATION_OPTIONS = {
    "drift_override_ttl_ms",
    "drift_confirmation_ms",
    "service_failure_backoff_ms",
    "service_failure_backoff_max_ms",
    "drift_transition_grace_ms",
}


def validate_preview_horizons(value: Any) -> list[int]:
    """Validate bounded, forward-only lifecycle preview horizons."""
    if (
        not isinstance(value, list)
        or len(value) > MAX_PREVIEW_HORIZONS
        or any(
            not isinstance(item, int) or isinstance(item, bool)
            or item < 0 or item > MAX_PREVIEW_HORIZON_MS
            for item in value
        )
        or value != sorted(value)
    ):
        raise ValueError(
            "`horizons_ms` must be an ascending list of at most 32 integers from 0 to 86400000"
        )
    return value


def validate_simulation_input(
    timeline: Any,
    options: Any,
    *,
    projected_rule_targets: int = 0,
    selector_memberships: Any = None,
    semantic_metadata: Any = None,
) -> None:
    """Strictly validate and bound an API simulation request."""
    if not isinstance(timeline, list):
        raise ValueError("`timeline` must be a list")
    if len(timeline) > MAX_TIMELINE_STEPS:
        raise ValueError(f"`timeline` may contain at most {MAX_TIMELINE_STEPS} steps")
    if not isinstance(options, dict):
        raise ValueError("`reconciliation` must be a mapping")
    unknown = set(options) - _RECONCILIATION_OPTIONS
    if unknown:
        raise ValueError(f"Unknown reconciliation options: {sorted(unknown)}")
    for name, value in options.items():
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"reconciliation.{name} must be a non-negative integer")
    base = options.get("service_failure_backoff_ms", 1_000)
    maximum = options.get("service_failure_backoff_max_ms")
    if maximum is not None and maximum < base:
        raise ValueError(
            "reconciliation.service_failure_backoff_max_ms must be at least the base backoff"
        )
    if projected_rule_targets > MAX_PROJECTED_TARGETS:
        raise ValueError(f"simulation may project at most {MAX_PROJECTED_TARGETS} Targets")
    _validate_selector_memberships(selector_memberships)
    _validate_semantic_metadata(semantic_metadata)
    total = 0
    allowed = {
        "advance_ms",
        "states",
        "actual",
        "reject_calls",
        "pause_rule_ids",
        "resume_rule_ids",
        "enabled",
        "restart",
        "time_of_day",
    }
    for index, step in enumerate(timeline):
        if not isinstance(step, dict):
            raise ValueError(f"timeline[{index}] must be a mapping")
        unknown = set(step) - allowed
        if unknown:
            raise ValueError(f"timeline[{index}] has unknown fields: {sorted(unknown)}")
        advance = step.get("advance_ms", 0)
        if not isinstance(advance, int) or isinstance(advance, bool) or advance < 0:
            raise ValueError(f"timeline[{index}].advance_ms must be a non-negative integer")
        for name in ("enabled", "restart"):
            if name in step and not isinstance(step[name], bool):
                raise ValueError(f"timeline[{index}].{name} must be a boolean")
        time_of_day = step.get("time_of_day")
        if time_of_day is not None and (
            not isinstance(time_of_day, str)
            or len(time_of_day) != 5
            or time_of_day[2] != ":"
            or not time_of_day[:2].isdigit()
            or not time_of_day[3:].isdigit()
            or int(time_of_day[:2]) > 23
            or int(time_of_day[3:]) > 59
        ):
            raise ValueError(f"timeline[{index}].time_of_day must be strict HH:MM")
        for name in ("pause_rule_ids", "resume_rule_ids"):
            value = step.get(name, [])
            if not isinstance(value, list) or not all(
                isinstance(item, str) and item for item in value
            ):
                raise ValueError(f"timeline[{index}].{name} must be a list of Rule IDs")
        reject = step.get("reject_calls", False)
        if not isinstance(reject, bool) and not (
            isinstance(reject, list) and all(isinstance(item, str) and item for item in reject)
        ):
            raise ValueError(
                f"timeline[{index}].reject_calls must be a boolean or list of service names"
            )
        states = step.get("states", {})
        if not isinstance(states, dict) or not all(
            isinstance(key, str) and "." in key for key in states
        ):
            raise ValueError(f"timeline[{index}].states must map state-field keys to values")
        if len(states) > MAX_STATE_UPDATES_PER_STEP:
            raise ValueError(
                f"timeline[{index}].states exceeds {MAX_STATE_UPDATES_PER_STEP} updates"
            )
        actual = step.get("actual", {})
        if not isinstance(actual, dict) or not all(
            isinstance(key, str) and "." in key for key in actual
        ):
            raise ValueError(f"timeline[{index}].actual must map Target IDs to states")
        if len(actual) > MAX_ACTUAL_STATES_PER_STEP:
            raise ValueError(
                f"timeline[{index}].actual exceeds {MAX_ACTUAL_STATES_PER_STEP} Targets"
            )
        for target, snapshot in actual.items():
            if isinstance(snapshot, dict):
                if set(snapshot) - {"state", "attributes", "user_id"}:
                    raise ValueError(f"timeline[{index}].actual[{target!r}] has unknown fields")
                if "attributes" in snapshot and not isinstance(snapshot["attributes"], dict):
                    raise ValueError(
                        f"timeline[{index}].actual[{target!r}].attributes must be a mapping"
                    )
        total += len(states) + len(actual)
        if total > MAX_TOTAL_STATE_UPDATES:
            raise ValueError(f"simulation exceeds {MAX_TOTAL_STATE_UPDATES} total state updates")


async def simulate_timeline(
    engine: Any,
    timeline: list[dict[str, Any]],
    *,
    reconciliation_options: dict[str, Any] | None = None,
    selector_memberships: list[dict[str, Any]] | None = None,
    semantic_metadata: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Evaluate Rules and Reconciliation over a deterministic timeline."""
    options = reconciliation_options or {}
    resolver = _simulation_selector_resolver(engine, selector_memberships, semantic_metadata)
    engine.set_selector_resolver(resolver)
    validate_simulation_input(timeline, options, projected_rule_targets=len(engine.list_active_targets()), selector_memberships=selector_memberships, semantic_metadata=semantic_metadata)
    adapter = FakeAdapter()
    tracker = _NoOwnership()
    reconciler = _new_reconciler(options)
    steps = []
    for index, step in enumerate(timeline):
        pulses: set[str] = set()
        if step.get("advance_ms"):
            engine.advance_clock(step["advance_ms"])
        if "enabled" in step:
            engine.set_enabled(bool(step["enabled"]))
        for rule_id in step.get("pause_rule_ids", []):
            engine.set_rule_paused(rule_id, True)
        for rule_id in step.get("resume_rule_ids", []):
            engine.set_rule_paused(rule_id, False)
        if step.get("restart"):
            lifecycle = engine.export_lifecycle_records()
            reconciliation_state = reconciler.export_runtime_state(engine)
            previous = engine
            engine = Engine(
                clock_fn=lambda now=previous.now_ms(): now, selector_resolver=resolver
            )
            engine.load_rules(previous.loaded_rules(), target_policies=previous.target_policies())
            if previous._time_of_day is not None:
                engine._time_of_day = previous._time_of_day
            for key, value in previous.state.items():
                if isinstance(key, str) and "." in key:
                    entity_id, _separator, field = key.rpartition(".")
                    engine.update_state(entity_id, value, field=field)
            engine.import_lifecycle_records(lifecycle)
            reconciler = _new_reconciler(options)
            reconciler.restore_pending_withdraws(
                {"reconciliation": reconciliation_state}, now_ms=engine.now_ms()
            )
        if "time_of_day" in step:
            engine.set_time_of_day("simulation", clock=step["time_of_day"])
        adapter.reject = _rejections(step.get("reject_calls", False))
        changed_actual = []
        for entity_id, value in step.get("actual", {}).items():
            changed_actual.append(adapter.set_state(entity_id, value))
        for key, value in step.get("states", {}).items():
            entity_id, separator, field = key.rpartition(".")
            if separator:
                old_key = f"{entity_id}.{field}"
                if field == "state" and old_key in engine.state and engine.state[old_key] != value:
                    engine.update_state(entity_id, True, field="changed")
                    pulses.add(entity_id)
                engine.update_state(entity_id, value, field=field)
        engine.evaluate_all()
        events = []
        for state in changed_actual:
            events.extend(reconciler.on_state_delta(engine, state, tracker, engine.now_ms()))
        _promote(engine, events)
        tick_events = await reconciler.tick(engine, adapter, tracker, engine.now_ms())
        _promote(engine, tick_events)
        if any(event.kind == "drift_promoted" for event in tick_events):
            engine.evaluate_all()
        events.extend(tick_events)
        record = simulation_step(engine, index=index)
        targets = sorted(
            set(engine.list_active_targets())
            | set(reconciler.pending_withdraw_targets())
            | set(adapter.states)
        )
        if len(targets) > MAX_PROJECTED_TARGETS:
            raise ValueError(f"simulation may project at most {MAX_PROJECTED_TARGETS} Targets")
        simulated_effects = []
        for due in engine.due_effects():
            effect = engine.begin_effect_attempt(due.activation_id, due.effect_index)
            if effect is None:
                continue
            simulated_effects.append({
                "rule_id": effect.rule_id, "domain": effect.domain,
                "service": effect.service, "target": effect.target, "data": effect.data,
            })
            engine.acknowledge_effect(effect.activation_id, effect.effect_index)
        record.update(
            {
                "events": [
                    {
                        "kind": event.kind,
                        "target": event.target,
                        "details": _json_details(event.details),
                    }
                    for event in events
                ],
                "calls": list(adapter.calls),
                "effects": simulated_effects,
                "targets": [
                    target_projection(
                        engine,
                        target,
                        actual_state=adapter.get_state(target),
                        reconciliation=reconciler,
                    )
                    for target in targets
                ],
                "checkpoint": "restart" if step.get("restart") else None,
            }
        )
        adapter.calls.clear()
        steps.append(record)
        for entity_id in pulses:
            engine.update_state(entity_id, False, field="changed")
        if index % 10 == 9:
            await asyncio.sleep(0)
    return steps


def _new_reconciler(options: dict[str, Any]) -> Reconciliation:
    return Reconciliation(
        drift_override_ttl_ms=int(options.get("drift_override_ttl_ms", 300_000)),
        drift_confirmation_ms=int(options.get("drift_confirmation_ms", 1_500)),
        service_failure_backoff_ms=int(options.get("service_failure_backoff_ms", 1_000)),
        drift_transition_grace_ms=int(options.get("drift_transition_grace_ms", 2_000)),
        service_failure_backoff_max_ms=int(options["service_failure_backoff_max_ms"])
        if "service_failure_backoff_max_ms" in options
        else None,
    )


def _promote(engine: Any, events: list[Any]) -> None:
    for event in events:
        if event.kind == "drift_promoted":
            engine.emit_user_intent(**event.details)


def _rejections(value: Any) -> bool | set[str]:
    return set(value) if isinstance(value, list) else bool(value)


def _json_details(details: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in details.items() if key != "state"}


def _selector_key(selector: Any) -> tuple[Any, ...]:
    return (selector.domain, selector.area, selector.label, getattr(selector, "device", None), getattr(selector, "entity", None), getattr(selector, "purpose", None))


def _validate_selector_memberships(value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, list) or len(value) > MAX_SELECTOR_MEMBERSHIPS:
        raise ValueError(f"`selectors` must be a list of at most {MAX_SELECTOR_MEMBERSHIPS} memberships")
    total_targets = 0
    seen = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict) or set(item) != {"selector", "targets"}:
            raise ValueError(f"selectors[{index}] must contain exactly `selector` and `targets`")
        selector = item["selector"]
        if not isinstance(selector, dict) or set(selector) - {"domain", "area", "label", "device", "entity", "purpose"} or not selector:
            raise ValueError(f"selectors[{index}].selector contains unsupported filters")
        if not all(isinstance(entry, str) and entry for entry in selector.values()):
            raise ValueError(f"selectors[{index}].selector values must be non-empty strings")
        targets = item["targets"]
        if not isinstance(targets, list) or not all(
            isinstance(target, str) and "." in target for target in targets
        ):
            raise ValueError(f"selectors[{index}].targets must be a list of Target IDs")
        key = tuple(selector.get(name) for name in ("domain", "area", "label", "device", "entity", "purpose"))
        if key in seen:
            raise ValueError(f"selectors[{index}] duplicates a selector membership")
        seen.add(key)
        total_targets += len(set(targets))
    if total_targets > MAX_PROJECTED_TARGETS:
        raise ValueError(f"selector memberships may expand to at most {MAX_PROJECTED_TARGETS} Targets")


def _validate_semantic_metadata(value: Any) -> None:
    if value is None:
        return
    allowed = {"entity_id", "area", "device", "device_class", "original_device_class"}
    if not isinstance(value, list) or len(value) > MAX_PROJECTED_TARGETS:
        raise ValueError("`semantic_metadata` must be a bounded list")
    seen_entity_ids: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict) or set(item) - allowed or not isinstance(item.get("entity_id"), str):
            raise ValueError(f"semantic_metadata[{index}] is invalid")
        entity_id = item["entity_id"]
        if "." not in entity_id or any(
            field in item and (not isinstance(item[field], str) or not item[field])
            for field in allowed - {"entity_id"}
        ):
            raise ValueError(f"semantic_metadata[{index}] fields must be non-empty strings")
        if entity_id in seen_entity_ids:
            raise ValueError(f"semantic_metadata[{index}] duplicates an entity ID")
        seen_entity_ids.add(entity_id)


def _simulation_selector_resolver(engine: Any, memberships: Any, semantic_metadata: Any = None):
    _validate_selector_memberships(memberships)
    required = {
        _selector_key(selector)
        for rule in engine.loaded_rules()
        for selector in (*rule.intent_selectors, *rule.observe_selectors, *(group.selector for group in getattr(rule, "observation_groups", ())), *(group.selector for group in getattr(rule, "hold_observation_groups", ())), *(group.selector for group in getattr(rule, "hold_until_observation_groups", ())))
    }
    registry = {
        tuple(item["selector"].get(name) for name in ("domain", "area", "label", "device", "entity", "purpose")): tuple(sorted(set(item["targets"])))
        for item in memberships or []
    }
    purpose_classes = {"motion": ("binary_sensor", "motion"), "occupancy": ("binary_sensor", "occupancy"), "door": ("binary_sensor", "door"), "window": ("binary_sensor", "window"), "moisture": ("binary_sensor", "moisture"), "temperature": ("sensor", "temperature"), "illuminance": ("sensor", "illuminance"), "power": ("sensor", "power")}
    for key in required - set(registry):
        domain, area, label, device, entity, purpose = key
        if purpose and semantic_metadata is not None and label is None:
            expected_domain, expected_class = purpose_classes[purpose]
            registry[key] = tuple(sorted(item["entity_id"] for item in semantic_metadata if item["entity_id"].partition(".")[0] == expected_domain and (item.get("device_class") or item.get("original_device_class")) == expected_class and (area is None or item.get("area") == area) and (device is None or item.get("device") == device) and (entity is None or item["entity_id"] == entity)))
    missing = required - set(registry)
    if missing:
        formatted = [dict(zip(("domain", "area", "label", "device", "entity", "purpose"), key, strict=True)) for key in sorted(missing, key=repr)]
        formatted = [{name: value for name, value in item.items() if value is not None} for item in formatted]
        raise ValueError(f"Missing simulated selector memberships: {formatted}")

    def resolve(selector: IntentSelector | ObserveSelector) -> list[str]:
        targets = registry[_selector_key(selector)]
        return [target for target in targets if target not in selector.exclude]

    return resolve
