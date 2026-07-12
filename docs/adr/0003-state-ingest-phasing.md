# State ingest lands in two phases: targeted set first, full subscriptions after selectors are solved

Phase 2 is implemented. Selector membership is resolved eagerly from registry metadata and cached by selector plus registry generation. Registry changes reconcile the targeted ingest set, and stable ticks no longer scan Home Assistant's complete state machine.

We collapse the dual state-sync paths (the all-entity 100ms poll at `custom_components/intentional/__init__.py:534` + the raw `state_changed` listener) into a single ingest module scoped to a relevant-entity set. We deliberately phase it: phase 1 builds the set from the three tractable inputs (rule-referenced entities via an AST collector, managed targets, active overrides) and scopes the poll to that set; selector-matched entities stay on the all-entity poll. Phase 2 adds eager selector resolution against the HA entity registry, kills the poll, and moves to `async_track_state_change_event` subscriptions.

## Considered options

- **Full §9 in one cut.** Build targeted subscriptions for all four inputs (rule-referenced, managed, overrides, selector-matched) immediately. Rejected: selectors resolve lazily during evaluation via an injected `selector_resolver` callback that hits the entity registry (`__init__.py:244`); eager resolution plus registry-change re-subscription is the "easy to get subtly wrong" risk the manual-override stability plan §9 explicitly defers. Doing it in one cut couples the valuable testable part (the set) to the risky part (selector timing).
- **Phased** (chosen). Phase 1 delivers the set module and the AST collector (the value, the test surface) with selectors covered by the existing poll. Phase 2 takes on selector eager-resolution and removes the poll. The set module doesn't change between phases; only the delivery mechanism and the selector input do.

## Consequences

- One ingest module (set composition + delivery + state-writing + pulse-arming) plus one pure AST collector function (`referenced_entities(rules)`) living beside the rule AST in `when_parser.py`/`rule_model.py`. No separate "planner" module in phase 1 — the union `collector(rules) ∪ list_active_targets() ∪ override_targets` is a one-liner that fails the deletion test as its own module. The planner earns its keep only in phase 2 when selector eager-resolution gives it real logic.
- Static input (rule AST) recomputed per reload and cached; dynamic inputs (active targets, overrides) queried per-tick as they already are. No invalidation machinery.
- The poll is scoped, not removed, in phase 1 — a future reader seeing both a set module and a residual poll should consult this ADR before "fixing" the poll or re-suggesting full subscriptions.
- After this candidate, `ha_adapter.py`'s residue (the state-sync pulses) has a proper home in the ingest module; the file's six-concern history is fully resolved across candidates 1, 2, and 4.
