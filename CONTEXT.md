# ha-intentional

Declarative, composable, intent-based automation for Home Assistant. Rules observe home facts, produce durable desired state, and the engine reconciles Home Assistant entities toward that state.

## Language

**Rule**:
An authored declaration of a `while` situation, a durable `intent`, and optional retention (`hold`) or one-shot `effect`s.
_Avoid_: automation, trigger.

**Intent**:
Durable desired state for one or more **Target**s, produced by an active **Rule**.
_Avoid_: command, action (those imply one-shot).

**Effect**:
A one-shot service call fired when a **Rule** activates; not durable state. Distinct from **Intent**.
_Avoid_: action, side-effect (too generic).

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

- A **Rule** produces zero or more **Intent**s when active.
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

## Flagged ambiguities

- `custom_components/intentional/_engine/reconciliation.py` now performs **Reconciliation** policy, but the **Tick runtime** cadence and Home Assistant state ingest still live in the integration layer. Avoid moving state ingest into **Reconciliation** unless ADR-0001 is reopened.
