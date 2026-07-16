# Alerting implementation plan

## Outcome

Intentional Rules can produce durable Alerts independently of Intents and Effects. A pure alerting subsystem tracks Alert instances, applies Alertmanager-inspired routing and suppression policy, and plans restart-safe Notifications through restricted Home Assistant Receivers.

Alerting remains private and disabled by default while the slices below are developed. It becomes public only after lifecycle, durable delivery, routing, grouping, repeats, suppression, inhibition, acknowledgment, mobile actions, simulation, HA entities, and operational UI pass the release gate.

ADR-0005 fixes the subsystem boundary. This plan records policy and sequencing that can evolve without reopening that boundary.

## Domain contract

| Concept | Contract |
| --- | --- |
| Alert | Durable assertion that an observed situation requires attention. A sibling Rule output to Intent and Effect. |
| Alert definition | Authored Alert declaration identified in v1 by Rule ID plus Alert name. |
| Alert instance | One uninterrupted occurrence with an opaque UUID. A recurrence after resolution creates a new instance. |
| Notification | Rendered delivery concerning one or more Alert instances. It is not an Effect. |
| Authored Rule | Declaration-level aggregate owning one observation, Alerts, Effects, and expanded Intent rules. |
| Receiver | Configured named set of Receiver destinations selected by routing. |
| Receiver destination | Typed delivery endpoint in a Receiver, backed by an allowed HA notification capability. |
| Acknowledgment | Shared human record that one firing instance was seen. It suppresses pending and further ordinary delivery but does not resolve the Alert; staleness and severity escalation are explicit exceptions. |
| Silence | Temporary attributed suppression, bound to one instance or label matchers. |
| Mute interval | Recurring configured delivery-suppression window. |
| Inhibition | Delivery suppression of a symptom Alert while a stronger causal Alert is firing. |

Canonical lifecycle:

```text
inactive -> pending -> firing -> resolved
                         |
                         +-- evaluation_status: current | grace | stale
```

State-observed Alerts resolve only after a successful inactive observation. Pulse-observed Alerts fire immediately and resolve at `last_pulse + resolve_after`; another pulse before resolution extends the same instance. Rule Intent retention does not retain Alerts.

## Authored Rule shape

`alert` accepts one mapping or a list. Alert-only Rules are valid. One Rule may produce multiple Alerts, but names must be unique within the authored Rule.

```yaml
- id: freezer-too-warm
  while:
    sensor.freezer_temperature:
      above: -10

  intent:
    switch.freezer_boost:
      state: on

  alert:
    name: FreezerTemperatureHigh
    severity: critical
    for: 10m
    labels:
      area: kitchen
      category: appliance
    annotations:
      summary: Freezer is too warm
      description: >-
        Current temperature is {{ states('sensor.freezer_temperature') }} degrees.
```

Required fields:

| Field | Rule |
| --- | --- |
| `name` | Stable within the Rule and suitable as machine identity. |
| `severity` | One of `info`, `warning`, or `critical`. |
| `annotations.summary` | Bounded human-readable fallback text. |

Optional fields:

| Field | Rule |
| --- | --- |
| `for` | Total pending duration from known condition truth. If omitted, inherit Rule `for`; it is not additive. |
| `labels` | Static routing/grouping classification. Templates are rejected. |
| `annotations` | Bounded templated presentation context. |
| `stale_after` | Override the global two-minute unknown-evaluation grace. |
| `resolve_after` | Required for pulse observations and invalid for state observations. |
| `escalations` | At most three timed severity steps, each with total `after` measured from `active_at`. Times and severities must strictly increase. |

Example escalation:

```yaml
escalations:
  - after: 30m
    severity: warning
  - after: 2h
    severity: critical
```

An escalation-policy edit applies against the instance's original `active_at`; an already-due new step applies immediately. It never resets an Alert instance or pending clock.

Reserved generated labels are `alertname`, `rule_id`, `severity`, `integration`, and future identity dimensions. Authored labels never define v1 identity. Future explicit selector expansion adds bounded generated identity dimensions such as `entity_id`; it is not part of v1.

Authored limits are hard validation errors, never silent truncation:

| Scope | Limit |
| --- | --- |
| Alerts | 16 per Authored Rule; 256 total published definitions. |
| Alert content | 32 labels and 16 annotations; each key at most 64 UTF-8 bytes; each label value and `summary` at most 256 bytes; each other annotation at most 4 KiB; one fully rendered Alert at most 16 KiB. |
| Policy | 256 routes; 64 Receivers; 8 destinations per Receiver; 64 active intervals, 64 mute intervals, and 64 inhibition rules; 16 matchers per route, Silence, or inhibition matcher side. |
| Group delivery | At most 20 rendered members and 16 KiB total rendered Notification payload. |

Validation measures encoded UTF-8 and rejects authored or rendered overflow before publication/planning. Only presentation may truncate: group rendering includes deterministic severity/age order plus explicit total and omitted counts, and UI previews mark every truncated field. Identity, matching, lifecycle, routing, persisted source, and audit data are never truncated into validity.

Rule pause, label pause, global disable, blocking, definition removal, and identity rename close active instances with explicit non-recovery reasons. Resuming or unblocking starts a new instance if the situation remains active. Existing notify Effects remain valid and receive no warning or automatic migration.

## Observation contract

Current observation evaluators collapse missing evidence and inactive conditions into booleans. Preserve that legacy boolean result byte-for-byte and add parallel possibility-based three-valued evidence for Alerts:

```python
ObservationResult(
    value: bool,
    evidence: "true" | "false" | "unknown",
    reason: str | None,
)
```

`evidence` denotes possible truth sets: `true={T}`, `false={F}`, and `unknown={T,F}`. Rules continue to use unchanged `value` for existing Intent and Effect behavior. Alert observations use `evidence`; no coercion from `unknown` to false is allowed. Explicit predicates such as `state: unknown` and `state: unavailable` are known true or known false because the unavailable value is the evidence being tested. Incidental unavailable/missing operands and runtime evaluation failures produce unknown.

The truth tables are normative:

| `and` | true | false | unknown |
| --- | --- | --- | --- |
| true | true | false | unknown |
| false | false | false | false |
| unknown | unknown | false | unknown |

| `or` | true | false | unknown |
| --- | --- | --- | --- |
| true | true | true | true |
| false | true | false | unknown |
| unknown | true | unknown | unknown |

| input | `not` |
| --- | --- |
| true | false |
| false | true |
| unknown | unknown |

Comparisons return known true/false when all operands are known and unknown otherwise, except explicit unavailable/unknown checks as above. Selector folds use the same possibility algebra:

| Selector fold | true when | false when | unknown when |
| --- | --- | --- | --- |
| `any` | At least one member is true. | Every member is false. | No member is true and at least one is unknown. |
| `all` | Every member is true. | At least one member is false. | No member is false and at least one is unknown. |
| `none` | Every member is false. | At least one member is true. | No member is true and at least one is unknown. |

A complete empty selector is known: `any=false`, `all=true`, and `none=true`. A selector whose membership cannot be established is unknown, not an empty selector. Hysteresis applies only to known numeric evidence: a known crossing updates the stored latch, a known value in the dead band returns the prior known latch, and unavailable/missing input returns unknown without changing the latch. On first evaluation in the dead band with no latch, evidence is unknown. The legacy boolean and its hysteresis behavior remain unchanged.

Introduce a first-class `AuthoredRule` aggregate before Target expansion. It owns the raw `while` observation, Alert declarations, Effects, and expanded Intent rules. The observation is evaluated once at authored level; expansion must not duplicate Alert evaluation or emission.

Rule evaluation emits authored-level Alert observations instead of exposing Engine internals:

```python
AlertObservation(
    definition_key: str,
    status: "active" | "inactive" | "unknown" | "pulse",
    observed_at: datetime,
    raw_condition_state: "true" | "false" | "unknown",
    first_true_at: datetime | None,
    effective_for: timedelta | None,
    pulse_id: str | None,
    source_timestamp: datetime | None,
    labels: Mapping[str, str],
    annotations: Mapping[str, str],
    explanation: ObservationExplanation,
)
```

Emission occurs once per authored Alert declaration. `first_true_at` belongs to the authored condition, while `effective_for` is snapshotted for each Alert when pending starts. A dynamic `for` expression is evaluated once at pending start; later sensor changes do not move the deadline. A definition edit recomputes the deadline from the original `active_at` using the edited duration rather than restarting from edit time.

For pulse observations, HA ingest assigns a stable `pulse_id` and source timestamp before Rule evaluation. Persist the highest consumed ID or equivalent bounded idempotency state per pulse source. Retries, duplicate observations, reloads, and restarts must not extend an existing instance or recreate a resolved instance; only a newly ingested ID may do so.

Annotation rendering failure does not change observation truth. Retain the last valid annotations or safe summary fallback, mark presentation degraded, and expose alerting health.

## Alerting policy document

Store alerting policy as a separate generation-controlled YAML document with its own history, validation, preview, rollback, and Draft -> Checked -> Reviewed -> Published workflow.

```yaml
route:
  id: root
  receiver: household
  group_by: [alertname, area]
  group_wait: 30s
  group_interval: 5m
  repeat_interval: 4h
  send_resolved: true
  routes:
    - id: critical-mobile
      matchers:
        - severity="critical"
      receiver: urgent-mobile
      active_intervals: [occupied-hours]
      mute_intervals: [overnight]
      group_wait: 0s
      repeat_interval: 30m
      continue: true

receivers:
  - name: household
    destinations:
      - type: persistent_notification

  - name: urgent-mobile
    destinations:
      - type: notify_entity
        entity_id: notify.alice_phone

active_intervals:
  - name: occupied-hours
    timezone: Europe/Amsterdam
    weekdays: [mon, tue, wed, thu, fri]
    times:
      - start: "08:00"
        end: "18:00"
mute_intervals:
  - name: overnight
    timezone: Europe/Amsterdam
    weekdays: [mon, tue, wed, thu, fri, sat, sun]
    times:
      - start: "22:00"
        end: "07:00"
inhibit_rules:
  - source_matchers: ['alertname="GatewayDown"']
    target_matchers: ['category="connectivity"']
    equal: [area]
```

Routes reference named intervals with `active_intervals: [occupied-hours]` and `mute_intervals: [overnight]`. An interval has a unique name, optional IANA `timezone` (HA timezone by default), one or more `weekdays`, and one or more local half-open `times` (`start` inclusive, `end` exclusive). If `end <= start`, the range runs overnight into the next day; the weekday names the day on which it starts. For example, Monday `22:00`-`07:00` is active Monday 22:00 through Tuesday 07:00, but not Tuesday 22:00 unless Tuesday is also listed. During an active interval a route is eligible only when at least one referenced active interval is open; any open referenced mute interval suppresses it. With the shown policy, Monday 17:00 is eligible, Monday 23:00 is muted, and Saturday 12:00 is inactive. DST resolution is deterministic and included in preview.

Policy rules:

| Concern | Contract |
| --- | --- |
| Root | A configured document requires a valid fallback Receiver. With no document, Alerts remain active and `unrouted`. |
| Traversal | All policy fields inherit. A matching child branch replaces its inherited Receiver if it declares one. Traverse matching children recursively; each branch delivers only at its deepest matching route. Without `continue`, stop after that matching sibling branch; with `continue: true`, evaluate later siblings at that same level. Nested traversal applies these rules independently at every level. If no child matches, the current route delivers. |
| Route identity | Every child has a stable ID; array position and full configuration hashes are not identity. |
| Matchers | Shared `=`, `!=`, anchored `=~`, and anchored `!~` operators. Regex patterns are at most 256 UTF-8 bytes and use a documented RE2-compatible, linear-time subset with no backreferences or lookaround plus bounded compilation. There is no unsafe-engine fallback; if a safe regex engine is unavailable, only exact and negative-exact matchers are accepted. Publication is rejected when current-state preview projects more than the runtime group/obligation caps or a greater than fourfold group/fan-out increase without an explicit administrator spike confirmation. |
| Grouping | Occurs after routing and is keyed by route ID, Receiver revision, and selected labels. |
| Defaults | `group_by: [alertname, area]`, `group_wait: 30s`, `group_interval: 5m`, `repeat_interval: 4h`, `send_resolved: true`. |
| Safety | Nonzero group wait is at least one second; group and repeat intervals are at least one minute. Urgent group wait may be zero. |
| Intervals | Named active and mute intervals use HA timezone by default and permit explicit IANA timezone overrides. |
| Receiver edit | Destination changes create a new effective Receiver revision; cosmetic template edits do not. |
| Duplicate fan-out | Deduplicate the same Receiver destination reached through multiple branches by first traversal occurrence. `allow_duplicate: true` opts that destination into duplicates and emits a validation/preview warning. |

Every route policy field inherits unless explicitly overridden: Receiver, grouping fields, `send_resolved`, active/mute interval references, and `continue`; matcher constraints accumulate down the branch. An inhibition rule has exactly `source_matchers`, `target_matchers`, and `equal`; both matcher lists are required and bounded, `equal` is a bounded label-key list, and source and target matching plus equality determine inhibition.

Publishing policy and Rules is independent. Policy preview evaluates current active Alerts and synthetic label sets, shows routes, groups, fan-out, suppression, cleanup obligations for removed/edited Receiver destinations, and newly eligible fan-out. Deterministic policy failure may roll back automatically; Receiver delivery failure may not. Receiver removal or destination-affecting edit retains the previous revision and creates durable cleanup obligations for destinations that accepted replaceable content. Keep old Receiver revisions until every cleanup is accepted or dead-lettered.

## Runtime model

Create a pure `intentional.alerting` sibling package. It owns lifecycle, grouping, routing, suppression, scheduling decisions, projections, retention decisions, and state import/export.

Create one HA alerting coordinator as the sole command owner. A serialized command stream handles observations, time advancement, policy/definition publication, acknowledgment, Silences, capability consumption, cancellation, and acceptance results against one runtime generation. No timer, API handler, dispatcher callback, or reload path may mutate runtime state directly.

The coordinator owns the versioned Store, durability writer, deadline timer, API authentication, entity publication, mobile-action event subscription, and Receiver adapters. Every state-changing command force-persists one atomic generation commit before external Receiver calls or API success. Capability consumption plus acknowledgment/Silence mutation and resulting obligation cancellations are one atomic commit. Persistence failure returns API failure and dispatches nothing.

The alerting runtime schedules the next durable deadline and calls a pure `advance(now)` operation. It does not depend on the Reconciliation tick for pending, grouping, retry, interval, Silence-expiry, or repeat deadlines.

Startup establishes a per-definition evidence barrier. After targeted HA state synchronization, evaluate every loaded state-observed definition once and commit its first post-startup observation, whether known or unknown. Known evidence permits condition-dependent pending/firing transitions and catch-up delivery. Unknown evidence advances grace/staleness but blocks those condition-dependent transitions and ordinary catch-up until evidence becomes known. Persisted pulse instances need no new observation: their durable `resolve_after`, grouping, and delivery deadlines resume after coordinator/Receiver startup, while only newly ingested `pulse_id`s may activate or extend them. Housekeeping deadlines, stale transitions, and pulse resolution continue across the barrier; overdue user-visible work is planned only when its required evidence is confirmed.

Tick orchestration becomes:

```text
ingest HA state
evaluate Rules
publish Alert observations to alerting runtime
persist Alert transitions and Notification obligations
run Reconciliation independently
dispatch durable Effects independently
dispatch durable Notifications independently
publish world/entities/health
```

Reconciliation may later return events consumed by a system-Alert producer. It never imports or calls alerting policy.

## Persisted model

Use a separate versioned Store, for example `intentional_alerting_<entry_id>_v1`. Store current snapshots plus bounded append-like audit records, not a fully event-sourced model.

Persist:

- Alert definitions known to runtime and their revisions.
- Pending, firing, and retained resolved Alert instances.
- Observation freshness, transition reasons, timestamps, and bounded explanations.
- Current labels and latest valid annotations.
- One shared active acknowledgment plus immutable acknowledgment audit.
- User instance Silences and administrator matcher Silences.
- Notification groups, timing deadlines, and last accepted state.
- Immutable Notification obligations with identity and frozen rendered payload, separate append-only attempt records, and separate mutable current-status records for cancellation, supersession, acceptance, retry, and dead letter.
- Single-use mobile action capability records, consumed pulse IDs, and expiry.
- Alerting health, schema version, and retention cursors.

Default retention is 30 days with count bounds. Never prune active instances, live Silences, nonterminal obligations, active mobile capabilities, pulse deduplication state required for correctness, or deduplication state required by firing groups. Terminal dead letters are not undelivered obligations: their detailed records may be pruned under the bounds below while retained counters preserve operational totals.

Runtime caps are 1,024 live Notification groups and 2,048 nonterminal obligations. Overflow sets degraded health and leaves lifecycle and source observations intact; planning retries when capacity becomes available instead of dropping Alerts or delivery intent. Retain at most 10,000 audit records and 1,000 detailed terminal dead-letter records; dead letters are terminal and prunable, with per-destination/reason counters retained after detail pruning.

Persist each Notification obligation before dispatch. Store failure exposes current in-memory Alerts with `persistence_degraded`, but pauses new Notification dispatch and does not block Rule evaluation or Reconciliation. A dispatch attempt is accepted only when the HA service call returns successfully with `blocking=True`. A timeout or cancellation is uncertain, remains nonterminal, and retries with the same obligation identity and payload.

If runtime state cannot load or migrate, fail alerting delivery closed and mark Alert entities unavailable. Recovery is an explicit administrator reset that exports the failed payload, previews current active Alerts and resulting fan-out, and requires confirmation before creating a fresh generation.

## Lifecycle policy

State observations:

- First known active observation creates a pending or immediately firing instance.
- Pending duration uses wall-clock elapsed time and survives restart.
- Unknown evidence preserves lifecycle and enters a two-minute grace by default.
- Grace expiry marks evaluation stale without replacing pending/firing lifecycle state.
- The transition to stale is one material update per stale episode. It may bypass `group_interval`, but remains subject to Silence, mute interval, and inhibition; acknowledgment does not suppress this one stale update. It pauses ordinary repeats and stops the instance from acting as an inhibition source.
- Successful active evidence clears staleness. Existing acknowledgment remains.
- Successful inactive evidence resolves the instance.

Pulse observations:

- `for` is invalid.
- The first pulse creates an immediately firing instance.
- Another pulse extends resolution to `last_pulse + resolve_after`.
- Deadline expiry resolves the instance.
- A pulse after resolution creates a new instance UUID.

Definition and control changes:

| Change | Result |
| --- | --- |
| Condition, message, severity, or routing labels change with stable Rule ID/name | Preserve the instance and apply the new definition revision. |
| Severity increases | Material escalation; notify immediately and supersede acknowledgment. |
| Severity decreases | Preserve the instance; schedule a normal group update. |
| Rule ID or Alert name changes | Resolve old instance and potentially create a new one. |
| Alert/Rule removed | Resolve with `definition_removed`. |
| Rule paused or globally disabled | Resolve with `evaluation_paused` or `evaluation_disabled`. |
| Rule becomes blocked | Resolve with `rule_blocked`; use inhibition when Alert truth should remain visible. |

Timed escalation is implemented after the core lifecycle but before public release. Explicit escalation steps change severity on the same instance, route immediately, and supersede acknowledgment. A step is a material severity increase measured from original `active_at`; definition edits apply all newly due steps against that original time.

## Routing, grouping, and scheduling

Grouping is Receiver-specific and happens after route matching. Suppressed instances are omitted from rendered Notifications. Acknowledged firing instances may appear in a compact context section but never independently schedule repeats.

Timing behavior:

| Event | Scheduling rule |
| --- | --- |
| First eligible group | Wait `group_wait`; send nothing if all members resolve or become acknowledged first. |
| New firing/resolved member | Wait `group_interval`. |
| Severity escalation | Bypass `group_interval` and notify immediately. |
| First stale transition in an episode | Bypass `group_interval` and notify immediately if not otherwise suppressed. |
| Unchanged firing group | Repeat after `repeat_interval`; `never` is valid. |
| Silence/mute/inhibition ends, active interval begins, or acknowledgment is revoked | Re-evaluate after a fixed five-second debounce. |
| Routing adds a Receiver for active Alerts | Treat it as initially unnotified and apply its `group_wait`. |
| Restart with overdue deadlines | Confirm current evidence, then send at most one current catch-up per eligible group. |

Resolved messages go only to still-configured destinations that previously accepted a firing Notification and have `send_resolved` enabled. Do not send a standalone resolution for an instance that cleared during group wait or was never accepted by that destination.

Current Silence/mute policy suppresses user-visible resolved messages. Still perform idempotent transport cleanup such as persistent-notification dismissal or mobile-tag clearing. Do not send a delayed resolution after suppression expires.

Only first staleness and severity escalation bypass `group_interval`. Annotation edits, severity decreases, route/definition edits, ordinary state changes, and suppression release do not bypass it. `group_wait` remains applicable only to a destination's first eligible firing state; escalation and staleness do not invent acceptance history.

Large groups render a bounded severity/age-ordered page with total and omitted counts plus a panel link. Full group membership remains in authenticated audit state.

## Suppression and acknowledgment

Silences, mute intervals, inhibition, and acknowledgment affect Notification eligibility only.

The suppression matrix is normative:

| Message kind | Silence | Mute interval | Inhibition | Acknowledgment |
| --- | --- | --- | --- | --- |
| Initial/pending initial | Suppress | Suppress | Suppress | Suppress |
| Ordinary update/repeat/resolved visible message | Suppress | Suppress | Suppress | Suppress |
| First stale update per stale episode | Suppress while still matching | Suppress while still in window | Suppress while still inhibited | Send once |
| Severity escalation | Suppress while still matching | Suppress while still in window | Suppress while still inhibited | Supersede acknowledgment and send |

Silence, mute interval, and inhibition suppress every user-visible message, including stale and escalation, for as long as the instance still matches the suppression. Acknowledgment suppresses initial, repeat, normal update, and visible resolved delivery; it permits one stale update and is superseded by a severity escalation. Annotation edits, severity decreases, route edits, and definition edits never bypass any suppression. Staleness and escalation bypass only `group_interval`, not routing, active intervals, `group_wait` for never-accepted destinations, or the three non-acknowledgment suppressors. Transport cleanup remains non-visible and is never suppressed.

User instance Silence:

- Available to any authenticated HA user for a firing instance.
- Binds directly to that instance UUID and never covers recurrence.
- Maximum duration is 24 hours; mobile MVP creates a fixed one-hour Silence.
- Critical instances may be Silenced under the same bound.
- Requires actor and reason; mobile uses a generated Notification audit reason.

Administrator matcher Silence:

- Uses current labels and shared matcher syntax.
- May cover current and future matching instances.
- Requires start, expiry, actor, and reason; maximum duration is one year.
- Match-all requires explicit `match_all: true`, affected-Alert preview, and extra confirmation for critical Alerts.

Inhibition:

- Requires a firing, current source and a distinct target instance.
- Every `equal` label must exist on both source and target and have equal values.
- Acknowledging or Silencing the source does not stop inhibition.
- A source stops inhibiting after its evaluation becomes stale.
- Reject statically detectable self-inhibition and cycles; warn and explain dynamic cases that cannot be proven statically.

Acknowledgment:

- Available only while firing to any authenticated HA user.
- One shared active acknowledgment records actor, time, and optional comment; later attempts are idempotent.
- Suppresses pending initial, ordinary update, repeat, and visible resolved delivery across all Receivers; the suppression matrix defines stale and escalation exceptions.
- Does not expire; resolution, revocation, or severity escalation ends/supersedes it.
- Revocation makes the instance eligible after the five-second release debounce.
- Replaceable persistent/mobile Notifications remain visible and are updated as acknowledged; non-replaceable transports do not receive a separate acknowledgment message.

## Receiver and delivery contract

Initial Receiver adapters:

| Type | HA contract |
| --- | --- |
| Notify entity | `notify.send_message` targeting a configured `notify.*` entity. |
| Legacy notification action | An explicitly configured and allowlisted legacy notification action. |
| Persistent notification | Stable `notification_id` for create/update and dismissal. |

The legacy adapter accepts only a fixed, explicitly configured `notify.*` service/action that exists in the HA service registry at publication and dispatch. Its schema exposes typed message, title, target, and allowlisted mobile option fields only. Scripts, events, templates, dynamic service names, and arbitrary service data are rejected.

Receiver destinations use typed, adapter-specific options. Alert or annotation templates cannot select service, domain, target, mobile priority, or arbitrary service data. Mobile channel, sound, interruption level, and similar options are allowlisted and capability-validated. Unsupported optional capability is visible but does not fail basic message delivery.

Create one immutable obligation per Receiver destination. Its immutable identity binds runtime generation, obligation ID, Receiver revision/destination, group state/version, message kind, member instance/revision set, stable transport tag, and exact frozen rendered payload. Attempt number/time/result/error class lives in a separate append-only attempt record. Current status (`planned`, `in_flight`, `accepted`, `cancelled`, `superseded`, or `dead_lettered`) lives in a separate mutable projection record; retry never mutates obligation identity or payload.

A successful `blocking=True` HA call is `accepted`, not delivered/read. Timeout is uncertain and retries. Exactly-once delivery is impossible; provide at-least-once attempts with stable IDs/tags, eight attempts, bounded exponential backoff with jitter, and dead-letter visibility.

Delivery evolution:

- A dead-lettered destination may receive a new obligation on the next repeat cycle.
- Resolve before first acceptance cancels obsolete firing obligations and sends no resolution to that destination.
- A newer group state cancels an older undelivered update as `superseded` and creates one current immutable obligation.
- Destination-affecting Receiver edits cancel obsolete pending delivery and make active groups initially eligible for the new Receiver revision.
- Receiver removal or destination-affecting edit plans cleanup against the retained old revision and new fan-out against the new revision in the same committed generation; old cleanup does not delay eligible new delivery.
- Retry sends the exact frozen rendered payload. Later updates/repeats render current annotations.

Receiver templates render bounded Alert annotations into safe single/group content. Rules provide context; Receivers own presentation. Diagnostics contain IDs, timing, route/Receiver metadata, and redacted error classes, never rendered messages by default.

## Mobile actions

Single-instance mobile Notifications contain:

- Acknowledge.
- Silence 1h.
- Open Alert.

Grouped Notifications contain only Open Alerts. Bulk acknowledgment or Silence requires explicit panel selection.

Each mutating action carries an HMAC-derived single-use token. Persist only a cryptographically random capability record ID plus its config-entry, instance UUID, operation, Receiver destination, expiry, and consumed state. The integration-wide HMAC secret is stored separately from alerting state. Derive the raw token from the record ID and binding, compare in constant time, and never persist, log, diagnose, export, or expose the raw token. Tokens remain valid for at most 24 hours and only while the instance is firing. Repeats issue fresh capability records.

The frozen action payload is deterministically rederived from the persisted capability record for every retry, so retrying the same obligation does not mint a new token. Consume atomically with the acknowledgment or Silence and all resulting obligation cancellations. Reject expiry, replay, wrong binding, or missing HA `context.user_id`; rejected missing-actor events leave the capability usable until expiry.

## APIs

Operational reads are available to authenticated users with redaction. Rule source, raw templates, Receiver configuration, delivery payloads/errors, broad Silence operations, simulation, reset, and policy mutation require administrators. Even administrator diagnostics, exports, audit, source views, and action-event logging redact HMAC secrets, raw mobile action tokens, action URLs/payloads containing tokens, and source fragments that contain token-bearing rendered content.

Initial endpoints:

```text
GET    /api/intentional/alerts
GET    /api/intentional/alerts/{instance_id}
GET    /api/intentional/alerting/status
POST   /api/intentional/alerts/{instance_id}/acknowledge
DELETE /api/intentional/alerts/{instance_id}/acknowledgment
POST   /api/intentional/alerts/{instance_id}/silence

GET    /api/intentional/silences
POST   /api/intentional/silences
DELETE /api/intentional/silences/{silence_id}

GET    /api/intentional/alerting/notifications
GET    /api/intentional/alerting/config
POST   /api/intentional/alerting/validate
POST   /api/intentional/alerting/preview
PUT    /api/intentional/alerting/config
GET    /api/intentional/alerting/history
POST   /api/intentional/alerting/rollback
POST   /api/intentional/alerting/simulate
POST   /api/intentional/alerting/test-receiver
POST   /api/intentional/alerting/reset
```

Every Alert explanation includes lifecycle reason, evidence freshness, next deadline, definition revision, current labels, matched route, group, destination status, suppression reasons, acknowledgment, last acceptance, next repeat, and alerting health.

## Home Assistant entities

Create one lifecycle sensor per Alert definition with stable unique ID derived from config entry, Rule ID, and Alert name. Its state is `inactive`, `pending`, or `firing`; evidence freshness is an orthogonal attribute. A failed alerting-store load makes the entity unavailable. Remove the registry entry after a definition is removed/renamed and any active instance is safely closed.

Expose minimal structural attributes: severity, bounded labels, short summary, active instance UUID, evaluation status, acknowledgment/suppression summary, lifecycle timestamps, and panel URL. Do not expose full descriptions, Receiver details, delivery payloads, or actor IDs in HA state.

Add aggregate entities for firing count, unacknowledged firing count, pending count, Notification backlog, and alerting health.

## Panel

Add an Alert Ledger as a sibling to the Intent Ledger. Cross-link Rule, Intent, Alert definition entity, and Alert instance without presenting Alerts as Targets or competing Intents.

Attention order:

1. Unacknowledged critical firing.
2. Other unacknowledged firing by severity and age.
3. Stale pending/firing Alerts.
4. Pending Alerts.
5. Acknowledged firing Alerts.
6. Suppressed firing Alerts.
7. Recently resolved Alerts.

Alert detail supports acknowledgment/revocation, instance Silence, lifecycle/evidence explanation, current routing/group, suppression source, delivery audit, next deadline, and safe Rule navigation.

The guided Rule editor supports Alert declarations. Alert review shows pending/firing/resolution consequences separately from Target and Effect consequences. The initial alerting-policy editor is validated YAML with structured route simulation and current fan-out preview; a visual route-tree editor is deferred.

## Simulation

Extend the deterministic timeline simulator with the same pure alerting core used in production. Simulation never calls HA services.

Inputs support observations, pulses, time advance, restart, Receiver rejection, policy publication, acknowledgment/revocation, Silence creation/expiry, active/mute windows, and mobile action events.

Each step projects Alert instances, lifecycle transitions, evidence freshness, routes, groups, suppression/inhibition explanations, planned/cancelled/accepted/dead-lettered Notifications, Receiver calls, deadlines, and restart checkpoints.

Required canonical scenarios:

- State Alert pending -> firing -> acknowledgment -> stale -> recovery -> resolution -> recurrence.
- Pulse Alert fire -> repeated pulse extension -> timed resolution -> recurrence.
- Resolve during group wait sends nothing.
- Restart during pending, group wait, retry, Silence, and repeat.
- Restart after missed repeats emits at most one confirmed catch-up.
- Partial Receiver destination failure retries independently.
- Resolve or newer update cancels obsolete undelivered obligations.
- Silence/mute/inhibition/acknowledgment do not alter lifecycle.
- Silence expiry, active-window start, inhibition end, and acknowledgment revocation use release debounce.
- Severity escalation reroutes immediately and supersedes acknowledgment.
- Rule removal, pause, disable, and blocking resolve with explicit reasons.
- Routing publication previews and applies new destination revisions to active Alerts.
- Store failure shows degraded in-memory state but prevents non-durable dispatch.
- Corrupt store fails delivery closed and reset previews new fan-out.
- Concurrent deadline, observation, API mutation, Receiver callback, and mobile-token commands serialize into one durable generation without lost updates or duplicate consumption.
- Startup evidence barriers block state-derived catch-up until known evidence, allow unknown evidence to advance grace/staleness, and resume persisted pulse deadlines without treating old pulse IDs as new activations.
- Regex subset rejection, no-engine exact-only behavior, and cardinality-spike publication confirmation are deterministic.
- Every authored and runtime bound rejects or degrades exactly as specified without lifecycle loss or implicit truncation.

## Implementation slices

### Slice 0: contracts and feature isolation

- Add schemas and immutable model types for Alert declarations, observations, instances, transitions, policy, and projections.
- Add the private feature flag and no-op alerting coordinator port.
- Teach capabilities/schema APIs that alerting is experimental and disabled.
- Keep canonical and bundled engines synchronized.

Gate: Existing Rules, Effects, Intents, API snapshots, simulation, lifecycle files, and HA startup are byte/behavior compatible with alerting disabled.

### Slice 1: evidence-aware authored observations

- Add observation-result siblings while preserving boolean facades.
- Cover missing, unavailable, explicit unavailable checks, negation, selectors, hysteresis, blocking, pause, and runtime evaluation errors.
- Emit one authored-level Alert observation per declaration despite Target expansion.
- Add Alert DSL validation, semantic fingerprinting, preview parsing, and raw-source round trips.

Gate: Existing Intent truth tables remain unchanged; the full three-valued `and`/`or`/`not`, comparison, selector, complete-empty-selector, explicit-unavailable, and hysteresis tables are independently proven for Alerts.

### Slice 2: pure lifecycle and deterministic simulation

- Implement state and pulse lifecycle, UUID instances, independent pending clocks, staleness, recurrence, definition/control transitions, annotation fallback, and timed deadlines.
- Add snapshot import/export and bounded transition audit.
- Extend simulation before HA persistence or delivery.

Gate: Canonical state and pulse scenarios pass across restart with deterministic clocks; duplicate/replayed pulse IDs cannot extend or recreate an instance.

### Slice 3: alerting Store, runtime, APIs, and entities

- Add separate Store/writer, forced durability boundaries, migrations, health, retention, and explicit reset.
- Add deadline-driven coordinator independent of Reconciliation cadence.
- Add read APIs, operational mutation authorization skeleton, per-definition sensors, aggregates, and world projection.
- Show operational Alert Ledger without delivery controls enabled.

Gate: Pending/firing/resolved state, history, entities, and degraded/corrupt-store behavior survive HA restart without any Receiver calls. Startup evidence-barrier and serialized concurrent-command tests prove no timer/dispatch race, lost update, or pre-evidence fan-out.

### Slice 4: policy document and route simulation

- Add generation-controlled policy store and staged publication workflow.
- Implement labels, shared matchers, route tree, route/Receiver revisions, grouping keys, intervals, inhibition validation, and synthetic/current-state preview.
- Add validated YAML policy UI and explain projections.

Gate: Route traversal, inheritance, continue, grouping identity, DST/overnight intervals, publication changes, duplicate fan-out, and cycle detection are deterministic and call no Receivers. Safe-regex subset/no-fallback, matcher limits, policy bounds, and cardinality-spike gates are covered.

### Slice 5: durable Receiver delivery

- Add typed notify-entity, legacy-action, and persistent-notification adapters.
- Add immutable per-destination obligations, forced persistence before dispatch, acceptance, retry, dead letter, cancellation, supersession, stable tags/IDs, and transport cleanup.
- Implement initial, update, repeat, stale, and resolved planning.

Gate: At-least-once crash-window tests, uncertain timeout retry, partial destination failure, restart deduplication, obsolete delivery cancellation, Receiver-revision cleanup, runtime-cap recovery, and one-catch-up startup behavior pass. Every external call follows a forced atomic commit.

### Slice 6: grouping and suppression

- Implement group wait/interval/repeat, large-group rendering, Silences, active/mute intervals, inhibition, release debounce, and suppression explanations.
- Add administrator Silence APIs and preview safeguards.
- Extend simulation across every suppression and restart boundary.

Gate: Every suppression-matrix cell passes; suppression never mutates Alert lifecycle, no suppressed member leaks into Notification content, stale/escalation are the only group-interval bypasses, and release scheduling is bounded and deterministic.

### Slice 7: acknowledgment and required mobile actions

- Add shared acknowledgment/revocation semantics and audits.
- Add user instance Silence permissions and duration limits.
- Add single-use capability issuance/storage/consumption for mobile action events.
- Add Acknowledge, Silence 1h, and Open Alert actions for singleton Notifications; grouped Notifications link to the panel.
- Update replaceable Notifications on acknowledgment.

Gate: HMAC derivation, secret separation, constant-time validation, actor, expiry, replay, wrong-binding, missing-user, concurrent-use, atomic mutation/cancellation, retry rederivation, redaction, group, recurrence, severity-escalation, and restart security tests pass.

### Slice 8: timed escalation and operational UI completion

- Add same-instance timed severity steps and acknowledgment supersession.
- Complete Alert Ledger/detail controls, delivery audit, suppression explanation, guided Alert authoring, policy review, mobile deep links, accessibility, and mobile layout.
- Add Receiver testing with admin authorization and rate limits.

Gate: Canonical workflows are operable without raw APIs; Alert and policy previews truthfully show every consequence.

### Slice 9: hardening and coherent public release

- Run HA-version CI, full restart/failure matrix, load/cardinality tests, privacy/redaction review, entity-registry migration tests, and independent architecture/UI/security reviews.
- Document DSL, policy, APIs, entities, guarantees, duplicate risk, recovery, and Effect migration guidance.
- Enable alerting only after upgrading an existing installation proves no Rule/Intent/Effect regression and no startup fan-out.

Gate: All acceptance criteria below pass. Only then advertise Alert support and remove the private/experimental guard.

### Later slices

- Explicit bounded selector `per_entity` Alert expansion.
- System Alerts from runtime, persistence, Effect, Reconciliation, cardinality, and evaluation-health observations.
- Optional Alert-as-Rule-input with cycle detection for physical Intent responses, never Notification delivery.
- Optional external Alertmanager-compatible Receiver/exporter.
- Optional visual route-tree editor.
- Opt-in migration proposals for notify Effects.

## Public release acceptance criteria

- Alerts are never represented as Intents, Targets, Service plans, Drift, Manual overrides, or Effects.
- Rule evaluation emits authored Alert observations without duplicate Target-expansion output.
- Existing Intent/Effect behavior and persisted lifecycle remain backward compatible.
- State and pulse Alert lifecycle is deterministic across reload, restart, pause, block, disable, edit, and removal.
- Unknown evidence never falsely resolves an Alert; stale state is orthogonal and explainable.
- Alert/policy/runtime stores have generation, migration, corruption, rollback/reset, and health coverage.
- No Notification dispatch occurs before its obligation is durable.
- All alerting mutations serialize through one coordinator generation; API success and external calls occur only after the corresponding atomic forced commit.
- Startup performs targeted HA synchronization before state-derived catch-up; unknown evidence advances grace/staleness but blocks condition-dependent delivery, while persisted pulse deadlines resume and only new pulse IDs may activate or extend instances.
- Restart cannot produce an unbounded notification storm or replay every missed repeat.
- Routing, grouping, repeats, Silences, mutes, active intervals, inhibition, and acknowledgment match the contracts above.
- Mobile mutations are single-use, attributed, bounded, replay-resistant, and instance-scoped.
- Receiver failures and alerting persistence failures never block Intent evaluation or Reconciliation.
- Per-definition and aggregate HA entities remain stable, bounded, private, and accurate.
- Simulation and preview use production policy and perform no real HA service calls.
- Panel users can understand why an Alert is active, suppressed, acknowledged, routed, repeated, stale, or failing delivery.
- Diagnostics and entities do not leak rendered sensitive content or unbounded labels.
- Safe matcher execution and all authored/runtime cardinality and rendered-size bounds are enforced with no unsafe regex fallback or lifecycle loss.
- Resolved and obsolete delivery behavior never reports a recovered Alert as newly firing.
