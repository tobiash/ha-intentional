# HTTP API

Intentional exposes a JSON-over-HTTP API on Home Assistant's existing web server. All endpoints require the normal Home Assistant bearer token.

```bash
curl -H "Authorization: Bearer <long-lived-token>" \
  http://homeassistant.local:8123/api/intentional/health
```

Long-lived tokens are created in Home Assistant under Profile -> Long-Lived Access Tokens.

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/intentional/health` | Integration status, version, rule count, active intent count. |
| `GET` | `/api/intentional/rules/document` | Read the storage-backed authored rule document. |
| `PUT` | `/api/intentional/rules/document` | Validate, write, and reload the storage-backed authored rule document. |
| `DELETE` | `/api/intentional/rules/document` | Clear the storage-backed authored rule document and reload. |
| `GET` | `/api/intentional/rules` | Compatibility list endpoint. Storage-backed installs expose `stored-rules.yaml`. |
| `GET` | `/api/intentional/rules/{filename}` | Compatibility file-shaped read endpoint. |
| `PUT` | `/api/intentional/rules/{filename}` | Compatibility file-shaped write endpoint. |
| `DELETE` | `/api/intentional/rules/{filename}` | Compatibility file-shaped clear/delete endpoint. |
| `PATCH` | `/api/intentional/rules/id/{rule_id}` | Generation-guarded update by authored rule ID. |
| `GET` | `/api/intentional/rules/history` | List previous storage-backed rule document generations. |
| `GET` | `/api/intentional/rules/history/{generation}` | Read one previous rule document generation. |
| `POST` | `/api/intentional/rules/rollback` | Restore a previous generation and reload. |
| `POST` | `/api/intentional/reload` | Reload rules from disk. |
| `GET` | `/api/intentional/state` | Active intents grouped by target. |
| `GET` | `/api/intentional/explain/{target}` | Detailed explanation for one target. |
| `GET` | `/api/intentional/schema` | Machine-readable DSL capabilities. |
| `POST` | `/api/intentional/validate` | Validate proposed YAML. |
| `POST` | `/api/intentional/dry-run` | Evaluate proposed YAML with optional state overrides. |
| `GET` | `/api/intentional/world` | Agent-friendly desired/actual world model. |
| `GET` | `/api/intentional/diagnostics` | Recent runtime events for rule firing, service calls, failures, and drift promotions. |

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
  "active_intent_count": 2
}
```

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

`expected_generation` is optional for full-document writes/deletes, but editors
should send it to avoid overwriting concurrent changes. A stale generation
returns `409` with `generation_mismatch`.

The older `/api/intentional/rules` and `/api/intentional/rules/{filename}`
routes remain for compatibility with clients that still expect file-shaped rule
documents.

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

`GET /api/intentional/explain/{target}` returns the active intents, resolved value, winning intent, rule firing state, and modifier details for one target.

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
  "warnings": []
}
```

Validation warnings are non-blocking. Current warnings include:

- `presence_light_without_stability`: a presence/occupancy/motion driven light rule lacks both dwell (`after`/`for`) and retention (`hold.after`/target `linger`), so short sensor flaps may toggle lights.
- `unsupported_light_color_temp`: a live light target does not advertise color temperature support for `color_temp_k`.
- `unsupported_light_color`: a live light target does not advertise color support for configured color fields.

Invalid YAML returns `400` with `valid: false` and an `errors` array.

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
- `effect_applied` and `effect_failed`
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
