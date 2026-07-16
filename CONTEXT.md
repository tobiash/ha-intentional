# ha-intentional

Declarative, composable, intent-based automation for Home Assistant. Rules observe home facts, produce durable desired state, and the engine reconciles Home Assistant entities toward that state.

## Language

**Rule**:
An authored declaration of a `while` situation and zero or more outputs: durable `intent`s, durable `alert`s, or one-shot `effect`s, with optional retention (`hold`).
_Avoid_: automation, trigger.

**Authored Rule**:
The declaration-level aggregate that owns one observation, its **Alert**s and **Effect**s, and the expanded engine Rules that produce **Intent**s for individual **Target**s.
_Avoid_: expanded rule (one implementation product of the aggregate), automation.

**Intent**:
Durable desired state for one or more **Target**s, produced by an active **Rule**.
_Avoid_: command, action (those imply one-shot).

**Effect**:
A one-shot service call fired when a **Rule** activates; not durable state. Distinct from **Intent**.
_Avoid_: action, side-effect (too generic).

**Alert**:
A durable assertion that an observed situation requires attention, produced by a **Rule** independently of **Intent** reconciliation and notification delivery.
_Avoid_: notification (a delivery concerning an Alert), alarm (a physical or audible Target may instead be controlled by an Intent).

**Alert instance**:
One uninterrupted occurrence of an **Alert**, distinct from earlier occurrences of the same situation.
_Avoid_: incident (implies a separate operational workflow), recurrence (describes the transition, not the occurrence).

**Notification**:
A rendered delivery concerning one or more **Alert instance**s, produced according to alerting policy independently of the Alerts' lifecycle.
_Avoid_: Alert (the assertion being reported), Effect (an authored one-shot Rule output).

**Receiver**:
A configured named set of one or more **Receiver destination**s selected by alerting policy.
_Avoid_: channel (ambiguous with transport capabilities), notifier (implies a component rather than a destination).

**Receiver destination**:
A typed delivery endpoint within a **Receiver**, backed by an allowed Home Assistant notification capability.
_Avoid_: Receiver (the named set selected by routing), service (only one implementation detail of delivery).

**Silence**:
A temporary, attributed record, bound either to one **Alert instance** or to label matchers, that suppresses matching **Notification**s without changing Alert lifecycle.
_Avoid_: mute (reserved for recurring configured policy), acknowledgment (records that a person has seen one Alert instance).

**Mute interval**:
A recurring configured time window that suppresses **Notification**s without changing matching **Alert instance**s' lifecycle.
_Avoid_: Silence (temporary operational record), quiet hours (one possible use rather than the general concept).

**Inhibition**:
Suppression of **Notification**s for a target **Alert instance** while a configured, causally stronger source Alert is firing.
_Avoid_: Silence (user-created and time-bounded), grouping (combines deliveries rather than suppressing them).

**Acknowledgment**:
An attributed record that a person has seen one **Alert instance**, suppressing its pending and further ordinary **Notification**s without resolving it; staleness and severity escalation have explicit exceptions.
_Avoid_: resolve, dismiss (both imply the underlying situation ended or was removed).

**Target**:
A Home Assistant entity whose state a **Rule**'s **Intent** aims to control.
_Avoid_: device, entity (too broad — every HA entity is an entity).

**Authority**:
The precedence tier of an **Intent**: `sensor < automation < user`. Within a tier, confidence then recency break ties.
_Avoid_: priority (overloaded).

**Service plan**:
The concrete `(domain, service, data)` tuple(s) used to move a **Target** toward a resolved **Intent**.
_Avoid_: service call (that's the act, not the plan).

**Drift**:
Observed divergence between a **Target**'s actual Home Assistant state and the **Service plan** last applied by the engine.
_Avoid_: mismatch, deviation.

**Manual override**:
A `user`-authority **Intent** created from confirmed **Drift** (or from the `intentional.fire` service), with a TTL.
_Avoid_: user intent (too broad — any user-authority intent qualifies).

**Reconciliation**:
The loop that compares resolved **Intent**s against actual state, decides which **Service plan**s to call, what to suppress, and what **Drift** to promote as a **Manual override**.
_Avoid_: apply loop, sync.

**Tick runtime**:
The Home Assistant integration state that drives periodic **Rule** evaluation and **Reconciliation**: scene activation memory, authored-rule activity memory, state-change pulse draining, and liveness counters.
_Avoid_: scheduler (too broad), health check (only one output of the runtime).

**State-change pulse**:
A one-cycle observation that a Home Assistant entity changed (`changed`, and `triggered` for event entities). Pulses are drained only after they have been visible to a **Tick runtime** cycle.
_Avoid_: trigger (overlaps HA automation language), event (too broad).

## Relationships

- An **Authored Rule** owns one observation, zero or more **Alert**s and **Effect**s, and zero or more expanded engine Rules that produce **Intent**s for individual **Target**s.
- An **Authored Rule**'s Alert observation is evaluated once at authored level, never once per expanded Intent rule.
- A **Rule** produces zero or more **Intent**s when active and may produce zero or more **Alert**s independently of its **Intent**s and **Effect**s.
- An **Alert** has at most one active **Alert instance**; resolving and later recurring creates a new instance.
- An **Alert instance** may remain pending for its own configured duration before firing, independently of when the producing **Rule** and its other outputs become active.
- A state-observed **Alert instance** resolves after a successful inactive observation; a pulse-observed Alert instance resolves after its configured duration following the latest pulse.
- A **Rule**'s Intent retention does not retain its Alerts.
- Alerting policy routes firing **Alert instance**s to zero or more named **Receiver**s; each **Receiver destination** may receive repeated **Notification**s without changing Alert state.
- **Silence**, **Mute interval**, **Inhibition**, and **Acknowledgment** suppress some Notifications without resolving an **Alert instance**.
- An **Intent** addresses one or more **Target**s with field-level values.
- A **Service plan** is derived from a resolved **Intent** for a single **Target**.
- **Drift** on a managed **Target** may become a **Manual override** through **Reconciliation**.
- A **Manual override** is an **Intent** at `user` authority and overrides all `automation`/`sensor` intents on its **Target** until its TTL expires.
- **Reconciliation** reads resolved **Intent**s and emits **Service plan**s and **Manual override** promotions; it does not evaluate **Rule**s.
- The **Tick runtime** owns the cadence and liveness of **Rule** evaluation plus **Reconciliation**; it does not decide **Service plan** policy.

## Example dialogue

> **Dev:** "When the living-room light is toggled at the wall, does that produce an **Intent**?"
> **Domain expert:** "Not directly. It produces **Drift** — actual state no longer matches the last **Service plan**. **Reconciliation** confirms the drift and may promote it to a **Manual override**, which *is* an **Intent** at `user` authority."
> **Dev:** "And a dashboard button that calls `intentional.fire`?"
> **Domain expert:** "That's a **Manual override** too, just created directly rather than through drift confirmation."
> **Dev:** "If a freezer stays too warm, is the warning message an **Effect**?"
> **Domain expert:** "No. The **Rule** produces an **Alert** that remains firing with the situation. Alerting policy may produce several **Notification**s for that Alert instance; a one-shot unrelated service call would be an Effect."

## Flagged ambiguities

- `custom_components/intentional/_engine/reconciliation.py` now performs **Reconciliation** policy, but the **Tick runtime** cadence and Home Assistant state ingest still live in the integration layer. Avoid moving state ingest into **Reconciliation** unless ADR-0001 is reopened.
