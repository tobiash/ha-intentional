"""Tests for the bundled Intentional rule editor panel."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from intentional.yaml_loader import load_rules_from_string

REPO_ROOT = Path(__file__).parent.parent
PANEL_PATH = REPO_ROOT / "custom_components" / "intentional" / "frontend" / "intentional-panel.js"


def test_panel_exposes_read_only_ha_migration_workflow() -> None:
    source = PANEL_PATH.read_text()
    assert 'data-action="migration-discover"' in source
    assert 'data-action="migration-propose"' in source
    assert 'data-action="migration-add"' in source
    assert "Source automation stays unchanged" in source
    assert 'data-action="migration-disable"' not in source


INTEGRATION_INIT = REPO_ROOT / "custom_components" / "intentional" / "__init__.py"


def _run_panel_js(expression: str) -> object:
    source = PANEL_PATH.read_text()
    script = f"""
globalThis.HTMLElement = class {{ attachShadow() {{ this.shadowRoot = {{}}; }} }};
globalThis.customElements = {{ define() {{}} }};
globalThis.confirm = () => true;
{source}
Promise.resolve({expression}).then((value) => console.log(JSON.stringify(value)));
"""
    result = subprocess.run(["node"], input=script, check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def test_frontend_panel_asset_is_bundled() -> None:
    source = PANEL_PATH.read_text()

    assert 'customElements.define("intentional-panel"' in source
    assert '"rules/document"' in source
    assert '"validate"' in source
    assert '"dry-run"' in source
    assert '"simulate"' in source
    assert '"rules/history"' in source
    assert '"rules/rollback"' in source


def test_alert_frontend_workspaces_use_existing_api_contracts() -> None:
    source = PANEL_PATH.read_text()

    assert '>Intent</button>' in source
    assert '>Alert</button>' in source
    assert '>Alerting Policy</button>' in source
    assert 'this._api("GET", "alerts")' in source
    assert 'this._api("GET", "alerting/policy")' in source
    assert 'this._api("POST", "alerting/simulate"' in source
    assert 'this._api("PUT", "alerting/policy"' in source
    assert "expected_generation: this._policyDocument?.generation" in source
    assert "request !== this._previewRequest || candidateFingerprint(this._candidateContents()) !== fingerprint" not in source.split("async _load()", 1)[1].split("async _validate", 1)[0]


def test_alert_ledger_order_is_deterministic_and_does_not_mutate_input() -> None:
    result = _run_panel_js(r'''(() => {
      const alerts = [
        {state: "inactive", severity: "critical", rule_id: "z", name: "Inactive"},
        {state: "firing", severity: "info", rule_id: "b", name: "Info"},
        {state: "pending", severity: "critical", rule_id: "a", name: "Pending"},
        {state: "firing", severity: "critical", rule_id: "c", name: "Critical C"},
        {state: "firing", severity: "critical", rule_id: "a", name: "Critical A"},
        {state: "firing", severity: "warning", rule_id: "a", name: "Warning"},
      ];
      const original = alerts.map(item => item.name);
      return {ordered: sortAlerts(alerts).map(item => item.name), original: alerts.map(item => item.name), expectedOriginal: original};
    })()''')

    assert result["ordered"] == ["Critical A", "Critical C", "Warning", "Info", "Pending", "Inactive"]
    assert result["original"] == result["expectedOriginal"]


def test_alert_ledger_is_truthful_stale_and_rule_linked() -> None:
    result = _run_panel_js(r'''(() => {
      const panel = new IntentionalPanel();
      panel._alerts = [{
        state: "firing", severity: "critical", evaluation_status: "stale",
        summary: "Freezer is too warm", rule_id: "freezer", name: "FreezerHigh", instance_id: "instance-1",
      }];
      panel._rules = [{id: "freezer", block: "- id: freezer\n"}];
      return panel._renderAlertWorkspace();
    })()''')

    assert "Freezer is too warm" in result
    assert "Stale evaluation" in result
    assert 'data-action="open-alert-rule"' in result
    assert "Instance: instance-1" in result
    assert "Desired" not in result
    assert "Target" not in result
    assert "competing" not in result


def test_alert_detail_fetches_instance_and_renders_safe_operational_state() -> None:
    result = _run_panel_js(r'''(async () => {
      const panel = new IntentionalPanel(); panel._render = () => {};
      const calls = [];
      panel._api = async (method, path) => {
        calls.push([method, path]);
        return {instance: {
          instance_id: "instance/1", rule_id: "freezer", name: "FreezerHigh",
          summary: "Freezer is too warm", severity: "critical", state: "firing",
          evaluation_status: "stale", active_at_ms: 1000, firing_at_ms: 2000,
          labels: {area: "kitchen"}, acknowledged: true,
          suppression: ["silence"], notification_suppressed: true,
        }, audit: [{from: "pending", to: "firing", at_ms: 2000, reason: "for_elapsed"}],
        routing: [{routes: [{route_id: "critical", receiver: "household", group_key: {area: "kitchen"}}]}],
        delivery: [{status: "accepted", destination: {type: "notify_entity"}, message_kind: "initial", attempt: 1, accepted_at_ms: 2100}],
        health: {status: "healthy"}};
      };
      await panel._loadAlertDetail("instance/1");
      return {calls, html: panel._renderAlertDetail()};
    })()''')

    assert result["calls"] == [["GET", "alerts/instance%2F1"]]
    for text in ["Lifecycle", "Evidence", "stale", "Labels", "Acknowledgment and suppression", "Acknowledged.", "Routing and grouping", "critical", "household", "Notification delivery", "accepted", "notify_entity", "Audit", "for_elapsed"]:
        assert text in result["html"]
    assert "notify.admin" not in result["html"]


def test_alert_controls_use_instance_endpoints_and_refresh_ledger_and_detail() -> None:
    result = _run_panel_js(r'''(async () => {
      const panel = new IntentionalPanel(); panel._render = () => {};
      panel._selectedAlertId = "instance-1"; panel._alertComment = "Investigating"; panel._silenceReason = "Repair in progress";
      const calls = [];
      panel._api = async (method, path, data) => {
        calls.push({method, path, data});
        if (path === "alerts") return {alerts: []};
        if (method === "GET") return {instance: {instance_id: "instance-1", state: "firing"}, audit: []};
        return {};
      };
      await panel._mutateAlert("acknowledge-alert");
      panel._silenceReason = "Repair in progress";
      await panel._mutateAlert("silence-alert");
      await panel._mutateAlert("revoke-acknowledgment");
      return calls;
    })()''')

    mutations = [call for call in result if call["method"] != "GET"]
    assert mutations == [
        {"method": "POST", "path": "alerts/instance-1/acknowledge", "data": {"comment": "Investigating"}},
        {"method": "POST", "path": "alerts/instance-1/silence", "data": {"reason": "Repair in progress", "duration_ms": 3_600_000}},
        {"method": "DELETE", "path": "alerts/instance-1/acknowledgment"},
    ]
    assert sum(call["path"] == "alerts" for call in result) == 3
    assert sum(call["path"] == "alerts/instance-1" and call["method"] == "GET" for call in result) == 3


def test_alert_load_failure_is_isolated_from_rule_load() -> None:
    result = _run_panel_js(r'''(async () => {
      const panel = new IntentionalPanel(); panel._render = () => {};
      panel._api = async (method, path) => {
        if (path === "alerts") throw new Error("alert store unavailable");
        if (path === "health") return {version: "test"};
        if (path === "rules/document") return {contents: "", generation: "g1"};
        if (path === "rules/history") return {history: []};
        if (path === "world") return {authored_rules: []};
        if (path === "validate") return {valid: true, normalized: []};
      };
      await panel._load();
      await Promise.resolve();
      return {ruleError: panel._error, alertsError: panel._alertsError, generation: panel._document.generation};
    })()''')

    assert result == {"ruleError": "", "alertsError": "alert store unavailable", "generation": "g1"}


def test_workspace_switch_preserves_rule_editor_state() -> None:
    result = _run_panel_js(r'''(() => {
      const panel = new IntentionalPanel(); panel._render = () => {};
      panel._screen = "edit"; panel._editorMode = "yaml"; panel._selectedRuleId = "one";
      panel._selectedRuleContents = "authored draft"; panel._dirty = true;
      panel._handleAction({dataset: {action: "switch-workspace", workspace: "alert"}});
      panel._handleAction({dataset: {action: "switch-workspace", workspace: "intent"}});
      return {workspace: panel._workspace, screen: panel._screen, mode: panel._editorMode, source: panel._selectedRuleContents, dirty: panel._dirty};
    })()''')

    assert result == {"workspace": "intent", "screen": "edit", "mode": "yaml", "source": "authored draft", "dirty": True}


def test_policy_has_independent_checked_reviewed_and_published_workflow() -> None:
    result = _run_panel_js(r'''(async () => {
      const panel = new IntentionalPanel(); panel._render = () => {};
      panel._policyLoaded = true; panel._policyDocument = {generation: "g1"};
      panel._policyContents = "route: {id: root, receiver: household}";
      panel._policySyntheticText = '[{"alertname":"Smoke","severity":"critical"}]';
      const calls = [];
      panel._api = async (method, path, data) => {
        calls.push({method, path, data});
        if (method === "POST" && data.alerts.length === 0) return {valid: true, alerts: []};
        if (method === "POST") return {valid: true, candidate_generation: "g2", alerts: [{labels: data.alerts[0], routes: [], fanout: [], warnings: []}]};
        return {valid: true, generation: "g2"};
      };
      await panel._checkPolicy(); const checked = panel._policyStage;
      await panel._reviewPolicy(); const reviewed = panel._policyStage;
      await panel._publishPolicy();
      return {checked, reviewed, published: panel._policyStage, calls};
    })()''')

    assert result["checked"] == "Checked"
    assert result["reviewed"] == "Reviewed"
    assert result["published"] == "Published"
    publish_call = next(call for call in result["calls"] if call["method"] == "PUT")
    assert publish_call == {
        "method": "PUT",
        "path": "alerting/policy",
        "data": {
            "contents": "route: {id: root, receiver: household}",
            "expected_generation": "g1",
        },
    }


def test_policy_fanout_spike_requires_explicit_confirmed_retry() -> None:
    result = _run_panel_js(r'''(async () => {
      const panel = new IntentionalPanel(); panel._render = () => {};
      panel._policyLoaded = true;
      panel._policyDocument = {generation: "g1"}; panel._policyContents = "route: {}";
      panel._policyStage = "Reviewed"; panel._policyReviewedFingerprint = candidateFingerprint(panel._policyContents);
      const calls = [];
      panel._api = async (method, path, data) => {
        calls.push(data);
        if (!data.confirm_spike) { const error = new Error("confirm"); error.body = {error: "confirmation_required", preview: {fanout: 5}}; throw error; }
        return {generation: "g2"};
      };
      await panel._publishPolicy(); const warning = panel._renderPolicyWorkspace();
      await panel._publishPolicy(true);
      return {calls, warning, stage: panel._policyStage};
    })()''')

    assert "Confirm delivery fanout increase" in result["warning"]
    assert "5 Receiver destinations" in result["warning"]
    assert result["calls"][1]["confirm_spike"] is True
    assert result["stage"] == "Published"


def test_policy_synthetic_preview_renders_structured_routing_fields() -> None:
    rendered = _run_panel_js(r'''renderPolicyPreview({valid: true, candidate_generation: "g2", alerts: [{
      labels: {alertname: "Smoke", severity: "critical"},
      routes: [{route_id: "urgent", receiver: "admins", group_by: ["alertname"], group_key: {alertname: "Smoke"}, suppression: {reason: "mute_interval"}}],
      fanout: [{receiver: "admins", destination: {type: "notify_entity", entity_id: "notify.admin"}}],
      warnings: [{code: "duplicate_fanout", receiver: "admins"}],
    }]})''')

    for text in ["Synthetic Alert 1", "Routes", "Group keys: alertname", "Group key:", "Fanout", "Suppression:", "Warnings", "duplicate_fanout"]:
        assert text in rendered


def test_frontend_panel_has_visual_rule_editor() -> None:
    source = PANEL_PATH.read_text()

    assert '_editorMode = "visual"' in source
    assert 'Visual rule editor' in source
    assert 'Rule editor' in source
    assert 'while → intent' in source
    assert 'Build a durable' in source
    assert 'Add Condition' in source
    assert 'Add Target' in source
    assert 'Add Effect' in source
    assert 'intentInputField("Linger"' not in source
    assert 'current[scalar[1]] = stripQuotes(scalar[2])' in source


def test_frontend_panel_keeps_yaml_escape_hatches() -> None:
    source = PANEL_PATH.read_text()

    assert 'Rule YAML' in source
    assert 'Document YAML' in source
    assert 'extractRuleBlock' in source
    assert 'replaceRuleBlock' in source
    assert '_uniqueRules()' in source


def test_frontend_panel_lists_authored_document_rules() -> None:
    source = PANEL_PATH.read_text()

    assert 'parseDocumentRuleSummaries(contents, validation.normalized || [])' in source
    assert 'function extractRuleBlocks(contents)' in source
    assert 'normalized || []' in source


def test_frontend_panel_has_no_install_validation_loop() -> None:
    source = PANEL_PATH.read_text()

    assert '_queueValidate()' in source
    assert '_validateLocally()' in source
    assert 'Fix the highlighted fields before saving.' in source
    assert 'Dry-run evaluates desired targets without applying services.' in source
    assert 'Use simulation for after/hold timing before installing on the live instance.' in source
    assert 'this._api("POST", "validate"' in source
    assert 'this._api("POST", "dry-run"' in source
    assert 'this._api("POST", "simulate"' in source


def test_compound_conditions_parse_and_round_trip() -> None:
    result = _run_panel_js(r'''(() => {
      const yaml = `- id: compound\n  while:\n    any:\n      - binary_sensor.one: "on"\n      - sensor.level:\n          gt: 3\n  intent:\n    light.room:\n      state: "on"\n`;
      const form = parseRuleForm(yaml, null);
      return { mode: form.conditionMode, conditions: form.conditions, output: stringifyRule(form) };
    })()''')
    assert result["mode"] == "any"
    assert result["conditions"] == [
        {"entity": "binary_sensor.one", "operator": "is", "value": "on"},
        {"entity": "sensor.level", "operator": "gt", "value": "3"},
    ]
    assert "    any:" in result["output"]


def test_guided_alert_only_rule_round_trips_current_alert_fields() -> None:
    generated = _run_panel_js(r'''(() => {
      const yaml = `- id: freezer\n  while:\n    sensor.freezer_temperature:\n      gt: -10\n  alert:\n    name: FreezerTemperatureHigh\n    severity: info\n    annotations:\n      summary: Freezer is too warm\n    for: 5m\n    stale_after: 3m\n    labels: {area: kitchen, category: appliance}\n    escalations:\n      - {after: 30m, severity: warning}\n      - {after: 2h, severity: critical}\n`;
      const form = parseRuleForm(yaml, null);
      return {error: visualModeError(yaml), form, output: stringifyRule(form)};
    })()''')

    assert generated["error"] == ""
    assert generated["form"]["alert"]["mode"] == "state"
    assert generated["form"]["alert"]["labels"] == "area=kitchen, category=appliance"
    assert "  intent:" not in generated["output"]
    rule = load_rules_from_string(generated["output"])[0]
    assert rule.target == ""
    assert rule.effects == ()
    alert = rule.alerts[0]
    assert alert.name == "FreezerTemperatureHigh"
    assert alert.for_ms == 300_000
    assert alert.stale_after_ms == 180_000
    assert alert.labels == {"area": "kitchen", "category": "appliance"}
    assert [(step.after_ms, step.severity) for step in alert.escalations] == [(1_800_000, "warning"), (7_200_000, "critical")]


def test_guided_pulse_alert_serializes_resolve_after_not_for() -> None:
    result = _run_panel_js(r'''(() => {
      const form = EMPTY_RULE(); form.id = "doorbell";
      form.conditions = [{entity: "event.doorbell.triggered", operator: "is", value: "true"}];
      form.intents = []; form.alert = emptyAlertForm();
      Object.assign(form.alert, {name: "DoorbellPressed", severity: "info", summary: "Doorbell pressed", mode: "pulse", for: "1m", resolveAfter: "5m"});
      return stringifyRule(form);
    })()''')

    assert "resolve_after: 5m" in result
    assert "    for:" not in result
    alert = load_rules_from_string(result)[0].alerts[0]
    assert alert.resolve_after_ms == 300_000


def test_unsupported_alert_yaml_refuses_visual_mode_without_data_loss() -> None:
    result = _run_panel_js(r'''(() => {
      const examples = [
        `- id: described\n  while:\n    sensor.x: on\n  alert:\n    name: X\n    severity: warning\n    annotations:\n      summary: X happened\n      runbook: Preserve me\n`,
      ];
      return examples.map((yaml) => {
        const panel = new IntentionalPanel(); panel._render = () => {}; panel._contents = yaml;
        panel._selectedRuleId = extractRuleBlocks(yaml)[0].id; panel._selectedRuleContents = extractRuleBlocks(yaml)[0].block;
        panel._editorMode = "yaml"; panel._showVisualRule();
        return {mode: panel._editorMode, error: panel._visualModeError, unchanged: panel._candidateContents() === yaml};
      });
    })()''')

    assert all(item["mode"] == "yaml" for item in result)
    assert all("prevent data loss" in item["error"] for item in result)
    assert all(item["unchanged"] for item in result)


def test_guided_authoring_round_trips_multiple_alerts() -> None:
    result = _run_panel_js(r'''(() => {
      const yaml = `- id: multiple\n  while:\n    sensor.x: on\n  alert:\n    - name: X\n      severity: warning\n      annotations:\n        summary: X happened\n    - name: Y\n      severity: critical\n      annotations:\n        summary: Y happened\n`;
      const block = extractRuleBlocks(yaml)[0];
      const form = parseRuleForm(block.block, {});
      return {count: [form.alert, ...(form.alerts || [])].filter(Boolean).length, yaml: stringifyRule(form)};
    })()''')

    assert result["count"] == 2
    alerts = load_rules_from_string(result["yaml"])[0].alerts
    assert [(alert.name, alert.severity) for alert in alerts] == [
        ("X", "warning"),
        ("Y", "critical"),
    ]


def test_simulation_renders_alert_consequences_and_transitions_separately() -> None:
    rendered = _run_panel_js(r'''renderSimulationSummary({steps: [{
      active_rules: ["freezer"], targets: [{target: "light.one"}], effects: [{service: "notify.one"}],
      alerts: [{name: "FreezerHigh", state: "firing", evaluation_status: "current"}],
      alert_transitions: [{name: "FreezerHigh", to: "firing", reason: "condition_active"}],
    }]})''')

    for text in ["Targets", "1 Target projection", "Effects", "1 Effect would fire", "Alert consequences", "current evidence", "Alert transitions", "condition_active"]:
        assert text in rendered


def test_all_effects_and_nested_json_are_serialized() -> None:
    result = _run_panel_js(r'''(() => {
      const form = EMPTY_RULE();
      form.id = "effects";
      form.effects = [
        {service: "notify.one", target: "", data: JSON.stringify({message: "hi", metadata: {tags: ["a", "b"]}})},
        {service: "light.turn_on", target: JSON.stringify({entity_id: ["light.one", "light.two"]}), data: ""},
      ];
      return stringifyRule(form);
    })()''')
    assert result.count("service:") == 2
    assert "metadata:\n          tags:\n            - a\n            - b" in result
    assert "entity_id:\n          - light.one\n          - light.two" in result


def test_frontend_non_finite_number_guard_checks_nested_parsed_values() -> None:
    result = _run_panel_js(
        "[containsNonFiniteNumber({value: 1}), containsNonFiniteNumber({value: NaN}), containsNonFiniteNumber([Infinity])]"
    )
    assert result == [False, True, True]


def test_normalized_effects_with_empty_mappings_round_trip_to_loader_valid_yaml() -> None:
    generated = _run_panel_js(r'''(() => {
      const normalized = {
        id: "effect-round-trip",
        effects: [
          {domain: "notify", service: "one", target: {}, data: {}},
          {domain: "notify", service: "two", target: {}, data: {message: "hi", metadata: {tags: ["a", "b"]}}},
        ],
      };
      const yaml = "- id: effect-round-trip\n  while:\n    binary_sensor.ready: on\n";
      return stringifyRule(parseRuleForm(yaml, normalized));
    })()''')

    assert generated.count("service:") == 2
    assert "target:" not in generated
    assert "    - service: notify.one\n    - service: notify.two" in generated
    assert "metadata:\n          tags:\n            - a\n            - b" in generated

    rules = load_rules_from_string(generated)
    assert [(effect.domain, effect.service) for effect in rules[0].effects] == [
        ("notify", "one"),
        ("notify", "two"),
    ]
    assert rules[0].effects[0].target == {}
    assert rules[0].effects[0].data == {}
    assert rules[0].effects[1].data == {"message": "hi", "metadata": {"tags": ["a", "b"]}}


def test_unsupported_constructs_block_visual_mode() -> None:
    assert "unsupported 'choose' field" in _run_panel_js(
        r'''visualModeError(`- id: unsafe\n  while:\n    sensor.one: on\n  choose:\n    nested: true\n`)'''
    )


def test_dynamic_hold_mapping_refuses_visual_mode_and_preserves_yaml() -> None:
    result = _run_panel_js(r'''(() => {
      const yaml = `- id: adaptive\n  while:\n    binary_sensor.office: on\n  hold:\n    after_when_stops:\n      tiers:\n        - active_for: 0s\n          duration: 30s\n      adjustments: []\n      max: 5m\n  intent:\n    light.office:\n      state: on\n`;
      const panel = new IntentionalPanel();
      panel._render = () => {};
      panel._contents = yaml;
      panel._selectedRuleId = "adaptive";
      panel._selectedRuleContents = yaml;
      panel._selectedRuleForm = parseRuleForm(yaml, null);
      panel._editorMode = "yaml";
      panel._showVisualRule();
      return {mode: panel._editorMode, error: panel._visualModeError, unchanged: panel._candidateContents() === yaml};
    })()''')

    assert result["mode"] == "yaml"
    assert "dynamic hold mappings" in result["error"]
    assert "prevent data loss" in result["error"]
    assert result["unchanged"]


def test_flow_dynamic_hold_mappings_refuse_visual_mode_without_modifying_yaml() -> None:
    result = _run_panel_js(r'''(() => {
      const values = {
        after: `{tiers: [{active_for: 0s, duration: 30s}], adjustments: [], max: 5m}`,
        after_when_stops: `{tiers: [{active_for: "0s", duration: '30s'}], adjustments: [{from: "22:00", multiply: 2}], max: 5m} # adaptive`,
      };
      return Object.entries(values).map(([alias, value]) => {
        const yaml = `- id: adaptive-${alias}\n  while: {binary_sensor.office: on}\n  hold:\n    ${alias}: ${value}\n  intent:\n    light.office: {state: on}\n`;
        const panel = new IntentionalPanel();
        panel._render = () => {};
        panel._contents = yaml;
        panel._selectedRuleId = `adaptive-${alias}`;
        panel._selectedRuleContents = yaml;
        panel._selectedRuleForm = parseRuleForm(yaml, null);
        panel._editorMode = "yaml";
        panel._showVisualRule();
        return {alias, mode: panel._editorMode, error: panel._visualModeError, unchanged: panel._candidateContents() === yaml};
      });
    })()''')

    assert {item["alias"] for item in result} == {"after", "after_when_stops"}
    assert all(item["mode"] == "yaml" for item in result)
    assert all("dynamic hold mappings" in item["error"] for item in result)
    assert all(item["unchanged"] for item in result)


def test_scalar_hold_durations_are_not_mistaken_for_flow_mappings() -> None:
    result = _run_panel_js(r'''(() => {
      const values = ["5m", '"{five minutes}"', "'{five minutes}'"];
      return values.map((value) => visualModeError(`- id: scalar\n  while:\n    binary_sensor.office: on\n  hold:\n    after_when_stops: ${value}\n  intent:\n    light.office:\n      state: on\n`));
    })()''')

    assert result == ["", "", ""]


def test_block_style_labels_refuse_visual_mode_with_normalized_api_rule() -> None:
    result = _run_panel_js(r'''(() => {
      const yaml = `- id: labelled\n  labels:\n    - lighting\n    - evening\n  while:\n    binary_sensor.office: on\n  intent:\n    light.office:\n      state: on\n`;
      const normalized = {id: "labelled", enabled: true, labels: ["lighting", "evening"], notes: "", when: "binary_sensor.office == on", target: "light.office", set: {state: "on"}, effects: []};
      const panel = new IntentionalPanel();
      panel._render = () => {};
      panel._contents = yaml;
      panel._selectedRuleId = "labelled";
      panel._selectedRuleContents = yaml;
      panel._selectedRuleForm = parseRuleForm(yaml, normalized);
      panel._editorMode = "yaml";
      panel._showVisualRule();
      return {mode: panel._editorMode, error: panel._visualModeError, unchanged: panel._candidateContents() === yaml};
    })()''')

    assert result["mode"] == "yaml"
    assert "block-style 'labels'" in result["error"]
    assert "prevent data loss" in result["error"]
    assert result["unchanged"]


def test_block_scalar_metadata_refuses_visual_mode_with_normalized_api_rules() -> None:
    result = _run_panel_js(r'''(() => {
      const normalized = {id: "unsafe", enabled: true, labels: [], notes: "normalized notes", when: "binary_sensor.office == on", target: "light.office", set: {state: "on"}, effects: []};
      const examples = {
        reason: "reason: |\n    Keep the room lit\n    while occupied",
        notes: "notes: >-\n    First line\n    second line",
        group: "group: |+\n    living-room",
        profile: "profile: >2-\n    settled",
        authority: "authority: >\n    automation",
        confidence: "confidence: |\n    1.0",
      };
      return Object.fromEntries(Object.entries(examples).map(([field, metadata]) => {
        const yaml = `- id: unsafe\n  ${metadata}\n  while:\n    binary_sensor.office: on\n  intent:\n    light.office:\n      state: on\n`;
        return [field, {error: visualModeError(yaml, normalized), output: stringifyRule(parseRuleForm(yaml, normalized))}];
      }));
    })()''')

    assert set(result) == {"reason", "notes", "group", "profile", "authority", "confidence"}
    for field, guarded in result.items():
        assert f"block scalar '{field}' metadata" in guarded["error"]
        assert "prevent data loss" in guarded["error"]
        assert guarded["output"] != ""


def test_inline_labels_and_scalar_metadata_round_trip_with_normalized_api_rule() -> None:
    result = _run_panel_js(r'''(() => {
      const yaml = `- id: safe-metadata\n  labels: [lighting, evening]\n  group: living-room\n  profile: settled\n  authority: sensor\n  confidence: 0.8\n  reason: Occupied room\n  notes: Keep this authored note\n  while:\n    binary_sensor.office: on\n  intent:\n    light.office:\n      state: on\n`;
      const normalized = {id: "safe-metadata", enabled: true, labels: ["lighting", "evening"], notes: "Keep this authored note", when: "binary_sensor.office == on", target: "light.office", set: {state: "on"}, effects: []};
      return {error: visualModeError(yaml), output: stringifyRule(parseRuleForm(yaml, normalized))};
    })()''')

    assert result["error"] == ""
    assert "labels: [lighting, evening]" in result["output"]
    for metadata in ["group: living-room", "profile: settled", "authority: sensor", "confidence: 0.8", 'reason: "Occupied room"', 'notes: "Keep this authored note"']:
        assert metadata in result["output"]


def test_inline_label_containing_comma_refuses_visual_mode_without_corrupting_yaml() -> None:
    result = _run_panel_js(r'''(() => {
      const yaml = `- id: comma-label\n  labels: ["heating, cooling", evening]\n  while:\n    binary_sensor.office: on\n  intent:\n    climate.office:\n      state: heat\n`;
      const panel = new IntentionalPanel();
      panel._render = () => {};
      panel._contents = yaml;
      panel._selectedRuleId = "comma-label";
      panel._selectedRuleContents = yaml;
      panel._selectedRuleForm = parseRuleForm(yaml, {id: "comma-label", labels: ["heating, cooling", "evening"]});
      panel._editorMode = "yaml";
      panel._showVisualRule();
      return {mode: panel._editorMode, error: panel._visualModeError, unchanged: panel._candidateContents() === yaml};
    })()''')

    assert result["mode"] == "yaml"
    assert "inline 'labels' containing commas" in result["error"]
    assert "prevent data loss" in result["error"]
    assert result["unchanged"]


def test_nested_unsupported_intent_constructs_refuse_visual_mode_without_serializing() -> None:
    result = _run_panel_js(r'''(() => {
      const examples = {
        select: `    select:\n      - domain: light\n        area: office`,
        suppress: `    suppress:\n      rules: [daylight]`,
        include: `    include: scene.movie`,
        linger: `    light.office:\n      state: on\n      linger: 2m`,
        generator: `    light.office:\n      rgb_color:\n        generate:\n          kind: sample\n          from: [[1, 2, 3]]`,
        animation: `    light.office:\n      brightness_pct: {animate: {kind: pulse, values: [0, 100]}}`,
        metadata: `    light.office:\n      state: on\n      transition: 5s`,
        application_metadata: `    light.office:\n      state: on\n      apply: {retries: 3}`,
      };
      return Object.entries(examples).map(([name, intent]) => {
        const yaml = `- id: unsafe-${name}\n  while:\n    binary_sensor.office: on\n  intent:\n${intent}\n`;
        const panel = new IntentionalPanel();
        panel._render = () => {};
        panel._contents = yaml;
        panel._selectedRuleId = `unsafe-${name}`;
        panel._selectedRuleContents = yaml;
        panel._selectedRuleForm = parseRuleForm(yaml, null);
        panel._editorMode = "yaml";
        panel._showVisualRule();
        return {
          name,
          mode: panel._editorMode,
          error: panel._visualModeError,
          unchanged: panel._candidateContents() === yaml,
        };
      });
    })()''')

    assert {item["name"] for item in result} == {
        "select", "suppress", "include", "linger", "generator", "animation", "metadata",
        "application_metadata",
    }
    assert all(item["mode"] == "yaml" for item in result)
    assert all("prevent data loss" in item["error"] for item in result)
    assert all(item["unchanged"] for item in result)


def test_supported_intent_metadata_can_still_enter_visual_mode() -> None:
    result = _run_panel_js(r'''visualModeError(`- id: safe\n  while:\n    binary_sensor.office: on\n  intent:\n    light.office:\n      state: on\n      brightness_pct:\n        max: 40\n      ttl: 30s\n      easing: ease-in\n      apply:\n        transition:\n          assert: 2s\n          change: 3s\n          withdraw: 4s\n`)''')

    assert result == ""


def test_block_nested_intent_values_refuse_visual_mode() -> None:
    result = _run_panel_js(r'''(() => {
      const values = {
        field_mapping: `      rgb_color:\n        red: 255\n        green: 0`,
        field_list: `      rgb_color:\n        - 255\n        - 0\n        - 128`,
        operator_mapping: `      rgb_color:\n        value:\n          red: 255`,
        operator_list: `      rgb_color:\n        value:\n          - 255\n          - 0\n          - 128`,
      };
      return Object.fromEntries(Object.entries(values).map(([name, value]) => [name,
        visualModeError(`- id: ${name}\n  while:\n    binary_sensor.office: on\n  intent:\n    light.office:\n${value}\n`)
      ]));
    })()''')

    assert set(result) == {"field_mapping", "field_list", "operator_mapping", "operator_list"}
    assert all("prevent data loss" in error for error in result.values())


def test_simple_intent_scalars_and_inline_lists_round_trip() -> None:
    result = _run_panel_js(r'''(() => {
      const yaml = `- id: safe-values\n  while:\n    binary_sensor.office: on\n  intent:\n    light.office:\n      state: on\n      rgb_color: [255, 0, 128]\n      brightness_pct:\n        max: 40\n      supported_colors:\n        value: [red, green]\n`;
      return {error: visualModeError(yaml), output: stringifyRule(parseRuleForm(yaml, null))};
    })()''')

    assert result["error"] == ""
    assert "rgb_color: [255, 0, 128]" in result["output"]
    assert "max: 40" in result["output"]
    assert "value: [red, green]" not in result["output"]
    assert "supported_colors: [red, green]" in result["output"]


def test_stale_validation_response_is_ignored() -> None:
    result = _run_panel_js(r'''(async () => {
      const panel = new IntentionalPanel();
      panel._render = () => {};
      panel._candidateContents = () => panel.contents;
      const resolvers = [];
      panel._api = () => new Promise((resolve) => resolvers.push(resolve));
      panel.contents = "old";
      const oldRequest = panel._validate({quiet: true});
      panel.contents = "new";
      const newRequest = panel._validate({quiet: true});
      resolvers[1]({valid: true, marker: "new", normalized: []});
      await newRequest;
      resolvers[0]({valid: true, marker: "old", normalized: []});
      await oldRequest;
      return panel._validation.marker;
    })()''')
    assert result == "new"


def test_validation_returns_api_valid_flag() -> None:
    result = _run_panel_js(r'''(async () => {
      const panel = new IntentionalPanel(); panel._render = () => {};
      panel._candidateContents = () => "- id: invalid\n";
      panel._api = async () => ({valid: false, errors: ["no target"], normalized: []});
      return await panel._validate({quiet: true});
    })()''')
    assert result is False


def test_draft_workflow_and_structured_preview_helpers() -> None:
    result = _run_panel_js(r'''({
      stages: [
        draftStageTransition("Draft", "validate-valid"),
        draftStageTransition("Checked", "review"),
        draftStageTransition("Reviewed", "publish"),
        draftStageTransition("Reviewed", "edit"),
      ],
      summary: summarizePreview({preview: [
        {target: "light.one", changes: {state: {from: "off", to: "on", changed: true}}},
        {target: "light.two", changes: {}},
      ], effects: [{}], withdrawals: [{}], errors: []}),
    })''')
    assert result["stages"] == ["Checked", "Reviewed", "Published", "Draft"]
    assert result["summary"]["changing"] == 1
    assert result["summary"]["unchanged"] == 1
    assert result["summary"]["effects"] == 1
    assert result["summary"]["withdrawals"] == 1


def test_panel_mvp_accessibility_responsiveness_and_dirty_guards() -> None:
    source = PANEL_PATH.read_text()
    for required in [
        'aria-live="polite"', 'role="alert"', 'aria-busy=', 'aria-current="true"',
        'aria-label="Remove target"', ':focus-visible', '@media (max-width: 700px)',
        'beforeunload', 'Discard unsaved editor changes and reload?',
        'this._api("POST", "preview"', 'expected_generation:',
    ]:
        assert required in source


def test_rename_replaces_original_source_id() -> None:
    result = _run_panel_js(r'''(() => {
      const panel = new IntentionalPanel(); panel._render = () => {};
      panel._contents = `- id: old-name\n  while:\n    binary_sensor.ready: on\n  intent:\n    light.room:\n      state: on\n`;
      panel._rules = parseDocumentRuleSummaries(panel._contents, []);
      panel._selectRule("old-name", {render: false});
      panel._selectedRuleForm.id = "new-name";
      panel._formEdited = true;
      return panel._candidateContents();
    })()''')
    assert "id: new-name" in result
    assert "id: old-name" not in result


def test_publish_puts_exact_reviewed_candidate_without_gate_demotion() -> None:
    result = _run_panel_js(r'''(async () => {
      const panel = new IntentionalPanel(); panel._render = () => {}; panel._loadHistory = async () => {}; panel._refreshWorld = async () => {};
      panel._document = {generation: "g1"}; panel._contents = "reviewed"; panel._dirty = true;
      panel._stage = "Reviewed"; panel._reviewedFingerprint = candidateFingerprint("reviewed");
      const calls = []; panel._api = async (method, path, data) => { calls.push({method, path, data}); return path === "validate" ? {valid: true, normalized: []} : {generation: "g2", contents: "reviewed"}; };
      await panel._save();
      return {calls, stage: panel._stage};
    })()''')
    put = next(call for call in result["calls"] if call["method"] == "PUT")
    assert put["data"] == {"contents": "reviewed", "expected_generation": "g1"}
    assert result["stage"] == "Published"


def test_in_flight_publish_preserves_newer_draft() -> None:
    result = _run_panel_js(r'''(async () => {
      const panel = new IntentionalPanel(); panel._render = () => {}; panel._loadHistory = async () => {}; panel._refreshWorld = async () => {};
      panel._document = {generation: "g1"}; panel._contents = "old"; panel._dirty = true; panel._stage = "Reviewed"; panel._reviewedFingerprint = candidateFingerprint("old");
      let release; panel._api = async (method, path) => path === "validate" ? {valid: true, normalized: []} : new Promise(resolve => { release = resolve; });
      const saving = panel._save(); await Promise.resolve(); await Promise.resolve(); panel._contents = "newer"; panel._dirty = true;
      release({generation: "g2", contents: "old"}); await saving;
      return {base: panel._document.generation, candidate: panel._candidateContents(), dirty: panel._dirty, stage: panel._stage};
    })()''')
    assert result == {"base": "g2", "candidate": "newer", "dirty": True, "stage": "Draft"}


def test_ledger_alignment_and_competition_are_target_scoped() -> None:
    result = _run_panel_js(r'''(() => {
      const block = `- id: one\n  while:\n    sensor.ready: on\n  intent:\n    light.one:\n      state: on\n`;
      return buildRuleViewModels(parseDocumentRuleSummaries(block, []), {targets: [
        {target: "light.one", plan_match: "match", active_intents: [{rule_id: "one"}, {rule_id: "rival"}]},
        {target: "light.other", plan_match: "mismatch", active_intents: [{rule_id: "unrelated"}]},
      ]}, {})[0];
    })()''')
    assert result["targets"][0]["aligned"] is True
    assert [item["rule_id"] for item in result["competing"]] == ["rival"]


def test_comment_guard_is_quote_aware() -> None:
    result = _run_panel_js(r'''[
      visualModeError(`- id: one # authored\n  while:\n    sensor.ready: on\n`),
      visualModeError(`- id: "one # literal"\n  while:\n    sensor.ready: "on # literal"\n`),
    ]''')
    assert "inline comments" in result[0]
    assert result[1] == ""


def test_structured_preview_counts_changed_flags_and_unique_effects_truthfully() -> None:
    result = _run_panel_js(r'''summarizePreview({preview: [
      {changes: {state: {changed: false}}}, {changes: {state: {changed: true}}}
    ], phases: [{effects: [{rule_id: "r", service: "notify.one"}]}, {effects: [{rule_id: "r", service: "notify.one"}]}]})''')
    assert result["changing"] == 1
    assert result["unchanged"] == 1
    assert result["effects"] == 1
    assert result["withdrawals"] is None


def test_preview_includes_future_service_plan_targets_without_false_no_change() -> None:
    result = _run_panel_js(r'''summarizePreview({preview: [
      {target: "light.now", changes: {state: {changed: true}}},
      {target: "light.later", changes: {state: {changed: false}}}
    ], phases: [{horizon_ms: 60000, service_plans: [{target: "light.later"}]}]})''')
    assert result["nowChanging"] == 1
    assert result["laterChanging"] == 1
    assert result["unchanged"] == 0
    assert "1 now, 1 later" in result["headline"]


def test_rule_detail_uses_composed_desired_and_projection_issues_take_precedence() -> None:
    result = _run_panel_js(r'''(() => {
      const block = `- id: one\n  while:\n    sensor.ready: on\n  intent:\n    light.one:\n      state: on\n`;
      return buildRuleViewModels(parseDocumentRuleSummaries(block, []), {authored_rules: [{rule_id: "one", active: true}], targets: [
        {target: "light.one", desired: {state: "off"}, resolved: {state: "wrong"}, plan_match: "mismatch"}
      ]}, {})[0];
    })()''')
    assert result["targets"][0]["desired"] == {"state": "off"}
    assert result["section"] == "attention"


def test_source_is_preserved_until_guided_form_is_actually_edited() -> None:
    result = _run_panel_js(r'''(() => {
      const panel = new IntentionalPanel(); panel._render = () => {};
      panel._contents = `# standalone\n- id: one\n  reason: 'kept formatting'\n  while:\n    sensor.ready: on\n  intent:\n    light.one:\n      state: on\n`;
      panel._rules = parseDocumentRuleSummaries(panel._contents, []); panel._selectRule("one", {render: false});
      const original = panel._selectedRuleContents; panel._showYamlRule();
      return {source: panel._selectedRuleContents, candidate: panel._candidateContents(), original};
    })()''')
    assert result["source"] == result["original"]
    assert result["candidate"] == "# standalone\n" + result["original"]


def test_stale_preview_cannot_review_newer_candidate() -> None:
    result = _run_panel_js(r'''(async () => {
      const panel = new IntentionalPanel(); panel._render = () => {}; panel._validate = async () => true;
      panel.contents = "old"; panel._candidateContents = () => panel.contents;
      let release; panel._api = () => new Promise(resolve => { release = resolve; });
      const reviewing = panel._review(); await Promise.resolve(); panel.contents = "new";
      panel._previewRequest += 1; panel._stage = "Draft"; panel._reviewedFingerprint = "";
      release({preview: []}); await reviewing;
      return {stage: panel._stage, preview: panel._preview, fingerprint: panel._reviewedFingerprint};
    })()''')
    assert result == {"stage": "Draft", "preview": None, "fingerprint": ""}


def test_mobile_edit_back_preserves_dirty_draft_after_confirmation() -> None:
    result = _run_panel_js(r'''(() => {
      const panel = new IntentionalPanel(); panel._render = () => {}; panel._screen = "edit"; panel._dirty = true;
      globalThis.confirm = () => true;
      panel._handleAction({dataset: {action: "leave-edit", destination: "detail"}});
      return {screen: panel._screen, dirty: panel._dirty, html: panel._renderEditBack.call({_selectedRuleId: "one", _dirty: true})};
    })()''')
    assert result["screen"] == "detail"
    assert result["dirty"] is True
    assert "Draft preserved" in result["html"]


def test_rollback_review_fetches_snapshot_before_separate_apply() -> None:
    result = _run_panel_js(r'''(async () => {
      const panel = new IntentionalPanel(); panel._render = () => {}; const calls = [];
      panel._api = async (method, path) => { calls.push([method, path]); return {generation: "generation-1", contents: "<unsafe>"}; };
      await panel._reviewRollback("generation-1");
      return {calls, html: panel._renderHistory.call({...panel, _history: [{generation: "generation-1"}]})};
    })()''')
    assert result["calls"] == [["GET", "rules/history/generation-1"]]
    assert "&lt;unsafe&gt;" in result["html"]
    assert "Apply this rollback" in result["html"]


def test_large_ledger_build_is_linear_enough_for_50_rules() -> None:
    result = _run_panel_js(r'''(() => {
      const rules = Array.from({length: 60}, (_, i) => ({id: `r${i}`, block: `- id: r${i}\n  while:\n    sensor.ready: on\n  intent:\n    light.t${i}:\n      state: on\n`}));
      const targets = rules.map((_, i) => ({target: `light.t${i}`, plan_match: "match", active_intents: [{rule_id: `r${i}`}] }));
      const built = buildRuleViewModels(rules, {targets}, {});
      return {count: built.length, targets: built.reduce((n, rule) => n + rule.targets.length, 0)};
    })()''')
    assert result == {"count": 60, "targets": 60}


def test_integration_registers_frontend_panel_asset() -> None:
    source = INTEGRATION_INIT.read_text()

    assert 'FRONTEND_URL_PATH = "/api/intentional/frontend"' in source
    assert 'PANEL_URL_PATH = "intentional"' in source
    assert 'DEPENDENCIES = ["http"]' in source
    assert "async_register_static_paths" in source
    assert "panel_custom.async_register_panel" in source
    assert 'webcomponent_name="intentional-panel"' in source
    assert 'module_url=f"{FRONTEND_URL_PATH}/intentional-panel.js?v={__version__}"' in source
