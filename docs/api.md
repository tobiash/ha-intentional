# HTTP API (v0.3+)

The integration exposes a JSON-over-HTTP API on Home Assistant's existing web server (port 8123). All endpoints require authentication via a long-lived access token (the same kind you use for the regular HA REST API).

## Authentication

```bash
curl -H "Authorization: Bearer <long-lived-token>" http://localhost:8123/api/intentional/...
```

Long-lived tokens are created in **HA → Profile → Long-Lived Access Tokens**.

## Endpoints

### `GET /api/intentional/health`

Integration health check. Returns 200 if the integration is configured, 503 otherwise.

**Response:**
```json
{
  "status": "ok",
  "version": "0.4.1",
  "rule_dir": "/config/intentional/rules",
  "rule_count": 3,
  "active_intent_count": 1
}
```

### `GET /api/intentional/rules`

List all rule files in the configured rule directory.

**Response:**
```json
{
  "rule_dir": "/config/intentional/rules",
  "count": 2,
  "files": [
    {"filename": "01-ambient.yaml", "size": "412"},
    {"filename": "02-scenes.yaml", "size": "287"}
  ]
}
```

### `GET /api/intentional/rules/{filename}`

Read a rule file's contents.

**Response:**
```json
{
  "filename": "welcome.yaml",
  "contents": "- id: ...",
  "size": 412
}
```

**Errors:**
- `404` if file doesn't exist
- `400` if filename contains path-traversal characters

### `PUT /api/intentional/rules/{filename}`

Write a rule file. Validates YAML before writing. Triggers `intentional.reload` on success.

**Request body:**
```json
{
  "contents": "- id: new-rule\n  when: ..."
}
```

**Response:**
```json
{
  "filename": "new-rule.yaml",
  "status": "saved",
  "size": 287
}
```

**Errors:**
- `400` if YAML is invalid (with the validation error in the `error` field)

### `DELETE /api/intentional/rules/{filename}`

Delete a rule file. Triggers `intentional.reload` on success.

**Response:**
```json
{
  "filename": "rule.yaml",
  "status": "deleted"
}
```

### `POST /api/intentional/reload`

Trigger the `intentional.reload` service. Useful after editing rule files outside the API (e.g. via SSH) and for tests.

**Response:**
```json
{
  "status": "reloaded",
  "rule_count": 3
}
```

### `GET /api/intentional/state`

Engine state snapshot: all active intents grouped by target, with the resolved value for each target.

**Response:**
```json
{
  "rule_count": 3,
  "active_intent_count": 1,
  "by_target": {
    "light.living_room": [
      {
        "rule_id": "movie-button",
        "target": "light.living_room",
        "set": {"brightness_pct": 30},
        "cap": {},
        "floor": {},
        "offset": {},
        "multiply": {},
        "authority": "user",
        "authority_name": "USER",
        "confidence": 100,
        "ttl_ms": 7200000,
        "reason": "manual override",
        "created_at_ms": 1717500000000
      }
    ]
  },
  "resolved": {
    "light.living_room": {
      "value": {"brightness_pct": 30},
      "ttl_remaining_ms": 7199000
    }
  }
}
```

This is the primary endpoint for agents that need to observe what the engine is currently doing.

### `GET /api/intentional/explain/{target}`

Detailed explanation of why a target is in its current state. Useful for debugging conflicts.

**Response:**
```json
{
  "target": "light.living_room",
  "resolved": {
    "value": {"brightness_pct": 30},
    "ttl_remaining_ms": 7199000
  },
  "active_intents": [
    {
      "rule_id": "movie-button",
      "authority": "user",
      "authority_name": "USER",
      "reason": "manual override",
      "set": {"brightness_pct": 30}
    },
    {
      "rule_id": "energy-cap",
      "authority": "automation",
      "authority_name": "AUTOMATION",
      "reason": "evening energy cap",
      "cap": {"brightness_pct": 50}
    }
  ],
  "winning_intent": { ... },
  "rules_for_target": [
    {
      "rule_id": "movie-button",
      "firing": true,
      "condition_firing": true,
      "blocked_by": [],
      "for_remaining_ms": null
    },
    {
      "rule_id": "energy-cap",
      "firing": true,
      "condition_firing": true,
      "blocked_by": [],
      "for_remaining_ms": null
    }
  ]
}
```

The `winning_intent` is the highest-priority active intent — that's the rule whose `set:` block determined the final value. The `cap:` from the lower-priority rule still applied (as a modifier), which is why the final value is 30 (the manual set) even though the cap is 50.
For each rule, `condition_firing` reports whether the `when` expression matched before `for:` dwell timing and `blocks:` suppression; `firing` reports whether the rule is effective after both. `for_remaining_ms` is the remaining dwell time before a matching rule becomes effective, or `null` when no dwell wait is pending.

## Error format

All errors return:
```json
{
  "error": "Rule validation failed: ...",
  "code": "validation_failed"
}
```

HTTP status codes:
- `400` — bad input (invalid filename, invalid YAML, etc.)
- `404` — resource not found
- `500` — internal error
- `503` — integration not configured

## Rate limits

None imposed by the integration itself. The integration inherits HA's general API rate limits, which are very permissive for local traffic.

## See also

- `custom_components/intentional/api.py` — implementation
- `tests/test_api.py` — unit tests (skip without HA installed)
- `tests/test_integration.py` — integration tests covering the full request lifecycle
