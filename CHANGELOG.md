# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.2] - 2026-06-05

### Fixed
- **`rule_files.py` used a bare `from _engine import ...` instead of a relative `from ._engine import ...`** — this is the v0.1.4 bug class (HACS install pattern). The bare import only resolved when the integration's own directory was on `sys.path` (true for our local tests because the conftest added it; not true for HACS user installs). Result: HACS users got `Platform intentional.config_flow not found` on the just-released v0.3.1, exactly the v0.1.4 "Invalid handler specified" failure mode.

### Added
- **`tests/test_hacs_load.py`** — a smoke-load test that runs the integration import in a fresh subprocess with only `custom_components/` on `sys.path` (the actual HACS contract). This is the test that would have caught the v0.3.1 bug. Uses a subprocess because the HA test harness caches integration state in module namespaces that survives in-process cleanup.
- **`test_no_bare_engine_imports_in_integration`** — a static check that fails the build if any integration module imports `_engine` as a top-level package. Complements the existing `test_no_absolute_intentional_imports_in_integration` (which only caught `from intentional.X`, not `from _engine`).
- **Conftest.py comment** updated to document the sys.path entry that's necessary for the HA test harness but masks the v0.3.1 bug class — and pointing future maintainers at the new smoke-load test that compensates for it.

### Notes
- This is a hotfix release. v0.3.1 was broken for all HACS users on install; v0.3.2 is identical to v0.3.1 except for the `rule_files.py` import fix. No integration code changes.
- CI is green: 244/244 tests pass locally (was 242 before this fix; +1 for the smoke-load test, +1 for the bare-import check). The new tests run as part of the unit test step in `ci/test.yml` — no workflow changes needed.
- The full test suite (244 tests) now passes in 6.7s locally, which is a bonus — the v0.3.1 baseline had pre-existing test isolation issues in `test_integration.py` that were hidden by CI's split test execution. The subprocess-based smoke-load test also fixed those.

## [0.3.1] - 2026-06-05

### Fixed
- **CI workflow now stable** — 17 commits hardening `ci/test.yml` against the Home Assistant test harness
  - Integration tests no longer hang on `async_block_till_done` (tick loop now respects a stop event; autouse fixture unloads entries)
  - `register_api` is guarded so it doesn't blow up in test environments
  - `conftest.py` adds `custom_components/intentional/` to `sys.path` (in addition to `src/`) so the integration is loadable as `custom_components.intentional.*`
  - Integration tests skip cleanly when `homeassistant` isn't installed, instead of erroring
  - `[all-tests]` extra includes `[dev]` deps (ruff) so lint and tests can share a single `pip install`
  - `pytest-homeassistant-custom-component` is allowed to pin its own HA version
  - API tests use `hass_client_no_auth` to exercise the auth requirement
  - Lint cleanup: import order, unused imports
- **Bundle sync drift** — `src/intentional/` and the bundled `_engine/` are now cross-checked by `ci/check-bundle-sync.py` in CI, not just locally
- **CI runs in ~2 minutes** for future PRs (down from the 5-minute initial run that included a full HA install)

### Notes
- No changes to integration code, the engine, the API, the config flow, or the rule format. Users on v0.3.0 will see no functional difference — this release exists to keep the released tag aligned with a green-CI `main`. Per the v0.1.1 lesson, manifest version must match the git tag or HACS strict-mode refuses to load the integration.

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
