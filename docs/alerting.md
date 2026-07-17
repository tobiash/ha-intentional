# Alerting

Intentional Rules may produce durable Alerts independently of Intents and Effects. An Alert records that an observed situation requires attention. Alerting policy separately decides when, where, and how Notifications are delivered.

## Authoring

`alert` accepts one mapping or a list of up to 16 mappings. Alert-only Rules are valid. Alert names must be unique within their Authored Rule.

```yaml
- id: freezer-monitoring
  while: {sensor.freezer_temperature: {gt: -10}}
  alert:
    - name: FreezerTemperatureHigh
      severity: warning
      for: 5m
      stale_after: 2m
      labels: {area: kitchen, category: appliance}
      annotations:
        summary: Freezer is too warm
        description: Check that the door is closed
      escalations:
        - {after: 30m, severity: critical}
```

State-observed Alerts resolve when their condition is successfully observed inactive. Unknown evidence retains lifecycle state, first in grace and then as stale. Pulse-observed Alerts require `resolve_after` and do not accept `for`.

Labels are bounded routing identity and cannot be templated. Annotations may use the same bounded templates as other Rule outputs. Rendering failure retains the last valid presentation, or the authored summary after restart, and marks presentation as degraded without resolving the Alert.

## Routing Policy

Alerting policy is stored and published separately from Rules. Routes inherit grouping and timing from their parent. Matchers support `=`, `!=`, `=~`, and `!~`; regular expressions use a bounded non-backtracking subset.

```yaml
route:
  id: root
  receiver: household
  group_by: [alertname, area]
  group_wait: 30s
  group_interval: 5m
  repeat_interval: 4h # or never
  send_resolved: true
receivers:
  - name: household
    destinations:
      - {type: notify_entity, entity_id: notify.family}
      - {type: persistent_notification}
```

Allowed Receiver destinations are `notify_entity`, allowlisted `legacy_action`, and `persistent_notification`. Unknown fields are rejected. Home Assistant service availability is checked at dispatch, and uncertain failures retry the same durable obligation with bounded exponential backoff before dead-lettering.

Policy preview includes current firing Alerts plus optional synthetic labels. Destination-affecting Receiver edits create cleanup only for replaceable persistent Notifications; removed ordinary destinations do not receive a misleading resolved message.

## Suppression

Acknowledgments record that an actor has seen one Alert instance. Instance and matcher Silences temporarily suppress Notifications. Mute intervals are recurring policy windows. Inhibition suppresses causally weaker Alerts while a matching, non-stale source Alert is firing.

Suppression never resolves an Alert. Release is re-evaluated after a five-second debounce. Acknowledgments do not suppress a newly stale episode or configured severity escalation. Mobile actions are actor-bound, single-use HMAC capabilities; raw capability tokens are never persisted.

## Operations

The Alert workspace shows pending, firing, stale, acknowledged, suppressed, and retained resolved instances. Detail includes lifecycle evidence, definition revision, matching routes, group identity, redacted destination status, acceptance/retry timing, and audit events.

The Alerting Policy workspace provides validation, current-plus-synthetic preview, generation-controlled publication, history rollback, and administrator-only rate-limited Receiver tests.

Operational endpoints are under `/api/intentional`:

- `GET /alerts` and `GET /alerts/{instance_id}`
- Alert acknowledgment and instance-Silence mutation endpoints
- `GET|POST /silences` and `DELETE /silences/{silence_id}`
- `GET /alerting/status` and `GET /alerting/notifications`
- `GET|PUT /alerting/policy`, policy history, and rollback
- `POST /alerting/simulate` and `POST /alerting/test-receiver`
- `POST /alerting/reset`

Simulation supports policy publication, acknowledgment/revocation, Silence expiry, Receiver rejection/retry, deadlines, and restart checkpoints using the production policy and Notification state machines.

## Persistence And Recovery

Alert lifecycle, audit, suppression, capabilities, Notification groups, immutable obligations, attempts, and dead-letter counters are versioned and persisted independently from Intent lifecycle. Persistence occurs before HA service dispatch. Startup blocks Notification delivery for restored state-observed Alerts until one known post-sync observation arrives.

Corrupt Alert state fails closed without disabling Rules, Intents, Effects, or Reconciliation. Administrator reset first returns a redacted failed-state summary and replacement fan-out preview, then requires explicit confirmation. Terminal details use 30-day and count bounds; aggregate dead-letter counters survive detail pruning.

Replacing an Effect-based notification with an Alert changes semantics: Effects fire once on Rule activation, while Alerts remain durable and may produce grouped, repeated, suppressed, resolved, or retried Notifications. Keep an Effect when a one-shot service call is the intended behavior.
