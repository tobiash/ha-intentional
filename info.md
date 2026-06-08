# ha-intentional

Declarative, composable, intent-based automation for Home Assistant.

Intentional turns automation into reconciliation:

```text
observe -> intent
```

Rules observe home facts, produce durable desired state, and the engine reconciles Home Assistant entities toward that state. Effects remain explicit side effects for notifications, scripts, and other one-shot service calls.

## Quick Start

1. Install through HACS.
2. Restart Home Assistant.
3. Add the `Intentional` integration in Settings -> Devices & Services.
4. Set the rule directory, usually `/config/intentional/rules/`.
5. Add rules in the Configure panel, or import YAML from the rule directory on first setup.

## Example

```yaml
- id: office-light
  observe:
    binary_sensor.office_occupancy: on
  intent:
    light.office:
      state: on
      brightness_pct: 70
      color_temp_k: 4000
      linger: 2m
  reason: Office occupied

- id: door-open-message
  observe:
    changed:
      binary_sensor.front_door:
        to: on
  effect:
    service: notify.mobile_app_phone
    data:
      message: Front door opened
```

## Features

- Structured `observe:` and `intent:` rules.
- Conflict resolution by authority, confidence, and recency.
- Field operators: direct values, caps, floors, offsets, and multipliers.
- Manual override detection with TTL.
- Generated durable values for ambient behavior.
- Global and per-rule Home Assistant switches.
- Manual override clear buttons.
- Agent-friendly HTTP API.

See the project README and `docs/rules.md` for full documentation.

## License

MIT
