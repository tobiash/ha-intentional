# Intentional VNext Design

Intentional is a reconciliation controller for Home Assistant. Rules observe current or recent home facts, derive desired records, resolve conflicts, and reconcile actual Home Assistant state toward desired state.

Core principle:

```text
observe -> intent
```

Effects are explicit side-effect escape hatches. They are not desired target state.

## Goals

- Preserve the original model: observed state produces target-state intents.
- Make edges and events first-class observations, not triggers.
- Keep durable state separate from side effects.
- Move toward Kubernetes-style reconciliation: desired records, actual state diffing, status conditions, and restart-safe lifecycle records.
- Optimize diagnostics and APIs for AI agents.

## Document Shape

Rule files may use a mixed document shape:

```yaml
scenes:
  movie:
    labels: [living-room]
    notes: Reusable movie bundle
    intent:
      light.living_room:
        brightness_pct: 15
        color_temp_k: 2200
      cover.blinds:
        state: closed

rules:
  - id: movie-mode
    enabled: true
    labels: [living-room]
    authority: user
    confidence: 1.0
    reason: Movie mode
    notes: Activated by helper
    observe:
      input_boolean.movie_mode: on
    intent:
      include: scene.movie
```

Simple files may remain list-only:

```yaml
- id: hallway-base
  intent:
    light.hallway:
      brightness_pct:
        min: 3
```

Missing `observe` means always active. Invalid new config is rejected and the last known good config remains active.

## Rule Metadata

Supported rule fields:

- `id` required
- `enabled` optional, default `true`
- `labels` optional list
- `observe` optional, default always true
- `intent` optional
- `effect` optional
- `authority` optional, default `automation`
- `confidence` optional, default `1.0`
- `reason` optional string
- `notes` optional string

Authority tiers remain:

```text
sensor < automation < user
```

Metadata applies to the whole rule. Per-target authority, confidence, and reason are not supported initially.

## Observations

`observe` is structured only. The old string expression language is not part of VNext.

Scalar values imply `is`:

```yaml
observe:
  media_player.tv: on
```

Multiple mapping entries imply `all`:

```yaml
observe:
  media_player.tv: on
  sensor.outdoor_light.illuminance:
    lt: 50
```

Explicit composition is supported:

```yaml
observe:
  any:
    - binary_sensor.office_motion: on
    - binary_sensor.office_presence: on
```

`not` wraps one observation:

```yaml
observe:
  not:
    input_boolean.guest_mode: on
```

`none` negates many observations:

```yaml
observe:
  none:
    - input_boolean.guest_mode: on
    - input_boolean.sleep_mode: on
```

Comparison operators:

- `is`
- `is_not`
- `lt`
- `lte`
- `gt`
- `gte`
- `in`
- `not_in`
- `contains`
- `exists`

`for` applies to the whole pure-level observation:

```yaml
observe:
  binary_sensor.front_door: on
  for: 30s
```

`for` is invalid if the observation contains `changed` or `happened`.

## Edges And Events

Edges are transient observations, not triggers.

State change observation:

```yaml
observe:
  changed:
    binary_sensor.front_door:
      from: off
      to: on
```

Field-explicit change observation:

```yaml
observe:
  changed:
    sensor.outdoor_light.illuminance:
      from: 20
      to: 40
```

Initially supported `changed` fields:

- `from`
- `to`
- `within`

Default edge lifetime without `within` is one evaluation cycle.

Event entity observation:

```yaml
observe:
  happened:
    event.espnow_recv_doorbell:
      event_type: ringer
      within: 5s
```

`happened` initially targets Home Assistant `event.*` entities, but internally it should use generalized event facts. If an observation contains `changed` or `happened`, it is edge-created.

## Observe Selectors

Observation selectors are included.

```yaml
observe:
  select:
    any:
      domain: binary_sensor
      area: living_room
      label: motion
      is: on
```

Supported aggregations:

- `any`
- `all`
- `none`

Supported filters:

- `domain`
- `area`
- `label`
- `exclude`
- `field`

`field` defaults to `state`.

Selector semantics:

- `any`: true if at least one match satisfies the condition.
- `all`: true if at least one match exists and all matches satisfy the condition.
- `none`: true if zero matches or no matches satisfy the condition.

Templates inside observe selectors are deferred. Selectors use an adapter-provided resolver. The pure engine supports selector specs through injection and remains Home Assistant agnostic.

## Templates

Jinja templates are allowed in scalar values under `intent` and `effect.data`.

```yaml
intent:
  light.office:
    brightness_pct: "{{ states('input_number.office_brightness') | int }}"
```

```yaml
effect:
  service: notify.mobile
  data:
    message: "Door opened at {{ now().strftime('%H:%M') }}"
```

Templates are not initially allowed in:

- target names
- field names
- operator names
- effect service names
- implicit observe values
- observe selectors

For complex observation templates, reserve explicit syntax:

```yaml
observe:
  template: "{{ state_attr('sun.sun', 'elevation') < 4 }}"
```

Jinja is preferred over CEL because it is native to Home Assistant. Template rendering occurs before composition. Rendered values are coerced by field/operator context. Template errors skip only the affected target or effect and appear in diagnostics.

## Intent Syntax

`intent` maps to one or more target intents.

```yaml
intent:
  light.living_room:
    state: on
    brightness_pct: 80
    color_temp_k: 2700
```

Plain scalar, list, and object values mean `value`/set.

Operator objects:

```yaml
intent:
  light.living_room:
    brightness_pct:
      value: 80
      max: 40
      min: 5
      offset: -10
      multiply: 0.8
```

Reserved field operator keys:

- `value`
- `min`
- `max`
- `offset`
- `multiply`
- `animate`

A mapping is an operator object only if it contains at least one reserved operator key. Otherwise it is a normal field value.

`min` maps to the current `floor`. `max` maps to the current `cap`. `state` is a normal target field.

Reserved target metadata keys:

- `ttl`
- `linger`
- `transition`
- `easing`

These live directly inside each target object.

Strict capability validation applies:

- known domain plus unsupported field is a validation error
- unknown domain under `intent` is a validation error
- action-like fields under `intent` are validation errors
- `toggle` is invalid under `intent`

Unobservable commands must be effects.

## Intent Lifecycle

Level observations may use `linger`:

```yaml
observe:
  media_player.tv: on
intent:
  light.living_room:
    linger: 2h
    brightness_pct: 30
```

Semantics:

- while observation holds, the intent is active
- after observation stops, a linger record remains until expiration
- reactivation replaces the lingering record with a fresh active intent

`linger` is target-level, keeps the same authority, confidence, and reason, and persists across restart/reload.

Edge-created observations require `ttl`:

```yaml
observe:
  changed:
    binary_sensor.front_door:
      to: on
intent:
  light.entry:
    ttl: 2m
    state: on
```

Semantics:

- edge observation creates a temporary intent
- `ttl` is required
- `ttl` persists across restart/reload

Lifecycle rules:

- edge/event intent requires `ttl`
- level intent may use `linger`
- `ttl` and `linger` are never both valid on the same target
- `for` is only valid on pure level observations

## Animations

Animations are inline on fields:

```yaml
intent:
  light.monitor_back_led:
    ttl: 20s
    color_temp_k: 2700
    brightness_pct:
      animate:
        pulse: [0, 100, 0]
        duration: 2s
        repeat: 4
```

One animated field per target is supported initially.

Animations may be used on level or edge-created intents:

- level observation: animation lives while observation holds, optionally lingers
- edge observation: animation lives until `ttl` or repeat completion

Implemented syntax maps field-local `animate` blocks to the existing animation
model. The field name becomes the animation parameter and the first animated
value seeds the target's base desired value.

## Dynamic Observation Selectors

Rules can observe dynamic entity sets using `observe.select`:

```yaml
observe:
  select:
    mode: any # any | all | none
    entities:
      - domain: binary_sensor
        label: motion
        state: on
intent:
  light.hallway:
    state: on
```

The selector resolver expands the filter (`domain`, `area`, `label`, `exclude`)
against the current HA entity set. The engine then compares the selected field
against the requested value. Selector diagnostics are exposed in the world model
so agents can see which selected entities matched.

## Templates

Scalar `intent` values and `effect.data` values may use Jinja templates:

```yaml
intent:
  light.desk:
    brightness_pct: "{{ states('input_number.target_brightness') | int }}"

effect:
  service: notify.mobile_app_phone
  data:
    message: "Temperature is {{ states('sensor.room_temp') }}"
```

Templates render at intent/effect emission time using a native Jinja environment,
so numeric templates can produce numbers. The supported helpers are:

- `states(entity_id)`
- `state_attr(entity_id, attr)`

## Lifecycle Persistence

The engine exposes lifecycle records for restart/reload recovery. HA stores them
under the integration storage key and restores them after loading rules.

Persisted records include:

- edge-created TTL intents
- lingering intents
- manual override intents
- active effect rule IDs used for once-per-activation dedupe

Expired records and records for missing rule IDs are discarded on restore.

## Agent API

Agent-facing endpoints include:

- `GET /api/intentional/schema`
- `POST /api/intentional/validate`
- `POST /api/intentional/dry-run`
- `GET /api/intentional/world`
- `PATCH /api/intentional/rules/id/<rule_id>` with `expected_generation`

The world endpoint exposes desired records, lifecycle records, selector
diagnostics, and actual-vs-desired conditions when HA state is available.

## Intentional Scenes

Scenes are reusable intent bundles, resolved through the compositor.

```yaml
scenes:
  movie:
    labels: [living-room]
    notes: Movie state bundle
    intent:
      light.living_room:
        brightness_pct: 15
      cover.blinds:
        state: closed
```

Include a scene:

```yaml
intent:
  include: scene.movie
```

Multiple includes plus inline intents:

```yaml
intent:
  include:
    - scene.movie
    - scene.relaxed
  light.bias:
    brightness_pct: 10
```

Scene rules:

- scene IDs are globally unique
- rule IDs and scene IDs have separate namespaces
- scene definitions may include target metadata like `ttl` and `transition`
- scene definitions may include selectors
- scene definitions may include other scenes
- cycle detection is required
- the including rule supplies authority, confidence, and reason
- included scene targets merge with inline targets
- inline wins conflicts

Merge precedence:

```text
included scene selector < included scene explicit < inline selector < inline explicit
```

Native Home Assistant scenes are explicit effects only, not intents.

## Intent Selectors

Target selectors are included.

```yaml
intent:
  select:
    - domain: light
      area: living_room
      state: off
    - domain: switch
      label: holiday
      state: on
      exclude:
        - switch.outdoor_socket
```

Supported filters:

- `domain`
- `area`
- `label`
- `exclude` entity IDs

Selector rules:

- at least one of `domain`, `area`, or `label` is required
- filters are ANDed
- selectors expand dynamically during planning/reconciliation
- selector membership removal withdraws immediately
- `linger` applies only when observation becomes false
- expanded records carry selector provenance
- explicit targets merge with selector-expanded targets

## Suppression

Top-level `blocks` is replaced by intent suppression:

```yaml
intent:
  suppress:
    rules:
      - phone-ringing-color-cycle
```

Suppression rules:

- explicit rule IDs only
- suppression applies while observation is active
- suppression does not linger initially
- suppression suppresses rule-generated intents and effects
- suppression does not suppress manual records
- suppression does not undo already-applied effects
- any active suppression wins
- suppression records are explainable

Rule and scene `labels` are metadata only initially, reserved for UI/filtering/future suppression selectors.

## Effects

Effects are explicit side-effect escape hatches.

```yaml
effect:
  service: telegram_bot.send_message
  data:
    message: Doorbell
```

`effect` may be a single object or list:

```yaml
effect:
  - service: notify.mobile
    data:
      message: Door open
  - service: logbook.log
    data:
      name: Door
      message: Opened
```

Effect service is a single `domain.service` string. Unknown services are allowed with minimal validation. Optional `target` is supported:

```yaml
effect:
  service: scene.turn_on
  target:
    entity_id: scene.movie
  data:
    transition: 3
```

Effect semantics:

- effects run once per observation activation
- level plus `for` runs effect when dwell completes
- a rule may have both `intent` and `effect`
- intent exists while observation is active
- effect runs on activation edge
- multiple effects run independently
- failed effect does not block later effects
- effects are not auto-retried initially
- authority does not arbitrate effects
- effect records carry authority/reason for diagnostics

Persist effect dedupe for level observation activations so restart/reload does not re-emit while the same observation remains active. Clear applied-effect records when observation becomes inactive.

## Durable Intent Vs Effect

Rule:

```text
If a field can be compared to Home Assistant actual state, it may be intent.
If a command or operation cannot be observed/reconciled, it must be effect.
```

Intent example:

```yaml
intent:
  media_player.tv:
    state: on
    volume_level: 0.35
    source: HDMI 2
```

Effect example:

```yaml
effect:
  service: media_player.play_media
  target:
    entity_id: media_player.tv
  data:
    media_content_id: media-source://album/1
    media_content_type: music
```

Effect-only examples:

- notifications initially
- `script.turn_on`
- `automation.trigger`
- `button.press`
- `input_button.press`
- `remote.send_command`
- relative helper commands like increment/decrement/select_next/select_previous
- `toggle`
- Home Assistant scene activation
- MQTT publish

Absolute helper values remain intents.

## Persistent Reconciliation Model

The controller source of truth is:

- valid config
- current Home Assistant state
- persisted lifecycle records
- manual records

Do not persist broad observation state. Current Home Assistant state is the source of truth after restart.

Persist only lifecycle records:

- manual override records
- lingering intent records
- edge TTL intent records
- effect applied records

Normal level-created intents are recomputed.

Desired record IDs should be deterministic:

```text
rule:<rule_id>:target:<entity_id>
rule:<rule_id>:selector:<selector_hash>:target:<entity_id>
rule:<rule_id>:scene:<scene_id>:target:<entity_id>
manual:<target>:<uuid-or-created-at>
edge:<rule_id>:target:<entity_id>:<edge_id>
effect:<rule_id>:<effect_index>:<activation_id>
suppress:<source_rule_id>:<target_rule_id>
```

Rule generation:

```text
rule_generation = hash(normalized observe + intent + effect + metadata)
```

Persisted records include generation.

Reload behavior:

- valid reload plus same rule generation preserves lifecycle records
- valid reload plus changed generation invalidates records for the changed rule
- invalid reload keeps old config and lifecycle records
- removed rule deletes records for that rule

## Manual Overrides

Manual drift becomes a persistent desired record only for currently managed targets.

```text
source: manual
target: light.desk
intent: { state: off }
authority: user
expires_at: now + ttl
reason: Manual HA state change
```

Newest manual override replaces previous manual records for the same target. Manual records are not tied to rule generation.

`intentional.clear` remains a service/effect to clear manual records:

```yaml
effect:
  service: intentional.clear
  data:
    target: light.office
```

## Reconciler

For durable intents, actual-state diff is primary.

Flow:

```text
1. evaluate observations
2. plan desired records
3. restore lifecycle records
4. resolve target values through compositor
5. compare desired vs actual HA state
6. apply minimal service calls only when actual differs
7. update status conditions
```

The service signature cache may remain as a status/dedupe aid, but it is not the primary reconciliation model.

Failed durable intent applies retry on later reconcile ticks with backoff. Effect failures are not auto-retried initially.

Use condition-style status:

```json
{
  "type": "Reconciled",
  "status": "False",
  "reason": "ServiceCallFailed",
  "message": "light.turn_on failed"
}
```

Condition types:

- `ObservationActive`
- `DesiredResolved`
- `Reconciled`
- `Blocked`
- `Suppressed`
- `EffectApplied`
- `Expired`
- `ValidationError`

Health summary:

```json
{
  "status": "ok|degraded|error",
  "active_rules": 12,
  "unreconciled_targets": 1,
  "failed_effects": 0,
  "validation_errors": 0
}
```

## AI-Optimized API

Add a comprehensive world endpoint:

```http
GET /api/intentional/world
```

Include scoped data:

- DSL version
- config generation
- rules
- scenes
- current observation status
- scoped Home Assistant entity state
- selector expansion diagnostics
- desired records
- resolved targets
- actual-vs-desired diff
- effect records/status
- conditions
- health
- errors/warnings
- natural-language summaries

Do not dump all Home Assistant state. Include entities referenced by observations, selected by selectors, with desired records, or with manual records.

Schema endpoint:

```http
GET /api/intentional/schema
```

Returns DSL schema and capability metadata:

- intent domains and fields
- field types/ranges/enums
- observe operators
- selector capabilities
- effect service policy

Validation endpoint:

```http
POST /api/intentional/validate
```

Returns validity, errors, warnings, normalized representation, and expanded scenes/selectors where possible.

Dry run endpoint:

```http
POST /api/intentional/dry-run
```

Accepts proposed YAML and optional state overrides. Returns planned active rules, desired records, resolved targets, effects, and errors. Does not apply.

Explain endpoint:

```http
GET /api/intentional/explain?target=light.living_room
```

Returns structured contributors plus concise natural summary.

Patch-by-ID endpoints:

```http
PATCH /api/intentional/rules/by-id/<id>
PATCH /api/intentional/scenes/by-id/<id>
```

Patch rules:

- full replacement initially
- updating existing ID locates owning file automatically
- creating requires file
- validation always runs
- dry run is optional
- `expected_generation` is required unless `force: true`
- comment preservation is best effort
- if comment preservation is too hard, rewrite normalized YAML and report `formatting_preserved: false`

Whole-file writes remain available:

```http
PUT /api/intentional/rules/<file>
```

`expected_generation` is optional for whole-file writes.

Use `notes` metadata for rules/scenes so agents do not need to preserve comments.

## Implementation Sequence

1. Add new domain model: observations, intent specs, effect specs, scene specs, selector specs.
2. Replace old loader schema with `observe` + `intent` + `effect` DSL.
3. Implement normalization from DSL to target intent specs.
4. Implement scene include expansion with merge and cycle detection.
5. Implement selector resolver abstraction and fake resolver tests.
6. Implement structured observation evaluator with `all`, `any`, `not`, `none`, comparisons, and `for`.
7. Implement edge/event facts with `changed`, `happened`, `within`, and one-cycle default.
8. Add lifecycle validation: edge requires `ttl`, level allows `linger`, never both.
9. Implement persistent lifecycle records for manual, linger, edge TTL, and effect dedupe.
10. Refactor active intents into desired records and reconciliation status.
11. Tighten HA adapter capability table: durable reconcilable fields only under `intent`.
12. Move action-like support to `effect`.
13. Implement actual-vs-desired diff before service calls.
14. Add condition/status model and health summary.
15. Add `/schema`, `/validate`, `/dry-run`, `/world`, and improved `/explain`.
16. Add patch-by-ID API with generation checks.
17. Rewrite docs/examples around `observe -> intent`.

## Minimal First Slice

Start with level observation to durable intent:

```yaml
- id: living-room-tv
  observe:
    media_player.tv: on
  intent:
    light.living_room:
      color_temp_k: 2700
      brightness_pct:
        max: 40
```

Then level observation with dwell to effect:

```yaml
- id: door-left-open
  observe:
    binary_sensor.front_door: on
    for: 30s
  effect:
    service: notify.mobile
    data:
      message: Front door has been open for 30 seconds
```

Then edge-created temporary animated intent:

```yaml
- id: front-door-pulse
  observe:
    changed:
      binary_sensor.front_door:
        to: on
  intent:
    light.monitor_back_led:
      ttl: 20s
      brightness_pct:
        animate:
          pulse: [0, 100, 0]
          duration: 2s
```

This lands the new conceptual model before selectors, scenes, and API expansion.
