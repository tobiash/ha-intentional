# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-06-04

### Added
- Initial release
- `Intent` data model with three-tier authority (sensor/automation/user) and confidence-based tiebreakers
- `AnimationSpec` with four kinds: pulse, breath, cycle, flash
- `ResolvedIntent` output of the compositor
- `resolve_intents()` — pure compositor with the 7-step composition pipeline
- `parse_when()` — safe recursive-descent parser for `when:` expressions
- `evaluate_when()` — AST evaluator
- `load_rules()` / `load_rules_from_string()` — YAML rule loader with strict schema validation
- `parse_duration()` — accepts `500ms`, `1.5s`, `2h`, `1h30m15s`
- `Engine` — orchestrator with state, rule evaluation, intent lifecycle, animation ticking
- **Scene support**: rules can reference HA scenes via `emit.scene: scene.xxx` instead of `emit.target:`. Integration layer fires `scene.turn_on` for these rules, bypassing the compositor.
- `intentional.activate_scene` service for manually firing scene rules from automations/buttons
- HACS custom component: `custom_components/intentional/`
- Config flow (UI setup) and options flow
- Services: `intentional.fire` (manual user intent), `intentional.activate_scene` (manual scene), `intentional.reload` (hot reload)
- Sensor entities: per-target resolution state + engine summary
- Hot reload of rules via directory watcher
- Example rule files in `examples/` (7 files, including scenes)
- Full test suite: 174 tests across 9 test modules
- GitHub Actions CI for Python 3.11, 3.12, 3.13 with ruff and coverage
