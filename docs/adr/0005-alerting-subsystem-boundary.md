# Alerting is a sibling subsystem to intents and effects

An **Alert** is durable attention state, not desired Target state or a one-shot service call. Rules emit Alert observations to a pure alerting core that owns Alert-instance lifecycle, routing, grouping, suppression, notification scheduling, projections, and restart-safe state transitions. Alerting has its own versioned runtime store and deadline-driven runtime; the Home Assistant integration owns persistence I/O, authentication, Alert entities, timers, mobile-action events, and restricted Receiver adapters.

## Considered options

- Modeling Alerts as **Intent**s was rejected because Authority, composition, Reconciliation, Drift, and Manual overrides only have meaning for desired Target state.
- Modeling Notifications as **Effect**s was rejected because Effect activation and delivery completion cannot represent pending/firing/resolved Alert instances, grouping, repeats, Silences, inhibition, or human acknowledgment. Alert delivery may share a narrow rendered-service adapter and retry utilities with Effects, but not their records or queue.
- Putting alerting policy in `Engine` or `Reconciliation` was rejected because notification deadlines and failures must evolve independently of Rule evaluation and Target convergence. Reconciliation may return events that a later system-Alert producer consumes, but it does not manage Alerts.
- Delegating alerting entirely to an external Alertmanager was rejected as the primary architecture because native Home Assistant Receivers, identities, mobile actions, simulation, and operation without external infrastructure are product requirements. An external exporter may be added later.

## Consequences

- A Rule may independently produce Intents, Alerts, and Effects.
- Rule evaluation exposes explicit Alert observations without giving the alerting core access to Engine internals.
- Alerting persistence failure degrades and pauses new Notification dispatch without blocking Rule evaluation or Reconciliation.
- Notification obligations are persisted before at-least-once delivery through Receiver adapters.
- Routing configuration and runtime alerting state are versioned independently from the Rule document and existing Intent/Effect lifecycle state.
