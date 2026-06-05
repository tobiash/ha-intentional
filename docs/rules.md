# Rule format reference

A complete reference for the YAML rule format. See `examples/` for working
rule files demonstrating each pattern.

## Top-level fields

```yaml
- id: rule-name                    # required, unique across all files
  extends: base-rule-id            # optional, inherit common fields
  when: "expression"               # required, see "When expressions" below
  for: 5m                          # optional, condition must hold this long
  emit:                            # required, what to claim
    target: light.living_room
    set: { brightness_pct: 80 }    # absolute values
    cap: { brightness_pct: 40 }    # ceiling
    floor: { brightness_pct: 5 }   # floor
    offset: { brightness_pct: -10 }
    multiply: { brightness_pct: 0.9 }
    merge: false
    transition: 1.5s
    easing: ease-in-out
    ttl: 2h
    animation:
      kind: pulse
      parameter: brightness_pct
      values: [0, 100, 0]
      duration: 2s
      repeat: 4
  authority: automation            # sensor | automation | user
  confidence: 0.9                  # 0.0 .. 1.0
  reason: "Dark outside"           # human-readable, surfaced in UI
  blocks: [other-rule-id]          # suppress these rules when this is active
```

| Field         | Required | Default       | Notes                                |
|---------------|----------|---------------|--------------------------------------|
| `id`          | yes      | —             | unique across all rule files         |
| `extends`     | no       | —             | inherit fields from another rule ID  |
| `when`        | yes      | —             | trigger expression                   |
| `for`         | no       | `0`           | require `when` to stay true this long|
| `emit`        | yes      | —             | what this rule claims                |
| `authority`   | no       | `automation`  | `sensor`, `automation`, or `user`    |
| `confidence`  | no       | `1.0`         | 0.0 to 1.0, tiebreaker within tier   |
| `reason`      | no       | `""`          | shown in UI/logs                     |
| `blocks`      | no       | `[]`          | list of rule IDs to suppress         |

When a rule is firing, every rule ID in its `blocks` list is treated as not
firing. Any existing intent from the blocked rule is withdrawn on the next
evaluation cycle. Blocking is explicit: if two currently firing rules block
each other, both are suppressed.

## Rule inheritance

Use `extends` to share common rule structure across a rule pack. The child
rule inherits the referenced rule's top-level fields and `emit` block, then
overrides the fields it defines.

```yaml
- id: living-room-default
  when: sensor.outdoor_light.illuminance < 50
  emit:
    target: light.living_room
    set: { state: on, brightness_pct: 70, color_temp_k: 2700 }
    transition: 1s
  confidence: 0.5

- id: living-room-tv
  extends: living-room-default
  when: media_player.tv == "on"
  emit:
    set: { brightness_pct: 25 }
    cap: { brightness_pct: 40 }
  confidence: 0.9
```

For `emit.set`, `emit.cap`, `emit.floor`, `emit.offset`, and
`emit.multiply`, dictionaries are merged per field. In the example above,
`living-room-tv` keeps `state: on` and `color_temp_k: 2700`, overrides
`brightness_pct`, and adds the brightness cap. Other child fields replace the
parent value. `extends` can reference rules in earlier or later YAML files in
the same rule directory. Cycles and unknown parent IDs fail rule loading.

## Emit fields

The `emit` block describes what the rule claims. All sub-fields are optional
except `target` OR `scene` (exactly one is required).

| Field        | Default | Description                                         |
|--------------|---------|-----------------------------------------------------|
| `target`     | —       | entity_id (required unless `scene` is set)           |
| `scene`      | —       | HA scene entity_id (required unless `target` is set) |
| `set`        | `{}`    | absolute values; per-field priority wins             |
| `cap`        | `{}`    | smallest cap wins; clamps from above                 |
| `floor`      | `{}`    | largest floor wins; clamps from below                |
| `offset`     | `{}`    | all offsets sum; additive                             |
| `multiply`   | `{}`    | each multiply applies once; not compounded           |
| `merge`      | `false` | reserved (per-field merge is always on)               |
| `transition` | `0`     | duration string; HA's light.turn_on transition param  |
| `easing`     | `linear`| `linear`, `ease-in`, `ease-out`, `ease-in-out`, `sine`|
| `ttl`        | `None`  | time-to-live; auto-releases the intent                 |
| `animation`  | `null`  | time-varying value spec, see below                    |

**`target` and `scene` are mutually exclusive.** A rule either claims an
intent for a specific entity (operates through the compositor), or it
references a HA scene (the integration calls `scene.turn_on`). See the
[Scenes](#scenes) section below.

### Target application

Resolved `target` intents are applied to Home Assistant through service calls.
The integration currently supports:

| Domain          | Service behavior                                      |
|-----------------|--------------------------------------------------------|
| `light.*`       | `light.turn_on`, `light.turn_off`, `light.toggle`      |
| `switch.*`      | `switch.turn_on`, `switch.turn_off`, `switch.toggle`   |
| `input_boolean.*` | `input_boolean.turn_on`, `input_boolean.turn_off`, `input_boolean.toggle` |
| `media_player.*` | `turn_on`, `turn_off`, `toggle`, transport controls, `play_media`, `volume_set`, `volume_mute`, `select_source`, `select_sound_mode`, `shuffle_set`, `repeat_set`, `media_seek`, `join`, `unjoin` |
| `cover.*`       | `open_cover`, `close_cover`, `stop_cover`, `toggle`, `set_cover_position`, `open_cover_tilt`, `close_cover_tilt`, `stop_cover_tilt`, `toggle_tilt`, `set_cover_tilt_position` |
| `fan.*`         | `turn_on`, `turn_off`, `toggle`, `set_percentage`, `set_preset_mode`, `set_direction`, `oscillate` |
| `climate.*`     | `turn_on`, `turn_off`, `toggle`, `set_hvac_mode`, `set_temperature`, `set_preset_mode`, `set_fan_mode`, `set_humidity`, `set_swing_mode`, `set_swing_horizontal_mode`, `set_aux_heat` |
| `humidifier.*`  | `turn_on`, `turn_off`, `set_humidity`, `set_mode`      |
| `water_heater.*` | `turn_on`, `turn_off`, `set_temperature`, `set_operation_mode`, `set_away_mode` |
| `vacuum.*`      | `start`, `pause`, `stop`, `return_to_base`, `locate`, `clean_spot`, `clean_area`, `set_fan_speed`, `send_command`, `turn_on`, `turn_off`, `toggle` |
| `lawn_mower.*`  | `start_mowing`, `pause`, `dock`                       |
| `remote.*`      | `turn_on`, `turn_off`, `toggle`, `send_command`       |
| `number.*`, `input_number.*` | `set_value`                            |
| `counter.*`    | `set_value`, `increment`, `decrement`, `reset`         |
| `select.*`, `input_select.*` | `select_option`, `select_next`, `select_previous`; `input_select` also supports `select_first`, `select_last` |
| `text.*`, `input_text.*` | `set_value`                                  |
| `todo.*`       | `add_item`, `update_item`, `remove_item`, `remove_completed_items`, `get_items` |
| `input_text.*`  | `set_value`                                            |
| `input_datetime.*` | `set_datetime`                                     |
| `lock.*`        | `lock`, `unlock`                                       |
| `alarm_control_panel.*` | `alarm_arm_home`, `alarm_arm_away`, `alarm_arm_night`, `alarm_arm_vacation`, `alarm_arm_custom_bypass`, `alarm_disarm` |
| `valve.*`       | `open_valve`, `close_valve`, `stop_valve`, `set_valve_position`, `toggle` |
| `siren.*`       | `turn_on`, `turn_off`, `toggle`                        |
| `notify.*`      | calls the matching notify service                      |
| `button.*`      | `button.press`                                         |
| `input_button.*` | `input_button.press`                                  |
| `scene.*`       | `scene.turn_on`                                        |
| `script.*`      | `script.turn_on`, or `script.turn_off` with `state: off` |
| `automation.*`  | `automation.trigger`, or `turn_on` / `turn_off` with `state` |
| `timer.*`       | `timer.start`, `timer.cancel`, `timer.pause`, `timer.finish` |

For lights, `state: off` calls `light.turn_off`, `state: toggle` calls
`light.toggle`, and any other resolved value calls `light.turn_on`. The engine
field `color_temp_k` is sent as HA's `color_temp_kelvin`, and
`color_temp_mired` is sent as `color_temp`. Light rules also support
`rgb_color`, `rgbw_color`, `rgbww_color`, `hs_color`, `xy_color`, `effect`,
and `flash`.
Media players support `state`, `media_action`, `volume_level`,
`is_volume_muted`, `source`, `sound_mode`, `media_content_id`,
`media_content_type`, `enqueue`, `announce`, `extra`, `shuffle`, `repeat`,
`seek_position`, and `group_members`. Use `state` for durable power states or
short action aliases such as `pause`, `next`, and `toggle`; use `media_action`
when the action needs extra fields, such as `play_media`, `mute`, `seek`, or
`join`. Covers support `state` (`open`, `closed`, `stop`, `toggle`,
`tilt_open`, `tilt_closed`, `tilt_stop`, or `tilt_toggle`), `position`, and
`tilt_position`. Fans support `state`
(`on`, `off`, or `toggle`), `percentage`, `preset_mode`, `direction`, and
`oscillating`. Switches and input booleans also
support `state: toggle` for button-style rules where the target is an action
rather than a durable state. Climate entities support `state: on`, `state: off`,
and `state: toggle` for power actions; other `state` values are treated as an
alias for HVAC mode. Climate targets also support
`hvac_mode`, `temperature`, `target_temp_low`, `target_temp_high`,
`preset_mode`, `fan_mode`, `humidity`, `swing_mode`, `swing_horizontal_mode`,
and `aux_heat`. Humidifiers support `state`, `humidity`, and
`mode`. Water heaters support `state`, `temperature`, `operation_mode`, and
`away_mode`; when both `temperature` and `operation_mode` are present, the
operation mode is included in the `set_temperature` call. Vacuums support
`state` values such as `cleaning`, `paused`, `stop`, `returning`, `locate`,
`clean_spot`, `start_pause`, and `toggle`, plus `fan_speed`,
`cleaning_area_id`, `command`, and optional `params`. Lawn mowers support
`state` values such as `mowing`, `paused`, `returning`, and `dock`. Remotes
support `state: on`, `state: off`, `state: toggle`, optional `activity`, and
`command` with optional `device`, `num_repeats`, `delay_secs`, and `hold_secs`.
Number helpers and counters support `value`.
Counters also support `state: increment`, `state: decrement`, and
`state: reset`. Select helpers
support `option`, with `state` as an alias for the selected option. `state:
next` and `state: previous` call the next/previous actions; `input_select`
also supports `state: first` and `state: last`. Add `cycle: false` to prevent
next/previous from wrapping where HA supports it. Text and input text
supports `value`, with `state` as an alias for the text value. Input datetime
helpers support `datetime`, `date`, `time`, and `timestamp`, with `state` as
an alias for `datetime`. Locks support `state: locked` and `state: unlocked`.
Valves support `state` (`open`, `closed`, `stop`, or `toggle`) and `position`.
Sirens support `state: on`, `state: off`, `state: toggle`, plus optional
`tone`, `duration`, and `volume_level`.
Button and input button targets press when their target intent is active; they
do not require a `set` payload.
Alarm panels support `state` values
`armed_home`, `armed_away`, `armed_night`, `armed_vacation`,
`armed_custom_bypass`, and `disarmed`, plus optional `code`. Identical resolved
service plans are de-duplicated so the 100ms engine tick does not repeatedly
call the same HA service. Notify targets use `target: notify.service_name` and
support `message`, optional `title`, and optional `data`; `state` can be used
as a shorthand for `message`. Button, scene, script, and automation targets are
treated as fire-and-forget action targets: once a resolved service plan is
called, normal HA state settling does not re-trigger the action while the same
intent remains active. Script targets support optional `variables`. Automation
targets default to `automation.trigger`, support optional `skip_condition`, and
use `automation.turn_on` / `automation.turn_off` when `state` is `on` or `off`.
To-do targets support `todo_action` values `add_item`, `update_item`,
`remove_item`, `clear_completed`, and `get_items`. Supplying `item` without
`todo_action` defaults to `add_item`; `state: completed` marks an item
complete.
Media-player transport actions that settle into a clear state are checked as
durable plans: `play` expects `playing`, `pause` expects `paused`, and `stop`
expects `idle` or `stopped`. Other media actions such as `play_media`, `seek`,
`join`, `shuffle_set`, and `repeat_set` remain action-style unless HA exposes
matching attributes in its state report.
Timer targets support `state: active`/`start` with optional `duration`,
`state: idle`/`cancel`, `state: paused`/`pause`, and `state: finish`.
Any target can include `update_entity: true` to append
`homeassistant.update_entity` for that entity after the normal service plan.
This is useful for entities whose integration needs an explicit refresh after
an action, such as some covers, travel-time sensors, and template-backed
entities.

## Duration shorthand

Time values (`for`, transition, ttl, animation timing) accept:

- `500ms` — 500 milliseconds
- `1.5s` — 1.5 seconds
- `5m` — 5 minutes
- `2h` — 2 hours
- `1h30m15s` — combined

Or an integer (interpreted as milliseconds).

## Dwell time with `for`

Use top-level `for` when a condition must stay true before the rule should
fire. This is the same practical shape as Home Assistant automation trigger
`for:` and is useful for motion, presence, humidity, or power-state rules
that should ignore brief spikes.

```yaml
- id: hallway-motion-held
  when: binary_sensor.hall_motion == "on"
  for: 2m
  emit:
    target: light.hallway
    set: { state: on, brightness_pct: 60 }
```

If the `when` expression becomes false before the dwell time finishes, the
timer resets. Once the rule fires, it withdraws as soon as `when` becomes
false, unless it is a forced manual intent such as `intentional.activate_scene`.

## When expressions

The `when` field is a string expression evaluated against the current state
of Home Assistant entities. Supported:

- Entity references: `sensor.x.state`, `light.y.brightness`, or just `sensor.x` (defaults to `.state`)
- Comparisons: `==`, `!=`, `<`, `<=`, `>`, `>=`
- Logical operators: `and`, `or`, `not` (with parentheses)
- String literals: `"on"`, `'off'`
- Numeric literals: `42`, `3.14`
- Boolean literals: `true`, `false`
- Time helper: `time_of_day` matches both buckets (`morning`, `afternoon`,
  `evening`, `night`) and exact local clock strings such as `23:00`

Examples:

```yaml
when: sensor.outdoor_light.illuminance < 50
when: media_player.tv.state == "on"
when: sensor.x.state == "on" and sensor.y.state == "ready"
when: time_of_day == "night" or binary_sensor.door == "on"
when: time_of_day >= "22:00" and time_of_day < "23:30"
when: not (sensor.x == "off" and input_boolean.focus == "on")
```

The built-in buckets use local Home Assistant time: `morning` is 05:00-11:59,
`afternoon` is 12:00-16:59, `evening` is 17:00-21:59, and `night` is
22:00-04:59.

## Animation

The `animation` block describes a time-varying value. Four kinds:

### pulse
Discrete values, linear-interpolated, looped.

```yaml
animation:
  kind: pulse
  parameter: brightness_pct
  values: [0, 100, 0]      # values to interpolate through
  duration: 2s             # one full traversal
  repeat: 4                # int count, or "forever"
  easing: sine             # interpolation easing
```

### breath
Smooth sine-wave between min and max.

```yaml
animation:
  kind: breath
  parameter: brightness_pct
  min: 10
  max: 80
  period: 4s               # one full min→max→min cycle
```

### cycle
Smooth oscillation through a list of values (peaks land on values).

```yaml
animation:
  kind: cycle
  parameter: color_temp_k
  values: [2200, 6500]
  period: 3s
```

### flash
Single bright spike with decay.

```yaml
animation:
  kind: flash
  parameter: brightness_pct
  peak: 100
  decay: 0.8s              # time to decay to zero
  repeat: 1                # optional
```

## Composition order

When multiple intents are active for the same target, the compositor
applies them in this order:

1. **`set`** — per-field, highest-priority intent's value wins
2. **`cap`** — smallest cap clamps from above
3. **`floor`** — largest floor clamps from below
4. **`offset`** — all offsets sum
5. **`multiply`** — each multiply applies once to the post-offset value
6. **Device bounds** — physical limits (e.g. 0-100 for brightness)
7. **cap/floor re-apply** — final safety clamp

**Authority is the primary sort key** (`user` > `automation` > `sensor`).
Within an authority tier, **confidence** breaks ties (higher wins).
Within the same confidence, **recency** breaks ties. Within the same
millisecond, the intent object's identity is the final tiebreaker.

## Manual overrides

To inject a user-authority intent from an automation, script, or the
Developer Tools UI, call the `intentional.fire` service:

```yaml
action: intentional.fire
data:
  target: light.living_room
  state: on
  brightness_pct: 80
  color_temp_k: 2700
  ttl: 7200                 # seconds; default 2h
```

This creates a USER-authority intent with a 2-hour TTL. The compositor
will resolve conflicts with automation rules the same way it resolves
two automation rules — user wins, but modifiers compose.

The integration also watches state changes for targets it is actively
managing. If Home Assistant reports state that conflicts with the last service
plan Intentional applied, the new HA state is captured as a USER-authority
manual intent with the same default 2-hour TTL. Matching state reports from
Intentional's own service calls are ignored, so successful automation updates
do not churn into manual overrides.

`intentional.fire` accepts the same supported target fields as rule `set`
payloads: `state`, `brightness_pct`, `brightness`, `color_temp_k`,
`color_temp_mired`, `rgb_color`, `rgbw_color`, `rgbww_color`, `hs_color`,
`xy_color`, `effect`, `flash`, `volume_level`, `is_volume_muted`, `tone`, `source`,
`sound_mode`, `media_action`, `media_content_id`, `media_content_type`,
`enqueue`, `announce`, `extra`, `shuffle`, `repeat`, `seek_position`,
`group_members`, `position`, `tilt_position`, `percentage`,
`hvac_mode`, `temperature`, `target_temp_low`, `target_temp_high`,
`preset_mode`, `fan_mode`, `direction`, `oscillating`, `value`, `option`, `cycle`,
`humidity`, `swing_mode`, `swing_horizontal_mode`, `aux_heat`, `code`,
`message`, `title`,
`data`, `todo_action`, `item`, `rename`, `status`, `due_date`, `due_datetime`,
`description`, `variables`, `skip_condition`, `datetime`, `date`, `time`,
`timestamp`, `duration`, `humidity`, `mode`, `operation_mode`, `away_mode`,
`fan_speed`, `command`, `params`, `cleaning_area_id`, `activity`, `device`,
`num_repeats`, `delay_secs`, `hold_secs`, and `update_entity`.
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

Climate rules and manual intents can set schedules or presence modes:

```yaml
- id: office-heat-workday
  when: input_boolean.workday == "on"
  for: 10m
  emit:
    target: climate.office
    set:
      hvac_mode: heat
      temperature: 21.5
      preset_mode: comfort
```

Helper entities can be first-class targets too:

```yaml
- id: guest-mode-select
  when: binary_sensor.guest_room_motion == "on"
  for: 5m
  emit:
    target: input_select.house_mode
    set: { option: Guest }

- id: night-charge-limit
  when: time_of_day == "night"
  emit:
    target: input_number.ev_charge_limit
    set: { value: 80 }

- id: count-doorbell-rings
  when: binary_sensor.doorbell == "on"
  emit:
    target: counter.doorbell_rings
    set: { state: increment }
    ttl: 5s

- id: quiet-hours-cutoff
  when: input_boolean.guest_mode == "on"
  emit:
    target: input_datetime.quiet_hours_until
    set:
      datetime: "2026-06-05 22:30:00"

- id: hallway-motion-grace
  when: binary_sensor.hallway_motion == "off"
  emit:
    target: timer.hallway_grace
    set:
      state: active
      duration: "00:05:00"
    ttl: 10s
```

Security entities can be controlled through state intents:

```yaml
- id: lock-doors-at-night
  when: time_of_day == "night"
  for: 10m
  emit:
    target: lock.front_door
    set: { state: locked }

- id: arm-home-when-away
  when: input_boolean.away_mode == "on"
  emit:
    target: alarm_control_panel.home
    set:
      state: armed_away
      code: "1234"

- id: close-water-main-on-leak
  when: binary_sensor.water_leak == "on"
  emit:
    target: valve.water_main
    set: { state: closed }
    ttl: 30m

- id: sound-leak-siren
  when: binary_sensor.water_leak == "on"
  emit:
    target: siren.utility_room
    set:
      state: on
      tone: alarm
      duration: 30
      volume_level: 0.8
    ttl: 30s
```

Notifications are also targets. Add a TTL so repeated service calls are
suppressed while the rule remains active, then allowed again after the trigger
withdraws and re-fires:

```yaml
- id: notify-front-door-opened
  when: binary_sensor.front_door == "on"
  emit:
    target: notify.mobile_app_phone
    set:
      title: Security
      message: Front door opened
      data: { tag: front-door }
    ttl: 30s
```

Action targets let rules call existing HA primitives without embedding service
calls in automations:

```yaml
- id: dinner-playlist-when-cooking
  when: input_boolean.cooking == "on"
  emit:
    target: media_player.kitchen
    set:
      media_action: play_media
      media_content_id: media-source://media_source/local/dinner.mp3
      media_content_type: music
      enqueue: play
      group_members: [media_player.dining_room]
      volume_level: 0.35
    ttl: 30m

- id: refresh-travel-time-when-garage-opens
  when: cover.garage == "open"
  emit:
    target: sensor.home_to_work_travel_time
    set:
      update_entity: true
    ttl: 30s

- id: run-movie-script-when-tv-starts
  when: media_player.tv == "on"
  emit:
    target: script.movie_mode
    set:
      variables:
        brightness: 20
    ttl: 10m

- id: trigger-arrival-flow
  when: binary_sensor.driveway_motion == "on"
  emit:
    target: automation.arrival_flow
    set:
      skip_condition: false
    ttl: 30s
```

Remote targets can start an activity or send platform-specific command
sequences:

```yaml
- id: start-movie-activity
  when: input_boolean.movie_mode == "on"
  emit:
    target: remote.living_room
    set:
      state: on
      activity: Watch TV
    ttl: 30m

- id: set-tv-home-screen
  when: input_boolean.movie_mode == "on"
  emit:
    target: remote.android_tv
    set:
      command: [HOME, DPAD_RIGHT, DPAD_CENTER]
      device: Android TV
      num_repeats: 1
      delay_secs: 0.4
    ttl: 30s
```

Vacuum targets can pause or resume cleaning when another household constraint
appears:

```yaml
- id: pause-vacuum-during-meeting
  when: vacuum.office == "cleaning" and binary_sensor.meeting == "on"
  emit:
    target: vacuum.office
    set: { state: paused }
    ttl: 15m

- id: clean-kitchen-after-dinner
  when: input_boolean.dinner_done == "on"
  emit:
    target: vacuum.downstairs
    set:
      cleaning_area_id: [kitchen]
      fan_speed: turbo
    ttl: 30m
```

Lawn mower targets can react to weather or yard safety conditions:

```yaml
- id: dock-mower-when-raining
  when: lawn_mower.backyard == "mowing" and binary_sensor.rain == "on"
  emit:
    target: lawn_mower.backyard
    set: { state: returning }
    ttl: 30m
```

To manually activate a scene rule (e.g. from a button or voice command),
use `intentional.activate_scene` instead of calling `scene.turn_on`
directly — that way the rule's transition and TTL are honored:

```yaml
action: intentional.activate_scene
data:
  rule_id: movie-scene-from-button
  ttl: 0                    # 0 = use the rule's default TTL
```

## Scenes

HA scenes bundle multiple entity states into one atomic activation:

```yaml
# scenes.yaml
scene:
  - name: Movie
    entities:
      light.living_room: { state: on, brightness: 30, color_temp: 400 }
      media_player.tv: { state: on, source: "HDMI 2" }
      cover.blinds: { state: closed }
```

ha-intentional rules can reference a scene by entity_id instead of
operating through the compositor:

```yaml
- id: movie-scene-from-mode
  when: input_boolean.movie_mode == "on"
  emit:
    scene: scene.movie         # NOT a target — references a HA scene
    transition: 3s
  authority: user
```

**What happens when the rule fires:**
1. The engine sees the rule's `when` is now true
2. The integration layer calls `scene.turn_on entity_id=scene.movie transition=3`
3. HA applies all the scene's entity states atomically
4. When `input_boolean.movie_mode` goes back to "off" (or the TTL expires),
   the rule stops firing — the integration tracks this so a future activation
   can turn the scene on cleanly

While the same scene rule remains active, `scene.turn_on` is not called on
every engine tick. The activation is cached until the rule withdraws, then a
future trigger can activate it again.

**Scenes don't go through the compositor** — they bypass the priority
system entirely. If you want a rule's cap/floor to apply to a light that
a scene controls, write a *separate* rule targeting that light:

```yaml
# Scene: sets brightness to whatever the scene defines
- id: movie-scene-from-mode
  when: input_boolean.movie_mode == "on"
  emit:
    scene: scene.movie
  authority: user

# Modifier: caps that light's brightness, regardless of source
- id: movie-energy-cap
  when: input_boolean.movie_mode == "on"
  emit:
    target: light.living_room
    cap: { brightness_pct: 50 }
  authority: automation
```

The two rules fire from the same `when`. The scene sets the light, the
cap rule clamps it on the next tick. They never conflict.

## Hot reload

The engine watches your rule directory. Save a file and the engine
reloads it within a few seconds — no Home Assistant
restart needed. Errors in the YAML are logged but don't crash the
integration; fix the file and the next save reloads it.

You can also force a reload manually:

```yaml
action: intentional.reload
```

## Rule IDs

Rule IDs are global — they must be unique across all files. They are
used in `blocks:` references and in event/log attribution. Pick a
naming convention that makes their purpose clear, e.g.:

- `dim-when-tv`
- `door-open-led-pulse`
- `motion-bright-hallway`
- `movie-scene`

The numerical prefix on filenames (`01-ambient.yaml`) is for human
organization only — it has no effect on priority or load order beyond
making the rules appear in that order in lists.
