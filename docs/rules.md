# Rule Format Reference

Intentional stores authored rules in Home Assistant storage. YAML is the
authoring, import, export, API, and Configure-panel format. On first setup, the
integration imports existing YAML files from the configured rule directory if no
stored rule document exists yet.

Rule YAML describes reconciliation rules:

```text
observe -> intent
```

`observe:` decides whether a rule is active. `intent:` describes durable desired state. `effect:` describes explicit side effects and is not treated as desired state.

## YAML Document Shapes

The stored YAML document may be a plain list of rules:

```yaml
- id: office-light
  observe:
    binary_sensor.office_occupancy: on
  intent:
    light.office:
      state: on
      brightness_pct: 70
```

Or a document with reusable scenes:

```yaml
scenes:
  focus:
    intent:
      light.office:
        state: on
        brightness_pct: 80
        color_temp_k: 4000

rules:
  - id: focus-mode
    observe:
      input_boolean.focus_mode: on
    intent:
      include: scene.focus
```

Invalid new config is rejected and the previous active config remains running.

## Rule Fields

```yaml
- id: office-after-hours
  enabled: true
  labels: [office, light]
  notes: Optional private authoring notes
  authority: automation
  confidence: 0.6
  reason: Office occupied outside working hours
  observe:
    binary_sensor.office_occupancy: on
    schedule.office_working_hours:
      is: off
    for: 2s
  intent:
    light.office:
      state: on
      brightness_pct: 40
      color_temp_k: 2700
      linger: 190s
      apply:
        transition:
          assert: 3s
          change: 5s
          withdraw: 7s
  effect:
    service: notify.mobile_app_phone
    data:
      message: Office occupied
```

| Field | Required | Default | Notes |
| --- | --- | --- | --- |
| `id` | yes | none | Unique authored rule ID. |
| `enabled` | no | `true` | Rule switches persist this field. |
| `labels` | no | `[]` | Metadata for humans and agents. |
| `notes` | no | `""` | Authoring notes. |
| `authority` | no | `automation` | `sensor`, `automation`, or `user`. |
| `confidence` | no | `1.0` | Tiebreaker within an authority tier. |
| `reason` | no | `""` | Human-readable explanation surfaced in status. |
| `observe` | no | always active | Structured observation. |
| `intent` | no | none | Durable desired target state. |
| `effect` | no | none | Side-effect service call. |

Authority order is `sensor < automation < user`. Within the same authority tier, higher confidence wins, then newer intents win.

## Observations

Scalar values imply `is`:

```yaml
observe:
  binary_sensor.office_occupancy: on
```

Multiple mapping entries imply `all`:

```yaml
observe:
  binary_sensor.office_occupancy: on
  sensor.office_illuminance:
    lt: 50
```

Supported comparison operators:

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

Explicit boolean composition:

```yaml
observe:
  any:
    - binary_sensor.office_motion: on
    - binary_sensor.office_presence: on
```

```yaml
observe:
  not:
    input_boolean.guest_mode: on
```

```yaml
observe:
  none:
    - input_boolean.sleep_mode: on
    - input_boolean.vacation_mode: on
```

### Dwell

Use `for` when a level observation must remain true before the rule activates:

```yaml
observe:
  binary_sensor.hallway_motion: on
  for: 2m
```

`for` can also read a numeric helper:

```yaml
observe:
  binary_sensor.office_occupancy: on
  for:
    entity: input_number.office_on_delay
    unit: s
    default: 2m
```

Supported units are `ms`, `s`, `m`, and `h`.

### Edges And Events

Use `changed` for state-change edges:

```yaml
observe:
  changed:
    binary_sensor.front_door:
      from: off
      to: on
```

Use `happened` for Home Assistant `event.*` entities:

```yaml
observe:
  happened:
    event.espnow_recv_doorbell:
      event_type: ringer
      within: 5s
```

Edge-created intents require `ttl`, because an edge does not remain true forever:

```yaml
- id: front-door-alert-light
  observe:
    changed:
      binary_sensor.front_door:
        to: on
  intent:
    light.hall:
      state: on
      brightness_pct: 100
      ttl: 30s
```

### Selectors

Selectors let a rule observe groups discovered from Home Assistant metadata:

```yaml
observe:
  select:
    any:
      domain: binary_sensor
      area: living_room
      label: motion
      is: on
```

Supported selector modes are `any`, `all`, and `none`. Supported filters are `domain`, `area`, `label`, `exclude`, and `field`; `field` defaults to `state`.

## Intents

An `intent:` maps target entity IDs to durable desired state:

```yaml
intent:
  light.living_room:
    state: on
    brightness_pct: 80
    color_temp_k: 2700
```

Field values can be direct values or operators:

```yaml
intent:
  light.living_room:
    brightness_pct:
      max: 40
    color_temp_k:
      value: 2700
```

Supported field operators:

- `value`: absolute value; same as writing the scalar directly.
- `min` or `floor`: lower bound.
- `max` or `cap`: upper bound.
- `offset`: additive adjustment.
- `multiply`: multiplicative adjustment.

Composition order:

1. Highest-priority `value` wins per field.
2. `max`/`cap` clamps from above.
3. `min`/`floor` clamps from below.
4. `offset` values sum.
5. `multiply` values apply once.
6. Basic device bounds are enforced where known.

## Target Lifecycle

Target metadata belongs beside the target fields:

```yaml
intent:
  light.office:
    state: on
    brightness_pct: 40
    linger: 190s
    apply:
      transition:
        assert: 3s
        change: 5s
        withdraw: 7s
```

Supported metadata:

- `ttl`: expire the intent after a duration.
- `linger`: keep the intent active for a duration after a level observation turns false.
- `transition`: HA-native transition duration for simple light changes.
- `easing`: animation easing value.
- `apply.transition.assert`: transition when the desired target is first asserted.
- `apply.transition.change`: transition when the desired value changes while active.
- `apply.transition.withdraw`: transition when the target withdraws or reveals a lower-priority state.

`ttl` and `linger` are mutually exclusive for the same target intent.

When a final `state: on` intent withdraws from a safe on/off domain such as `light`, `switch`, `input_boolean`, `fan`, or `siren`, Intentional can reconcile to `state: off` using the withdraw transition. If another lower-priority intent remains, Intentional reconciles to that revealed state instead.

## Generated Values

Generated values periodically sample a durable field while the intent remains active:

```yaml
intent:
  light.monitor_backlight:
    state: on
    brightness_pct: 35
    rgb_color:
      generate:
        kind: sample
        from:
          - [255, 120, 40]
          - [255, 70, 120]
          - [140, 70, 255]
        every:
          min: 45s
          max: 4m
        transition:
          min: 8s
          max: 25s
```

Supported generated-value fields:

- `kind: sample`: choose one value from `from`.
- `every`: fixed duration or `{min, max}` random duration.
- `transition`: optional fixed duration or `{min, max}` random HA transition duration.

Generated values persist across restarts and avoid immediate repeats when alternatives exist.

## Effects

Effects are service calls, not desired state:

```yaml
- id: doorbell-message
  observe:
    happened:
      event.espnow_recv_doorbell:
        event_type: ringer
        within: 5s
  effect:
    service: telegram_bot.send_message
    data:
      message: Doorbell
```

Effects run once per observation activation. Use effects for notifications, announcements, one-shot scripts, or other side effects that cannot be represented as durable target state.

## Templates

Jinja templates are allowed in scalar values under `intent` and `effect.data`:

```yaml
intent:
  light.office:
    brightness_pct: "{{ states('input_number.office_brightness') | int }}"
```

```yaml
effect:
  service: notify.mobile_app_phone
  data:
    message: "Door opened at {{ now().strftime('%H:%M') }}"
```

Templates are not supported in target names, field names, operator names, or service names.

## Scenes

Scenes are reusable intent bundles:

```yaml
scenes:
  bedtime:
    intent:
      light.bedroom:
        state: on
        brightness_pct: 15
      fan.bedroom:
        state: on

rules:
  - id: bedtime-helper
    observe:
      input_boolean.bedtime: on
    intent:
      include: scene.bedtime
```

## Manual Overrides

Intentional tracks manual overrides in two ways:

- `intentional.fire` emits a `user` authority intent with a TTL.
- State drift on an actively managed target is captured as a `user` authority intent.

Example service call:

```yaml
action: intentional.fire
data:
  target: light.living_room
  state: on
  brightness_pct: 80
  color_temp_k: 2700
  ttl: 7200
```

Clear overrides with the service or HA buttons:

```yaml
action: intentional.clear
data:
  target: light.living_room
```

Omit `target` to clear all manual overrides.

`intentional.fire` accepts the same supported target fields as rule target
payloads: `state`, `brightness_pct`, `brightness`, `color_temp_k`,
`color_temp_mired`, `rgb_color`, `rgbw_color`, `rgbww_color`, `hs_color`,
`xy_color`, `effect`, `flash`, `volume_level`, `is_volume_muted`, `tone`,
`source`, `sound_mode`, `media_action`, `media_content_id`,
`media_content_type`, `enqueue`, `announce`, `extra`, `shuffle`, `repeat`,
`seek_position`, `group_members`, `position`, `tilt_position`, `percentage`,
`hvac_mode`, `temperature`, `target_temp_low`, `target_temp_high`,
`preset_mode`, `fan_mode`, `direction`, `oscillating`, `value`, `option`,
`cycle`, `humidity`, `swing_mode`, `swing_horizontal_mode`, `aux_heat`,
`code`, `message`, `name`, `title`, `data`, `service`, `service_data`,
`media_player_entity_id`, `cache`, `language`, `options`, `browser_id`,
`user_id`, `path`, `action_text`, `action`, `parse_mode`,
`disable_notification`, `disable_web_page_preview`, `keyboard`,
`inline_keyboard`, `message_tag`, `chat_id`, `todo_action`, `item`, `rename`,
`status`, `due_date`, `due_datetime`, `description`, `variables`,
`skip_condition`, `datetime`, `date`, `time`, `timestamp`, `duration`, `mode`,
`operation_mode`, `away_mode`, `fan_speed`, `command`, `params`,
`cleaning_area_id`, `activity`, `device`, `num_repeats`, `delay_secs`,
`hold_secs`, `camera_action`, `filename`, `media_player`, `format`, `lookback`,
`update_action`, `version`, `backup`, `mac`, `dev_id`, `host_name`,
`location_name`, `gps`, `gps_accuracy`, `battery`, `reverse`, and
`update_entity`.

For example, a dashboard button can force TV settings without creating a YAML
rule:

```yaml
action: intentional.fire
data:
  target: media_player.tv
  state: on
  source: HDMI 2
  volume_level: 0.35
  ttl: 1800
```

## Supported Target Fields

Intentional can plan service calls for common HA domains, including:

- `light`, `switch`, `input_boolean`
- `media_player`, `cover`, `fan`, `climate`, `humidifier`, `water_heater`
- `vacuum`, `lawn_mower`, `remote`, `camera`
- `number`, `input_number`, `counter`, `select`, `input_select`, `text`, `input_text`, `input_datetime`
- `lock`, `alarm_control_panel`, `valve`, `siren`
- `button`, `input_button`, `scene`, `script`, `automation`, `timer`, `update`
- `notify`, `telegram_bot`, `browser_mod`, `tts`, `persistent_notification`, `logbook`, `system_log`, `mqtt`, `rest_command`

For lights, `color_temp_k` is sent to HA as `color_temp_kelvin`; `color_temp_mired` is sent as `color_temp`. HA devices that clamp to nearby achievable Kelvin values are treated as matching within tolerance.

Use `effect:` for action-like operations that should not be treated as durable state. Durable `intent:` rejects action-only patterns such as `state: toggle` for reconciliation targets.

## Duration Shorthand

Duration fields accept:

- `500ms`
- `1.5s`
- `5m`
- `2h`
- `1h30m15s`

An integer is interpreted as milliseconds.
