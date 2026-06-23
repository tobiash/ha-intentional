# Reconciliation module owns decide + apply + drift-promotion, not state ingest or rule evaluation

We extracted a **Reconciliation** module from the inline loop in `custom_components/intentional/__init__.py` (`_apply_resolved_targets`, `_on_ha_state_change_factory`, `_confirm_pending_state_drift`). The module owns the decide + apply + drift-promotion policy and the five mutable dicts it reads (`last_applied`, `drift_candidates`, `drift_suppressed_until`, `service_failure_backoff`, `last_resolved`). It deliberately does **not** own HA state ingest (`sync_state_object_into_engine`, the 100 ms `_sync_state_into_engine` poll, the `state_changed` listener's sync step) or rule evaluation (`engine.evaluate_all()`); those stay in the integration and run before the module is called.

## Considered options

- **B1 — Reconciliation owns the whole HA↔engine bridge** (state sync + evaluate + decide + apply + promote). Rejected: it would marry the module to the dual state-sync problem and the evaluation cadence, both of which are slated to change independently (candidate 4 and the manual-override stability plan §9).
- **B2 — Reconciliation owns only decide + apply + drift-promotion** (chosen). State ingest and evaluation stay outside, so the state-sync seam (candidate 4) can be fixed without reopening this module, and evaluation cadence remains an integration concern.

## Consequences

- The module has four collaborators: `engine` (read-only: `list_active_targets`, `resolve`, `now_ms`), an `HAAdapter` (`get_state`, `async_call`), the context tracker (`owns_state`), and the clock (`now_ms`). It is pure-read on the engine; **Manual override** promotions are returned as events (`PromoteOverride`) and applied by the integration, not written back directly.
- Diagnostics are returned as events, not pushed through an injected sink.
- Two entry points: `on_state_delta` (listener path, classifies one target's drift) and `tick` (periodic path: apply all targets + confirm pending drift). The listener and confirmer both collapse into these.
- The drift classifier (`emit_manual_override_for_state_drift`) becomes an internal function with its own edge-case tests (internal seam); policy is tested through the two entry points.
- The service-call translator, state extractor, matcher, and signer stay in a shared adapter module (candidate 1) — they have multiple consumers (Reconciliation + the dry-run/explain/simulate API) and are not internal to Reconciliation.
- The module owns restart-safety for `last_resolved` only (`export_pending_withdraws` / `restore_pending_withdraws`); the other four dicts are not persisted.
