---
name: intentional-api
description: Helps AI agents operate ha-intentional through its Home Assistant HTTP API for rule inspection, validation, dry-run, storage-backed edits, history, rollback, explain, and world-model debugging. Use when modifying Intentional rules, diagnosing Intentional desired-vs-actual behavior, editing HA storage-backed rules, or working with /api/intentional endpoints.
---

# Intentional API

Use this skill to inspect and safely edit Intentional rules through Home Assistant's authenticated HTTP API. Do not edit live YAML files as the source of truth; authored rules live in HA storage.

## Environment

If available, load credentials from `~/.ha-env` without printing secrets:

```bash
set -a; source "$HOME/.ha-env"; set +a
```

Use `HASS_URL` and `HASS_TOKEN`. Never echo the token.

## Read First

Start every live edit session by reading health, world, and the storage document:

```bash
curl -fsS -H "Authorization: Bearer $HASS_TOKEN" "$HASS_URL/api/intentional/health"
curl -fsS -H "Authorization: Bearer $HASS_TOKEN" "$HASS_URL/api/intentional/world"
curl -fsS -H "Authorization: Bearer $HASS_TOKEN" "$HASS_URL/api/intentional/rules/document"
```

Preserve the returned `generation`; use it as `expected_generation` on saves and rollbacks.

## Safe Edit Workflow

1. Fetch `GET /api/intentional/rules/document`.
2. Edit the returned `contents` locally.
3. Validate with `POST /api/intentional/validate`.
4. Dry-run with `POST /api/intentional/dry-run`.
5. Save with `PUT /api/intentional/rules/document` and the original `expected_generation`.
6. Re-check `GET /api/intentional/world` and relevant `GET /api/intentional/explain/{target}`.

Save payload:

```json
{
  "expected_generation": "current-generation",
  "contents": "- id: example\n  while:\n    input_boolean.example: true\n  intent:\n    light.example:\n      state: true\n"
}
```

If save returns `generation_mismatch`, fetch the document again and merge. Do not overwrite blindly.

## Debugging Desired State

Use `/world` for compact desired-vs-actual records and `/explain/{target}` for one entity:

```bash
curl -fsS -H "Authorization: Bearer $HASS_TOKEN" "$HASS_URL/api/intentional/explain/light.office"
```

Look for:

- `active_intents`: all current claims.
- `winning_intent`: claim currently controlling the target.
- `rules_for_target`: firing, blocked, and dwell status.
- `ActualMatchesDesired`: reconciliation status in `/world`.

## History And Rollback

List history before risky edits:

```bash
curl -fsS -H "Authorization: Bearer $HASS_TOKEN" "$HASS_URL/api/intentional/rules/history"
```

Rollback payload:

```http
POST /api/intentional/rules/rollback
```

```json
{
  "generation": "generation-to-restore",
  "expected_generation": "current-generation"
}
```

Rollback records the pre-rollback document in history, so it can be undone.

## Rule Authoring Notes

- Prefer `while -> intent` rules. Use `after` for dwell and `hold` for retention after the original situation changes.
- Use durable target-state intents for ongoing desired state.
- Use `effect:` only for side-effect service calls.
- Use `apply.transition.assert/change/withdraw` for HA-native transitions.
- Use generated durable values for ambient variation.
- Do not create runtime-intent entities or edit synthetic `stored-rules.yaml` as the primary API.
- Use `intentional.clear` for manual override clearing when needed.

## Local Repository Checks

After code changes in this repo, run:

```bash
python "tools/sync_bundled_engine.py"
python "ci/check-bundle-sync.py"
ruff check src/ tests/ custom_components/intentional/
pytest -q
```
