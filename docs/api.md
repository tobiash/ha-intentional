# HTTP API

Intentional exposes a JSON-over-HTTP API on Home Assistant's existing web server. All endpoints require the normal Home Assistant bearer token.

Rule-document request bodies are limited to 1,000,000 UTF-8 bytes. HA migration
discovery returns at most 500 automations; each source and generated proposal is
limited to 256,000 bytes, and an oversized merged document is rejected without
returning its contents.

Non-admin inspection responses recursively replace credentials, lock/alarm codes,
tokens, passwords, secrets, opaque sensitive fields, and Service plan data with
`"[redacted]"`. Raw authored documents, history snapshots, runtime diagnostics,
simulation, and replay require a Home Assistant administrator because their free-form
payloads cannot be safely redacted without destroying diagnostic meaning.

```bash
curl -H "Authorization: Bearer <long-lived-token>" \
  http://homeassistant.local:8123/api/intentional/health
```

Long-lived tokens are created in Home Assistant under Profile -> Long-Lived Access Tokens.

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/intentional/health` | Integration status, version, rule count, active intent count, and reconciliation runtime liveness. |
| `GET` | `/api/intentional/rules/document` | Read the storage-backed authored rule document (admin). |
| `PUT` | `/api/intentional/rules/document` | Validate, write, and reload the storage-backed authored rule document. |
| `DELETE` | `/api/intentional/rules/document` | Clear the storage-backed authored rule document and reload. |
| `GET` | `/api/intentional/rules` | Compatibility list endpoint. Storage-backed installs expose `stored-rules.yaml`. |
| `GET` | `/api/intentional/rules/{filename}` | Compatibility file-shaped read endpoint (admin). |
| `PUT` | `/api/intentional/rules/{filename}` | Compatibility file-shaped write endpoint. |
| `DELETE` | `/api/intentional/rules/{filename}` | Compatibility file-shaped clear/delete endpoint. |
| `PATCH` | `/api/intentional/rules/id/{rule_id}` | Generation-guarded update by authored rule ID. |
| `GET` | `/api/intentional/rules/history` | List previous storage-backed rule document generations (admin). |
| `GET` | `/api/intentional/rules/history/{generation}` | Read one previous rule document generation (admin). |
| `POST` | `/api/intentional/rules/rollback` | Restore a previous generation and reload. |
| `POST` | `/api/intentional/reload` | Reload rules from disk. |
| `GET` | `/api/intentional/state` | Active intents grouped by target. |
| `GET` | `/api/intentional/explain/{target}` | Detailed explanation for one target. |
| `GET` | `/api/intentional/schema` | Machine-readable DSL capabilities. |
| `POST` | `/api/intentional/validate` | Validate proposed YAML. |
| `POST` | `/api/intentional/dry-run` | Evaluate proposed YAML with optional state overrides. |
| `GET` | `/api/intentional/world` | Agent-friendly desired/actual world model. |
| `GET` | `/api/intentional/diagnostics` | Recent runtime events for rule firing, service calls, failures, and drift promotions (admin). |
| `GET` | `/api/intentional/migrate-ha` | Discover loaded HA automations using bounded, redacted metadata (admin). |
| `GET` | `/api/intentional/migrate-ha/{entity_id}` | Inspect migration support and diagnostics without exposing raw config (admin). |
| `POST` | `/api/intentional/migrate-ha/propose` | Generate and merged-validate a deterministic Rule proposal (admin). |
| `GET` | `/api/intentional/alerts` | List current definitions and retained resolved Alert instances. |
| `GET` | `/api/intentional/alerts/{instance_id}` | Read one Alert instance with redacted routing and delivery status. |
| `POST` | `/api/intentional/alerts/{instance_id}/acknowledge` | Acknowledge one firing Alert instance. |
| `DELETE` | `/api/intentional/alerts/{instance_id}/acknowledge` | Revoke an acknowledgment. |
| `POST` | `/api/intentional/alerts/{instance_id}/silence` | Silence one firing Alert instance temporarily. |
| `GET` | `/api/intentional/silences` | List visible operational Silences. |
| `POST` | `/api/intentional/silences` | Preview and create a matcher Silence (admin; critical impact requires confirmation). |
| `DELETE` | `/api/intentional/silences/{silence_id}` | Remove an authorized Silence. |
| `GET` | `/api/intentional/alerting/status` | Bounded Alert and Notification health counters. |
| `GET` | `/api/intentional/alerting/notifications` | Redacted durable Notification obligation audit (admin). |
| `GET`, `PUT` | `/api/intentional/alerting/policy` | Read or generation-guard publication of routing policy (admin). |
| `GET` | `/api/intentional/alerting/policy/history` | List prior routing policy generations (admin). |
| `GET` | `/api/intentional/alerting/policy/history/{generation}` | Read one prior policy generation (admin). |
| `POST` | `/api/intentional/alerting/policy/rollback` | Restore a prior policy generation (admin). |
| `POST` | `/api/intentional/alerting/simulate` | Preview current-plus-synthetic routing and fan-out (admin). |
| `POST` | `/api/intentional/alerting/test-receiver` | Send a rate-limited test to every destination in one Receiver (admin). |
| `POST` | `/api/intentional/alerting/reset` | Export/preview and explicitly replace unavailable Alert state (admin). |

## HA Automation Migration

Migration is proposal-only. It reads copied `raw_config` from loaded automation
entities and never edits, disables, or calls the source automation. Every response
states `source_mutated: false` and includes a stable source fingerprint.

The first release accepts state triggers with explicit literal `to`, numeric-state
triggers with literal `above` and/or `below`, fixed `for`, and flat explicit
`light`/`switch` `turn_on`/`turn_off` actions. Multiple triggers become deterministic
Rules and distinct actions become targets in each Rule. Conditions, templates,
blueprints, device actions, delays, choose, scripts, scenes, dynamic targets, and
conflicting target values are rejected with diagnostics. Proposals warn that an HA
trigger edge becomes a durable level and that Intent withdrawal differs from an HA
action that remains applied.

```http
POST /api/intentional/migrate-ha/propose
```

```json
{"entity_id":"automation.hall_lights"}
```

The response includes `yaml`, `merged_candidate`, `merged_validation`,
`starter_timeline`, `source_fingerprint`, diagnostics, and `source_mutated:false`.

## Health

```http
GET /api/intentional/health
```

```json
{
  "status": "ok",
  "version": "0.6.3",
  "rule_dir": "/config/intentional/rules",
  "rule_count": 4,
  "active_intent_count": 2,
  "rollback": {"state": "disarmed"},
  "runtime": {
    "status": "ok",
    "tick_interval_ms": 100,
    "stale_after_ms": 10000,
    "last_success_age_ms": 42,
    "last_failure_age_ms": null,
    "consecutive_failures": 0,
    "success_count": 128,
    "failure_count": 0,
    "current_error": null,
    "last_failure_error": null,
    "pending_pulse_count": 0
  }
}
```

After a successful storage-backed Rule mutation, Intentional arms the current
generation against its immediate predecessor. It rolls back only after three
consecutive, identically fingerprinted Rule-evaluation failures at the same
generation/revision fence and before any Effect, scene, or Service plan dispatch.
Other internal phases are not advertised as rollback-eligible until the runtime
can classify them without confusing adapter, storage, or environmental failures.
The safeguard disarms at whichever occurs first: 10 successful ticks or five
stable minutes. Service failures, unavailable Targets, storage/network errors,
Drift, stale revisions, policy denials, and user activity are ineligible. The
separate versioned journal survives restart; rollback failures become
`manual_intervention_required` and are never retried automatically.
After any rollback, a five-minute cooldown prevents a newly edited generation
from being armed. Automatic rollback history reasons start with `auto_rollback:`.

The top-level `status` becomes `degraded` when the reconciliation tick runtime
has not completed successfully within its liveness window or is currently failing.
This distinguishes "the integration loaded" from "Intentional is actively
evaluating and reconciling Targets".

## Rule Documents

Rules are stored in Home Assistant storage. Existing YAML files are imported on
first setup if no stored rule document exists. The primary editing surface is a
single storage-backed authored rule document, not a filesystem file.

The bundled Intentional sidebar panel uses this endpoint for its document
editor, validation, dry-run preview, and history/rollback controls.

Read the storage document:

```http
GET /api/intentional/rules/document
```

```json
{
  "contents": "- id: office-light\n  while:\n    binary_sensor.office_occupancy: on\n  intent:\n    light.office:\n      state: on\n",
  "size": 118,
  "generation": "...",
  "rule_count": 1,
  "source": "storage"
}
```

Write the storage document:

```http
PUT /api/intentional/rules/document
```

```json
{
  "expected_generation": "current-generation",
  "contents": "- id: office-light\n  while:\n    binary_sensor.office_occupancy: on\n  intent:\n    light.office:\n      state: on\n"
}
```

Clear the storage document:

```http
DELETE /api/intentional/rules/document
```

```json
{
  "expected_generation": "current-generation"
}
```

The integration validates YAML before writing to HA storage and calls
`intentional.reload` after a successful write or delete.

`expected_generation` is required for storage-backed full-document writes and
deletes. Missing preconditions return `428`; stale generations return `409`
with `generation_mismatch`.

The older `/api/intentional/rules` and `/api/intentional/rules/{filename}`
routes remain for compatibility with clients that still expect file-shaped rule
documents. Their storage-backed `PUT` and `DELETE` operations require the same
`expected_generation` field.

## Patch By Rule ID

```http
PATCH /api/intentional/rules/id/office-light
```

```json
{
  "expected_generation": "sha256:...",
  "contents": "- id: office-light\n  enabled: false\n  while:\n    binary_sensor.office_occupancy: on\n  intent:\n    light.office:\n      state: on\n"
}
```

If the stored generation does not match, the endpoint returns `409` with `generation_mismatch`. This lets agents avoid overwriting concurrent user edits.

## History And Rollback

Storage-backed rules keep a bounded history of previous documents. Every
successful write, delete, patch-by-rule-id, enable toggle, and rollback records
the document being replaced before saving the new one.

List previous generations:

```http
GET /api/intentional/rules/history
```

```json
{
  "current_generation": "...",
  "count": 2,
  "history": [
    {
      "generation": "...",
      "created_at": "2026-06-09T21:55:01.000000+00:00",
      "reason": "patch:office-light",
      "size": 812,
      "rule_count": 4
    }
  ]
}
```

Read a snapshot, including its YAML contents:

```http
GET /api/intentional/rules/history/{generation}
```

Restore a previous generation:

```http
POST /api/intentional/rules/rollback
```

```json
{
  "generation": "generation-to-restore",
  "expected_generation": "current-generation"
}
```

Rollback is optimistic. If `expected_generation` is stale, the endpoint returns
`409` with `generation_mismatch`. The pre-rollback current document is also
recorded in history, so a rollback can itself be undone.

## State And Explain

`GET /api/intentional/state` returns active intents grouped by target plus resolved values.

`GET /api/intentional/explain/{target}` preserves the legacy active-intent and
resolved fields and adds `projection`, a deep Target record. The projection
contains per-field providers, losing providers, ordered modifiers, Rule states
(`winning`, `losing`, `blocked`, `waiting`, or `inactive`), actual state, the
Service plan, tri-state `plan_match` (`match`, `mismatch`, or `unknown`), and
Reconciliation facts. Reconciliation facts include ownership, transition
suppression, pending Drift, retry/backoff, and partial Service-plan progress.
Manual overrides include expiry and the winner/value revealed after withdrawal.

Rule status objects in `/world`, `/explain/{target}.rules_for_target`, and rule
switch attributes include lifecycle diagnostics:

- `phase`: one of `idle`, `waiting`, `active`, `held`, or `lingering`.
- `active_for_ms`: how long the rule has had active runtime intents.
- `condition_active_for_ms`: how long the starting situation has been true.
- `held_for_ms`: how long the rule has been retained by `hold` after the starting situation stopped.
- `group` and `profile`: optional author metadata for modes and behavior profiles.

For a Rule in dynamic timed retention, status and explain projections include
`hold_after`: `frozen`, the `active_for_ms` selection basis, selected tier
index/threshold/base, matching adjustment index/window/add (or `null`), `max_ms`,
unclamped and final durations, start/expiry timestamps, and remaining time.

## Manual Drift Overrides

Intentional treats HA state changes on managed targets as observations first. A
state mismatch is promoted to a `USER` manual override only after it remains
stable beyond the confirmation window and is not inside an owned transition
grace period. This avoids treating HA transition frames, device-side clamping, or
integration echo lag as user intent.

When a drift observation is promoted, it replaces any existing manual override
for that target instead of stacking duplicate user intents.

## Schema

```http
GET /api/intentional/schema
```

Returns machine-readable capabilities, including supported top-level fields, lifecycle fields, observation operators, field operators, target metadata, and selector filters. The schema currently reports `dsl_version: vnext-draft`.

## Validate

```http
POST /api/intentional/validate
```

```json
{
  "contents": "- id: office-light\n  while:\n    binary_sensor.office_occupancy: on\n  intent:\n    light.office:\n      state: on\n"
}
```

Successful response:

```json
{
  "valid": true,
  "rule_count": 1,
  "normalized": [],
  "errors": [],
  "warnings": []
}
```

After YAML and per-Rule validation, validate runs a pure document-wide policy
preflight. Policy errors set `valid` to `false` but still return `rule_count` and
`normalized`, allowing API and `intentionalctl validate` clients to inspect the
loaded document. The same preflight is enforced atomically before every write,
patch, rollback, reload, dry-run, preview, simulation, and replay. Policy errors
include:

- `missing_suppression_rule`: a suppression declaration references no Rule ID in the document.
- `suppression_cycle`: suppression declarations form a cycle.
- `effect_only_durable_target`: an effect-only domain such as `button` or `input_button` is used as a durable Target.

Validation warnings are non-blocking. Current warnings include:

- `contradictory_floor_cap`: a Target field's highest numeric floor exceeds its lowest numeric cap. Preflight cannot prove that the contributing Rules are simultaneously active.
- `modifier_without_document_baseline`: a Target field uses `offset` or `multiply`, but no Rule for that Target in the document provides a `set` baseline.
- `presence_light_without_stability`: a presence/occupancy/motion driven light rule lacks both dwell (`after`/`for`) and retention (`hold.after`/target `linger`), so short sensor flaps may toggle lights.
- `unsupported_light_color_temp`: a live light target does not advertise color temperature support for `color_temp_k`.
- `unsupported_light_color`: a live light target does not advertise color support for configured color fields.

Invalid YAML returns `400` with `valid: false` and an `errors` array. Document
policy errors return `200` with `valid: false`, structured `errors`, normalized
Rules, and any non-blocking warnings.

## Dry Run

```http
POST /api/intentional/dry-run
```

```json
{
  "contents": "- id: office-light\n  while:\n    binary_sensor.office_occupancy: on\n  intent:\n    light.office:\n      state: on\n      brightness_pct: 70\n",
  "state_overrides": {
    "binary_sensor.office_occupancy.state": "on"
  }
}
```

Response:

```json
{
  "valid": true,
  "active_targets": ["light.office"],
  "resolved_targets": [
    {"target": "light.office", "value": {"state": "on", "brightness_pct": 70}}
  ],
  "effects": [],
  "errors": []
}
```

`state_overrides` keys use `<entity_id>.<field>`. Use `.state` for the entity state.

## Simulate

```http
POST /api/intentional/simulate
```

Evaluates proposed YAML over a timeline without applying services to Home
Assistant. Use this for lifecycle rules involving `after`, `hold`, and stable
absence with `hold.until.for`.

```json
{
  "contents": "- id: living-room-dark\n  while:\n    binary_sensor.living_room_presence: on\n  hold:\n    until:\n      binary_sensor.living_room_presence: off\n      for: 15m\n  intent:\n    light.living_room:\n      state: on\n",
  "timeline": [
    {"states": {"binary_sensor.living_room_presence.state": "on"}},
    {"advance_ms": 60000, "states": {"binary_sensor.living_room_presence.state": "off"}},
    {"advance_ms": 840000},
    {"advance_ms": 60000}
  ],
  "selectors": [
    {
      "selector": {"domain": "light", "area": "living_room"},
      "targets": ["light.living_room"]
    }
  ]
}
```

Rules using `intent.select` or selector-backed observations require a top-level
`selectors` membership list. Each entry maps the selector's `domain`, `area`,
and/or `label` filters to deterministic simulated Target IDs. Missing or
duplicate memberships are rejected; an empty `targets` list explicitly models
no matches. Membership is retained across simulated restarts.

For purpose-specific observations, simulations may instead provide
`semantic_metadata`, a list of `{entity_id, area?, device?, device_class?,
original_device_class?}` records. The simulator resolves semantic memberships
from these records and retains them across simulated restarts. State changes in
the timeline derive one-cycle `changed` pulses automatically.

`/api/intentional/replay` accepts the same top-level `selectors` and
`semantic_metadata` fields and uses the same membership validation and semantic
resolution rules. A replay containing selector-backed Rules is rejected when
the required membership or semantic metadata is missing.

Each timeline step may include:

- `time_of_day`: strict local `HH:MM` time context. It is retained across a
  simulated `restart` and is used when freezing dynamic `hold.after`.

- `advance_ms`: non-negative integer clock advance before evaluation.
- `states`: mapping of `<entity_id>.<field>` to value. Use `.state` for normal entity state.
- `actual`: mapping of Target IDs to an HA-shaped `{state, attributes, user_id}` snapshot.
- `reject_calls`: `true` to reject all fake Adapter calls, or a list of service names to reject.
- `pause_rule_ids` / `resume_rule_ids`: Rule IDs whose evaluation is paused or resumed.
- `enabled`: globally enable or disable Rule evaluation.
- `restart`: restore lifecycle and Target ownership into fresh Reconciliation state.

An optional top-level `reconciliation` mapping configures
`drift_override_ttl_ms`, `drift_confirmation_ms`,
`service_failure_backoff_ms`, and `drift_transition_grace_ms`.

Response steps preserve `now_ms`, `active_targets`, `resolved_targets`, and
`active_rules`, and add fake Adapter `calls`, Reconciliation `events`, a
structured `targets` projection, and a restart `checkpoint`. The simulator
models Service plans, rejected calls and retry, transition grace, staged Drift
promotion, Manual override expiry, withdrawal, pause/global disable, and
restart ownership without touching Home Assistant.

Simulation and replay require an administrator. Requests are limited to 1 MB
of Rule YAML, 500 timeline steps, 200 state updates and 100 actual states per
step, 5,000 updates overall, 200 selector memberships, and 200 projected or
selector-expanded Targets. Unknown, missing selector, or incorrectly typed
fields return `400`; long timelines yield periodically to Home Assistant.

## World Model

```http
GET /api/intentional/world
```

Returns an agent-friendly snapshot containing:

- `desired_records`: resolved desired targets, reasons, conditions, and actual snapshots where available.
- `authored_rules`: authored rule statuses grouped by authored rule ID, including enabled state, active state, targets, desired payload, blocked-by status, and metadata.
- `active_rules`: subset of authored rules currently firing or holding active intents.
- `lifecycle`: persisted lifecycle state such as generated values and global enabled state.
- `selector_diagnostics`: selector resolution details.
- `health`: rule and intent counts.
- `entities`: actual HA state snapshots for desired targets.
- `targets`: the same deep Target projection returned by `/explain`, including
  owned targets retained for possible withdrawal. This is API data only; no
  Home Assistant entities are created.

Actual-state projections expose only operational attributes used for
Reconciliation, such as brightness, position, temperature, source, and volume.
Names, tokens, and arbitrary integration-specific attributes are redacted.

`desired_records[].conditions` includes `DesiredResolved` and, when an actual HA state is available, `ActualMatchesDesired`.

## Diagnostics

```http
GET /api/intentional/diagnostics?limit=50
```

Returns a bounded in-memory ring of recent runtime events. This endpoint is for
answering questions like "why did this lamp toggle?" without reconstructing
everything from Home Assistant logbook entries.

Event types include:

- `rule_fired` and `rule_withdrawn`
- `service_applied`, `service_failed`, and `service_skipped_matching_state`
- `effect_applied` and `effect_failed`, including activation ID, effect index,
  attempt count, and the next retry time for failures
- `drift_promoted`

The ring is intentionally volatile; it resets when Home Assistant restarts.

## Error Format

Errors use this shape:

```json
{
  "error": "Rule validation failed: ...",
  "code": "validation_failed"
}
```

Common status codes:

- `400`: bad input, invalid JSON, invalid filename, or invalid YAML.
- `404`: rule file not found.
- `409`: generation mismatch on rule-ID patch.
- `500`: internal error.
- `503`: integration not configured.

## See Also

- `custom_components/intentional/api.py`
- `custom_components/intentional/rule_files.py`
- `tests/test_api.py`
- `tests/test_service_contract.py`
# Target Policy Denials

Validation reports static Target-policy conflicts as errors and warns when a safety-sensitive Target has no explicit policy. Dry-run previews, Target explanations, and simulation Target records include `policy_denial` when the resolved Intent cannot be dispatched. Simulations also emit `service_denied_target_policy` events with a stable `details.code` such as `observe_only`, `field_not_allowed`, `automatic_state_forbidden`, `user_authority_required`, `target_unavailable`, or `max_retries_exhausted`.
