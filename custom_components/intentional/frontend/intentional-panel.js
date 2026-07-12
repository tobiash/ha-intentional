const API_BASE = "intentional";

const EMPTY_RULE = () => ({
  id: "",
  enabled: true,
  reason: "",
  labels: "",
  group: "",
  profile: "",
  notes: "",
  authority: "automation",
  confidence: "1.0",
  conditionMode: "all",
  conditions: [{ entity: "", operator: "is", value: "on" }],
  after: "",
  holdMode: "none",
  holdAfter: "",
  holdEntity: "",
  holdOperator: "is",
  holdValue: "off",
  holdFor: "",
  intents: [{ target: "", fields: [{ name: "state", operator: "value", value: "on" }], transitionAssert: "", transitionChange: "", transitionWithdraw: "", ttl: "", linger: "", easing: "linear" }],
  effects: [],
});

class IntentionalPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._document = null;
    this._health = null;
    this._history = [];
    this._rules = [];
    this._selectedRuleId = "";
    this._selectedRuleContents = "";
    this._selectedRuleForm = null;
    this._editorMode = "visual";
    this._contents = "";
    this._dirty = false;
    this._busy = false;
    this._error = "";
    this._localErrors = [];
    this._validation = null;
    this._preview = null;
    this._simulation = null;
    this._timelineText = "[{\"states\":{}}]";
    this._validateTimer = null;
    this._validationRequest = 0;
    this._visualModeError = "";
    this._migration = { automations: [], selected: "", inspection: null, proposal: null };
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
      throw new Error(body.error || body.message || (body.errors || []).join("; ") || response.statusText);
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
      this._selectedRuleId = "";
      this._selectedRuleContents = "";
      this._selectedRuleForm = null;
      this._editorMode = "visual";
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
    clearTimeout(this._validateTimer);
    const request = ++this._validationRequest;
    this._localErrors = this._validateLocally();
    if (this._localErrors.length) {
      this._validation = { valid: false, errors: this._localErrors };
      if (!quiet) this._error = "Fix the highlighted fields before saving.";
      this._render();
      return false;
    }
    try {
      const contents = this._candidateContents();
      const validation = await this._api("POST", "validate", { contents });
      if (request !== this._validationRequest) return false;
      this._validation = validation;
      this._rules = parseDocumentRuleSummaries(contents, validation.normalized || []);
      this._error = quiet ? this._error : "";
      return true;
    } catch (err) {
      if (request !== this._validationRequest) return false;
      this._validation = { valid: false, errors: [err.message || String(err)] };
      if (!quiet) this._error = "Validation failed";
      return false;
    } finally {
      if (request === this._validationRequest) this._render();
    }
  }

  _queueValidate() {
    clearTimeout(this._validateTimer);
    this._validateTimer = setTimeout(() => this._validate({ quiet: true }), 700);
  }

  async _dryRun() {
    if (!(await this._validate())) return;
    this._busy = true;
    this._preview = null;
    this._render();
    try {
      this._preview = await this._api("POST", "dry-run", { contents: this._candidateContents() });
      this._error = "";
    } catch (err) {
      this._error = err.message || String(err);
    } finally {
      this._busy = false;
      this._render();
    }
  }

  async _simulate() {
    if (!(await this._validate())) return;
    let timeline;
    try {
      timeline = JSON.parse(this._timelineText || "[]");
      if (containsNonFiniteNumber(timeline)) throw new Error("non-finite numbers are not supported");
    } catch (err) {
      this._error = `Timeline JSON is invalid: ${err.message || err}`;
      this._render();
      return;
    }
    this._busy = true;
    this._simulation = null;
    this._render();
    try {
      this._simulation = await this._api("POST", "simulate", { contents: this._candidateContents(), timeline });
      this._error = "";
    } catch (err) {
      this._error = err.message || String(err);
    } finally {
      this._busy = false;
      this._render();
    }
  }

  async _save() {
    if (!(await this._validate())) return;
    this._busy = true;
    this._render();
    try {
      const saved = await this._api("PUT", "rules/document", {
        contents: this._candidateContents(),
        expected_generation: this._document?.generation,
      });
      this._document = saved;
      this._contents = saved.contents || this._candidateContents();
      this._dirty = false;
      this._error = "";
      await this._loadHistory();
      await this._validate({ quiet: true });
      if (this._selectedRuleId) this._selectRule(this._selectedRuleId, { render: false, keepDirty: false });
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

  async _discoverMigrations() {
    this._busy = true;
    this._render();
    try {
      const result = await this._api("GET", "migrate-ha");
      this._migration = { automations: result.automations || [], selected: "", inspection: null, proposal: null };
      this._error = "";
    } catch (err) { this._error = err.message || String(err); }
    finally { this._busy = false; this._render(); }
  }

  async _inspectMigration(entityId) {
    const request = (this._migrationInspectionRequest || 0) + 1;
    this._migrationInspectionRequest = request;
    this._migration.selected = entityId;
    this._migration.proposal = null;
    if (!entityId) { this._migration.inspection = null; this._render(); return; }
    try {
      const inspection = await this._api("GET", `migrate-ha/${encodeURIComponent(entityId)}`);
      if (request !== this._migrationInspectionRequest || this._migration.selected !== entityId) return;
      this._migration.inspection = inspection; this._error = "";
    }
    catch (err) { if (request === this._migrationInspectionRequest) this._error = err.message || String(err); }
    this._render();
  }

  async _proposeMigration() {
    if (!this._migration.selected) return;
    const entityId = this._migration.selected;
    const request = (this._migrationProposalRequest || 0) + 1;
    this._migrationProposalRequest = request;
    try {
      const proposal = await this._api("POST", "migrate-ha/propose", { entity_id: entityId });
      if (request !== this._migrationProposalRequest || this._migration.selected !== entityId) return;
      this._migration.proposal = proposal; this._error = "";
    }
    catch (err) { if (request === this._migrationProposalRequest) this._error = err.message || String(err); }
    this._render();
  }

  _addMigrationProposal() {
    const proposal = this._migration.proposal;
    if (!proposal?.supported || !proposal?.merged_validation?.valid) return;
    this._contents = proposal.merged_candidate;
    this._editorMode = "document";
    this._selectedRuleId = "";
    this._dirty = true;
    this._validation = proposal.merged_validation;
    this._render();
  }

  async _rollback(generation) {
    if (!confirm(`Restore generation ${generation.slice(0, 12)}? Unsaved editor changes will be discarded.`)) return;
    this._busy = true;
    this._render();
    try {
      await this._api("POST", "rules/rollback", { generation, expected_generation: this._document?.generation });
      await this._load();
    } catch (err) {
      this._error = err.message || String(err);
    } finally {
      this._busy = false;
      this._render();
    }
  }

  _newRule() {
    if (this._dirty && !confirm("Discard unsaved editor changes?")) return;
    const nextNumber = this._uniqueRules().length + 1;
    this._selectedRuleId = "__new__";
    this._selectedRuleForm = EMPTY_RULE();
    this._selectedRuleForm.id = `new-rule-${nextNumber}`;
    this._selectedRuleForm.enabled = false;
    this._selectedRuleForm.reason = "Describe why this rule exists";
    this._selectedRuleContents = stringifyRule(this._selectedRuleForm);
    this._editorMode = "visual";
    this._visualModeError = "";
    this._dirty = true;
    this._validation = null;
    this._preview = null;
    this._simulation = null;
    this._render();
  }

  _selectRule(ruleId, { render = true, keepDirty = false } = {}) {
    if (!keepDirty && this._dirty && !confirm("Discard unsaved editor changes?")) return;
    const block = extractRuleBlock(this._contents, ruleId);
    const apiRule = this._rules.find((rule) => rule.id === ruleId) || null;
    this._selectedRuleId = ruleId;
    this._selectedRuleContents = block;
    this._selectedRuleForm = parseRuleForm(block, apiRule);
    this._visualModeError = visualModeError(block);
    this._editorMode = this._visualModeError ? "yaml" : "visual";
    this._dirty = false;
    this._validation = null;
    this._preview = null;
    this._simulation = null;
    if (render) this._render();
  }

  _showDocument() {
    if (this._dirty && !confirm("Discard unsaved editor changes?")) return;
    this._editorMode = "document";
    this._selectedRuleId = "";
    this._selectedRuleContents = "";
    this._selectedRuleForm = null;
    this._dirty = false;
    this._render();
  }

  _showYamlRule() {
    if (this._editorMode === "visual" && this._selectedRuleForm) {
      this._selectedRuleContents = stringifyRule(this._selectedRuleForm);
    }
    this._editorMode = "yaml";
    this._render();
  }

  _showVisualRule() {
    const error = visualModeError(this._selectedRuleContents);
    if (error) {
      this._visualModeError = error;
      this._error = error;
      this._render();
      return;
    }
    if (this._editorMode === "yaml") {
      this._selectedRuleForm = parseRuleForm(this._selectedRuleContents, this._rules.find((rule) => rule.id === this._selectedRuleId));
    }
    this._visualModeError = "";
    this._editorMode = "visual";
    this._render();
  }

  _candidateContents() {
    if (this._editorMode === "document") return this._contents;
    if (!this._selectedRuleId) return this._contents;
    const ruleContents = this._editorMode === "visual" ? stringifyRule(this._selectedRuleForm) : this._selectedRuleContents.trimEnd() + "\n";
    if (this._selectedRuleId === "__new__") return appendRuleBlock(this._contents, ruleContents);
    return replaceRuleBlock(this._contents, this._selectedRuleId, ruleContents);
  }

  _validateLocally() {
    if (this._editorMode !== "visual" || !this._selectedRuleForm) return [];
    const errors = [];
    const form = this._selectedRuleForm;
    if (!form.id.trim()) errors.push("Rule ID is required.");
    if (!/^[a-zA-Z0-9_.:-]+$/.test(form.id.trim())) errors.push("Rule ID may only contain letters, numbers, dot, underscore, colon, and dash.");
    if (this._selectedRuleId === "__new__" && this._uniqueRules().some((rule) => rule.id === form.id.trim())) errors.push("Rule ID already exists.");
    if (!form.conditions.some((condition) => condition.entity.trim())) errors.push("Add at least one When condition.");
    if (!form.intents.some((intent) => intent.target.trim()) && !form.effects.some((effect) => effect.service.trim())) errors.push("Add at least one target intent or effect.");
    for (const intent of form.intents.filter((item) => item.target.trim())) {
      if (!intent.fields.some((field) => field.name.trim())) errors.push(`Target ${intent.target} has no desired state fields.`);
    }
    for (const effect of form.effects.filter((item) => item.service.trim())) {
      if (!/^\w+\.\w+$/.test(effect.service.trim())) errors.push(`Effect service ${effect.service} must look like domain.service.`);
      for (const key of ["target", "data"]) {
        const value = effect[key].trim();
        if (value) {
          try {
            const parsed = JSON.parse(value);
            if (containsNonFiniteNumber(parsed)) throw new Error("non-finite numbers are not supported");
          } catch (err) { errors.push(`Effect ${key} JSON is invalid: ${err.message || err}`); }
        }
      }
    }
    return errors;
  }

  _uniqueRules() {
    const rules = new Map();
    for (const rule of this._rules) {
      const existing = rules.get(rule.id);
      if (existing) existing.count += 1;
      else rules.set(rule.id, { ...rule, count: 1 });
    }
    return [...rules.values()];
  }

  _entityOptions(domain = "") {
    const states = this._hass?.states || {};
    return Object.keys(states)
      .filter((entityId) => !domain || entityId.startsWith(`${domain}.`))
      .sort()
      .slice(0, 800)
      .map((entityId) => `<option value="${escapeHtml(entityId)}"></option>`)
      .join("");
  }

  _ruleStatus(rule) {
    if (rule.enabled === false) return "Disabled";
    if (rule.count > 1) return `${rule.count} targets`;
    if (rule.target) return rule.target;
    return "Multi-target or effect";
  }

  _renderRules() {
    const rules = this._uniqueRules();
    if (!rules.length) return `<div class="empty">No rules yet. Create one visually or paste YAML in Advanced.</div>`;
    return rules.map((rule) => `
      <button class="rule ${this._selectedRuleId === rule.id ? "selected" : ""}" data-rule-id="${escapeHtml(rule.id)}">
        <span class="rule-title">${escapeHtml(rule.id)}</span>
        <span class="rule-meta">${escapeHtml(this._ruleStatus(rule))}</span>
        ${rule.reason ? `<span class="rule-reason">${escapeHtml(rule.reason)}</span>` : ""}
      </button>
    `).join("");
  }

  _renderEditor() {
    if (this._editorMode === "document") return this._renderDocumentEditor();
    if (this._editorMode === "yaml") return this._renderYamlRuleEditor();
    if (!this._selectedRuleForm) return `<section class="card editor empty-state"><h2>Select or Create a Rule</h2><p>Use the list on the left to edit an existing rule, or create a new one. The visual editor writes the same storage-backed YAML used by the API.</p></section>`;
    return this._renderVisualEditor();
  }

  _renderVisualEditor() {
    const form = this._selectedRuleForm;
    return `
      <section class="editor-stack" data-form-root>
        <section class="card hero-card">
          <div class="hero-row">
            <div>
              <span class="eyebrow">Rule editor</span>
              <h2>${escapeHtml(form.id || "New rule")}</h2>
              <p>Build a durable <code>while → intent</code> rule visually. Every change can be validated and simulated before it reaches Home Assistant storage.</p>
            </div>
            <div class="mode-switch">
              <button class="secondary small" data-action="show-yaml-rule">Edit YAML</button>
              <button class="secondary small" data-action="show-document">Document YAML</button>
            </div>
          </div>
        </section>
        <section class="card form-card">
          <h3>Details</h3>
          <div class="form-grid">
            ${inputField("ID", "id", form.id, "new-rule", "Rule ID used for status, history, and switches")}
            ${selectField("Enabled", "enabled", String(form.enabled), [["true", "Enabled"], ["false", "Disabled"]])}
            ${inputField("Reason", "reason", form.reason, "Turn on the sofa lamp when the room is occupied", "Shown in diagnostics and explanations")}
            ${inputField("Labels", "labels", form.labels, "living-room, lighting", "Comma-separated")}
            ${inputField("Group", "group", form.group, "living-room-lighting")}
            ${inputField("Profile", "profile", form.profile, "settled")}
            ${selectField("Authority", "authority", form.authority, [["sensor", "Sensor"], ["automation", "Automation"], ["user", "User"]])}
            ${inputField("Confidence", "confidence", form.confidence, "0.8")}
          </div>
          <label class="field wide"><span>Notes</span><textarea class="small-textarea" data-field="notes" placeholder="Private authoring notes">${escapeHtml(form.notes)}</textarea></label>
        </section>
        <section class="card form-card">
          <div class="section-title"><div><h3>When</h3><p>Conditions that make the intent active.</p></div>${selectInline("conditionMode", form.conditionMode, [["all", "All conditions"], ["any", "Any condition"], ["none", "None match"], ["not", "Not first condition"]])}</div>
          <div class="rows">${form.conditions.map((condition, index) => this._renderCondition(condition, index, "condition")).join("")}</div>
          <button class="secondary" data-action="add-condition">Add Condition</button>
          <div class="form-grid single">${inputField("Activate after", "after", form.after, "5m", "Optional dwell before first activation")}</div>
        </section>
        <section class="card form-card">
          <div class="section-title"><div><h3>Hold</h3><p>Prevent flicker by retaining an active intent after the original condition stops.</p></div>${selectInline("holdMode", form.holdMode, [["none", "No hold"], ["after", "Keep for duration"], ["until_for", "Until condition stays true"], ["while_after", "While condition, then duration"]])}</div>
          ${this._renderHold(form)}
        </section>
        <section class="card form-card">
          <div class="section-title"><div><h3>Intent</h3><p>Desired target state. Add multiple targets for one authored rule.</p></div><button class="secondary" data-action="add-intent">Add Target</button></div>
          ${form.intents.map((intent, index) => this._renderIntent(intent, index)).join("")}
        </section>
        <section class="card form-card">
          <div class="section-title"><div><h3>Effects</h3><p>Optional explicit side effects. Use sparingly; durable state belongs above.</p></div><button class="secondary" data-action="add-effect">Add Effect</button></div>
          ${form.effects.length ? form.effects.map((effect, index) => this._renderEffect(effect, index)).join("") : `<div class="empty inline-empty">No side effects configured.</div>`}
        </section>
      </section>
    `;
  }

  _renderCondition(condition, index, prefix) {
    return `
      <div class="row condition-row" data-${prefix}-index="${index}">
        <label><span>Entity</span><input data-${prefix}-field="entity" value="${escapeHtml(condition.entity)}" list="entity-list" placeholder="binary_sensor.room_presence"></label>
        <label><span>Operator</span>${operatorSelect(`data-${prefix}-field="operator"`, condition.operator)}</label>
        <label><span>Value</span><input data-${prefix}-field="value" value="${escapeHtml(condition.value)}" placeholder="on"></label>
        <button class="icon secondary" data-action="remove-${prefix}" data-index="${index}" title="Remove">×</button>
      </div>
    `;
  }

  _renderHold(form) {
    if (form.holdMode === "none") return `<div class="empty inline-empty">Rule withdraws as soon as When stops matching.</div>`;
    if (form.holdMode === "after") return `<div class="form-grid single">${inputField("Keep active for", "holdAfter", form.holdAfter, "5m")}</div>`;
    const condition = { entity: form.holdEntity, operator: form.holdOperator, value: form.holdValue };
    const extra = form.holdMode === "until_for" ? inputField("Must stay true for", "holdFor", form.holdFor, "15m") : inputField("Then keep active for", "holdAfter", form.holdAfter, "5m");
    return `${this._renderCondition(condition, 0, "hold")}<div class="form-grid single">${extra}</div>`;
  }

  _renderIntent(intent, index) {
    return `
      <div class="subcard" data-intent-index="${index}">
        <div class="subcard-head">
          <label class="target-field"><span>Target</span><input data-intent-field="target" value="${escapeHtml(intent.target)}" list="entity-list" placeholder="light.office"></label>
          <button class="icon secondary" data-action="remove-intent" data-index="${index}" title="Remove target">×</button>
        </div>
        <div class="rows compact">${intent.fields.map((field, fieldIndex) => this._renderIntentField(field, index, fieldIndex)).join("")}</div>
        <button class="secondary small" data-action="add-intent-field" data-index="${index}">Add Field</button>
        <details class="advanced"><summary>Application options</summary>
          <div class="form-grid">
            ${intentInputField("Assert transition", "transitionAssert", intent.transitionAssert, "2s", "Used when asserting an existing target")}
            ${intentInputField("Change transition", "transitionChange", intent.transitionChange, "5s", "Used for value changes")}
            ${intentInputField("Withdraw transition", "transitionWithdraw", intent.transitionWithdraw, "7s", "Used when turning off")}
            ${intentInputField("TTL", "ttl", intent.ttl, "30s")}
            ${intentInputField("Easing", "easing", intent.easing, "linear")}
          </div>
        </details>
      </div>
    `;
  }

  _renderIntentField(field, intentIndex, fieldIndex) {
    return `
      <div class="row field-row" data-intent-index="${intentIndex}" data-field-index="${fieldIndex}">
        <label><span>Field</span><input data-intent-field-row="name" value="${escapeHtml(field.name)}" placeholder="brightness_pct"></label>
        <label><span>Mode</span>${fieldOperatorSelect(field.operator)}</label>
        <label><span>Value</span><input data-intent-field-row="value" value="${escapeHtml(field.value)}" placeholder="70"></label>
        <button class="icon secondary" data-action="remove-intent-field" data-intent-index="${intentIndex}" data-field-index="${fieldIndex}" title="Remove field">×</button>
      </div>
    `;
  }

  _renderEffect(effect, index) {
    return `
      <div class="subcard" data-effect-index="${index}">
        <div class="subcard-head">
          <label class="target-field"><span>Service</span><input data-effect-field="service" value="${escapeHtml(effect.service)}" placeholder="notify.mobile_app_phone"></label>
          <button class="icon secondary" data-action="remove-effect" data-index="${index}" title="Remove effect">×</button>
        </div>
        <div class="form-grid">
          <label class="field"><span>Target JSON</span><textarea class="small-textarea mono" data-effect-field="target" placeholder='{"entity_id":"light.office"}'>${escapeHtml(effect.target)}</textarea></label>
          <label class="field"><span>Data JSON</span><textarea class="small-textarea mono" data-effect-field="data" placeholder='{"message":"Office occupied"}'>${escapeHtml(effect.data)}</textarea></label>
        </div>
      </div>
    `;
  }

  _renderYamlRuleEditor() {
    return `
      <section class="card editor">
        <div class="card-header"><div><h2>Rule YAML</h2><p>Advanced escape hatch for fields the visual editor does not expose yet.</p></div><div class="actions"><button class="secondary small" data-action="show-visual-rule">Visual Editor</button><button class="secondary small" data-action="show-document">Document YAML</button></div></div>
        ${this._visualModeError ? `<div class="warning-box">${escapeHtml(this._visualModeError)} Edit this rule as YAML to avoid losing unsupported fields.</div>` : ""}
        <textarea class="yaml-editor" spellcheck="false">${escapeHtml(this._selectedRuleContents)}</textarea>
      </section>
    `;
  }

  _renderDocumentEditor() {
    return `
      <section class="card editor">
        <div class="card-header"><div><h2>Document YAML</h2><p>Bulk edit the complete storage document. Validate before saving.</p></div><button class="secondary small" data-action="new-rule">Back to Visual</button></div>
        <textarea class="yaml-editor" spellcheck="false">${escapeHtml(this._contents)}</textarea>
      </section>
    `;
  }

  _renderValidation() {
    if (!this._validation) return `<div class="muted">Validation runs automatically after edits.</div>`;
    if (!this._validation.valid) return `<div class="error-box">${(this._validation.errors || []).map(escapeHtml).join("<br>")}</div>`;
    const warnings = this._validation.warnings || [];
    return `
      <div class="ok">Valid. ${this._validation.rule_count} rule(s).</div>
      ${warnings.length ? `<div class="warning-box"><strong>${warnings.length} warning(s)</strong>${warnings.map((warning) => `<p>${escapeHtml(warning.rule_id || "rule")}: ${escapeHtml(warning.message || warning.code)}</p>`).join("")}</div>` : ""}
    `;
  }

  _renderPreview() {
    if (!this._preview) return `<div class="muted">Dry-run evaluates desired targets without applying services.</div>`;
    return `<pre>${escapeHtml(JSON.stringify(this._preview, null, 2))}</pre>`;
  }

  _renderSimulation() {
    return `
      <label class="field"><span>Simulation timeline JSON</span><textarea class="small-textarea mono" data-timeline>${escapeHtml(this._timelineText)}</textarea></label>
      <button class="secondary small" data-action="simulate">Run Simulation</button>
      ${this._simulation ? `<pre>${escapeHtml(JSON.stringify(this._simulation, null, 2))}</pre>` : `<div class="muted">Use simulation for after/hold timing before installing on the live instance.</div>`}
    `;
  }

  _renderHistory() {
    if (!this._history.length) return `<div class="empty">No history yet.</div>`;
    return this._history.slice(0, 8).map((item) => `
      <div class="history-item"><div><strong>${escapeHtml((item.generation || "").slice(0, 12))}</strong><span>${escapeHtml(item.reason || "unknown")}</span></div><button class="secondary small" data-rollback="${escapeHtml(item.generation)}">Rollback</button></div>
    `).join("");
  }

  _renderMigration() {
    const migration = this._migration;
    const proposal = migration.proposal;
    return `<section class="card migration">
      <div class="card-header"><div><h2>Migrate HA automation</h2><p>Inspect a strict supported subset and add proposed Rules to this editor.</p></div><button class="secondary small" data-action="migration-discover">Discover</button></div>
      <div class="warning-box"><strong>Source automation stays unchanged.</strong><p>Intentional never disables, edits, or calls the source automation. Review overlap before saving.</p></div>
      ${migration.automations.length ? `<label class="field"><span>Loaded automation</span><select data-migration-select><option value="">Select…</option>${migration.automations.map((item) => `<option value="${escapeHtml(item.entity_id)}" ${migration.selected === item.entity_id ? "selected" : ""}>${escapeHtml(item.alias || item.entity_id)}</option>`).join("")}</select></label>` : `<div class="muted">Discover loaded automations to begin.</div>`}
      ${migration.inspection ? `<div class="muted">${migration.inspection.supported ? "Supported candidate" : "Unsupported"}. ${(migration.inspection.diagnostics || []).map((item) => escapeHtml(item.message)).join(" ")}</div><button class="secondary small" data-action="migration-propose" ${migration.inspection.supported ? "" : "disabled"}>Propose</button>` : ""}
      ${proposal ? `<pre>${escapeHtml(proposal.yaml || JSON.stringify(proposal.diagnostics, null, 2))}</pre><button data-action="migration-add" ${proposal.supported && proposal.merged_validation?.valid ? "" : "disabled"}>Add to editor</button>` : ""}
    </section>`;
  }

  _render() {
    if (!this.shadowRoot) return;
    const version = this._health?.version ? `v${this._health.version}` : "";
    const generation = this._document?.generation ? this._document.generation.slice(0, 12) : "not loaded";
    this.shadowRoot.innerHTML = `
      <style>${styles}</style>
      <main class="${this._narrow ? "narrow" : ""}">
        <header>
          <div><h1>Intentional</h1><p>Visual rule editor ${escapeHtml(version)} · generation ${escapeHtml(generation)}</p></div>
          <div class="actions"><button class="secondary" data-action="reload" ${this._busy ? "disabled" : ""}>Reload</button><button class="secondary" data-action="validate" ${this._busy ? "disabled" : ""}>Validate</button><button class="secondary" data-action="dry-run" ${this._busy ? "disabled" : ""}>Dry-run</button><button data-action="save" ${this._busy || !this._dirty ? "disabled" : ""}>Save</button></div>
        </header>
        ${this._error ? `<div class="banner">${escapeHtml(this._error)}</div>` : ""}
        <datalist id="entity-list">${this._entityOptions()}</datalist>
        <section class="grid">
          <aside class="card rules"><div class="card-header"><h2>Rules</h2><button class="secondary small" data-action="new-rule">New</button></div>${this._renderRules()}</aside>
          ${this._renderEditor()}
          <aside class="inspector-stack"><aside class="card inspector"><h2>Validation</h2>${this._renderValidation()}<h2>Preview</h2>${this._renderPreview()}<h2>Simulation</h2>${this._renderSimulation()}<h2>History</h2>${this._renderHistory()}</aside>${this._renderMigration()}</aside>
        </section>
      </main>
    `;
    this._bindEvents();
  }

  _bindEvents() {
    this.shadowRoot.querySelector("textarea.yaml-editor")?.addEventListener("input", (event) => {
      if (this._editorMode === "document") this._contents = event.target.value;
      else this._selectedRuleContents = event.target.value;
      this._markDirty();
    });
    this.shadowRoot.querySelector("[data-timeline]")?.addEventListener("input", (event) => { this._timelineText = event.target.value; });
    this.shadowRoot.querySelector("[data-form-root]")?.addEventListener("input", (event) => this._onFormInput(event));
    this.shadowRoot.querySelector("[data-form-root]")?.addEventListener("change", (event) => this._onFormInput(event));
    this.shadowRoot.querySelectorAll("[data-action]").forEach((button) => button.addEventListener("click", () => this._handleAction(button)));
    this.shadowRoot.querySelectorAll("[data-rule-id]").forEach((button) => button.addEventListener("click", () => this._selectRule(button.dataset.ruleId)));
    this.shadowRoot.querySelectorAll("[data-rollback]").forEach((button) => button.addEventListener("click", () => this._rollback(button.dataset.rollback)));
    this.shadowRoot.querySelector("[data-migration-select]")?.addEventListener("change", (event) => this._inspectMigration(event.target.value));
  }

  _onFormInput(event) {
    const form = this._selectedRuleForm;
    if (!form) return;
    const target = event.target;
    if (target.dataset.field) {
      form[target.dataset.field] = target.dataset.field === "enabled" ? target.value === "true" : target.value;
    }
    const conditionIndex = nearestIndex(target, "conditionIndex");
    if (target.dataset.conditionField && conditionIndex >= 0) form.conditions[conditionIndex][target.dataset.conditionField] = target.value;
    const holdIndex = nearestIndex(target, "holdIndex");
    if (target.dataset.holdField && holdIndex >= 0) {
      const map = { entity: "holdEntity", operator: "holdOperator", value: "holdValue" };
      form[map[target.dataset.holdField]] = target.value;
    }
    const intentIndex = nearestIndex(target, "intentIndex");
    if (target.dataset.intentField && intentIndex >= 0) form.intents[intentIndex][target.dataset.intentField] = target.value;
    const fieldIndex = nearestIndex(target, "fieldIndex");
    if (target.dataset.intentFieldRow && intentIndex >= 0 && fieldIndex >= 0) form.intents[intentIndex].fields[fieldIndex][target.dataset.intentFieldRow] = target.value;
    const effectIndex = nearestIndex(target, "effectIndex");
    if (target.dataset.effectField && effectIndex >= 0) form.effects[effectIndex][target.dataset.effectField] = target.value;
    this._markDirty();
  }

  _markDirty() {
    this._dirty = true;
    this._validation = null;
    this._preview = null;
    this._simulation = null;
    this._queueValidate();
    const save = this.shadowRoot.querySelector('[data-action="save"]');
    if (save) save.disabled = this._busy || !this._dirty;
  }

  _handleAction(button) {
    const action = button.dataset.action;
    if (action === "reload") this._load();
    if (action === "save") this._save();
    if (action === "validate") this._validate();
    if (action === "dry-run") this._dryRun();
    if (action === "simulate") this._simulate();
    if (action === "migration-discover") this._discoverMigrations();
    if (action === "migration-propose") this._proposeMigration();
    if (action === "migration-add") this._addMigrationProposal();
    if (action === "new-rule") this._newRule();
    if (action === "show-document") this._showDocument();
    if (action === "show-yaml-rule") this._showYamlRule();
    if (action === "show-visual-rule") this._showVisualRule();
    if (action === "add-condition") this._mutateForm((form) => form.conditions.push({ entity: "", operator: "is", value: "on" }));
    if (action === "remove-condition") this._mutateForm((form) => form.conditions.splice(Number(button.dataset.index), 1));
    if (action === "add-intent") this._mutateForm((form) => form.intents.push({ target: "", fields: [{ name: "state", operator: "value", value: "on" }], transitionAssert: "", transitionChange: "", transitionWithdraw: "", ttl: "", linger: "", easing: "linear" }));
    if (action === "remove-intent") this._mutateForm((form) => form.intents.splice(Number(button.dataset.index), 1));
    if (action === "add-intent-field") this._mutateForm((form) => form.intents[Number(button.dataset.index)].fields.push({ name: "", operator: "value", value: "" }));
    if (action === "remove-intent-field") this._mutateForm((form) => form.intents[Number(button.dataset.intentIndex)].fields.splice(Number(button.dataset.fieldIndex), 1));
    if (action === "add-effect") this._mutateForm((form) => form.effects.push({ service: "", target: "", data: "" }));
    if (action === "remove-effect") this._mutateForm((form) => form.effects.splice(Number(button.dataset.index), 1));
  }

  _mutateForm(mutator) {
    if (!this._selectedRuleForm) return;
    mutator(this._selectedRuleForm);
    this._markDirty();
    this._render();
  }
}

function parseRuleForm(block, apiRule) {
  const form = EMPTY_RULE();
  form.id = extractScalar(block, "id") || apiRule?.id || "";
  const enabled = extractScalar(block, "enabled");
  form.enabled = enabled === "" ? apiRule?.enabled !== false : !["false", "False", "off", "0"].includes(enabled);
  form.reason = extractScalar(block, "reason") || "";
  form.labels = extractInlineList(block, "labels").join(", ");
  form.group = extractScalar(block, "group") || "";
  form.profile = extractScalar(block, "profile") || "";
  form.notes = extractScalar(block, "notes") || "";
  form.authority = extractScalar(block, "authority") || "automation";
  form.confidence = extractScalar(block, "confidence") || "1.0";
  form.after = extractScalar(block, "after") || "";
  let parsedConditions = parseConditions(sectionLines(block, "while"));
  if (!parsedConditions.conditions.length) parsedConditions = parseConditions(sectionLines(block, "observe"));
  form.conditionMode = parsedConditions.mode;
  form.conditions = parsedConditions.conditions;
  if (!form.conditions.length && apiRule?.when) form.conditions = [{ entity: apiRule.when.split(/\s+/)[0] || "", operator: "is", value: "on" }];
  const holdLines = sectionLines(block, "hold");
  if (holdLines.some((line) => /^\s{4}after:/.test(line))) { form.holdMode = "after"; form.holdAfter = extractIndentedScalar(holdLines, "after"); }
  if (holdLines.some((line) => /^\s{4}until:/.test(line))) { form.holdMode = "until_for"; form.holdFor = extractIndentedScalar(holdLines, "for"); const cond = parseConditionSection(nestedSectionLines(holdLines, "until"))[0]; if (cond) Object.assign(form, { holdEntity: cond.entity, holdOperator: cond.operator, holdValue: cond.value }); }
  if (holdLines.some((line) => /^\s{4}while:/.test(line))) { form.holdMode = "while_after"; form.holdAfter = extractIndentedScalar(holdLines, "after"); const cond = parseConditionSection(nestedSectionLines(holdLines, "while"))[0]; if (cond) Object.assign(form, { holdEntity: cond.entity, holdOperator: cond.operator, holdValue: cond.value }); }
  form.intents = parseIntentSection(sectionLines(block, "intent"), apiRule);
  if (!form.intents.length) form.intents = [{ target: apiRule?.target || "", fields: objectToFields(apiRule?.set || {}), transitionAssert: "", transitionChange: "", transitionWithdraw: "", ttl: "", linger: "", easing: "linear" }];
  form.effects = parseEffects(block, apiRule);
  return form;
}

function parseDocumentRuleSummaries(contents, normalizedRules = []) {
  const normalizedById = new Map((normalizedRules || []).map((rule) => [rule.id, rule]));
  return extractRuleBlocks(contents).map(({ id, block }) => {
    const normalized = normalizedById.get(id) || {};
    const intents = parseIntentSection(sectionLines(block, "intent"), null);
    const enabled = extractScalar(block, "enabled");
    return {
      ...normalized,
      id,
      enabled: enabled === "" ? normalized.enabled !== false : !["false", "False", "off", "0"].includes(enabled),
      reason: extractScalar(block, "reason") || normalized.reason || "",
      target: intents.length === 1 ? intents[0].target : extractNestedScalar(block, "emit", "target") || normalized.target || "",
      count: Math.max(1, intents.length || (normalized.target ? 1 : 0)),
    };
  });
}

function stringifyRule(form) {
  const lines = [`- id: ${yamlScalar(form.id.trim())}`];
  if (form.enabled === false) lines.push("  enabled: false");
  if (form.labels.trim()) lines.push(`  labels: [${form.labels.split(",").map((item) => yamlScalar(item.trim())).filter(Boolean).join(", ")}]`);
  for (const key of ["group", "profile", "authority", "confidence", "reason", "notes"]) {
    const value = String(form[key] ?? "").trim();
    if (value && !(key === "authority" && value === "automation") && !(key === "confidence" && value === "1.0")) lines.push(`  ${key}: ${yamlScalar(value)}`);
  }
  writeConditions(lines, "while", form.conditionMode, form.conditions, 2);
  if (form.after.trim()) lines.push(`  after: ${yamlScalar(form.after.trim())}`);
  writeHold(lines, form);
  writeIntents(lines, form.intents);
  writeEffects(lines, form.effects);
  return `${lines.join("\n")}\n`;
}

function writeConditions(lines, key, mode, conditions, indent) {
  const usable = conditions.filter((condition) => condition.entity.trim());
  if (!usable.length) return;
  const pad = " ".repeat(indent);
  lines.push(`${pad}${key}:`);
  if (mode === "all") {
    for (const condition of usable) writeCondition(lines, condition, indent + 2);
    return;
  }
  lines.push(`${pad}  ${mode}:`);
  for (const condition of usable.slice(0, mode === "not" ? 1 : usable.length)) {
    const child = [];
    writeCondition(child, condition, 0);
    lines.push(`${pad}    - ${child[0].trim()}`);
    for (const extra of child.slice(1)) lines.push(`${pad}      ${extra.trim()}`);
  }
}

function writeCondition(lines, condition, indent) {
  const pad = " ".repeat(indent);
  const entity = condition.entity.trim();
  const op = condition.operator || "is";
  const value = yamlScalar(condition.value);
  if (op === "is") lines.push(`${pad}${entity}: ${value}`);
  else lines.push(`${pad}${entity}:`, `${pad}  ${op}: ${value}`);
}

function writeHold(lines, form) {
  if (form.holdMode === "none") return;
  lines.push("  hold:");
  if (form.holdMode === "after") { if (form.holdAfter.trim()) lines.push(`    after: ${yamlScalar(form.holdAfter.trim())}`); return; }
  const key = form.holdMode === "until_for" ? "until" : "while";
  lines.push(`    ${key}:`);
  writeCondition(lines, { entity: form.holdEntity, operator: form.holdOperator, value: form.holdValue }, 6);
  if (form.holdMode === "until_for" && form.holdFor.trim()) lines.push(`      for: ${yamlScalar(form.holdFor.trim())}`);
  if (form.holdMode === "while_after" && form.holdAfter.trim()) lines.push(`    after: ${yamlScalar(form.holdAfter.trim())}`);
}

function writeIntents(lines, intents) {
  const usable = intents.filter((intent) => intent.target.trim());
  if (!usable.length) return;
  lines.push("  intent:");
  for (const intent of usable) {
    lines.push(`    ${intent.target.trim()}:`);
    for (const field of intent.fields.filter((item) => item.name.trim())) {
      const op = field.operator || "value";
      if (op === "value") lines.push(`      ${field.name.trim()}: ${yamlScalar(field.value)}`);
      else lines.push(`      ${field.name.trim()}:`, `        ${op}: ${yamlScalar(field.value)}`);
    }
    if (intent.ttl.trim()) lines.push(`      ttl: ${yamlScalar(intent.ttl.trim())}`);
    if (intent.easing.trim() && intent.easing.trim() !== "linear") lines.push(`      easing: ${yamlScalar(intent.easing.trim())}`);
    if (intent.transitionAssert.trim() || intent.transitionChange.trim() || intent.transitionWithdraw.trim()) {
      lines.push("      apply:", "        transition:");
      if (intent.transitionAssert.trim()) lines.push(`          assert: ${yamlScalar(intent.transitionAssert.trim())}`);
      if (intent.transitionChange.trim()) lines.push(`          change: ${yamlScalar(intent.transitionChange.trim())}`);
      if (intent.transitionWithdraw.trim()) lines.push(`          withdraw: ${yamlScalar(intent.transitionWithdraw.trim())}`);
    }
  }
}

function writeEffects(lines, effects) {
  const usable = effects.filter((effect) => effect.service.trim());
  if (!usable.length) return;
  lines.push("  effect:");
  for (const effect of usable) {
    const prefix = usable.length > 1 ? "    -" : "   ";
    lines.push(`${prefix} service: ${yamlScalar(effect.service.trim())}`);
    for (const key of ["target", "data"]) {
      if (!effect[key].trim()) continue;
      const value = JSON.parse(effect[key]);
      if (!Array.isArray(value) && value !== null && typeof value === "object" && !Object.keys(value).length) continue;
      lines.push(`${usable.length > 1 ? "      " : "    "}${key}:`);
      writeYamlValue(lines, value, usable.length > 1 ? 8 : 6);
    }
  }
}

function containsNonFiniteNumber(value) {
  if (typeof value === "number") return !Number.isFinite(value);
  if (Array.isArray(value)) return value.some(containsNonFiniteNumber);
  if (value !== null && typeof value === "object") return Object.values(value).some(containsNonFiniteNumber);
  return false;
}

function writeYamlValue(lines, value, indent) {
  const pad = " ".repeat(indent);
  if (Array.isArray(value)) {
    for (const item of value) {
      if (item !== null && typeof item === "object") {
        lines.push(`${pad}-`);
        writeYamlValue(lines, item, indent + 2);
      } else lines.push(`${pad}- ${yamlScalar(item)}`);
    }
    return;
  }
  for (const [key, nested] of Object.entries(value || {})) {
    if (nested !== null && typeof nested === "object") {
      lines.push(`${pad}${key}:`);
      writeYamlValue(lines, nested, indent + 2);
    } else lines.push(`${pad}${key}: ${yamlScalar(nested)}`);
  }
}

function parseConditions(lines) {
  const compound = lines.find((line) => /^\s{4}(all|any|none|not):\s*$/.test(line));
  const mode = compound ? compound.trim().slice(0, -1) : "all";
  return { mode, conditions: parseConditionSection(lines, Boolean(compound)) };
}

function parseConditionSection(lines, compound = false) {
  const conditions = [];
  for (let index = 0; index < lines.length; index += 1) {
    const line = compound ? lines[index].replace(/^\s{6}-\s*/, "    ").replace(/^\s{10}/, "      ") : lines[index];
    const nextLine = compound ? (lines[index + 1] || "").replace(/^\s{10}/, "      ") : lines[index + 1];
    const scalar = line.match(/^\s{4}([\w.-]+\.[\w.-]+):\s*(.+?)\s*$/);
    if (scalar) { conditions.push({ entity: scalar[1], operator: "is", value: stripQuotes(scalar[2]) }); continue; }
    const object = line.match(/^\s{4}([\w.-]+\.[\w.-]+):\s*$/);
    const op = nextLine?.match(/^\s{6}(\w+):\s*(.+?)\s*$/);
    if (object && op) { conditions.push({ entity: object[1], operator: op[1], value: stripQuotes(op[2]) }); index += 1; }
  }
  return conditions;
}

function parseIntentSection(lines, apiRule) {
  const intents = [];
  let current = null;
  for (let index = 0; index < lines.length; index += 1) {
    const target = lines[index].match(/^\s{4}([\w.-]+\.[\w.-]+):\s*$/);
    if (target) { current = { target: target[1], fields: [], transitionAssert: "", transitionChange: "", transitionWithdraw: "", ttl: "", linger: "", easing: "linear" }; intents.push(current); continue; }
    if (!current) continue;
    const scalar = lines[index].match(/^\s{6}([\w_]+):\s*(.+?)\s*$/);
    if (scalar && !["ttl", "linger", "easing"].includes(scalar[1])) current.fields.push({ name: scalar[1], operator: "value", value: stripQuotes(scalar[2]) });
    if (scalar && ["ttl", "easing"].includes(scalar[1])) current[scalar[1]] = stripQuotes(scalar[2]);
    const object = lines[index].match(/^\s{6}([\w_]+):\s*$/);
    const op = lines[index + 1]?.match(/^\s{8}(value|min|max|offset|multiply):\s*(.+?)\s*$/);
    if (object && op) { current.fields.push({ name: object[1], operator: op[1], value: stripQuotes(op[2]) }); index += 1; }
    const trans = lines[index].match(/^\s{10}(assert|change|withdraw):\s*(.+?)\s*$/);
    if (trans) current[`transition${capitalize(trans[1])}`] = stripQuotes(trans[2]);
  }
  if (!intents.length && apiRule?.target) intents.push({ target: apiRule.target, fields: objectToFields(apiRule.set || {}), transitionAssert: "", transitionChange: "", transitionWithdraw: "", ttl: "", linger: "", easing: "linear" });
  return intents;
}

function parseEffects(_block, apiRule) {
  return (apiRule?.effects || []).map((effect) => ({ service: `${effect.domain}.${effect.service}`, target: JSON.stringify(effect.target || {}, null, 2), data: JSON.stringify(effect.data || {}, null, 2) }));
}

function visualModeError(block) {
  const supportedTopLevel = new Set(["id", "enabled", "reason", "labels", "group", "profile", "notes", "authority", "confidence", "while", "observe", "after", "hold", "intent", "effect"]);
  for (const line of String(block || "").split("\n")) {
    const key = line.match(/^  ([\w.-]+):/);
    if (key && !supportedTopLevel.has(key[1])) return `Visual mode cannot safely edit the unsupported '${key[1]}' field.`;
  }
  if (/^\s{2}hold:\s*\{[^\n}]*\buse\s*:/m.test(block) || /^\s{4}use:\s*\S+/m.test(block)) return "Visual mode is unavailable to prevent data loss: retention profile references are not represented by the visual editor. Edit this rule as YAML.";
  if (/^\s+time_window:\s*(?:\{|$)/m.test(block) || /^\s+-?\s*window:\s*\S+/m.test(block)) return "Visual mode is unavailable to prevent data loss: named time windows are not represented by the visual editor. Edit this rule as YAML.";
  if (/^\s{4}power:\s*(?:\{|$)/m.test(block)) return "Visual mode is unavailable to prevent data loss: semantic power observations are not represented by the visual editor. Edit this rule as YAML.";
  if (/^  labels:\s*(?:#.*)?$/m.test(block)) return "Visual mode is unavailable to prevent data loss: block-style 'labels' are not represented by the visual editor. Edit this rule as YAML.";
  if (inlineLabelsContainQuotedComma(block)) return "Visual mode is unavailable to prevent data loss: inline 'labels' containing commas cannot be represented by the comma-separated visual editor. Edit this rule as YAML.";
  const blockScalar = String(block || "").match(/^  ([\w.-]+):\s*[>|](?:[1-9]?[-+]?|[-+]?[1-9]?)?\s*(?:#.*)?$/m);
  if (blockScalar) return `Visual mode is unavailable to prevent data loss: block scalar '${blockScalar[1]}' metadata is not represented by the visual editor. Edit this rule as YAML.`;
  if (/[&*][A-Za-z0-9_-]+|(^|\s)<<:\s/m.test(block)) return "Visual mode cannot safely edit YAML anchors, aliases, or merge keys.";
  if (/^\s{4}for:\s/m.test(block) || /^\s{6}-?\s*(all|any|none|not):\s*$/m.test(block)) return "Visual mode cannot safely edit nested or duration-qualified conditions.";
  if (hasDynamicHoldMapping(sectionLines(block, "hold"))) return "Visual mode is unavailable to prevent data loss: dynamic hold mappings are not represented by the visual editor. Edit this rule as YAML.";
  const intentError = unsupportedIntentConstruct(sectionLines(block, "intent"));
  if (intentError) return `Visual mode is unavailable to prevent data loss: ${intentError} Edit this rule as YAML.`;
  return "";
}

function hasDynamicHoldMapping(lines) {
  for (const line of lines) {
    const match = line.match(/^\s{4}(?:after|after_when_stops):\s*(.*)$/);
    if (!match) continue;
    const value = match[1].trim();
    if (!value || value.startsWith("#")) return true;
    if (isFlowMapping(value)) return true;
  }
  return false;
}

function isFlowMapping(value) {
  if (!value.startsWith("{")) return false;
  let quote = "";
  let escaped = false;
  let depth = 0;
  for (let index = 0; index < value.length; index += 1) {
    const character = value[index];
    if (quote) {
      if (quote === '"' && character === "\\" && !escaped) { escaped = true; continue; }
      if (character === quote && !escaped) quote = "";
      escaped = false;
      continue;
    }
    if (character === "'" || character === '"') { quote = character; continue; }
    if (character === "{") depth += 1;
    if (character === "}") {
      depth -= 1;
      if (depth === 0) return /^(?:\s*#.*)?$/.test(value.slice(index + 1));
      if (depth < 0) return false;
    }
  }
  return false;
}

function inlineLabelsContainQuotedComma(block) {
  const match = String(block || "").match(/^  labels:\s*\[([^\n]*)\]\s*(?:#.*)?$/m);
  if (!match) return false;
  let quote = "";
  for (let index = 0; index < match[1].length; index += 1) {
    const character = match[1][index];
    if (!quote && (character === "'" || character === '"')) quote = character;
    else if (character === quote && (quote === "'" || match[1][index - 1] !== "\\")) quote = "";
    else if (quote && character === ",") return true;
  }
  return false;
}

function unsupportedIntentConstruct(lines) {
  const directives = new Set(["select", "suppress", "include"]);
  const unsupportedMetadata = new Set(["linger", "transition", "animation", "animations", "generator", "generators"]);
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    if (!line.trim() || /^\s*#/.test(line)) continue;
    const intentKey = line.match(/^\s{4}([\w.-]+):(?:\s.*)?$/);
    if (intentKey && directives.has(intentKey[1])) return `the nested intent '${intentKey[1]}' construct is not represented by the visual editor.`;

    const targetMetadata = line.match(/^\s{6}([\w.-]+):(?:\s*(.*))?$/);
    if (targetMetadata && unsupportedMetadata.has(targetMetadata[1])) return `target metadata '${targetMetadata[1]}' is not represented by the visual editor.`;
    if (targetMetadata && targetMetadata[1] === "apply") {
      if (targetMetadata[2]) return "inline application metadata is not represented by the visual editor.";
      const applyError = unsupportedApplyMetadata(lines, index);
      if (applyError) return applyError;
    }

    const fieldValue = line.match(/^\s{6}[\w.-]+:\s*(.*)$/);
    if (fieldValue && /(?:^|[{,]\s*)(animate|animation|generate|generator|generators)\s*:/.test(fieldValue[1])) return `intent ${fieldValue[1].match(/(animate|animation|generate|generator|generators)\s*:/)[1]} values are not represented by the visual editor.`;
    if (fieldValue?.[1].trim().startsWith("{")) return "nested intent mappings are not represented by the visual editor.";
    if (fieldValue && !fieldValue[1].trim() && targetMetadata[1] !== "apply") {
      const valueError = unsupportedBlockIntentValue(lines, index);
      if (valueError) return valueError;
    }
    const nestedOperator = line.match(/^\s{8}(\w+):/);
    if (nestedOperator && ["animate", "animation", "generate", "generator", "generators"].includes(nestedOperator[1])) return `intent ${nestedOperator[1]} values are not represented by the visual editor.`;
    if (nestedOperator && !["value", "min", "max", "offset", "multiply", "transition"].includes(nestedOperator[1])) return `nested intent operator '${nestedOperator[1]}' is not represented by the visual editor.`;
  }
  return "";
}

function unsupportedBlockIntentValue(lines, start) {
  const operators = new Set(["value", "min", "max", "offset", "multiply"]);
  const children = [];
  for (let index = start + 1; index < lines.length; index += 1) {
    const line = lines[index];
    if (!line.trim() || /^\s*#/.test(line)) continue;
    const indent = line.match(/^\s*/)[0].length;
    if (indent <= 6) break;
    children.push({ line, indent });
  }
  if (children.length !== 1 || children[0].indent !== 8) return "nested intent field values are not represented by the visual editor.";
  const operator = children[0].line.match(/^\s{8}(\w+):\s*(.+?)\s*$/);
  if (!operator || !operators.has(operator[1]) || operator[2].startsWith("{")) return "nested intent field values are not represented by the visual editor.";
  return "";
}

function unsupportedApplyMetadata(lines, start) {
  const transitionKeys = new Set(["assert", "change", "withdraw"]);
  for (let index = start + 1; index < lines.length; index += 1) {
    const line = lines[index];
    if (!line.trim() || /^\s*#/.test(line)) continue;
    const indent = line.match(/^\s*/)[0].length;
    if (indent <= 6) break;
    const key = line.match(/^\s+(\w+):/);
    if (indent === 8 && key?.[1] !== "transition") return `application metadata '${key?.[1] || line.trim()}' is not represented by the visual editor.`;
    if (indent === 10 && (!key || !transitionKeys.has(key[1]))) return `transition metadata '${key?.[1] || line.trim()}' is not represented by the visual editor.`;
    if (indent > 10) return "nested application metadata is not represented by the visual editor.";
  }
  return "";
}

function sectionLines(contents, key) {
  const lines = String(contents || "").split("\n");
  const start = lines.findIndex((line) => new RegExp(`^\\s{2}${key}:\\s*$`).test(line));
  if (start < 0) return [];
  const out = [];
  for (let index = start + 1; index < lines.length; index += 1) {
    if (/^\s{2}\w[\w.-]*:/.test(lines[index])) break;
    out.push(lines[index]);
  }
  return out;
}

function nestedSectionLines(lines, key) {
  const start = lines.findIndex((line) => new RegExp(`^\\s{4}${key}:\\s*$`).test(line));
  if (start < 0) return [];
  const out = [];
  for (let index = start + 1; index < lines.length; index += 1) {
    if (/^\s{4}\w[\w.-]*:/.test(lines[index])) break;
    out.push(lines[index].replace(/^  /, ""));
  }
  return out;
}

function extractRuleBlock(contents, ruleId) {
  const bounds = findRuleBlock(contents, ruleId);
  if (!bounds) return "";
  const lines = String(contents || "").split("\n");
  return lines.slice(bounds.start, bounds.end).join("\n").trimEnd() + "\n";
}

function replaceRuleBlock(contents, ruleId, replacement) {
  const bounds = findRuleBlock(contents, ruleId);
  if (!bounds) return contents;
  const lines = String(contents || "").split("\n");
  return [...lines.slice(0, bounds.start), ...String(replacement).trimEnd().split("\n"), ...lines.slice(bounds.end)].join("\n").trimEnd() + "\n";
}

function appendRuleBlock(contents, replacement) {
  const trimmed = String(contents || "").trimEnd();
  return `${trimmed}${trimmed ? "\n" : ""}${String(replacement).trimEnd()}\n`;
}

function extractRuleBlocks(contents) {
  const lines = String(contents || "").split("\n");
  const blocks = [];
  for (let index = 0; index < lines.length; index += 1) {
    const match = lines[index].match(/^\s*-\s+id:\s*['"]?([^'"#\s]+)['"]?\s*(?:#.*)?$/);
    if (!match) continue;
    let end = lines.length;
    for (let cursor = index + 1; cursor < lines.length; cursor += 1) {
      if (/^---\s*$/.test(lines[cursor]) || /^\s*-\s+id:\s*/.test(lines[cursor])) { end = cursor; break; }
    }
    blocks.push({ id: match[1], block: lines.slice(index, end).join("\n").trimEnd() + "\n" });
    index = end - 1;
  }
  return blocks;
}

function findRuleBlock(contents, ruleId) {
  const lines = String(contents || "").split("\n");
  const idPattern = new RegExp(`^\\s*-\\s+id:\\s*['\"]?${escapeRegExp(ruleId)}['\"]?\\s*(?:#.*)?$`);
  let start = -1;
  for (let index = 0; index < lines.length; index += 1) if (idPattern.test(lines[index])) { start = index; break; }
  if (start < 0) return null;
  let end = lines.length;
  for (let index = start + 1; index < lines.length; index += 1) if (/^---\s*$/.test(lines[index]) || /^\s*-\s+id:\s*/.test(lines[index])) { end = index; break; }
  return { start, end };
}

function extractScalar(contents, key) {
  const match = String(contents || "").match(new RegExp(`^\\s{2}${key}:\\s*(.+?)\\s*$`, "m"));
  return match ? stripQuotes(match[1]) : "";
}

function extractIndentedScalar(lines, key) {
  const match = lines.join("\n").match(new RegExp(`^\\s{4,6}${key}:\\s*(.+?)\\s*$`, "m"));
  return match ? stripQuotes(match[1]) : "";
}

function extractNestedScalar(contents, section, key) {
  return extractIndentedScalar(sectionLines(contents, section), key);
}

function extractInlineList(contents, key) {
  const value = extractScalar(contents, key);
  if (!value.startsWith("[") || !value.endsWith("]")) return [];
  return value.slice(1, -1).split(",").map((item) => stripQuotes(item.trim())).filter(Boolean);
}

function objectToFields(object) {
  const fields = Object.entries(object || {}).map(([name, value]) => ({ name, operator: "value", value: Array.isArray(value) ? `[${value.join(", ")}]` : String(value) }));
  return fields.length ? fields : [{ name: "state", operator: "value", value: "on" }];
}

function inputField(label, field, value, placeholder = "", help = "") {
  return `<label class="field"><span>${escapeHtml(label)}</span><input data-field="${field}" value="${escapeHtml(value)}" placeholder="${escapeHtml(placeholder)}">${help ? `<small>${escapeHtml(help)}</small>` : ""}</label>`;
}

function intentInputField(label, field, value, placeholder = "", help = "") {
  return `<label class="field"><span>${escapeHtml(label)}</span><input data-intent-field="${field}" value="${escapeHtml(value)}" placeholder="${escapeHtml(placeholder)}">${help ? `<small>${escapeHtml(help)}</small>` : ""}</label>`;
}

function selectField(label, field, value, options) {
  return `<label class="field"><span>${escapeHtml(label)}</span><select data-field="${field}">${options.map(([optionValue, text]) => `<option value="${optionValue}" ${String(value) === optionValue ? "selected" : ""}>${escapeHtml(text)}</option>`).join("")}</select></label>`;
}

function selectInline(field, value, options) {
  return `<select data-field="${field}">${options.map(([optionValue, text]) => `<option value="${optionValue}" ${String(value) === optionValue ? "selected" : ""}>${escapeHtml(text)}</option>`).join("")}</select>`;
}

function operatorSelect(attribute, selected) {
  const operators = ["is", "is_not", "lt", "lte", "gt", "gte", "in", "not_in", "contains", "exists"];
  return `<select ${attribute}>${operators.map((op) => `<option value="${op}" ${op === selected ? "selected" : ""}>${op}</option>`).join("")}</select>`;
}

function fieldOperatorSelect(selected) {
  const operators = [["value", "Set"], ["min", "Minimum"], ["max", "Maximum"], ["offset", "Offset"], ["multiply", "Multiply"]];
  return `<select data-intent-field-row="operator">${operators.map(([op, label]) => `<option value="${op}" ${op === selected ? "selected" : ""}>${label}</option>`).join("")}</select>`;
}

function yamlScalar(value) {
  if (typeof value === "boolean" || typeof value === "number") return String(value);
  const text = String(value ?? "").trim();
  if (!text) return "''";
  if (/^(true|false|on|off|null|yes|no)$/i.test(text)) return JSON.stringify(text);
  if (/^-?\d+(\.\d+)?$/.test(text)) return text;
  if (/^\[[^\n]*\]$/.test(text)) return text;
  if (/^[A-Za-z0-9_.:\/-]+$/.test(text)) return text;
  return JSON.stringify(text);
}

function stripQuotes(value) {
  return String(value ?? "").trim().replace(/^['"]|['"]$/g, "");
}

function nearestIndex(target, key) {
  const node = target.closest(`[data-${kebab(key)}]`);
  return node ? Number(node.dataset[key]) : -1;
}

function kebab(value) { return value.replace(/[A-Z]/g, (letter) => `-${letter.toLowerCase()}`); }
function capitalize(value) { return value.charAt(0).toUpperCase() + value.slice(1); }
function escapeRegExp(value) { return String(value).replace(/[.*+?^${}()|[\]\\]/g, "\\$&"); }
function escapeHtml(value) { return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;"); }

const styles = `
  :host { display: block; color: var(--primary-text-color); background: var(--primary-background-color); }
  main { padding: 24px; box-sizing: border-box; min-height: 100vh; }
  header { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; margin-bottom: 18px; }
  h1, h2, h3 { margin: 0; font-weight: 600; }
  h1 { font-size: 28px; } h2 { font-size: 18px; } h3 { font-size: 16px; }
  p { color: var(--secondary-text-color); margin: 6px 0 0; }
  code { background: var(--secondary-background-color); border-radius: 5px; padding: 1px 5px; }
  button, select, input, textarea { font: inherit; }
  button { border: 0; border-radius: 10px; padding: 10px 14px; background: var(--primary-color); color: var(--text-primary-color); cursor: pointer; }
  button:disabled { opacity: .45; cursor: default; }
  button.secondary { background: var(--secondary-background-color); color: var(--primary-text-color); }
  button.small { padding: 6px 10px; font-size: 13px; }
  button.icon { min-width: 38px; padding: 8px 10px; font-size: 18px; }
  .actions, .mode-switch { display: flex; gap: 8px; flex-wrap: wrap; }
  .grid { display: grid; grid-template-columns: minmax(220px, 300px) minmax(480px, 1fr) minmax(300px, 380px); gap: 16px; align-items: start; }
  .narrow .grid { grid-template-columns: 1fr; } .narrow header { flex-direction: column; }
  .card { background: var(--card-background-color); border-radius: 16px; box-shadow: var(--ha-card-box-shadow, 0 2px 8px rgba(0,0,0,.15)); padding: 16px; }
  .card-header, .section-title, .hero-row, .subcard-head, .history-item { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
  .rules, .inspector, .editor-stack { display: flex; flex-direction: column; gap: 12px; }
  .rule { text-align: left; background: var(--secondary-background-color); color: var(--primary-text-color); display: flex; flex-direction: column; gap: 3px; }
  .rule.selected { outline: 2px solid var(--primary-color); background: color-mix(in srgb, var(--primary-color) 12%, var(--secondary-background-color)); }
  .rule-title { font-weight: 600; } .rule-meta, .rule-reason, .muted, .empty, small { color: var(--secondary-text-color); font-size: 13px; }
  .hero-card { background: linear-gradient(135deg, color-mix(in srgb, var(--primary-color) 18%, var(--card-background-color)), var(--card-background-color)); }
  .eyebrow { color: var(--primary-color); font-size: 12px; text-transform: uppercase; letter-spacing: .08em; font-weight: 700; }
  .form-card { display: flex; flex-direction: column; gap: 14px; }
  .form-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; } .form-grid.single { grid-template-columns: 1fr; }
  .field, .row label, .target-field { display: flex; flex-direction: column; gap: 5px; min-width: 0; }
  .field span, .row span, .target-field span { font-size: 12px; color: var(--secondary-text-color); font-weight: 600; }
  input, select, textarea { box-sizing: border-box; width: 100%; border: 1px solid var(--divider-color); border-radius: 10px; padding: 9px 10px; background: var(--primary-background-color); color: var(--primary-text-color); }
  textarea.yaml-editor { min-height: 70vh; resize: vertical; font: 13px/1.45 ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; }
  .small-textarea { min-height: 76px; resize: vertical; } .mono, pre { font: 12px/1.45 ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; }
  .rows { display: flex; flex-direction: column; gap: 8px; } .rows.compact { margin: 12px 0; }
  .row { display: grid; grid-template-columns: minmax(190px, 1.4fr) minmax(120px, .8fr) minmax(120px, 1fr) auto; gap: 8px; align-items: end; }
  .subcard { border: 1px solid var(--divider-color); border-radius: 14px; padding: 12px; background: color-mix(in srgb, var(--secondary-background-color) 50%, transparent); margin-bottom: 10px; }
  .target-field { flex: 1; } .advanced { margin-top: 10px; } .advanced summary { cursor: pointer; color: var(--primary-color); }
  pre { max-height: 280px; overflow: auto; background: var(--secondary-background-color); border-radius: 12px; padding: 12px; }
  .banner, .error-box { background: var(--error-color); color: white; border-radius: 12px; padding: 12px; }
  .ok { background: color-mix(in srgb, var(--success-color, #43a047) 18%, transparent); border-radius: 12px; padding: 12px; }
  .warning-box { background: color-mix(in srgb, var(--warning-color, #ffa000) 18%, transparent); border-radius: 12px; padding: 12px; }
  .warning-box p { margin: 8px 0 0; color: var(--primary-text-color); }
  .history-item { padding: 10px 0; border-bottom: 1px solid var(--divider-color); }
  .history-item span { display: block; color: var(--secondary-text-color); font-size: 12px; }
  .empty-state { min-height: 320px; display: grid; place-content: center; text-align: center; }
  @media (max-width: 900px) { main { padding: 12px; } .grid, .form-grid, .row { grid-template-columns: 1fr; } .card-header, .section-title, .hero-row, header { flex-direction: column; align-items: stretch; } }
`;

customElements.define("intentional-panel", IntentionalPanel);
