"""Tests for the bundled Intentional rule editor panel."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from intentional.yaml_loader import load_rules_from_string

REPO_ROOT = Path(__file__).parent.parent
PANEL_PATH = REPO_ROOT / "custom_components" / "intentional" / "frontend" / "intentional-panel.js"
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
    result = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
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


def test_integration_registers_frontend_panel_asset() -> None:
    source = INTEGRATION_INIT.read_text()

    assert 'FRONTEND_URL_PATH = "/api/intentional/frontend"' in source
    assert 'PANEL_URL_PATH = "intentional"' in source
    assert 'DEPENDENCIES = ["http"]' in source
    assert "async_register_static_paths" in source
    assert "panel_custom.async_register_panel" in source
    assert 'webcomponent_name="intentional-panel"' in source
    assert 'module_url=f"{FRONTEND_URL_PATH}/intentional-panel.js?v={__version__}"' in source
