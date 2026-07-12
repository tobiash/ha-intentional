---
name: intentional-api
description: Helps AI agents operate ha-intentional through its Home Assistant HTTP API for rule inspection, validation, dry-run, storage-backed edits, history, rollback, explain, and world-model debugging. Use when modifying Intentional rules, diagnosing Intentional desired-vs-actual behavior, editing HA storage-backed rules, or working with /api/intentional endpoints.
---

# Intentional API

Use this skill to inspect and safely edit Intentional rules through Home Assistant's authenticated HTTP API. Do not edit live YAML files as the source of truth; authored rules live in HA storage.

## Environment

Use the local `intentionalctl` helper from this repository. It reads `HASS_URL` and `HASS_TOKEN` from the environment, with fallback to `HOMEASSISTANT_URL`, `HOMEASSISTANT_TOKEN`, and `~/.ha-env`. Never echo the token.

```bash
go run ./cmd/intentionalctl --help
```

If you need a reusable binary during a session:

```bash
go build -buildvcs=false -o /tmp/opencode/intentionalctl ./cmd/intentionalctl
```

The CLI wraps the live API endpoints, including `/api/intentional/rules/document`, `/api/intentional/dry-run`, `/api/intentional/simulate`, and `/api/intentional/rules/rollback`.

For read-only HA automation migration, use `intentionalctl migrate-ha list`,
`intentionalctl migrate-ha inspect automation.example`, and
`intentionalctl migrate-ha propose automation.example --output yaml`. Migration
only proposes Rules: it never edits or disables the source automation. Review the
edge-to-level and withdrawal warnings, then merge/save through the normal
generation-guarded document workflow.

## Read First

Start every live edit session by reading health, world, and the storage document:

```bash
go run ./cmd/intentionalctl health
go run ./cmd/intentionalctl world
go run ./cmd/intentionalctl rules-get
```

Preserve the returned `generation`; use it as `expected_generation` on saves and rollbacks.

## Safe Edit Workflow

1. Fetch `go run ./cmd/intentionalctl rules-get`.
2. Edit the returned `contents` locally.
3. Validate with `go run ./cmd/intentionalctl validate --file /path/to/rules.yaml`.
4. Dry-run with `go run ./cmd/intentionalctl dry-run --file /path/to/rules.yaml --state binary_sensor.example.state=on`.
5. Simulate lifecycle behavior with `go run ./cmd/intentionalctl simulate --file /path/to/rules.yaml --timeline /path/to/timeline.json` when timing matters.
6. Save with `go run ./cmd/intentionalctl rules-save --file /path/to/rules.yaml --expected-generation current-generation`.
7. Re-check `go run ./cmd/intentionalctl world` and relevant `go run ./cmd/intentionalctl explain light.example`.

Save command:

```bash
go run ./cmd/intentionalctl rules-save --file /tmp/opencode/rules.yaml --expected-generation current-generation
```

If save returns `generation_mismatch`, fetch the document again and merge. Do not overwrite blindly.

## Debugging Desired State

Use `/world` for compact desired-vs-actual records and `/explain/{target}` for one entity:

```bash
go run ./cmd/intentionalctl explain light.office
```

Look for:

- `active_intents`: all current claims.
- `winning_intent`: claim currently controlling the target.
- `rules_for_target`: firing, blocked, lifecycle `phase`, dwell, hold, and timing status.
- `phase`: `idle`, `waiting`, `active`, `held`, or `lingering`.
- `active_for_ms`, `condition_active_for_ms`, `held_for_ms`: lifecycle timing clues.
- `ActualMatchesDesired`: reconciliation status in `/world`.

## Simulate Lifecycle

Use `simulate` to preview timing-sensitive rules without touching Home Assistant devices:

```bash
go run ./cmd/intentionalctl simulate --file /tmp/opencode/rules.yaml --timeline /tmp/opencode/timeline.json
```

Timeline file example:

```json
{
  "timeline": [
    {"states": {"binary_sensor.room_presence.state": "on"}},
    {"advance_ms": 60000, "states": {"binary_sensor.room_presence.state": "off"}},
    {"advance_ms": 840000},
    {"advance_ms": 60000}
  ]
}
```

The CLI accepts either a raw timeline array or an object with a `timeline` array.

Each response step includes `now_ms`, `active_targets`, `resolved_targets`, and `active_rules` with lifecycle phase/timing fields.

## History And Rollback

List history before risky edits:

```bash
go run ./cmd/intentionalctl history
```

Read a history snapshot:

```bash
go run ./cmd/intentionalctl history-get generation-to-read
```

Rollback command:

```bash
go run ./cmd/intentionalctl rollback --generation generation-to-restore --expected-generation current-generation
```

Rollback records the pre-rollback document in history, so it can be undone.

## Rule Authoring Notes

- Prefer `while -> intent` rules. Use `after` for dwell and `hold` for retention after the original situation changes.
- Reuse document-level `retention_profiles` with `hold: {use: NAME}` and strict `HH:MM` `time_windows` with `time_window: {in: NAME}` or `{not_in: NAME}`. Semantic `power` matches only `sensor` entities with device class `power` and compares raw native numeric state values.
- Use `hold.until` with `for` for noisy false-off presence sensors, e.g. “hold until presence has been off for 15m”.
- Use `group` for related modes and `profile` for reusable behavior names like `pass-through`, `settled`, or `occupied-session`.
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
