# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.4] - 2026-06-05

### Fixed
- **Blocking I/O on the event loop in the config flow options handlers** (v0.3.0 origin, 4 versions live before being reported). `async_step_rules`, `async_step_edit_existing`, `async_step_edit_new`, and `async_step_delete_pick` all called `rule_files._list_rule_files` / `_read_rule_file` / `_write_rule_file` / `_delete_rule_file` *synchronously*. HA logged `Detected blocking call to scandir/read_text/write_text/unlink` and the flow returned 500 — every click in the "Manage rules" hub hit this. Fix: route all four operations through new module-level `_*_in_executor` helpers that wrap each call in `hass.async_add_executor_job`. `_validate_rule_dir` stays sync (pure-string validation, no I/O).
- **Invalid `rows` key in `TextSelector` configs** (v0.2.0 origin, 4 versions live). All four `{"text": {"multiline": True, "rows": N}}` literals used `rows`, which is not a valid `TextSelectorConfig` key on any HA version. HA's `validate_selector` raises `InvalidData` ("extra keys not allowed @ data['rows']") at flow-show time → 500. Fix: drop the `rows` key from all four selectors. The frontend defaults to a sensible multiline height without it. Valid keys: `read_only`, `multiline`, `prefix`, `suffix`, `type`, `autocomplete`, `multiple`.

### Added
- **`tests/test_config_flow_no_blocking_io.py`** — four static regression guards that fail the build on the exact bug classes that bit us:
  - `test_no_sync_rule_files_called_from_config_flow` — fails if `_list_rule_files` / `_read_rule_file` / `_write_rule_file` / `_delete_rule_file` is called directly from `config_flow.py`. AST-inspected, so it can't be accidentally disabled. (Note: the import check is `node.module in {"rule_files", ends-with-".rule_files"}` because the AST resolves `from .rule_files import X` as `module="rule_files", level=1`, not `".rule_files"`. The first version of this test had a wrong check that matched nothing and silently passed — caught during regression testing.)
  - `test_validate_rule_dir_may_be_called_sync` — documents the one allowed sync callsite (pure-string validation).
  - `test_executor_wrappers_use_hass_async_add_executor_job` — fails if any of the four `_*_in_executor` helpers stop using the executor. Defends against "I optimized it to be sync because it's only one call" regressions.
  - `test_no_invalid_text_selector_keys` — fails if any `{"text": {...}}` literal in `config_flow.py` uses a key not in `TextSelectorConfig`'s schema. Catches the `rows` bug class. (Uses regex over source rather than importing `TextSelectorConfig` so the test runs without HA installed.)
- **`tests/test_e2e_config_flow.py`** — 15 end-to-end config-flow tests that drive the *real* HA flow manager (`hass.config_entries.options.async_init` / `async_configure`) through a real `HomeAssistant` test instance. This is the runtime complement to the AST guards. Coverage:
  - User flow: initial form, rejects relative/empty paths, creates entry, prevents duplicate entries
  - Options flow: init doesn't 500, shows menu with `general` + `rules`, lists files, create/edit/delete rule (asserts file on disk AND engine state updated), general step changes dir + rejects invalid paths
  - `test_options_flow_uses_executor_for_io` — wraps `hass.async_add_executor_job` and asserts the config flow actually goes through it for `_list_rule_files` and `_read_rule_file`. Catches "I optimized it to be sync" regressions at runtime.
  - `test_no_blocking_io_warnings_during_full_options_flow` — drives the full flow end-to-end as a smoke test.
- **CI workflow updates** (`ci/test.yml`):
  - Pins `homeassistant==2026.5.1` and `pytest-homeassistant-custom-component==0.13.316` explicitly. The local venv was on HA 2026.2.3, the live cluster on 2026.5.1 — the version difference is what hid three of the four bugs. Pinning means CI tests the same HA users run.
  - Adds a dedicated step for the e2e tests in a *separate* `pytest` invocation. The e2e tests have to run in a fresh Python process because `pytest-homeassistant-custom-component` ≥ 0.13.250 has a `@singleton` translation cache that gets polluted by `services.yaml` `required: true` bools after a config flow is exercised. The next test in the same process then crashes in `_validate_placeholders` with `TypeError: expected str, got bool`. See the docstring at the top of `test_e2e_config_flow.py` for the full root-cause analysis.
  - Adds a step for the HACS smoke-load test (`test_hacs_load.py`) explicitly so it's obvious in CI which step guards the v0.3.1 bug class.

### Notes
- The four bugs fixed in v0.3.3 + v0.3.4 all had a common shape: they only manifested in real HA (not the local venv), only on specific user actions (Configure → rules hub), and only after HA's runtime had a chance to load something. Every one of them would have been caught by the new tests in this release. The release-checklist skill has been updated to require an e2e config flow smoke test before any release.
- Test counts: 250 unit/integration tests + 15 e2e + 1 optional bonus = 266 total. The e2e tests must be run in a separate `pytest` invocation; the default `pytest` invocation deselects them via `-m "not e2e_config_flow"`. CI runs both invocations.

## [0.3.3] - 2026-06-05

### Fixed
- **Options flow 500 on Configure** — `IntentionalOptionsFlow.__init__` did `self.config_entry = config_entry`, which raises `AttributeError: property 'config_entry' has no setter` on HA 2025+ (where `OptionsFlow.config_entry` is a read-only property). Configure now works. Fix: inherit from `OptionsFlowWithConfigEntry` (the documented base for custom integrations), which sets `self._config_entry` correctly and inherits the parent's read-only `config_entry` property.
- **Blocking scandir in `_maybe_install_starter_rules`** — `starter_source.glob("*.yaml")` was called inline in `async_setup_entry`, blocking the event loop on every integration load. HA logged `Detected blocking call to scandir` and the bootstrap timed out waiting on the tick loop task. Fix: wrap the entire glob+copy loop in a single `hass.async_add_executor_job` call.
- **Missing `services.yaml`** — HA logged `Failed to load services.yaml for integration: intentional` (warning, not fatal, but noisy). Added a services.yaml with descriptions for `fire`, `activate_scene`, and `reload`.

### Added
- **`test_options_flow_inherits_from_modern_base`** — regression guard that fails if `IntentionalOptionsFlow` doesn't inherit from `OptionsFlowWithConfigEntry`. Catches the v0.3.3 bug class.
- **`test_options_flow_instantiable_with_config_entry`** — bonus test that actually constructs the flow with a fake config entry. Skipped if HA's frame helper isn't set up (i.e. outside the HA test harness).
- **`test_no_blocking_io_in_async_paths`** — static AST-based check that fails if any blocking I/O call (`.glob()`, `.iterdir()`, `.scandir()`, `.listdir()`) appears in an async function and is NOT wrapped in `hass.async_add_executor_job`. Catches the v0.3.3 scandir bug class.

### Notes
- This release has integration code changes (unlike v0.3.2 which was only an import fix). Three real bugs fixed, all caught by the live HA instance but not by local CI.
- The local test suite was green on v0.3.2 because (a) it uses HA 2026.2.3 in the venv, (b) no test exercised the Configure flow, and (c) no test caught inline blocking I/O in async functions. v0.3.3 closes those test gaps.

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
