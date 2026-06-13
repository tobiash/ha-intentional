# Manual Override Stability Plan

## Goal

Make Intentional more stable as a Home Assistant integration by making reconciliation attributable, reducing false manual overrides, using Home Assistant-native integration patterns, and improving observability when the system suppresses automation.

## Core Reasoning

Manual override detection is the critical design problem because Intentional deliberately competes with Home Assistant's normal imperative automation model. If every unexplained drift becomes a high-authority `USER` intent, then Intentional can incorrectly give up control when the real cause was:

- Its own previous service call still settling.
- A Home Assistant automation or script changing the entity.
- A device integration reporting stale or intermediate state.
- A physical switch or remote changing the device.
- A device rejecting or ignoring a service call.
- A dashboard or user action that really should become a manual override.

The current code already has useful safeguards: confirmation delay, transition suppression, service-plan matching, and manual override TTLs. The missing piece is attribution. Home Assistant provides event and service `Context` objects with `id`, `parent_id`, and `user_id`; Intentional should use those instead of relying only on "actual state differs from last applied service plan."

## Current Observations

- Intentional service calls in `custom_components/intentional/__init__.py` do not pass a Home Assistant `Context`, so later `state_changed` events cannot be reliably identified as caused by Intentional.
- `emit_manual_override_for_state_drift` in `custom_components/intentional/_engine/ha_adapter.py` promotes stable drift into a `USER` intent without distinguishing dashboard user actions, HA automations/scripts, physical-device events, integration polling corrections, or device failures.
- Reconciliation must not reassert an already-applied active target while a drift candidate is pending. The v0.7.19 active-retry fix is a separate stability rule from context attribution: once possible manual drift is observed, active retry must pause until the candidate is confirmed or cleared.
- Light color descriptors are mutually exclusive in practice. The v0.7.20 Paulmann color bounce showed that composing a higher-priority `rgb_color` or `xy_color` user intent with a lower-priority `color_temp_k` automation intent can create invalid or unstable service plans.
- `service_plan_matches_state` treats missing comparable fields as non-conflicting, which can make incomplete actual state look like a successful match.
- `_sync_state_into_engine` snapshots all HA state every 100ms, while a raw all-entity `state_changed` listener is also registered.
- Services and API behavior are not cleanly scoped to config entries.
- Runtime diagnostics exist as an in-memory ring, but important problems are not surfaced through native Home Assistant Repairs or diagnostics export.

## Plan

### 1. Add Home Assistant Context Attribution

Create a small runtime tracker for Intentional-owned contexts.

Changes:

- Import `Context` from `homeassistant.core`.
- For every Intentional service plan application, create and pass an Intentional-owned `Context`.
- Store recent context IDs in a bounded TTL map, for example 5 to 10 minutes.
- When a `state_changed` event arrives, classify it as Intentional-owned if `new_state.context.id` or `new_state.context.parent_id` is in the tracker.
- Treat context attribution as best-effort. Delayed or polled integrations may report later state with no useful context even when the originating service call came from Intentional.

Primary call sites:

- `_apply_resolved_targets`
- `_activate_scene_rules`
- `_apply_pending_effects`
- Any future direct service-call helpers

Reasoning:

This prevents Intentional from promoting its own changes into manual overrides, especially where devices emit multiple intermediate updates after a service call.

### 2. Introduce Explicit Drift Source Classification

Add a classifier that separates drift by source before deciding whether it should become a manual override.

Conceptual source categories:

```python
class DriftSource(Enum):
    INTENTIONAL = "intentional"
    HA_USER = "ha_user"
    HA_AUTOMATION = "ha_automation"
    PHYSICAL_OR_DEVICE = "physical_or_device"
    INTEGRATION = "integration"
    UNKNOWN = "unknown"
```

Classifier inputs:

- `new_state.context.user_id`
- `new_state.context.id`
- `new_state.context.parent_id`
- Intentional-owned context tracker
- Optional recent service-call tracker
- Entity domain and capability metadata
- Current `last_applied` service plan
- Whether the entity is actively managed

Initial classification rules:

- If context `id` or `parent_id` is Intentional-owned, classify as `INTENTIONAL`.
- If `parent_id` points to an observed HA automation/script/service context, classify as `HA_AUTOMATION`.
- If `user_id` is non-null and no automation/script lineage is known, classify as probable `HA_USER`.
- If there is no `user_id` and no known parent, classify as `PHYSICAL_OR_DEVICE` or `UNKNOWN`.
- If the state looks like an ignored activation or device failure, preserve the current retry behavior.

Reasoning:

Different drift sources need different policy. A dashboard tap and a failed `light.turn_on` should not both become identical `USER` intents.

Important guardrail:

`user_id` alone must not be treated as definitive proof of a dashboard/manual user action. Home Assistant automations, scripts, and service calls can carry user/context lineage depending on how they were triggered. Classification should prefer known Intentional context first, then observed automation/script parent context where available, then `user_id`.

Lineage limitation:

Home Assistant does not provide a general public lookup from arbitrary context IDs to "automation", "script", or "user". If Intentional needs automation/script classification, it must maintain its own bounded context lineage cache by observing relevant events such as service calls, automation triggers, script runs, and state changes. Absence of a known context must not prove physical/manual change.

### 3. Make Manual Override Policy Configurable

Add options to the config flow for drift behavior.

Suggested options:

- `dashboard_user_changes`: default `manual_override`
- `physical_device_changes`: default `confirm_then_override`
- `automation_changes`: default `do_not_override`
- `unknown_changes`: default `confirm_then_override`
- `manual_override_ttl`: default existing `7200s`
- `drift_confirmation_ms`: default existing `1500ms`
- `transition_grace_ms`: default existing `2000ms`

Reasoning:

Homes differ. For some users, a wall switch should always override automations. For others, physical device state should be treated as unreliable telemetry unless explicitly configured.

### 4. Feed Classified Events Into Existing Stable-Drift Confirmation

Keep the current stable-drift confirmation window, but decide eligibility before promotion.

New flow:

- Listener receives `state_changed`.
- Sync state into the engine.
- Classify source.
- Ignore Intentional-owned changes for manual override promotion.
- Confirm stability for ambiguous physical or unknown changes.
- Promote only sources allowed by policy.
- Record source metadata in the emitted intent reason or metadata.

Reasoning:

The existing confirmation window is still valuable because it filters transient device state. The improvement is deciding what kind of confirmed drift it is.

### 5. Gate Active Reassertion While Drift Is Pending

Preserve the v0.7.19 reconciliation rule: a pending drift candidate suppresses active retry until the candidate is confirmed or cleared.

Required behavior:

- If `last_applied[target]` matches the active desired service plan but the current HA state differs, create or update a drift candidate.
- While that drift candidate is pending, do not re-call the active desired service plan for the same target.
- If the state returns to matching desired, clear the candidate and keep the cached applied plan.
- If the candidate remains stable through the confirmation window and policy allows promotion, emit the manual override and clear `last_applied[target]`.
- If the candidate changes, restart the confirmation window.
- If the active desired value changes materially, clear or reclassify the candidate before applying the new plan.

Reasoning:

Without this gate, the reconciliation loop can race the manual-override detector: it sees drift, immediately reasserts automation, and prevents the candidate from ever stabilizing. That makes physical/user changes feel ignored and can produce bounce loops.

### 6. Add Mutually Exclusive Field Groups To Composition

Treat alternate representations of the same device capability as mutually exclusive field groups during composition, starting with light color modes.

Initial light color group:

- `color_temp_k`
- `color_temp_mired`
- `color_temp_kelvin`
- `color_temp`
- `white`
- `rgb_color`
- `rgbw_color`
- `rgbww_color`
- `hs_color`
- `xy_color`

Required behavior:

- A higher-priority intent that sets any light color-mode field should suppress lower-priority fields from the same color group.
- A user override with `rgb_color` or `xy_color` must not inherit a lower-priority automation `color_temp_k`.
- A user override with `color_temp_k` must not inherit lower-priority RGB/XY/HS fields.
- Service generation should emit only one color descriptor per `light.turn_on` call, matching the existing adapter behavior.
- Diagnostics should record when lower-priority fields are suppressed by a mutually exclusive group.

Reasoning:

Home Assistant light integrations commonly expose multiple color descriptors but accept only one active color mode at a time. Composing descriptors across priorities can produce service plans that bounce between modes or get normalized by the device into a different state, causing false drift and repeated reassertion.

Home Assistant naming note:

Some names in the group are Intentional internal aliases or legacy HA service/entity names. The implementation should normalize aliases internally while emitting current Home Assistant service fields where possible, for example `color_temp_kelvin` for Kelvin color temperature.

### 7. Improve Service-Plan Matching For Missing Attributes

Current behavior treats missing actual values as non-conflicting. That can hide drift.

Changes:

- Distinguish "field absent but supported" from "field absent because unsupported."
- Use entity capabilities and supported features where available.
- For observable fields on durable domains, missing actual state should probably mean `unknown`, not `matched`.
- For action-like domains where state is not observable, preserve permissive behavior.
- Respect mutually exclusive field groups. For lights, missing `color_temp_kelvin` while `rgb_color`, `xy_color`, or another active color descriptor is present should not by itself mean "reassert color temperature."
- Treat transiently missing attributes cautiously. Missing fields should not cause immediate reassertion without domain/capability checks, confirmation, or an explicit unknown-state policy.
- For lights, inactive color-mode attributes should often be treated as "not applicable under current `color_mode`", not as drift.

Reasoning:

Manual override detection depends on knowing whether actual state matches desired state. Treating missing values as matching can suppress needed service calls and hide drift.

Guardrails:

- Missing-field handling must be domain-aware.
- Missing-field handling must be capability-aware.
- Missing-field handling must respect mutually exclusive field groups.
- Missing-field handling must avoid bounce loops on integrations that only report attributes for the currently active mode.

### 8. Add Explicit Override And Reconciliation Observability

Expose active manual overrides and reconciliation suppression decisions in native entities and the HTTP API.

Suggested summary sensor attributes:

- `manual_override_count`
- `manual_override_targets`
- `last_override_source`
- `pending_drift_targets`
- `suppressed_retry_targets`

Suggested diagnostics events:

- `drift_candidate_started`
- `drift_candidate_changed`
- `drift_candidate_cleared`
- `drift_promoted`
- `active_retry_suppressed_pending_drift`
- `intentional_context_ignored_for_drift`
- `exclusive_field_suppressed`

Suggested API and explanation additions:

- Override source
- Drift classification
- Context id
- First seen time
- Confirmed at time
- Expires at time
- Observed actual state
- Previous desired state
- Whether active retry is currently suppressed
- Which mutually exclusive fields were suppressed during composition

Optional UI/entity addition:

- Disabled-by-default per-target diagnostic sensors for active overrides or pending drift.

Reasoning:

Manual override behavior must be transparent. The user needs to understand why a light is no longer being reconciled, why a target was reasserted, or why reassertion was intentionally suppressed.

Redaction note:

Diagnostics exported through Home Assistant should redact or omit sensitive data. Runtime event names and entity IDs are usually acceptable, but service data can contain secrets for notification, alarm, lock, webhook, browser, or assistant integrations.

### 9. Track Only Relevant Home Assistant State Changes

Replace raw all-entity event handling and broad polling with targeted subscriptions.

Changes:

- Use Home Assistant's `async_track_state_change_event` helper.
- Use `async_track_state_change_filtered` if selectors or currently managed targets need frequent dynamic updates.
- Subscribe to entities referenced by rules.
- Subscribe to entities selected by selectors.
- Subscribe to currently managed targets.
- Subscribe to entities needed for active manual overrides.
- Recompute subscriptions after rule reload, rule edit, and selector expansion changes.

Reasoning:

This reduces event noise, CPU work, and accidental interactions with unrelated entities. It also makes override detection easier because any tracked target has an explicit reason for being tracked.

Ordering note:

This is a larger architectural change than the manual override fix. Dynamic selectors and currently managed targets make targeted subscriptions easy to get subtly wrong. Do this after context attribution, pending-drift retry gating, mutually exclusive field groups, and diagnostics visibility are in place.

### 10. Separate Durable State Targets From Action Targets

Some domains in `ha_adapter.py` represent durable state targets, while others are one-shot action targets.

Examples of likely action targets:

- `notify`
- `tts`
- `button`
- `input_button`
- `scene`
- `script.turn_on`
- `automation.trigger`
- `persistent_notification`

Changes:

- Mark domains or generated service plans as `durable` or `action`.
- Only perform manual override and drift detection for durable targets.
- Keep `effect:` as the preferred mechanism for one-shot side effects.
- Avoid adding `last_applied` drift state for action-only service calls.

Reasoning:

Manual override makes sense for `light.kitchen = off`; it does not make sense for `notify.mobile_app = message sent`.

### 11. Improve Lifecycle Cleanup And Service Registration

Current service/API behavior is ambiguous if multiple config entries exist.

Changes:

- Prefer registering integration service actions once in `async_setup`, not once per config entry.
- If multiple entries are supported, make services accept and resolve `config_entry_id` or otherwise validate which loaded entry they operate on.
- Validate entry existence and loaded state inside service handlers.
- Make API routes entry-aware if multiple entries remain supported.
- Add unload cleanup for entry-scoped listeners, tasks, runtime data, context trackers, and subscription callbacks.

Reasoning:

Home Assistant integrations should unload cleanly. Multiple-entry ambiguity is especially risky for an automation engine.

### 12. Add Native Home Assistant Repairs And Diagnostics Export

Current runtime diagnostics are only an in-memory ring.

Add native HA surfaces:

- Implement `async_get_config_entry_diagnostics`.
- Create Repairs issues for invalid stored rules.
- Create Repairs issues for rule reload failures.
- Create Repairs issues for repeated service failures for a target.
- Create Repairs issues for repeated drift promotion on the same target.
- Create Repairs issues for unsupported target domains used as durable intents.
- Optionally create persistent notifications for severe rule-load failures.
- Create, update, and clear Repairs issues as the underlying problem appears and resolves.

Reasoning:

Users should not need logs or the custom API to discover that Intentional has zero valid rules or is repeatedly failing to apply a target.

Scope guardrail:

Repairs should be reserved for persistent, actionable user-facing problems. Normal manual drift, pending drift, retry suppression, and other expected reconciliation decisions should be visible through diagnostics, entities, logs, or API explanations rather than Repairs.

## Implementation Order

1. Add tests for Home Assistant context classification.
2. Add Intentional-owned `Context` tracking and pass context into service calls.
3. Update drift promotion to ignore Intentional-owned contexts.
4. Preserve and test pending-drift active-retry gating.
5. Add mutually exclusive field groups to composition, starting with light color modes.
6. Add source classification and policy defaults.
7. Add tests for dashboard user, Intentional-owned, automation-like, physical/unknown, ignored-device, pending-drift, and color-mode override cases.
8. Add diagnostics/API visibility for active overrides, pending drift, retry suppression, and exclusive-field suppression.
9. Improve service-plan matching for missing attributes with domain/capability/color-mode guardrails.
10. Replace broad raw bus state tracking with targeted `async_track_state_change_event`.
11. Add Home Assistant Repairs integration.
12. Document override semantics in `README.md` and `docs/rules.md`.

## Most Valuable First PR

Start with context attribution only.

Smallest useful slice:

- Add an Intentional context tracker.
- Pass `context=...` into `_apply_resolved_targets`, `_activate_scene_rules`, and `_apply_pending_effects`.
- In `_on_ha_state_change_factory`, skip drift promotion when `new_state.context` belongs to Intentional.
- Preserve pending-drift active-retry suppression.
- Preserve mutually exclusive light color-mode composition.
- Add diagnostics for source/classification and retry suppression.
- Add unit tests around a wrapper classifier, pending-drift gating, and light color-mode composition.

Acceptance criteria:

- Intentional-owned state changes never promote drift.
- Pending drift candidate blocks active retry until confirmed or cleared.
- User `rgb_color` or `xy_color` override does not inherit lower-priority `color_temp_k`.
- User `color_temp_k` override does not inherit lower-priority RGB/XY/HS fields.
- Diagnostics record source/classification and whether reconciliation was suppressed.

Why first:

It reduces false overrides without changing the DSL, UI, or storage model. It also preserves the concrete v0.7.19 and v0.7.20 fixes while creating the foundation for more nuanced policy work.

## Open Questions

- Should physical device changes default to manual overrides, or should users opt into that behavior?
- Should external Home Assistant automations be treated as lower-priority external authority instead of ignored drift?
- Should manual override TTL differ by source, for example short for unknown drift and longer for explicit dashboard user changes?
- Should unsupported durable targets fail validation, warn through Repairs, or silently behave as action targets?
- Should multiple config entries be supported, or should the config flow enforce one Intentional engine per Home Assistant instance?
- Should mutually exclusive field groups live in the compositor, the HA adapter, or a shared capability schema?
- Should pending-drift retry gating be visible as target state, diagnostics only, or both?
