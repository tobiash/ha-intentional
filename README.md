# ha-intentional

**Declarative, composable, intent-based automation for Home Assistant.**

`ha-intentional` is a HACS-installable custom component for Home Assistant that replaces imperative `automation:` rules with a *declarative intent engine*. Instead of writing "when X, do Y, unless Z," you write **intents** — claims about how a device should be — and the engine resolves conflicts between them using priorities, modifiers, and time.

## Why?

Home Assistant's `automation:` system is powerful but has a familiar scaling problem:

- Rules need to be **ordered manually** to handle conflicts ("if the rule order changes, the bedroom light stops working")
- Adding a new automation can **silently break** older ones that depended on the order
- **Modifiers** like "dim this to a max of 40%" require a separate `input_number` + script + automation, and the modifier *itself* needs to be coordinated
- **Animations** (pulses, fades) require either Node-RED or per-rule `light.turn_on` chains
- **Manual overrides** are basically impossible to do gracefully — once you touch the light, automations don't know what to do

`ha-intentional` solves all of these by changing the abstraction. You don't write rules. You write **intents** — claims with priority metadata. The engine resolves them.

## The mental model

> An *intent* is a claim about how a target entity should be, with **priority metadata** explaining where the claim came from and how strongly it's held.
>
> The *compositor* is a pure function: given a set of active intents for a target, compute the final value to apply. Higher-priority intents win. Modifiers (caps, floors, offsets, animations) compose across all intents.
>
> Manual overrides are just intents with `authority: user` and a TTL. They lose gracefully when the TTL expires — no cleanup, no special-casing.

## Quick example

```yaml
# rules/01-ambient.yaml
- id: brighten-when-dark
  when: sensor.outdoor_light.illuminance < 50
  emit:
    target: light.living_room
    set: { brightness_pct: 80 }
  authority: automation
  confidence: 0.7
  reason: "Dark outside"

# rules/02-tv.yaml
- id: dim-when-tv
  when: media_player.tv.state == "on"
  emit:
    target: light.living_room
    cap: { brightness_pct: 40 }     # respects user, just caps
    set: { color_temp_k: 2700 }
  authority: automation
  confidence: 0.9
  reason: "TV on"

# rules/03-notifications.yaml
- id: door-open-led
  when: binary_sensor.front_door == "on"
  emit:
    target: light.monitor_back_led
    animation:
      kind: pulse
      parameter: brightness_pct
      values: [0, 100, 0]
      duration: 2s
      repeat: 4
    set: { color: warm_white }
  authority: automation
  ttl: 20s
  reason: "Front door opened"
```

**What happens:**
1. It's dark → living room brightens to 80%.
2. You turn it up to 100% manually → user intent wins, light is 100%.
3. TV turns on → dim rule's `cap: 40` clamps the light to 40% **even though the user set it to 100** (you wanted bright but the rule has a reason). Color temp drops to 2700K.
4. You press the dashboard light toggle → fresh user intent at 80% with a 2-hour TTL → no rule can override `set` while the TTL is alive, but the TV cap still applies → 40%.
5. TV turns off → dim intent expires → your 80% is back automatically.
6. 2 hours later → user intent expires → bright-when-dark resumes control if still dark.

**No rule ordering, no priority numbers, no separate "manual mode" tracking.** The compositor handles all of it.

## Features

- **Declarative YAML rule format** — no Python, no DSL, designed to be writable by AI agents
- **Three-tier authority** — `sensor` < `automation` < `user` — with confidence as a tiebreaker
- **Per-field modifiers** — `set`, `cap`, `floor`, `offset`, `multiply`, with `merge: true` for partial updates
- **Time** — `transition` and `easing` for smooth changes
- **Animations** — `pulse`, `breath`, `cycle`, `flash` with device-native fallbacks
- **Manual override tracking** — wrapped service calls auto-emit user intents with configurable TTL
- **Hot reload** — edit a rule file, the engine reloads without restarting Home Assistant
- **Zero-config** — discover the rule directory, validate on load, log errors clearly
- **HACS-installable** — one-click install, standard HA integration patterns

## Installation

### HACS (recommended)

1. Install [HACS](https://hacs.xyz/) if you don't have it
2. HACS → Integrations → ⋯ (top right) → Custom repositories
3. Add `https://github.com/tobiash/ha-intentional` as **Integration**
4. Install, restart Home Assistant
5. Settings → Devices & Services → Add Integration → "Intentional"
6. Set your rule directory (default: `/config/intentional/rules/`)
7. Create that directory and start writing rule files

### Manual

```bash
cd /config/custom_components
git clone https://github.com/tobiash/ha-intentional.git intentional
# Restart Home Assistant
# Settings → Devices & Services → Add Integration → "Intentional"
```

## Rule directory structure

```
/config/intentional/
├── rules/                    # your rule files
│   ├── 01-ambient.yaml       # sensor-driven rules
│   ├── 02-automation.yaml    # device-state rules (TV, motion, etc.)
│   ├── 03-notifications.yaml # animations and alerts
│   └── 04-manual-scenes.yaml # user-only rules (movie, bedtime, focus)
├── examples/                 # optional, copy/rename to rules/
└── README.md                 # your own notes (optional)
```

Rule files are loaded in alphabetical order. The order is for **organization**, not priority — priority is per-intent, derived from `authority` and `confidence`.

## Authoring rules

See [`docs/rules.md`](docs/rules.md) for the full schema reference, and [`examples/`](examples/) for working rule files.

## Status

**v0.1.0** — initial release. Compositor, animations, YAML loader, manual override detection, hot reload, and HACS packaging are all working. Tested against Home Assistant 2026.5+.

## License

MIT
