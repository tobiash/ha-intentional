# ha-intentional

**Declarative, composable, intent-based automation for Home Assistant.**

`ha-intentional` is a HACS-installable custom component that turns Home Assistant automation into reconciliation: rules describe **while** situations in the home, produce durable **intents** for how entities should be, and the engine reconciles actual state toward the resolved desired state.

Rule documents support reusable retention profiles, named local-clock windows, and metadata-aware semantic observations such as power. See [Rule authoring](docs/rules.md).

Core vocabulary:

```text
while -> intent
```

Effects are explicit side-effect escape hatches for one-shot service calls. They are separate from durable target-state intents.

## Why?

Home Assistant automations are powerful, but large setups often drift into ordering and conflict problems:

- Rules need manual ordering to avoid conflicts.
- A new automation can silently break an older one.
- Manual overrides need helper booleans, reset automations, and special cases.
- Device modifiers such as brightness caps are hard to compose.
- Ambient behavior often requires scripts or repeated service-call chains.

Intentional changes the abstraction. You write claims with priority metadata; the compositor resolves them by authority, confidence, recency, and field-level operators.

## Quick Example

```yaml
- id: living-room-dark
  while:
    sensor.outdoor_light.illuminance:
      lt: 50
  intent:
    light.living_room:
      state: on
      brightness_pct: 80
      color_temp_k: 2700
  confidence: 0.7
  reason: Dark outside

- id: living-room-tv-cap
  while:
    media_player.tv: on
  intent:
    light.living_room:
      brightness_pct:
        max: 40
      color_temp_k: 2200
  confidence: 0.9
  reason: TV is on

- id: front-door-notification
  observe:
    changed:
      binary_sensor.front_door:
        to: on
  effect:
    service: notify.mobile_app_phone
    data:
      title: Door
      message: Front door opened
```

What happens:

1. If it is dark, the living-room light is desired on at 80%.
2. If the TV turns on, brightness is capped at 40% and color temperature becomes warmer.
3. If you manually change the managed light, Intentional records that HA state as a `user` intent with a TTL.
4. When the manual override expires or is cleared, automation resumes without reset helpers.
5. Door notifications use `effect:` because they are side effects, not durable state.

## Features

- **Structured rules** using `while:`, `intent:`, optional `hold:`, and optional `effect:`.
- **Reconciliation loop** that compares desired records with actual HA state and skips redundant calls.
- **Authority tiers**: `sensor < automation < user`, with confidence and recency tiebreakers.
- **Field-level operators**: direct values, `min`/`floor`, `max`/`cap`, `offset`, and `multiply`.
- **Dwell and lifecycle**: top-level `after`, `hold.while`, stable `hold.until`, `hold.after`, target `ttl`, and restart-safe lifecycle persistence.
- **Transition policies**: `apply.transition.assert`, `change`, and `withdraw` for HA-native light transitions.
- **Generated values**: sample durable fields, such as RGB colors, on fixed or random intervals.
- **Manual override handling**: stable state drift on managed targets becomes a temporary user intent.
- **HA UI controls**: sidebar rule editor, global automation switch, per-rule enable switches, reload, and clear-manual-override controls.
- **Storage-backed YAML editor** for validation, dry-run preview, save, and rollback history.
- **Agent-friendly HTTP API** for schema, validation, dry run, world model, stored rules, and explanations.
- **Simulation API** for previewing lifecycle behavior over a timeline without applying HA services.
- **Hot reload** after rule edits.
- **HACS installable** with CI-covered bundle sync and Home Assistant integration tests.

## Installation

### HACS

1. Install [HACS](https://hacs.xyz/) if needed.
2. HACS -> Integrations -> Custom repositories.
3. Add `https://github.com/tobiash/ha-intentional` as an **Integration**.
4. Install and restart Home Assistant.
5. Settings -> Devices & Services -> Add Integration -> `Intentional`.
6. Set the rule directory, usually `/config/intentional/rules/`.

### Manual

```bash
cd /config/custom_components
git clone https://github.com/tobiash/ha-intentional.git intentional
```

Restart Home Assistant, then add the `Intentional` integration from Settings.

## Stored Rules And YAML

Intentional stores authored rules in Home Assistant storage. YAML remains the
authoring, import, export, API, and advanced-editor format, but live YAML files
are no longer the source of truth. The Intentional sidebar panel edits the
storage document directly.

On first setup, if the storage document does not exist yet, Intentional imports
existing YAML files from the configured rule directory, for example:

```text
/config/intentional/
└── rules/
    ├── living-room.yaml
    ├── office.yaml
    └── notifications.yaml
```

After import, rule switches and reloads operate from HA storage. The old files
are left untouched as a migration backup.

The stored YAML document may be either a list of rules or a document with
`scenes:` and `rules:`:

```yaml
scenes:
  movie:
    intent:
      light.living_room:
        state: on
        brightness_pct: 15
        color_temp_k: 2200

rules:
  - id: movie-mode
    while:
      input_boolean.movie_mode: on
    intent:
      include: scene.movie
```

See [`docs/rules.md`](docs/rules.md) for the rule reference and [`examples/`](examples/) for copyable examples.

## Generated Values

Use field-local `generate` when an active intent should vary durable desired state over time, such as an ambient monitor backlight:

```yaml
- id: monitor-backlight-random
  while:
    binary_sensor.office_occupancy: on
  intent:
    light.monitor_backlight:
      state: on
      brightness_pct: 35
      rgb_color:
        generate:
          kind: walk
          from:
            - [255, 120, 40]
            - [255, 70, 120]
            - [140, 70, 255]
            - [40, 170, 255]
          every:
            min: 2m
            max: 6m
          transition:
            min: 30s
            max: 75s
```

Generated values are held until the next interval, persisted across restarts, and reconciled like ordinary target state. Supported strategies include `sample`, `walk`, `weighted_sample`, `gradient`, and `noise`.

## Home Assistant UI Controls

Intentional exposes entities for common control tasks:

- `Intentional` sidebar panel edits the storage-backed rule document with validation, dry-run preview, save, and rollback history.
- `switch.intentional_automation_enabled` globally enables or disables rule evaluation and automation effects.
- `switch.intentional_rule_<rule_id>` toggles an authored rule and persists `enabled: true/false` in HA storage.
- `button.intentional_reload_rules` reloads the stored rule document.
- `button.intentional_clear_all_manual_overrides` clears every user/manual override intent.

Intentional does not create one entity per runtime intent or one control per
target. Per-target manual override clearing remains available through the
`intentional.clear` service with a `target` value.

## HTTP API

All API endpoints use Home Assistant's normal bearer-token authentication:

```bash
TOKEN="<long-lived access token>"
HA="http://homeassistant.local:8123"

curl -H "Authorization: Bearer $TOKEN" "$HA/api/intentional/health"
curl -H "Authorization: Bearer $TOKEN" "$HA/api/intentional/world"
curl -H "Authorization: Bearer $TOKEN" "$HA/api/intentional/rules"
```

Useful agent endpoints:

- `GET /api/intentional/schema`
- `POST /api/intentional/validate`
- `POST /api/intentional/dry-run`
- `GET /api/intentional/world`
- `GET /api/intentional/explain/<target>`
- `PATCH /api/intentional/rules/id/<rule_id>`

See [`docs/api.md`](docs/api.md) for the full endpoint reference.

## UI Development Loop

The sidebar panel can be exercised without installing a new build into a real
Home Assistant instance:

```bash
python tools/serve_intentional_panel.py
```

Open `http://127.0.0.1:8765`. The harness serves the bundled panel, injects a
mock `hass` object with sample entities, and backs `validate`, `dry-run`, and
`simulate` with the pure Python engine. Use it with:

```bash
node --check custom_components/intentional/frontend/intentional-panel.js
pytest tests/test_frontend_panel.py -q
```

This catches syntax, editor contract, rule validation, dry-run, and lifecycle
simulation issues before installing on a live Home Assistant instance.

## Services

- `intentional.fire`: emit a temporary user-authority intent for a target.
- `intentional.clear`: clear user/manual override intents globally or per target.
- `intentional.reload`: reload rules from HA storage.

## Status

Current releases focus on the `while -> intent` rule format, lifecycle hold semantics, reconciliation status, generated durable values, and Home Assistant UI controls. The DSL is still marked `vnext-draft` in the machine-readable schema while the project converges on final compatibility guarantees.

## License

MIT
