# Examples

Copy these patterns into the Intentional YAML editor, or place them in
`/config/intentional/rules/` before first setup to import them into Home
Assistant storage.

## Ambient Light

```yaml
- id: living-room-dark
  observe:
    sensor.outdoor_light.illuminance:
      lt: 50
  intent:
    light.living_room:
      state: on
      brightness_pct: 80
      color_temp_k: 2700
  confidence: 0.7
  reason: Dark outside

- id: living-room-bright-cap
  observe:
    sensor.outdoor_light.illuminance:
      gt: 5000
  intent:
    light.living_room:
      brightness_pct:
        max: 30
  confidence: 0.7
  reason: Bright outside
```

## TV Mode Modifier

```yaml
- id: living-room-tv-mode
  observe:
    media_player.living_room_tv: on
  intent:
    light.living_room:
      brightness_pct:
        max: 40
      color_temp_k: 2200
  confidence: 0.9
  reason: TV is on
```

## Presence With Linger

```yaml
- id: office-presence-light
  observe:
    binary_sensor.office_occupancy: on
    schedule.office_working_hours: on
    for: 2s
  intent:
    light.office:
      state: on
      brightness_pct: 100
      color_temp_k: 4000
      linger: 190s
      apply:
        transition:
          assert: 3s
          change: 4s
          withdraw: 7s
  reason: Office occupied during working hours
```

## Generated Backlight

```yaml
- id: office-monitor-backlight
  observe:
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
      linger: 90s
  confidence: 0.35
  reason: Gently varying occupied-office backlight
```

## Door Notification Effect

```yaml
- id: front-door-opened-notification
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

## Reusable Scene

```yaml
scenes:
  movie:
    intent:
      light.living_room:
        state: on
        brightness_pct: 15
        color_temp_k: 2200
      cover.living_room_blinds:
        state: closed

rules:
  - id: movie-mode
    observe:
      input_boolean.movie_mode: on
    intent:
      include: scene.movie
    authority: user
    confidence: 1.0
    reason: Movie mode
```

## Clear Manual Override When Empty

Prefer HA UI buttons for manual clearing. A rule can also clear overrides through an effect:

```yaml
- id: clear-office-light-override-when-empty
  observe:
    binary_sensor.office_occupancy: off
    for: 5m
  effect:
    service: intentional.clear
    data:
      target: light.office
```
