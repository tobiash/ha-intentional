# Rule format reference

A complete reference for the YAML rule format. See `examples/` for working
rule files demonstrating each pattern.

## Top-level fields

```yaml
- id: rule-name                    # required, unique across all files
  when: "expression"               # required, see "When expressions" below
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
| `when`        | yes      | —             | trigger expression                   |
| `emit`        | yes      | —             | what this rule claims                |
| `authority`   | no       | `automation`  | `sensor`, `automation`, or `user`    |
| `confidence`  | no       | `1.0`         | 0.0 to 1.0, tiebreaker within tier   |
| `reason`      | no       | `""`          | shown in UI/logs                     |
| `blocks`      | no       | `[]`          | list of rule IDs to suppress         |

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

## Duration shorthand

Time values (transition, ttl, animation timing) accept:

- `500ms` — 500 milliseconds
- `1.5s` — 1.5 seconds
- `5m` — 5 minutes
- `2h` — 2 hours
- `1h30m15s` — combined

Or an integer (interpreted as milliseconds).

## When expressions

The `when` field is a string expression evaluated against the current state
of Home Assistant entities. Supported:

- Entity references: `sensor.x.state`, `light.y.brightness`, or just `sensor.x` (defaults to `.state`)
- Comparisons: `==`, `!=`, `<`, `<=`, `>`, `>=`
- Logical operators: `and`, `or`, `not` (with parentheses)
- String literals: `"on"`, `'off'`
- Numeric literals: `42`, `3.14`
- Boolean literals: `true`, `false`
- Time helper: `time_of_day` (one of: `morning`, `afternoon`, `evening`, `night`)

Examples:

```yaml
when: sensor.outdoor_light.illuminance < 50
when: media_player.tv.state == "on"
when: sensor.x.state == "on" and sensor.y.state == "ready"
when: time_of_day == "night" or binary_sensor.door == "on"
when: not (sensor.x == "off" and input_boolean.focus == "on")
```

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
  brightness_pct: 80
  ttl: 7200                 # seconds; default 2h
```

This creates a USER-authority intent with a 2-hour TTL. The compositor
will resolve conflicts with automation rules the same way it resolves
two automation rules — user wins, but modifiers compose.

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
- id: movie-scene-from-button
  when: input_button.movie_button == "pressed"
  emit:
    scene: scene.movie         # NOT a target — references a HA scene
    transition: 3s
  authority: user
```

**What happens when the rule fires:**
1. The engine sees the rule's `when` is now true
2. The integration layer calls `scene.turn_on entity_id=scene.movie transition=3`
3. HA applies all the scene's entity states atomically
4. When `input_button.movie_button` goes back to "not pressed" (or the
   TTL expires), the rule stops firing — the integration tracks this
   so the next press re-activates the scene cleanly

**Scenes don't go through the compositor** — they bypass the priority
system entirely. If you want a rule's cap/floor to apply to a light that
a scene controls, write a *separate* rule targeting that light:

```yaml
# Scene: sets brightness to whatever the scene defines
- id: movie-scene-from-button
  when: input_button.movie_button == "pressed"
  emit:
    scene: scene.movie
  authority: user

# Modifier: caps that light's brightness, regardless of source
- id: movie-energy-cap
  when: input_button.movie_button == "pressed"
  emit:
    target: light.living_room
    cap: { brightness_pct: 50 }
  authority: automation
```

The two rules fire from the same `when`. The scene sets the light, the
cap rule clamps it on the next tick. They never conflict.

## Hot reload

The engine watches your rule directory. Save a file and the engine
reloads it within a few hundred milliseconds — no Home Assistant
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
