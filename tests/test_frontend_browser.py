"""Optional real-browser panel regressions (Playwright + system Chromium)."""

from pathlib import Path
from shutil import which

import pytest
import yaml

playwright = pytest.importorskip("playwright.sync_api", reason="optional browser tests need Playwright")
CHROMIUM = which("chromium") or which("google-chrome")
pytestmark = pytest.mark.skipif(not CHROMIUM, reason="system Chromium is required")
PANEL = Path(__file__).parent.parent / "custom_components/intentional/frontend/intentional-panel.js"


@pytest.fixture
def page():
    with playwright.sync_playwright() as manager:
        browser = manager.chromium.launch(executable_path=CHROMIUM, headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.set_default_timeout(5_000)
        page.set_content("""<style>
          body { margin: 0; font: 16px Arial; --primary-text-color: #202124;
            --secondary-text-color: #59636b; --primary-background-color: #fafafa;
            --secondary-background-color: #edf1f4; --card-background-color: white;
            --divider-color: #ddd; --primary-color: #006b8f; --text-primary-color: white; }
          #ha-scroll { height: 800px; overflow: auto; }
        </style><div id="ha-scroll"><intentional-panel></intentional-panel></div>""")
        page.add_script_tag(path=str(PANEL))
        page.evaluate("""() => {
          window.panel = document.querySelector('intentional-panel');
          window.rules = '- id: occupied\\n  reason: Keep the room comfortable\\n  while:\\n    binary_sensor.motion: on\\n  intent:\\n    light.desk:\\n      state: on\\n      brightness_pct: 60\\n';
          window.world = {authored_rules: [{rule_id: 'occupied', active: true}], targets: [{
            target: 'light.desk', desired: {state: 'on', brightness_pct: 60},
            actual: {state: 'off'}, plan_match: 'mismatch',
            fields: [{field: 'brightness_pct', value: 60, provider: {rule_id: 'occupied', authority: 'automation'}}]
          }]};
          window.api = async (_method, path) => {
            if (path === 'rules/document') return {contents: rules, generation: 'g1', authored_rules: [{id: 'occupied', definition: {id: 'occupied', reason: 'Keep the room comfortable', while: {'binary_sensor.motion': 'on'}, intent: {'light.desk': {state: 'on', brightness_pct: 60}}}}]};
            if (path === 'world') return world;
            if (path === 'rules/history') return new Promise(() => {});
            if (path === 'alerts?scope=current') return {alerts: [], resolved_total: 0};
            if (path === 'validate') return {valid: true, normalized: []};
            return {status: 'ok', version: 'local'};
          };
          window.hass = {states: {'binary_sensor.motion': {state: 'on', attributes: {friendly_name: 'Room motion'}}}, callApi: (_method, path, data) => api(_method, path.replace('intentional/', ''), data)};
        }""")
        yield page
        browser.close()


def test_delayed_data_shows_loading_and_never_false_alignment(page):
    page.evaluate("""() => {
      const original = api;
      window.api = (method, path, data) => path === 'world' ? new Promise(resolve => {window.releaseWorld = resolve;}) : original(method, path, data);
      panel.hass = hass;
    }""")
    page.get_by_role("heading", name="Loading intentions…").wait_for()
    assert page.get_by_text("Your home is aligned").count() == 0
    page.evaluate("releaseWorld(world)")
    page.get_by_role("heading", name="1 rule needs attention").wait_for()
    assert page.evaluate("panel._busy") is False
    page.locator("button[data-rule-id='occupied']").click()
    page.get_by_role("heading", name="Targets: requested, resolved and actual").wait_for()
    assert page.get_by_text("Room motion", exact=True).is_visible()


@pytest.mark.parametrize("mode", ["visual", "yaml"])
def test_editor_node_focus_selection_and_outer_scroll_survive_updates(page, mode):
    page.evaluate("panel.hass = hass")
    page.wait_for_function("panel._busy === false")
    page.evaluate("""mode => {
      panel._selectRule('occupied', {screen: 'edit'});
      if (mode === 'yaml') {
        panel._selectedRuleContents += '\\n' + '# A long comment\\n'.repeat(100);
        panel._showYamlRule();
      }
      window.editor = panel.shadowRoot.querySelector(mode === 'yaml' ? '.yaml-editor' : '[data-field="notes"]');
      editor.focus();
      editor.value = mode === 'yaml' ? editor.value : 'Unsaved note';
      editor.dispatchEvent(new Event('input', {bubbles: true}));
      editor.setSelectionRange(3, 8);
      if (mode === 'yaml') { editor.style.height = '420px'; editor.scrollTop = 180; }
      document.querySelector('#ha-scroll').scrollTop = 360;
      window.before = {outer: document.querySelector('#ha-scroll').scrollTop, inner: editor.scrollTop, contents: panel._candidateContents()};
    }""", mode)
    page.evaluate("""async () => {
      for (let i = 0; i < 3; i++) { await panel._refreshWorld(); await panel._validate({quiet: true}); }
    }""")
    actual = page.evaluate("""() => ({
      connected: editor.isConnected, focused: panel.shadowRoot.activeElement === editor,
      selection: [editor.selectionStart, editor.selectionEnd],
      sameOuter: document.querySelector('#ha-scroll').scrollTop === before.outer,
      sameInner: editor.scrollTop === before.inner,
      sameDraft: panel._candidateContents() === before.contents,
    })""")
    assert actual == {"connected": True, "focused": True, "selection": [3, 8], "sameOuter": True, "sameInner": True, "sameDraft": True}
    if mode == "visual":
        page.locator("button[data-action='add-condition']").click()
        assert page.evaluate("panel._selectedRuleForm.conditions.length") == 2


def test_change_guidance_opens_correct_editor_section_on_mobile(page):
    page.set_viewport_size({"width": 390, "height": 800})
    page.evaluate("panel.hass = hass")
    page.wait_for_function("panel._busy === false")
    page.locator("button[data-rule-id='occupied']").click()
    page.get_by_role("button", name="Change how long it stays active").click()
    assert page.evaluate("panel.shadowRoot.activeElement.dataset.field") == "holdMode"
    page.locator("select[data-field='holdMode']").select_option("after")
    assert page.locator("input[data-field='holdAfter']").is_visible()
    assert page.evaluate("document.body.scrollWidth <= innerWidth")


def test_live_refresh_preserves_rule_buttons_and_search_focus(page):
    page.evaluate("panel.hass = hass")
    page.wait_for_function("panel._busy === false")
    page.evaluate("window.originalRule = panel.shadowRoot.querySelector('[data-rule-id]')")
    search = page.get_by_placeholder("Name, room or target")
    search.fill("comfortable")
    page.evaluate("""async () => {
      window.originalSearch = panel.shadowRoot.querySelector('[data-rule-search]');
      for (let i = 0; i < 3; i++) await panel._refreshWorld();
    }""")
    assert page.evaluate("originalRule.isConnected && originalSearch.isConnected && panel.shadowRoot.activeElement === originalSearch")
    page.locator("button[data-rule-id='occupied']").click()
    assert page.evaluate("panel._selectedRuleId") == "occupied"


def test_realistic_rule_shapes_have_truthful_details_and_safe_editor_modes(page):
    contents = (Path(__file__).parent / "fixtures/ui_realistic_rules.yaml").read_text()
    descriptors = [{"id": rule["id"], "definition": rule} for rule in yaml.safe_load(contents)]
    page.evaluate("""({contents, descriptors}) => {
      window.rules = contents;
      const oldApi = api;
      window.api = (m, p, d) => p === 'rules/document' ? Promise.resolve({contents, authored_rules: descriptors, generation:'realistic'}) : oldApi(m,p,d);
      panel.hass = hass;
    }""", {"contents": contents, "descriptors": descriptors})
    page.wait_for_function("panel._busy === false")
    page.locator("button[data-rule-id='bedroom-off-at-23']").click()
    assert page.get_by_text('time_of_day == "23:00" and light.bedroom.state == "on"', exact=True).is_visible()
    assert page.get_by_text("state = off", exact=True).is_visible()
    assert page.get_by_role("button", name="Edit YAML", exact=True).is_visible()
    page.evaluate("panel._selectRule('office-soft-light')")
    page.get_by_role("button", name="Edit guided", exact=True).click()
    assert page.locator("input[data-field='reason']").input_value().endswith("for comfortable evenings")
    assert page.locator("input[data-field='labels']").input_value() == "office, lighting"
    assert page.evaluate("panel._dirty") is False
