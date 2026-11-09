"""Real-browser contract tests for the bundled status-first Lovelace card."""

from pathlib import Path
from shutil import which
from types import SimpleNamespace

import pytest

from intentional.dashboard_presentation import dashboard_presentation
from intentional.engine import Engine
from intentional.yaml_loader import Rule

playwright = pytest.importorskip("playwright.sync_api", reason="optional browser tests need Playwright")
CHROMIUM = which("chromium") or which("google-chrome")
pytestmark = pytest.mark.skipif(not CHROMIUM, reason="system Chromium is required")
CARD = Path(__file__).parent.parent / "custom_components/intentional/frontend/intentional-card.js"


@pytest.fixture
def page():
    engine = Engine(clock_fn=lambda: 0)
    engine.load_rules([
        Rule(id="evening:a", authored_rule_id="evening", when="true", target="light.a",
             set={"brightness_pct": 80}, group="lighting", reason="Evening presence <script>alert(1)</script>"),
        Rule(id="evening:b", authored_rule_id="evening", when="true", target="light.b",
             set={"brightness_pct": 35}, group="lighting", reason="Evening presence"),
        Rule(id="shield", when="true", target="light.a", cap={"brightness_pct": 25}, reason="Dim during playback"),
    ])
    engine.evaluate_all()
    engine.emit_user_intent("light.a", {"brightness_pct": 60}, ttl_ms=5000)
    data = dashboard_presentation(
        engine, area="living", domain="light", area_for_target=lambda t: "living" if t == "light.a" else "office",
        actual_for_target=lambda t: SimpleNamespace(entity_id=t, state="on", attributes={"brightness": 102}),
    )
    data.update(runtime_status="ok", generated_at="2026-11-10T00:00:00Z")
    data["capabilities"]["configure"] = True
    for row in data["rules"]:
        row.update(name=row["rule_id"].title(), enable_entity_id=f"switch.rule_{row['rule_id']}")
    with playwright.sync_playwright() as manager:
        browser = manager.chromium.launch(executable_path=CHROMIUM, headless=True)
        page = browser.new_page(viewport={"width": 390, "height": 844})
        page.set_default_timeout(5000)
        page.set_content("""<style>body{margin:12px;font:14px system-ui;background:#f5f5f5}
          ha-card{display:block;border-radius:16px;background:white;box-shadow:0 2px 8px #ddd}
          </style><intentional-card></intentional-card>""")
        page.add_script_tag(path=str(CARD))
        page.evaluate("""data => {
          window.payload = data; window.calls = []; window.services = [];
          window.card = document.querySelector('intentional-card');
          window.hass = {language:'en', states:{}, areas:{living:{name:'Living room'}},
            callApi: async (method,path) => {calls.push([method,path]); return structuredClone(payload);},
            callService: async (...args) => {services.push(args);
              if(args[0] === 'switch') payload.rules.find(r => r.enable_entity_id === args[2].entity_id).variants.forEach(v => v.enabled = args[1] === 'turn_on');
            }};
          card.setConfig({type:'custom:intentional-card',area:'living',domain:'light',refresh_seconds:5});
          card.hass = hass;
        }""", data)
        page.get_by_role("button", name="Automation").wait_for()
        page.wait_for_function("card._data !== null")
        yield page
        browser.close()


def test_local_popup_status_first_shared_scope_and_deliberate_settings(page):
    page.evaluate("""() => {
      window.second = document.createElement('intentional-card'); document.body.append(second);
      second.setConfig({area:'living'}); second.hass = hass;
    }""")
    page.locator("intentional-card").first.get_by_role("button", name="Automation").click()
    dialog = page.get_by_role("dialog")
    assert dialog.count() == 1
    assert page.get_by_role("button", name="Disable rule").count() == 0
    page.locator('details[data-detail="evening"] > summary').first.click()
    assert dialog.get_by_text("Evening presence <script>alert(1)</script>", exact=True).is_visible()
    assert dialog.get_by_text("Also affects: light.b", exact=True).is_visible()
    assert dialog.get_by_text("Manual precedence", exact=True).is_visible()
    assert page.locator("script").count() == 1  # only the loaded module, never authored markup
    assert dialog.get_by_text("Observed state differs from demand", exact=True).is_visible()
    assert 'Brightness: 25%' in dialog.inner_text()
    assert 'Brightness: 102/255' in dialog.inner_text()
    dialog.get_by_role("button", name="Settings", exact=True).click()
    assert dialog.get_by_text("Also affects: light.b", exact=True).is_visible()
    dialog.get_by_role("button", name="Disable rule").first.click()
    page.wait_for_function("services.length === 1 && !card._busy")
    assert page.evaluate("services[0]") == ["switch", "turn_off", {"entity_id": "switch.rule_evening"}]
    dialog.get_by_role("button", name="Resume listed overrides").click()
    page.wait_for_function("services.length === 2 && !card._busy")
    assert page.evaluate("services[1]") == ["intentional", "clear", {"target": "light.a"}]
    page.keyboard.press("Escape")
    assert page.get_by_role("dialog").count() == 0
    page.wait_for_function("card.shadowRoot.activeElement?.dataset.action === 'open'")
    assert page.evaluate("second.shadowRoot.querySelector('dialog').open") is False


def test_refresh_keeps_details_focus_and_stale_snapshot_disables_controls(page):
    page.get_by_role("button", name="Automation").click()
    page.locator('details[data-detail="evening"] > summary').click()
    page.get_by_role("button", name="Settings", exact=True).focus()
    page.evaluate("card._refresh()")
    assert page.locator('details[data-detail="evening"]').get_attribute("open") == ""
    assert page.evaluate("card.shadowRoot.activeElement.dataset.action") == "settings"
    page.evaluate("hass.callApi = async () => {throw new Error('offline')}; card._refresh()")
    assert page.get_by_role("alert").is_visible()
    page.get_by_role("button", name="Settings", exact=True).click()
    assert page.get_by_role("button", name="Disable rule").first.is_disabled()
    assert page.get_by_role("button", name="Resume listed overrides").is_disabled()


def test_scope_change_ignores_old_response_and_polling_does_not_fan_out(page):
    page.evaluate("""() => {
      hass.callApi = () => new Promise(resolve => {window.release = resolve});
      card._refresh(); card.setConfig({area:'office',refresh_seconds:5});
      for(let i=0;i<50;i++) card.hass = {...hass};
      window.newPayload = {...payload,rules:[],targets:[],scope:{area:'office'},capabilities:{resume_targets:[],configure:true}};
      hass.callApi = async (_method,path) => {calls.push(['GET',path]);return newPayload};
      card.hass = hass; release(payload);
    }""")
    page.wait_for_function("card._data?.scope.area === 'office'")
    assert page.evaluate("calls.length") == 2
    assert page.evaluate("calls[1][1]") == "intentional/presentation?area=office"
    page.get_by_role("button", name="Automation").click()
    assert page.get_by_text("No rules in this scope", exact=True).last.is_visible()
    page.evaluate("card.remove()")
    assert page.evaluate("card._timer") is None


def test_inline_german_editor_roundtrip_and_non_admin_settings(page):
    page.evaluate("""() => {
      payload.capabilities.configure = false;
      card.setConfig({display:'inline',language:'de',area:'living',domain:'light'});
      window.editor = document.createElement('intentional-card-editor');document.body.append(editor);
      editor.setConfig({type:'custom:intentional-card',area:'living',grid_options:{columns:6}});editor.hass=hass;
      editor.addEventListener('config-changed',e => window.edited=e.detail.config);
    }""")
    page.get_by_role("heading", name="Automatik", exact=True).wait_for()
    page.get_by_role("button", name="Einstellungen", exact=True).click()
    assert page.get_by_text("Regeleinstellungen benötigen Administratorrechte.").is_visible()
    assert page.get_by_role("button", name="Regel deaktivieren").count() == 0
    page.get_by_label("Rule group", exact=True).fill("lighting")
    page.get_by_label("Rule group", exact=True).press("Tab")
    assert page.evaluate("edited") == {"type": "custom:intentional-card", "area": "living", "grid_options": {"columns": 6}, "group": "lighting"}
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def test_action_failure_visible_and_popup_mobile_screenshot(page):
    page.get_by_role("button", name="Automation").click()
    page.locator('details[data-detail="evening"] > summary').click()
    assert page.evaluate("card.shadowRoot.querySelector('dialog').scrollWidth <= innerWidth")
    page.screenshot(path="/tmp/opencode/intentional-card-mobile.png")
    page.get_by_role("button", name="Settings", exact=True).click()
    page.evaluate("() => {hass.callService = async () => {throw new Error('permission denied')}}")
    page.get_by_role("button", name="Resume listed overrides").click()
    page.get_by_role("alert").get_by_text("Action failed: permission denied").wait_for()


def test_unavailable_targets_and_global_off_are_explicit(page):
    page.evaluate("""async () => {
      payload.targets[0].availability='unavailable'; payload.targets[0].actual={state:'unavailable',attributes:{}};
      payload.targets[0].plan_match='unknown'; await card._refresh();
    }""")
    assert "1 targets unavailable" in page.get_by_role("button", name="Automation").inner_text()
    page.get_by_role("button", name="Automation").click()
    assert page.get_by_text("Unavailable", exact=True).count() == 2
    assert page.get_by_text("Observed state matches demand", exact=True).count() == 0
    page.evaluate("async () => {payload.global_enabled=false;await card._refresh()}")
    assert page.get_by_text("Automation globally off", exact=True).count() >= 1
