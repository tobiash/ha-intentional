const API_BASE = "intentional";

class IntentionalPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._document = null;
    this._health = null;
    this._history = [];
    this._rules = [];
    this._selectedRuleId = "";
    this._contents = "";
    this._dirty = false;
    this._busy = false;
    this._error = "";
    this._validation = null;
    this._preview = null;
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._loaded && hass) {
      this._loaded = true;
      this._load();
    }
  }

  set narrow(value) {
    this._narrow = Boolean(value);
    this._render();
  }

  async _api(method, path, data) {
    if (this._hass?.callApi) {
      return this._hass.callApi(method, `${API_BASE}/${path}`, data);
    }
    const response = await fetch(`/api/${API_BASE}/${path}`, {
      method,
      headers: { "Content-Type": "application/json" },
      body: data === undefined ? undefined : JSON.stringify(data),
      credentials: "same-origin",
    });
    const body = await response.json();
    if (!response.ok) {
      throw new Error(body.error || body.message || response.statusText);
    }
    return body;
  }

  async _load() {
    this._busy = true;
    this._error = "";
    this._render();
    try {
      const [health, document, history] = await Promise.all([
        this._api("GET", "health"),
        this._api("GET", "rules/document"),
        this._api("GET", "rules/history"),
      ]);
      this._health = health;
      this._document = document;
      this._contents = document.contents || "";
      this._history = history.history || [];
      this._dirty = false;
      await this._validate({ quiet: true });
    } catch (err) {
      this._error = err.message || String(err);
    } finally {
      this._busy = false;
      this._render();
    }
  }

  async _validate({ quiet = false } = {}) {
    try {
      const validation = await this._api("POST", "validate", { contents: this._contents });
      this._validation = validation;
      this._rules = validation.normalized || [];
      this._error = quiet ? this._error : "";
      return true;
    } catch (err) {
      this._validation = { valid: false, errors: [err.message || String(err)] };
      if (!quiet) {
        this._error = "Validation failed";
      }
      return false;
    } finally {
      this._render();
    }
  }

  async _dryRun() {
    this._busy = true;
    this._preview = null;
    this._render();
    try {
      this._preview = await this._api("POST", "dry-run", { contents: this._contents });
      this._error = "";
    } catch (err) {
      this._error = err.message || String(err);
    } finally {
      this._busy = false;
      this._render();
    }
  }

  async _save() {
    if (!(await this._validate())) {
      return;
    }
    this._busy = true;
    this._render();
    try {
      const saved = await this._api("PUT", "rules/document", {
        contents: this._contents,
        expected_generation: this._document?.generation,
      });
      this._document = saved;
      this._contents = saved.contents || this._contents;
      this._dirty = false;
      this._error = "";
      await this._loadHistory();
    } catch (err) {
      this._error = err.message || String(err);
    } finally {
      this._busy = false;
      this._render();
    }
  }

  async _loadHistory() {
    const history = await this._api("GET", "rules/history");
    this._history = history.history || [];
  }

  async _rollback(generation) {
    if (!confirm(`Restore generation ${generation.slice(0, 12)}?`)) {
      return;
    }
    this._busy = true;
    this._render();
    try {
      await this._api("POST", "rules/rollback", {
        generation,
        expected_generation: this._document?.generation,
      });
      await this._load();
    } catch (err) {
      this._error = err.message || String(err);
    } finally {
      this._busy = false;
      this._render();
    }
  }

  _newRule() {
    const template = `\n---\n- id: new-rule\n  enabled: false\n  reason: Describe why this rule exists\n  observe:\n    input_boolean.example: true\n  intent:\n    light.example:\n      state: true\n      brightness_pct: 50\n      apply:\n        transition:\n          assert: 2s\n          change: 4s\n          withdraw: 6s\n  confidence: 0.5\n`;
    this._contents = `${this._contents.trimEnd()}${template}`;
    this._dirty = true;
    this._render();
  }

  _onInput(event) {
    this._contents = event.target.value;
    this._dirty = true;
    this._validation = null;
    this._preview = null;
    const save = this.shadowRoot.querySelector('[data-action="save"]');
    if (save) {
      save.disabled = this._busy || !this._dirty;
    }
  }

  _selectRule(ruleId) {
    this._selectedRuleId = ruleId;
    const index = this._contents.indexOf(`id: ${ruleId}`);
    const editor = this.shadowRoot.querySelector("textarea");
    if (index >= 0 && editor) {
      editor.focus();
      editor.setSelectionRange(index, index + ruleId.length + 4);
    }
    this._render();
  }

  _ruleStatus(rule) {
    if (rule.enabled === false) return "disabled";
    if (rule.target) return rule.target;
    return "multi-target or effect";
  }

  _renderRules() {
    if (!this._rules.length) {
      return `<div class="empty">No parsed rules yet. Validate the document to refresh this list.</div>`;
    }
    return this._rules.map((rule) => `
      <button class="rule ${this._selectedRuleId === rule.id ? "selected" : ""}" data-rule-id="${escapeHtml(rule.id)}">
        <span class="rule-title">${escapeHtml(rule.id)}</span>
        <span class="rule-meta">${escapeHtml(this._ruleStatus(rule))}</span>
      </button>
    `).join("");
  }

  _renderHistory() {
    if (!this._history.length) {
      return `<div class="empty">No history yet.</div>`;
    }
    return this._history.slice(0, 8).map((item) => `
      <div class="history-item">
        <div>
          <strong>${escapeHtml((item.generation || "").slice(0, 12))}</strong>
          <span>${escapeHtml(item.reason || "unknown")}</span>
        </div>
        <button class="secondary small" data-rollback="${escapeHtml(item.generation)}">Rollback</button>
      </div>
    `).join("");
  }

  _renderValidation() {
    if (!this._validation) {
      return `<div class="muted">Not validated since last edit.</div>`;
    }
    if (this._validation.valid) {
      return `<div class="ok">Valid YAML. ${this._validation.rule_count} rule(s).</div>`;
    }
    return `<div class="error-box">${(this._validation.errors || []).map(escapeHtml).join("<br>")}</div>`;
  }

  _renderPreview() {
    if (!this._preview) {
      return `<div class="muted">Run dry-run to preview desired targets.</div>`;
    }
    return `<pre>${escapeHtml(JSON.stringify(this._preview, null, 2))}</pre>`;
  }

  _render() {
    if (!this.shadowRoot) return;
    const version = this._health?.version ? `v${this._health.version}` : "";
    const generation = this._document?.generation ? this._document.generation.slice(0, 12) : "not loaded";
    this.shadowRoot.innerHTML = `
      <style>${styles}</style>
      <main class="${this._narrow ? "narrow" : ""}">
        <header>
          <div>
            <h1>Intentional</h1>
            <p>Authored rule editor ${escapeHtml(version)} · generation ${escapeHtml(generation)}</p>
          </div>
          <div class="actions">
            <button class="secondary" data-action="reload" ${this._busy ? "disabled" : ""}>Reload</button>
            <button data-action="save" ${this._busy || !this._dirty ? "disabled" : ""}>Save</button>
          </div>
        </header>
        ${this._error ? `<div class="banner">${escapeHtml(this._error)}</div>` : ""}
        <section class="grid">
          <aside class="card rules">
            <div class="card-header">
              <h2>Rules</h2>
              <button class="secondary small" data-action="new-rule">New</button>
            </div>
            ${this._renderRules()}
          </aside>
          <section class="card editor">
            <div class="card-header">
              <h2>Document</h2>
              <div class="actions">
                <button class="secondary small" data-action="validate">Validate</button>
                <button class="secondary small" data-action="dry-run">Dry-run</button>
              </div>
            </div>
            <textarea spellcheck="false">${escapeHtml(this._contents)}</textarea>
          </section>
          <aside class="card inspector">
            <h2>Validation</h2>
            ${this._renderValidation()}
            <h2>Preview</h2>
            ${this._renderPreview()}
            <h2>History</h2>
            ${this._renderHistory()}
          </aside>
        </section>
      </main>
    `;
    this.shadowRoot.querySelector("textarea")?.addEventListener("input", (event) => this._onInput(event));
    this.shadowRoot.querySelectorAll("[data-action]").forEach((button) => {
      button.addEventListener("click", () => this._handleAction(button.dataset.action));
    });
    this.shadowRoot.querySelectorAll("[data-rule-id]").forEach((button) => {
      button.addEventListener("click", () => this._selectRule(button.dataset.ruleId));
    });
    this.shadowRoot.querySelectorAll("[data-rollback]").forEach((button) => {
      button.addEventListener("click", () => this._rollback(button.dataset.rollback));
    });
  }

  _handleAction(action) {
    if (action === "reload") this._load();
    if (action === "save") this._save();
    if (action === "validate") this._validate();
    if (action === "dry-run") this._dryRun();
    if (action === "new-rule") this._newRule();
  }
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

const styles = `
  :host { display: block; color: var(--primary-text-color); background: var(--primary-background-color); }
  main { padding: 24px; box-sizing: border-box; min-height: 100vh; }
  header { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; margin-bottom: 18px; }
  h1, h2 { margin: 0; font-weight: 600; }
  h1 { font-size: 28px; }
  h2 { font-size: 16px; }
  p { color: var(--secondary-text-color); margin: 6px 0 0; }
  button { border: 0; border-radius: 10px; padding: 10px 14px; background: var(--primary-color); color: var(--text-primary-color); cursor: pointer; font: inherit; }
  button:disabled { opacity: .45; cursor: default; }
  button.secondary { background: var(--secondary-background-color); color: var(--primary-text-color); }
  button.small { padding: 6px 10px; font-size: 13px; }
  .actions { display: flex; gap: 8px; flex-wrap: wrap; }
  .grid { display: grid; grid-template-columns: minmax(220px, 280px) minmax(420px, 1fr) minmax(280px, 360px); gap: 16px; align-items: start; }
  .narrow .grid { grid-template-columns: 1fr; }
  .narrow header { flex-direction: column; }
  .card { background: var(--card-background-color); border-radius: 16px; box-shadow: var(--ha-card-box-shadow, 0 2px 8px rgba(0,0,0,.15)); padding: 16px; }
  .card-header { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 12px; }
  .rules { display: flex; flex-direction: column; gap: 8px; }
  .rule { text-align: left; background: var(--secondary-background-color); color: var(--primary-text-color); display: flex; flex-direction: column; gap: 3px; }
  .rule.selected { outline: 2px solid var(--primary-color); }
  .rule-title { font-weight: 600; }
  .rule-meta, .muted, .empty { color: var(--secondary-text-color); font-size: 13px; }
  textarea { width: 100%; min-height: 68vh; box-sizing: border-box; resize: vertical; border: 1px solid var(--divider-color); border-radius: 12px; padding: 14px; background: var(--code-editor-background-color, var(--primary-background-color)); color: var(--primary-text-color); font: 13px/1.45 ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; }
  pre { max-height: 280px; overflow: auto; background: var(--secondary-background-color); border-radius: 12px; padding: 12px; font-size: 12px; }
  .banner, .error-box { background: var(--error-color); color: white; border-radius: 12px; padding: 12px; margin-bottom: 16px; }
  .ok { background: color-mix(in srgb, var(--success-color, #43a047) 18%, transparent); border-radius: 12px; padding: 12px; }
  .history-item { display: flex; justify-content: space-between; gap: 8px; align-items: center; padding: 10px 0; border-bottom: 1px solid var(--divider-color); }
  .history-item span { display: block; color: var(--secondary-text-color); font-size: 12px; }
  .inspector { display: flex; flex-direction: column; gap: 12px; }
`;

customElements.define("intentional-panel", IntentionalPanel);
