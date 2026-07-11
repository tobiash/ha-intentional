# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.7.37] - 2026-07-11

### Fixed
- **Entity-removal integration coverage** now waits for Home Assistant to finish asynchronous state-change dispatch before verifying cached facts.

## [0.7.36] - 2026-07-11

### Fixed
- **Removed Home Assistant entities** now clear every cached engine fact through the bundled engine API, preventing deleted entities from keeping Rules active.

## [0.7.35] - 2026-07-11

### Fixed
- **Initial Home Assistant state ingestion** now populates Rule facts during config-entry setup, before the first periodic Tick runtime cycle.

## [0.7.34] - 2026-07-11

### Fixed
- **Rule API authorization** now restricts mutations to Home Assistant administrators and rejects non-object JSON payloads cleanly.
- **Manual override reconciliation** no longer reapplies an automation Intent in the same cycle that confirmed Drift is promoted.
- **Selector-backed Intents** now refresh active membership without losing explicit Targets, duplicating overlapping Targets, or compounding modifiers.
- **Multi-target Rules** preserve suppression and selector semantics without duplicating Effects, and suppression follows authored Rule identity safely.
- **Target reconciliation** now distinguishes matched, mismatched, and unobservable Service plan fields to avoid both false matches and repeated calls.
- **Cover and climate Service plans** now apply compatible compound fields without issuing contradictory calls.
- **Tick runtime lifecycle** removes deleted HA state, coalesces lifecycle persistence, isolates storage by entry, and shuts down safely across unload failures.
- **Rule editor round trips** preserve compound conditions, multiple Effects, and nested Effect data while refusing unsupported YAML forms that could lose semantics.
- **Rule validation** now rejects malformed modifier mappings and invalid weighted generator configurations during loading.

### Changed
- **Config entry ownership** is explicitly single-entry so domain services, API routes, and lifecycle state cannot bind to different engines.
- **Conflicting cap and floor modifiers** resolve deterministically and report contradictory bounds in diagnostics.

## [0.7.33] - 2026-06-30

### Fixed
- **Integration timing seam** remains patchable for deterministic Home Assistant runtime tests while using the shared tick runtime clock internally.

## [0.7.32] - 2026-06-30

### Added
- **Tick runtime health** now reports reconciliation-loop liveness, recent tick failures, and pending state-change pulse count through `/api/intentional/health` and `/api/intentional/world`.

### Changed
- **State-change pulse handling** now uses tokenized drain semantics so pulses added during a tick survive to the next reconciliation cycle instead of being cleared before they were observed.

## [0.7.31] - 2026-06-30

### Fixed
- **Tick-loop resilience** now snapshots state-change pulse clearing and records/retries unexpected tick failures, preventing one pulse-set mutation error from permanently stopping reconciliation.

## [0.7.30] - 2026-06-30

### Fixed
- **Storage-backed `patch-rule` updates** now replace only the matching authored rule inside the stored rule document, preserving all other rules and accepting authored multi-target rule IDs before expansion.

## [0.7.29] - 2026-06-29

### Fixed
- **Reload/restart reconciliation** now persists Intentional's target ownership records while targets are still active, so stale light targets can be withdrawn after integration reloads or restarts instead of being stranded with skipped service calls.

## [0.7.28] - 2026-06-28

### Fixed
- **Manual override drift detection** now tolerates small Home Assistant light brightness quantization gaps when matching `brightness_pct` service plans against echoed `brightness` attributes, avoiding false manual overrides after Intentional's own light transitions.

### Changed
- **Reconciliation internals** now own drift classification directly, removing the legacy adapter wrapper and keeping drift decisions with service application state.
- **State ingestion** now scopes most poll ticks to rule-referenced entities plus active targets, while retaining periodic full sweeps for selector-backed rules.
- **Home Assistant adapter code** is split into translator, matcher, signer, and extractor modules to isolate service translation, plan matching, signature freezing, and state-to-manual-intent extraction.

## [0.7.27] - 2026-06-25

### Fixed
- **Rule enable switches** no longer write a new state on every 100ms engine tick, which caused runaway Home Assistant recorder database growth. The continuously-changing elapsed-time attributes (`active_for_ms`, `condition_active_for_ms`, `held_for_ms`, `for_remaining_ms`) are now excluded from the switch entity so Home Assistant's no-change short-circuit suppresses the per-tick writes; live values remain available via the HTTP API and diagnostic sensors.

## [0.7.26] - 2026-06-21

### Fixed
- **Room status sensors** are no longer removed by legacy target-sensor cleanup during sensor setup.
- **Generated room dashboard cards** now use Home Assistant-style room name slugs for suggested entity IDs.

## [0.7.25] - 2026-06-21

### Added
- **Area-derived room controls** now expose per-room pause switches, clear-manual-overrides buttons, and status sensors for Lovelace, inferred from Home Assistant area assignments on rule targets.
- **Intent preview and explain tooling** with `/api/intentional/preview`, `/api/intentional/card`, `/api/intentional/dashboard`, and `/api/intentional/replay`, plus matching `intentionalctl preview`, `card`, `dashboard`, and `replay` commands.
- **Stable context guard alias** via `stable_for`, equivalent to the existing `after`/`for` dwell behavior for debouncing flappy context entities.

## [0.7.24] - 2026-06-18

### Fixed
- **Revealed intent transitions** now use the outgoing higher-confidence intent's withdraw transition when a lower-confidence target intent is revealed, making media-dim restores fade back instead of briefly brightening abruptly.

## [0.7.23] - 2026-06-14

### Fixed
- **Light transition stutter** by suppressing duplicate transition-only service calls while the initial assert transition is still in flight, avoiding immediate reapply with the change transition for the same desired value.

## [0.7.22] - 2026-06-14

### Added
- **Target defaults** can now be authored with document-level `targets:` defaults, producing low-priority baseline intents for managed idle states such as room lights defaulting to off.
- **Intentional API CLI** at `cmd/intentionalctl` provides agent-friendly health, world, rules, validation, dry-run, simulation, history, rollback, and reload workflows.

### Fixed
- **Brightness-only light activations** now remember that their Home Assistant `light.turn_on` service plan activated the target, so final withdrawal can reconcile the light back to off even when the rule did not explicitly set `state: on`.
- **Home Assistant translations** now contain only translation strings, avoiding translation validation failures from schema metadata in current Home Assistant versions.

## [0.7.21] - 2026-06-13

### Fixed
- **Owned Home Assistant context attribution** now tags Intentional service calls and ignores matching returned state updates for drift promotion, reducing false manual overrides from Intentional's own reconciliation actions.

### Added
- **Manual override stability plan** documenting the next integration-hardening steps for context lineage, pending drift gating, mutually exclusive light color modes, diagnostics, Repairs, and targeted state tracking.

## [0.7.20] - 2026-06-13

### Fixed
- **Manual light color overrides** now prevent lower-priority alternate color modes from being mixed into the composed target, avoiding color-temperature reassertions when users pick an RGB/XY color.

## [0.7.19] - 2026-06-13

### Fixed
- **Manual off while active** now waits for drift confirmation instead of immediately reasserting the automation target, allowing user off overrides to win while presence remains on.

## [0.7.18] - 2026-06-12

### Fixed
- **Restart-time linger finalization** now restores expired lingered intents as pending withdraw work, so lights do not stay on when a release window expires while Home Assistant is restarting.
- **Visual editor rule list** now shows authored storage rules instead of expanded runtime target rules.

### Changed
- **Visual editor lifecycle UI** no longer exposes target-level `linger`; use rule-level `hold` for retention semantics.

## [0.7.17] - 2026-06-12

### Fixed
- **Active intent retry** now rechecks actual Home Assistant state before suppressing duplicate service calls, so ignored activations retry after the transition/grace window even if HA emitted no state-change event.

## [0.7.16] - 2026-06-12

### Fixed
- **Ignored light activations** now retry instead of being promoted to short-lived manual off overrides when Home Assistant accepts `light.turn_on` but the light remains off without HA user context.

## [0.7.15] - 2026-06-12

### Fixed
- **Withdraw/reactivation reconciliation** now reasserts an active intent when a target is still matching only because a previously issued withdraw is pending/in-flight.

## [0.7.14] - 2026-06-12

### Fixed
- **Home Assistant startup completion** by registering Intentional's long-lived tick loop as a background task instead of a setup task that HA bootstrap waits on.

## [0.7.13] - 2026-06-12

### Fixed
- **Withdraw reconciliation** now keeps withdrawn light/switch targets pending until Home Assistant reports the target state matches the requested off state, retrying the withdraw after the transition/grace window if the device ignored the first off call.
- **False drift overrides from owned actions** are reduced by suppressing drift detection briefly after Intentional service calls and by shortening auto-detected drift override TTLs. Explicit `intentional.fire` manual overrides keep their normal TTL.
- **User changes after withdraw** with Home Assistant user context cancel pending withdraw retries so Intentional does not fight an explicit UI/service action.

## [0.7.12] - 2026-06-12

### Fixed
- **Sidebar editor upgrade visibility** by versioning the registered panel module URL, forcing browsers to fetch the new bundled editor after an integration upgrade instead of reusing the previous JavaScript module.

## [0.7.11] - 2026-06-12

### Added
- **Visual sidebar rule editor** with form-based rule details, conditions, lifecycle hold settings, target intents, effects, live validation, dry-run preview, simulation, and YAML escape hatches.
- **Local panel development harness** at `tools/serve_intentional_panel.py` for testing the bundled editor with mocked Home Assistant state and pure-engine validation before installing on a live instance.

## [0.7.10] - 2026-06-12

### Fixed
- **Validation stability warnings** now treat `hold.until.for` as retention for presence-driven light rules, avoiding false warnings after migrating from target `linger`.

## [0.7.9] - 2026-06-12

### Added
- **Stable hold release conditions** with `hold.until` and `hold.until.for`, allowing rules to remain active until an off/absence condition has been continuously true for a configured duration.
- **Lifecycle diagnostics** in rule status, world model, explain responses, and rule switch attributes: `phase`, `active_for_ms`, `condition_active_for_ms`, and `held_for_ms`.
- **Rule grouping metadata** via top-level `group` and `profile` fields for mode/profile-oriented authoring.
- **Simulation API** at `/api/intentional/simulate` for evaluating proposed YAML over a timeline of state changes without applying Home Assistant services.

## [0.7.8] - 2026-06-12

### Added
- **Lifecycle rule vocabulary** with `while`, `after`, and `hold` as the primary way to describe stateful situations. `hold.while` retains an already-active intent while a secondary situation remains true, and `hold.after`/`hold.after_when_stops` controls withdrawal delay after the hold condition stops.

### Changed
- **Rule documentation and schema** now present `while -> intent` as the preferred mental model, while still accepting existing `observe` rules.

## [0.7.7] - 2026-06-12

### Fixed
- **Rule switch active semantics** now treat lingering active intents as active while keeping `condition_firing` as the raw current condition state.

## [0.7.6] - 2026-06-12

### Added
- **Runtime diagnostics ring and API** at `/api/intentional/diagnostics`, recording recent rule fire/withdraw events, service applications/failures/skips, effect applications/failures, and drift promotions.
- **Validation warnings** for presence-driven light rules without dwell/linger and live light target capability mismatches such as unsupported color temperature or color fields.
- **Clearer world model fields**: `/api/intentional/world` now includes `authored_rules` and `active_rules` separately from resolved `desired_records`.

### Changed
- **Authored rule switch runtime attributes** now aggregate expanded multi-target runtime rules back onto the authored rule ID, so rule switches keep `state` as enabled/disabled while exposing accurate `active`, `condition_firing`, `targets`, and related debug attributes.
- **Failed service-call backoff** now suppresses repeated retries of the same failing target/signature for 30 seconds and records the failure in diagnostics.

## [0.7.5] - 2026-06-12

### Fixed
- **Invalid Home Assistant light payloads**. Optional light service fields with `None` values are now omitted instead of being sent to Home Assistant, preventing repeated failed `light.turn_on` calls such as `effect: None` for generated ambient light rules.

## [0.7.4] - 2026-06-10

### Changed
- **Sidebar rule editor now uses a focused rule workflow**. The rules list is de-duplicated by authored rule id, selecting a rule opens a per-rule YAML editor, and the primary save action patches that rule instead of forcing full-document editing.
- **New rule creation is visible and editable**. The New button opens a draft rule in the focused editor instead of silently appending YAML to the full document.
- **Full-document YAML editing is now an explicit advanced mode** behind the Document YAML action.

## [0.7.3] - 2026-06-10

### Added
- **Storage-native authored rule document API** at `/api/intentional/rules/document`, so editors can read/write the HA storage document without synthetic file semantics.
- **Bundled Intentional sidebar panel** for storage-backed rule editing. The first version provides a rule list, YAML document editor, validation, dry-run preview, save, and rollback history.
- **Repo-local AI agent skill** at `.agents/skills/intentional-api/SKILL.md` documenting safe use of the Intentional HTTP API for inspection, edits, dry-run, history, rollback, and debugging.

### Fixed
- **Manual override drift handling**. HA state drift is now observed first and only promoted to a user override after it remains stable beyond a confirmation window outside owned transition grace periods. Promoted user/manual intents for the same target replace the previous target-scoped manual override instead of accumulating duplicate active intents.

## [0.7.2] - 2026-06-08

### Added
- **Additional generated durable value strategies**: `walk`, `weighted_sample`, `gradient`, and `noise`, alongside the existing `sample` generator. These support smoother ambient light behavior without using software animations or transient runtime entities.

### Fixed
- **CI E2E config-flow expectations** for storage-backed authored rules. The Home Assistant Configure flow tests now assert the synthetic `stored-rules.yaml` storage document instead of the old file create/edit/delete manager behavior.

## [0.7.1] - 2026-06-08

### Fixed
- **CI integration test coverage** for storage-backed reload behavior. The HA integration test now edits the stored rule document instead of expecting `intentional.reload` to read newly-created YAML files from disk.

## [0.7.0] - 2026-06-08

### Added
- **Storage-backed authored rules**. Intentional now stores authored rules in Home Assistant storage and imports existing YAML files once when no stored rule document exists. YAML remains the Configure-panel, API, import, and export format through a synthetic `stored-rules.yaml` document.
- **More natural Home Assistant rule entities**. Authored rules appear as config switch entities with richer status attributes such as `active`, `condition_firing`, `targets`, `desired`, `authority`, `confidence`, `reason`, `labels`, and `active_intent_count`.
- **Reload rules button** as a Home Assistant config entity.

### Changed
- **Reduced entity registry bloat** by no longer creating per-target intent sensors or per-target clear-manual-override buttons. Legacy per-target entries are cleaned from the registry on setup; per-target override clearing remains available through the `intentional.clear` service.
- **Rule entity lifecycle** now reconciles with stored authored rules, dynamically adding new rule switches and removing deleted rule switch registry entries on refresh.
- **Documentation** now treats rules as authored intents stored in Home Assistant, with YAML as an editing/import/export format. The old VNext migration/design document was removed.

## [0.6.3] - 2026-06-08

### Fixed
- **False light drift for clamped Kelvin values**. HA devices that report a nearby achievable `color_temp_kelvin` value, such as `2702` for a requested `2700`, now satisfy reconciliation instead of looking permanently out of sync.

## [0.6.2] - 2026-06-08

### Added
- **Home Assistant UI controls** for Intentional: a global automation enable switch, per-rule enable switches that persist `enabled: true/false` in YAML, and clear-manual-override buttons globally and per target.
- **README guidance** for generated values and Home Assistant UI controls.

## [0.6.1] - 2026-06-08

### Fixed
- **HACS release eligibility metadata** by adding root `hacs.json`. The same metadata remains in the integration folder for compatibility, but HACS evaluates new releases from the repository root.

## [0.6.0] - 2026-06-08

### Added
- **Generated intent values** with field-local `generate.kind: sample`. Active intents can now periodically sample durable desired-state fields from a list, optionally using fixed or random `every` intervals and fixed or random HA-native `transition` durations. Generated values persist across lifecycle restore and avoid repeating the same sample when alternatives exist.

### Changed
- **Bundled engine sync** now includes the new generation module.

## [0.5.1] - 2026-06-07

### Fixed
- **CI regression test fixture** for transition-policy withdrawal revealing a lower-priority intent. The test now gives the ambient fallback rule lower confidence than the presence rule it is meant to sit behind.

## [0.5.0] - 2026-06-07

### Added
- **Resolved-state transition policies** for VNext intents via `apply.transition.assert/change/withdraw`. Intentional now chooses HA-native transition durations based on whether a resolved target is first asserted, changes while still desired, or withdraws.
- **Withdrawal reconciliation without durable off intents**. When a safe on/off domain (`light`, `switch`, `input_boolean`, `fan`, `siren`) loses its final `state: on` desire, Intentional can issue a default `off` service call using the withdrawn intent's `withdraw` transition. If another lower-priority intent remains, Intentional reconciles to that revealed state instead of calling off.

## [0.4.4] - 2026-06-07

### Fixed
- **Stale `/api/intentional/health` version reporting**. The health endpoint now reads the bundled engine runtime version instead of returning a hardcoded string, and manifest/runtime/API version consistency is covered by tests.

## [0.4.3] - 2026-06-07

### Fixed
- **Invalid light service payloads from merged manual/automation intents**. Light service planning now emits at most one brightness representation and one color descriptor before calling `light.turn_on` or `light.toggle`, preventing HA validation errors such as `two or more values in the same group of exclusion 'Color descriptors'` when manual state-drift intents carry current light attributes.

## [0.4.2] - 2026-06-07

### Fixed
- **Stale live-rule intents after rule edits**. Reloading a level observation rule with the same `id` but a changed target/value now drops the old rule-bound intent so the next evaluation recreates it from the current rule definition. Edge-created TTL intents and manual/user intents are still preserved across reloads.

## [0.4.1] - 2026-06-07

### Fixed
- **Blocking I/O in HTTP rule-file endpoints** introduced in v0.4.0. `GET /api/intentional/rules`, rule read/write/delete, and patch-by-rule-id now route rule-file work through `hass.async_add_executor_job` via the API's `_rule_file_job` seam. This fixes HA warnings for `scandir`, `read_bytes`, and `open` on the event loop.

### Added
- **API blocking-I/O regression guards** in `tests/test_config_flow_no_blocking_io.py` so future API rule-file endpoints cannot call sync rule-file helpers directly from async view handlers.

## [0.4.0] - 2026-06-07

### Added
- **VNext `observe -> intent` DSL draft** with structured observations, multi-target intents, scenes, selector expansion, inline animations, effects, metadata, suppression, TTL, and linger semantics.
- **Persistent lifecycle records** for edge-created TTL intents, lingering intents, manual overrides, and once-per-activation effect dedupe state. The HA integration restores these records after rule load and saves them during the tick loop.
- **Agent-facing endpoints**: schema, validation, dry-run, world model, and generation-guarded patch-by-rule-id editing.
- **Desired/status world model** exposing desired records, lifecycle records, selector diagnostics, actual state snapshots, and actual-vs-desired conditions.
- **Jinja rendering** for scalar `intent` values and `effect.data`, including native numeric coercion through Jinja's native environment.
- **Dynamic `observe.select`** with `any`, `all`, and `none` modes plus selector provenance diagnostics.
- **Architecture deepening modules** for lifecycle persistence, templates, selector matching, reconciliation status, capability policy, VNext records, and bundled-engine sync.

### Changed
- **Intent/effect separation is stricter**: durable `intent` rejects action-like fields such as `state: toggle` and remote `command`; these must use `effect`.
- **Reconciliation skips redundant HA calls** when actual HA state already matches the desired record.
- **Rule-file editing** now uses a deeper rule workspace module with generation hashes and safe patch semantics.
- **Bundled engine sync** is now repeatable via `tools/sync_bundled_engine.py`.

### Notes
- Test counts: 375 local tests pass, with 8 HA-dependent tests skipped when Home Assistant is not installed locally. CI runs the full HA harness.
- This release is a large VNext foundation release. The VNext DSL remains marked as `vnext-draft` while compatibility with the legacy `when`/`emit` format is preserved.

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
