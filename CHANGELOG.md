# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.0] - 2026-06-04

### Added
- **HTTP API** for external agents: 6 endpoints at `/api/intentional/*`
  - `GET /health` — integration status, engine state
  - `GET /rules` — list rule files
  - `GET/PUT/DELETE /rules/{file}` — manage rule files
  - `POST /reload` — trigger reload
  - `GET /state` — engine state snapshot (active intents by target)
  - `GET /explain/{target}` — why is target in this state? (debugging aid)
  - All endpoints require HA bearer-token auth (same as rest of HA API)
- **Integration test suite** (`tests/test_integration.py`) using `pytest-homeassistant-custom-component`
  - 10 tests covering setup, services, sensors, rule loading, reload, API auth, unload
  - Tests skip gracefully when HA isn't installed
- **GitHub Actions workflow** in `ci/test.yml` (lives in `ci/` not `.github/workflows/` to avoid the workflow-scope issue)
  - Lint, bundle sync check, unit tests, integration tests
  - 5-min runtime on a fresh runner
- **Bundle sync check** (`ci/check-bundle-sync.py`) catches drift between `src/intentional/` and the bundled `_engine/`
- **aiohttp** added to test dependencies for the API unit tests
- **`[all-tests]` extra** in `pyproject.toml` installs the heavy HA test harness

## [0.2.0] - 2026-06-04

### Added
- **UI rule editor**: configure integration → Rules → list/create/edit/delete rule files via multi-line YAML editor
  - Validates YAML before writing
  - Rejects path-traversal attempts on filenames
  - Auto-reloads after save
- **Starter rules** (`starter_rules/welcome.yaml`) auto-installed on first install
- **`rule_files.py`** module extracted from `config_flow.py` so it's unit-testable without HA
- **33 new tests** in `test_config_flow.py`

### Fixed
- **`No module named 'custom_components.intentional.sensor'`** — renamed `entity.py` → `sensor.py` to match HA's platform loader convention
- **Rule directory auto-creation** now happens *before* initial load, not after, so no spurious error on first install

## [0.1.1] - 2026-06-04

### Fixed
- Manifest version bumped to 0.1.1 to match the v0.1.1 tag (HACS strict-mode rejected the previous mismatch)

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
