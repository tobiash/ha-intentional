# 01 — Ambient rules

Sensor-driven rules that set baselines based on environment state.
These are the foundation — other rules layer modifiers on top.

```yaml
- id: brighten-when-dark
  when: sensor.outdoor_light.illuminance < 50
  emit:
    target: light.living_room
    set: { brightness_pct: 80 }
  authority: automation
  confidence: 0.7
  reason: "Dark outside"

- id: dim-when-bright
  when: sensor.outdoor_light.illuminance > 5000
  emit:
    target: light.living_room
    set: { brightness_pct: 30 }
  authority: automation
  confidence: 0.7
  reason: "Bright outside"
```

# 02 — Device state rules

Rules that respond to the state of other devices. The classic "TV on"
scenario from the README.

```yaml
- id: dim-when-tv
  when: media_player.living_room_tv.state == "on"
  emit:
    target: light.living_room
    cap: { brightness_pct: 40 }      # respects user, just caps
    set: { color_temp_k: 2700 }      # warm white for movie viewing
  authority: automation
  confidence: 0.9
  reason: "TV is on, dim lights for viewing"

- id: gentle-wake-on-motion
  when: binary_sensor.bedroom_motion.state == "on" and time_of_day == "night"
  emit:
    target: light.bedroom
    cap: { brightness_pct: 30 }      # never wake anyone up fully
    transition: 2s
    easing: ease-in
    ttl: 5m                          # auto-release after 5 minutes
  authority: automation
  confidence: 0.6
  reason: "Motion detected at night"
```

# 03 — Notification animations

Time-varying intents that flash, pulse, or cycle to notify. These
demonstrate the animation system. The example is a real-world use case:
"flash the monitor back-LED when the front door opens."

```yaml
- id: door-open-led-pulse
  when: binary_sensor.front_door.state == "on"
  emit:
    target: light.monitor_back_led
    animation:
      kind: pulse
      parameter: brightness_pct
      values: [0, 100, 0]
      duration: 2s
      repeat: 4
      easing: sine
    set: { color: warm_white, color_temp_k: 2700 }
    transition: 0.3s
  authority: automation
  ttl: 20s                          # animation is ~16s, give a buffer
  reason: "Front door opened"

- id: phone-ringing-color-cycle
  when: sensor.phone.state == "ringing"
  emit:
    target: light.office_desk
    animation:
      kind: cycle
      parameter: color_temp_k
      values: [2200, 6500]
      period: 3s
    set: { brightness_pct: 80 }
  authority: automation
  reason: "Phone is ringing"
```

# 04 — Manual / user scenes

User-triggered scenes. These typically come from a dashboard button,
a voice command via an automation, or a script. The TTL means they
auto-release — your "movie scene" doesn't lock out the lights forever.

```yaml
- id: movie-scene
  trigger: manual
  emit:
    target: light.living_room
    set:
      brightness_pct: 15
      color_temp_k: 2200
      effect: candle
    transition: 3s
    easing: ease-in
  authority: user
  ttl: 2h
  reason: "Manual: Movie scene"

- id: focus-mode-suppresses-notifications
  when: input_boolean.focus_mode.state == "on"
  emit:
    target: light.office_desk
    set: { brightness_pct: 0 }
  authority: user
  reason: "Focus mode active"
  blocks: [phone-ringing-color-cycle]    # suppress notifications
```

# 05 — Modifier-based composition

Rules that only contribute modifiers (cap, floor, offset, multiply)
without setting a value. Multiple modifiers compose additively.

```yaml
- id: peak-tariff-energy-cap
  when: utility_meter.peak.state == "on"
  emit:
    target: light.*
    cap: { brightness_pct: 60 }      # cap for ALL lights during peak
    set: { color_temp_k: 4000 }      # colder = more efficient LED
  authority: automation
  confidence: 0.8
  reason: "Peak energy tariff — cap brightness"

- id: always-on-hallway-base
  when: sun.sun.state == "below_horizon"
  emit:
    target: light.hallway
    floor: { brightness_pct: 3 }     # base glow, never fully off
  authority: automation
  confidence: 0.5
  reason: "Hallway base glow at night"
```
