# Intentional dashboard card and presentation contract

## Card setup

The integration loads `intentional-card` automatically through Home Assistant's
frontend module registration. After installing/updating Intentional, reload the
browser. Choose **Intentional Automation** in the dashboard card picker; its
visual editor supports title, area, domain, Rule group, display, language and
refresh interval. No Browser Mod or dashboard templates are needed.

Example for a room's lighting (paste into a card's YAML editor):

```yaml
type: custom:intentional-card
title: Automatik
area: living_room
domain: light
group: living-room-lighting
display: popup
language: auto
refresh_seconds: 10
```

All three filters are optional and intersect. Use the HA area **ID** and the
Rule's exact `group` value. `display: popup` is the default compact launcher;
opening it creates a native dialog only in that browser. Escape or Close returns
focus to the launcher. `display: inline` renders the same content as an ordinary
expanded card, and also works inside a third-party popup's card container.

English and German labels follow HA's language (`auto`), or set `en` / `de`.
Rule `reason` and `notes` remain canonical authored text, not machine-translated
or duplicated in dashboard configuration. Rule names follow the associated HA
switch's friendly name (including registry renames); Targets use their HA names.
The admin-only **Open in Intentional** link opens that Rule's detail view.

Refresh defaults to 10 seconds, configurable from 5 to 60. Requests never overlap
within a card, responses from old scopes are ignored, and hidden browser tabs or
detached cards stop polling. Failures mark the last snapshot stale and disable
actions. The snapshot time identifies the age of countdown values. Live refresh
preserves expanded Rules and keyboard focus.

### Controls and scope

The first view is status-only. **Settings** deliberately discloses controls:

- **Enable/disable rule** uses the existing Rule switch and changes the **entire
  authored Rule**. Its targets outside the card's scope are listed before the
  control. Effects/Alerts/scenes are flagged as additional impact. Rule settings
  and panel links are shown to administrators.
- **Resume listed overrides** calls the existing `intentional.clear` service
  once for each explicitly listed Target, always with `target` set. Filtering by
  `area` + `domain: light` therefore does not clear camera overrides. The list is
  frozen at click time; it never sends a global clear or changes Rule enablement.
- Existing room pause switches and room clear buttons retain their existing
  semantics. The card does not use them for scoped actions.

## HTTP contract (version 1)

Authenticated `GET /api/intentional/presentation` accepts optional `area`,
`domain`, and `group` filters. Filters intersect; unknown values return an empty
selection. Empty filters and unknown parameter names return 400. This endpoint
is read-only and does not change existing room pause/clear semantics.

## Reading the response

- `global_enabled` is independent of Rule enablement and retained Intents.
- `rules` groups expanded variants by authored Rule ID. `reason` and `notes`
  come directly from the Rule. Each `variants` item preserves its own requested
  `desired`, enabled/paused flags, condition, phase, blockers and timers; never
  use the first expanded variant's request for every Target.
- `scope.targets` contains matching concrete Targets; `outside_targets` lists
  other known Targets affected by the same Rule. `shared` flags that wider
  impact. These are disclosure facts, not authorization for a scoped action.
- `scope.coverage: current_membership` includes current intent-selector members,
  even when the Rule is idle, and any retained Intent Targets. The Engine shares
  selector precedence and exclusions with actual Intent spawning. Future registry
  membership can change. Effects/Alerts/scenes are flagged by `non_target_effects`;
  their transitive device effects are not inferred as room membership.
- Variant `requests` is the per-Target authored declaration (including templates).
  Contribution `active_requests` contains the rendered, live Intent requests.
- `presentation_state` has explicit precedence: `global_off`, `disabled`,
  `paused`, `blocked`, then the Engine phase (`waiting`, `held`, `lingering`,
  `active`, `idle`). A multi-variant Rule can be `mixed`. The underlying flags,
  condition result, blockers and timers remain available separately.
- `scoped_active_intent_count` counts only that Rule's live Intents on matching
  Targets. Variant `active_intent_count` retains its original whole-Rule scope.
- `targets` reuses the existing Target projection. `fields[].provider` comes
  from the compositor's actual field provenance. `baseline_rule_ids` identifies
  expanded Rules supplying baseline fields, not a single overall winner.
- `fields[].modifiers` lists participating caps/floors/offsets/multipliers.
  Participation does not prove that a modifier changed the resulting value.
- Per-Rule `contributions` reports baseline fields, modifier fields, manual
  precedence, and fields whose composed value would differ with that Rule's
  Intents removed. Counterfactuals use the compositor itself. A redundant cap can
  be present without changing output (`modifier_no_change`); for animated
  Targets this counterfactual is explicitly unknown (`impact_known: false`,
  `changed_fields_without_rule: null`), rather than comparing different frames.
  Contribution is never a claim of device application.
- `desired` is composed demand; `actual` is observed HA state; `plan_match` is
  a comparison, not evidence that Intentional caused that state. Dispatch and
  retry evidence remains in `reconciliation`, denial in `policy_denial`.
- `availability` is `missing`, `unknown`, `unavailable`, or `available`.
  Manual override recovery is exposed separately in `manual_override`; a
  manual Intent can own some fields while automation supplies others.
- `manual_override.owner` identifies the manual/user Intent and its reason;
  `fields` identifies its current field ownership. This does not identify a
  person: human actor identity is not stored by the Engine. Recovery includes
  timed expiry or vacancy/resume policy and demand after withdrawal.
- `runtime_status` and `generated_at` describe snapshot health/freshness.
  `capabilities.resume_targets` enumerates scoped manual Targets; `configure`
  reflects the authenticated user's administrator status. `enable_entity_id`
  resolves the actual registered switch rather than guessing an entity ID.

Existing Target projection `rules[].state` is a legacy coarse explanation;
use field provenance rather than its `winning` label for contributions.

The card deliberately does not collapse enablement, condition firing, retained
Intent, contribution, observed agreement and dispatch evidence into one
“applying” flag. For example an 80% evening request capped to 25% can coexist
with an observed 40% lamp and a pending retry; all four facts stay visible.
