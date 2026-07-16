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
    this._world = null;
    this._workspace = "intent";
    this._screen = "overview";
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
    this._stage = "Draft";
    this._reviewedFingerprint = "";
    this._rollbackReview = null;
    this._worldRefreshTimer = null;
    this._simulation = null;
    this._timelineText = "[{\"states\":{}}]";
    this._validateTimer = null;
    this._validationRequest = 0;
    this._previewRequest = 0;
    this._formEdited = false;
    this._visualModeError = "";
    this._migration = { automations: [], selected: "", inspection: null, proposal: null, loading: "" };
    this._alerts = [];
    this._alertsError = "";
    this._alertsBusy = false;
    this._policyDocument = null;
    this._policyContents = "";
    this._policyStage = "Draft";
    this._policyCheckedFingerprint = "";
    this._policyReviewedFingerprint = "";
    this._policyCheck = null;
    this._policyPreview = null;
    this._policyError = "";
    this._policyBusy = false;
    this._policyLoaded = false;
    this._policySyntheticText = '[\n  {"alertname":"FreezerTemperatureHigh","severity":"critical","rule_id":"example-rule"}\n]';
    this._beforeUnload = (event) => { if (this._dirty) { event.preventDefault(); event.returnValue = ""; } };
  }

  connectedCallback() { globalThis.addEventListener?.("beforeunload", this._beforeUnload); }
  disconnectedCallback() {
    globalThis.removeEventListener?.("beforeunload", this._beforeUnload);
    clearTimeout(this._worldRefreshTimer);
  }

  set hass(hass) {
    const previous = this._hass;
    this._hass = hass;
    if (!this._loaded && hass) {
      this._loaded = true;
      this._load();
    } else if (hass && previous && hass !== previous) {
      clearTimeout(this._worldRefreshTimer);
      this._worldRefreshTimer = setTimeout(() => this._refreshWorld(), 500);
    }
  }

  async _refreshWorld() {
    this._worldRefreshTimer = null;
    try {
      this._world = await this._api("GET", "world");
      this._rules = parseDocumentRuleSummaries(this._contents, this._world.authored_rules || []);
      this._render();
    } catch (err) { this._error = err.message || String(err); this._render(); }
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
    void this._loadAlerts();
    try {
      const [health, document, history, world] = await Promise.all([
        this._api("GET", "health"),
        this._api("GET", "rules/document"),
        this._api("GET", "rules/history"),
        this._api("GET", "world"),
      ]);
      this._health = health;
      this._document = document;
      this._contents = document.contents || "";
      this._history = history.history || [];
      this._world = world;
      this._rules = parseDocumentRuleSummaries(this._contents, world.authored_rules || []);
      this._screen = "overview";
      this._selectedRuleId = "";
      this._selectedRuleContents = "";
      this._selectedRuleForm = null;
      this._editorMode = "visual";
      this._dirty = false;
      this._stage = "Draft";
      this._reviewedFingerprint = "";
      await this._validate({ quiet: true });
    } catch (err) {
      this._error = err.message || String(err);
    } finally {
      this._busy = false;
      this._render();
    }
  }

  async _loadAlerts() {
    this._alertsBusy = true;
    this._alertsError = "";
    try {
      const result = await this._api("GET", "alerts");
      this._alerts = Array.isArray(result?.alerts) ? result.alerts : [];
    } catch (err) {
      this._alerts = [];
      this._alertsError = err.message || String(err);
    } finally {
      this._alertsBusy = false;
      this._render();
    }
  }

  async _loadPolicy() {
    if (this._policyBusy) return;
    this._policyBusy = true;
    this._policyError = "";
    this._render();
    try {
      const document = await this._api("GET", "alerting/policy");
      this._policyDocument = document;
      this._policyContents = document.contents || "";
      this._policyStage = "Published";
      this._policyCheckedFingerprint = "";
      this._policyReviewedFingerprint = "";
      this._policyCheck = null;
      this._policyPreview = null;
      this._policyLoaded = true;
    } catch (err) {
      this._policyError = err.message || String(err);
    } finally {
      this._policyBusy = false;
      this._render();
    }
  }

  async _checkPolicy() {
    const contents = this._policyContents;
    const fingerprint = candidateFingerprint(contents);
    this._policyBusy = true;
    this._policyError = "";
    this._render();
    try {
      const result = await this._api("POST", "alerting/simulate", { contents, alerts: [] });
      if (candidateFingerprint(this._policyContents) !== fingerprint) return;
      this._policyCheck = result;
      this._policyCheckedFingerprint = fingerprint;
      this._policyReviewedFingerprint = "";
      this._policyPreview = null;
      this._policyStage = result.valid === false ? "Draft" : "Checked";
    } catch (err) {
      if (candidateFingerprint(this._policyContents) === fingerprint) this._policyError = err.message || String(err);
    } finally {
      this._policyBusy = false;
      this._render();
    }
  }

  async _reviewPolicy() {
    const contents = this._policyContents;
    const fingerprint = candidateFingerprint(contents);
    if (this._policyStage !== "Checked" || this._policyCheckedFingerprint !== fingerprint) return;
    let alerts;
    try {
      alerts = JSON.parse(this._policySyntheticText || "[]");
      if (!Array.isArray(alerts) || containsNonFiniteNumber(alerts)) throw new Error("expected a finite JSON array");
    } catch (err) {
      this._policyError = `Synthetic alerts JSON is invalid: ${err.message || err}`;
      this._render();
      return;
    }
    this._policyBusy = true;
    this._policyError = "";
    this._render();
    try {
      const result = await this._api("POST", "alerting/simulate", { contents, alerts });
      if (candidateFingerprint(this._policyContents) !== fingerprint) return;
      this._policyPreview = result;
      this._policyReviewedFingerprint = fingerprint;
      this._policyStage = result.valid === false ? "Draft" : "Reviewed";
    } catch (err) {
      if (candidateFingerprint(this._policyContents) === fingerprint) this._policyError = err.message || String(err);
    } finally {
      this._policyBusy = false;
      this._render();
    }
  }

  async _publishPolicy() {
    const contents = this._policyContents;
    const fingerprint = candidateFingerprint(contents);
    if (this._policyStage !== "Reviewed" || this._policyReviewedFingerprint !== fingerprint) return;
    this._policyBusy = true;
    this._policyError = "";
    this._render();
    try {
      const saved = await this._api("PUT", "alerting/policy", {
        contents,
        expected_generation: this._policyDocument?.generation,
      });
      if (candidateFingerprint(this._policyContents) !== fingerprint) return;
      this._policyDocument = { ...this._policyDocument, ...saved, contents };
      this._policyStage = "Published";
      this._policyCheckedFingerprint = "";
      this._policyReviewedFingerprint = "";
    } catch (err) {
      this._policyError = err.message || String(err);
    } finally {
      this._policyBusy = false;
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
      const fingerprint = candidateFingerprint(contents);
      this._stage = validation.valid && this._stage === "Reviewed" && this._reviewedFingerprint === fingerprint ? "Reviewed" : validation.valid ? "Checked" : "Draft";
      return validation.valid;
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

  async _review() {
    if (!(await this._validate())) return;
    const request = ++this._previewRequest;
    const candidate = this._candidateContents();
    const fingerprint = candidateFingerprint(candidate);
    this._busy = true;
    this._preview = null;
    this._render();
    try {
      const preview = await this._api("POST", "preview", { contents: candidate, horizons_ms: [0, 60000] });
      if (request !== this._previewRequest || candidateFingerprint(this._candidateContents()) !== fingerprint) return;
      this._preview = preview;
      this._reviewedFingerprint = fingerprint;
      this._stage = "Reviewed";
      this._error = "";
    } catch (err) {
      this._error = err.message || String(err);
    } finally {
      if (request === this._previewRequest) this._busy = false;
      if (request === this._previewRequest) this._render();
    }
  }

  // Kept as an advanced API capability; publishing is gated by the richer preview review.
  async _dryRun() {
    if (!(await this._validate())) return null;
    return this._api("POST", "dry-run", { contents: this._candidateContents() });
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
    const candidate = this._candidateContents();
    const fingerprint = candidateFingerprint(candidate);
    const expectedGeneration = this._document?.generation;
    if (this._stage !== "Reviewed" || this._reviewedFingerprint !== fingerprint) {
      this._error = "Review the current changes before publishing.";
      this._render();
      return;
    }
    if (!(await this._validate()) || this._reviewedFingerprint !== fingerprint || candidateFingerprint(this._candidateContents()) !== fingerprint) return;
    this._busy = true;
    this._render();
    try {
      const saved = await this._api("PUT", "rules/document", {
        contents: candidate,
        expected_generation: expectedGeneration,
      });
      const newerCandidate = this._candidateContents();
      const editedDuringSave = candidateFingerprint(newerCandidate) !== fingerprint;
      this._document = saved;
      this._contents = editedDuringSave && (this._editorMode === "document" || !this._selectedRuleId) ? newerCandidate : saved.contents || candidate;
      this._dirty = editedDuringSave;
      this._stage = editedDuringSave ? "Draft" : "Published";
      if (editedDuringSave) this._reviewedFingerprint = "";
      this._error = "";
      await this._loadHistory();
      await this._refreshWorld();
      if (!editedDuringSave) {
        if (this._selectedRuleId) this._selectRule(this._selectedRuleForm?.id || this._selectedRuleId, { render: false, keepDirty: false, screen: "detail" });
      }
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
    if (this._dirty && !confirm("Discover automations while keeping your unsaved draft?")) return;
    this._busy = true;
    this._migration = { automations: [], selected: "", inspection: null, proposal: null, loading: "discover" };
    this._render();
    try {
      const result = await this._api("GET", "migrate-ha");
      this._migration = { automations: result.automations || [], selected: "", inspection: null, proposal: null, loading: "" };
      this._error = "";
    } catch (err) { this._error = err.message || String(err); }
    finally { this._busy = false; this._render(); }
  }

  async _inspectMigration(entityId) {
    const request = (this._migrationInspectionRequest || 0) + 1;
    this._migrationInspectionRequest = request;
    this._migration.selected = entityId;
    this._migration.inspection = null;
    this._migration.proposal = null;
    this._migration.loading = entityId ? "inspect" : "";
    if (!entityId) { this._migration.inspection = null; this._render(); return; }
    try {
      const inspection = await this._api("GET", `migrate-ha/${encodeURIComponent(entityId)}`);
      if (request !== this._migrationInspectionRequest || this._migration.selected !== entityId) return;
      this._migration.inspection = inspection; this._error = "";
    }
    catch (err) { if (request === this._migrationInspectionRequest) this._error = err.message || String(err); }
    if (request === this._migrationInspectionRequest) this._migration.loading = "";
    this._render();
  }

  async _proposeMigration() {
    if (!this._migration.selected) return;
    const entityId = this._migration.selected;
    const request = (this._migrationProposalRequest || 0) + 1;
    this._migrationProposalRequest = request;
    this._migration.loading = "propose";
    this._migration.proposal = null;
    try {
      const proposal = await this._api("POST", "migrate-ha/propose", { entity_id: entityId });
      if (request !== this._migrationProposalRequest || this._migration.selected !== entityId) return;
      this._migration.proposal = proposal; this._error = "";
    }
    catch (err) { if (request === this._migrationProposalRequest) this._error = err.message || String(err); }
    if (request === this._migrationProposalRequest) this._migration.loading = "";
    this._render();
  }

  _addMigrationProposal() {
    const proposal = this._migration.proposal;
    if (!proposal?.supported || !proposal?.merged_validation?.valid) return;
    if (this._dirty && !confirm("Replace the current unsaved draft with this migration proposal?")) return;
    this._contents = proposal.merged_candidate;
    this._editorMode = "document";
    this._selectedRuleId = "";
    this._dirty = true;
    this._validation = proposal.merged_validation;
    this._timelineText = "[{\"states\":{}}]";
    this._stage = "Checked";
    this._render();
  }

  async _reviewRollback(generation) {
    this._busy = true;
    this._render();
    try {
      this._rollbackReview = await this._api("GET", `rules/history/${encodeURIComponent(generation)}`);
    } catch (err) {
      this._error = err.message || String(err);
    } finally {
      this._busy = false;
      this._render();
    }
  }

  async _applyRollback() {
    const generation = this._rollbackReview?.generation;
    if (!generation || !confirm(`Restore generation ${generation.slice(0, 12)}? Unsaved editor changes will be discarded.`)) return;
    this._busy = true; this._render();
    try {
      await this._api("POST", "rules/rollback", { generation, expected_generation: this._document?.generation });
      this._rollbackReview = null;
      await this._load();
    } catch (err) { this._error = err.message || String(err); }
    finally { this._busy = false; this._render(); }
  }

  _newRule() {
    if (this._dirty && !confirm("Discard unsaved editor changes?")) return;
    const nextNumber = this._uniqueRules().length + 1;
    this._selectedRuleId = "__new__";
    this._screen = "edit";
    this._selectedRuleForm = EMPTY_RULE();
    this._selectedRuleForm.id = `new-rule-${nextNumber}`;
    this._selectedRuleForm.enabled = false;
    this._selectedRuleForm.reason = "Describe why this rule exists";
    this._selectedRuleContents = stringifyRule(this._selectedRuleForm);
    this._editorMode = "visual";
    this._visualModeError = "";
    this._formEdited = true;
    this._dirty = true;
    this._validation = null;
    this._preview = null;
    this._simulation = null;
    this._render();
  }

  _selectRule(ruleId, { render = true, keepDirty = false, screen = "detail" } = {}) {
    if (!keepDirty && this._dirty && !confirm("Discard unsaved editor changes?")) return;
    const block = extractRuleBlock(this._contents, ruleId);
    const apiRule = this._rules.find((rule) => rule.id === ruleId) || null;
    this._selectedRuleId = ruleId;
    this._selectedRuleContents = block;
    this._selectedRuleForm = parseRuleForm(block, apiRule);
    this._screen = screen;
    this._visualModeError = visualModeError(block);
    this._formEdited = false;
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
    this._screen = "edit";
    this._selectedRuleId = "";
    this._selectedRuleContents = "";
    this._selectedRuleForm = null;
    this._dirty = false;
    this._render();
  }

  _showYamlRule() {
    if (this._editorMode === "visual" && this._selectedRuleForm && this._formEdited) {
      this._selectedRuleContents = stringifyRule(this._selectedRuleForm);
    }
    this._editorMode = "yaml";
    this._screen = "edit";
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
    const ruleContents = this._editorMode === "visual" && this._formEdited ? stringifyRule(this._selectedRuleForm) : this._selectedRuleContents;
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

  _friendlyName(entityId) {
    return this._hass?.states?.[entityId]?.attributes?.friendly_name || entityId;
  }

  _ledgerRules() {
    return this._renderRuleViewModels || buildRuleViewModels(this._uniqueRules(), this._world || {}, this._hass?.states || {});
  }

  _renderRules() {
    const rules = this._ledgerRules();
    if (!rules.length) return `<div class="empty">No rules yet. Create one visually or paste YAML in Advanced.</div>`;
    const sections = [["Needs attention", "attention"], ["Active now", "active"], ["Waiting", "waiting"], ["Disabled / Paused", "disabled"]];
    return sections.map(([title, phase]) => {
      const matches = rules.filter((rule) => rule.section === phase);
      if (!matches.length) return "";
      return `<section class="ledger-section"><h2>${title}<span>${matches.length}</span></h2>${matches.map((rule) => `
        <button class="rule ${this._selectedRuleId === rule.id ? "selected" : ""}" data-rule-id="${escapeHtml(rule.id)}" ${this._selectedRuleId === rule.id ? 'aria-current="true"' : ""}>
          <span class="rule-title">${escapeHtml(rule.title)}</span><span class="phase">${escapeHtml(rule.phaseText)}</span>
          <span class="rule-reason">${escapeHtml(describeRule(rule))}</span>
          <span class="rule-meta">${rule.targetCount} target${rule.targetCount === 1 ? "" : "s"}${rule.group ? ` · ${escapeHtml(rule.group)}` : ""}${rule.profile ? ` · ${escapeHtml(rule.profile)}` : ""}</span>
        </button>`).join("")}</section>`;
    }).join("");
  }

  _renderOverview() {
    const rules = this._ledgerRules();
    const attention = rules.filter((rule) => rule.section === "attention").length;
    return `<section class="overview">
      <div class="ledger-head"><span class="eyebrow">Intent Ledger</span><h2>${attention ? `${attention} need${attention === 1 ? "s" : ""} attention` : "Your home is aligned"}</h2><p>${attention ? "Review intentions that are blocked, drifting, or reporting an error." : "Active intentions and Home Assistant agree."}</p></div>
      <div class="ledger">${this._renderRules()}</div>
      <details class="create"><summary>Create or import</summary><div class="actions"><button data-action="new-rule">Create Rule</button><button class="secondary" data-action="show-document">Import or edit document YAML</button><button class="secondary" data-action="migration-discover">Migrate HA automation</button></div>${this._renderMigration()}</details>
    </section>`;
  }

  _renderAlertWorkspace() {
    if (this._alertsError) return `<section class="workspace"><div class="ledger-head alert-head"><span class="eyebrow">Alert Ledger</span><h2>Alert state unavailable</h2><p>This workspace could not load. Rules and policy remain available.</p></div><div class="error-box" role="alert">${escapeHtml(this._alertsError)}</div><button class="secondary" data-action="reload-alerts">Retry Alerts</button></section>`;
    const alerts = sortAlerts(this._alerts);
    const firing = alerts.filter((alert) => alert.state === "firing").length;
    return `<section class="workspace">
      <div class="ledger-head alert-head"><span class="eyebrow">Alert Ledger</span><h2>${firing ? `${firing} firing` : "No Alerts firing"}</h2><p>Read-only durable Alert state reported by the current integration.</p></div>
      ${this._alertsBusy ? `<div role="status" class="muted">Loading Alerts...</div>` : ""}
      ${alerts.length ? `<div class="alert-ledger">${alerts.map((alert) => this._renderAlertRow(alert)).join("")}</div>` : `<div class="empty card">No Alert definitions are currently reported.</div>`}
    </section>`;
  }

  _renderAlertRow(alert) {
    const state = ["firing", "pending", "inactive"].includes(alert.state) ? alert.state : "inactive";
    const severity = ["critical", "warning", "info"].includes(alert.severity) ? alert.severity : "info";
    const stale = alert.evaluation_status === "stale";
    const ruleExists = this._uniqueRules().some((rule) => rule.id === alert.rule_id);
    const rule = ruleExists
      ? `<button class="link-button" data-action="open-alert-rule" data-rule-id="${escapeHtml(alert.rule_id)}">Rule ${escapeHtml(alert.rule_id)}</button>`
      : `<span>Rule ${escapeHtml(alert.rule_id || "unknown")}</span>`;
    return `<article class="alert-row ${state} severity-${severity}${stale ? " stale" : ""}">
      <div class="alert-row-head"><div><span class="eyebrow">${escapeHtml(severity)}</span><h2>${escapeHtml(alert.summary || alert.name || "Unnamed Alert")}</h2></div><div class="alert-badges"><span class="state-badge">${escapeHtml(state)}</span>${stale ? `<span class="stale-badge">Stale evaluation</span>` : ""}</div></div>
      <p>${escapeHtml(alert.name || "Unnamed Alert")}</p>
      <div class="alert-meta">${rule}<span>Evaluation: ${escapeHtml(alert.evaluation_status || "unknown")}</span><span>Instance: ${escapeHtml(alert.instance_id || "none")}</span></div>
    </article>`;
  }

  _renderPolicyWorkspace() {
    if (!this._policyLoaded && !this._policyBusy && !this._policyError) void this._loadPolicy();
    const fingerprint = candidateFingerprint(this._policyContents);
    const checked = this._policyStage === "Checked" && this._policyCheckedFingerprint === fingerprint;
    const reviewed = this._policyStage === "Reviewed" && this._policyReviewedFingerprint === fingerprint;
    const generation = this._policyDocument?.generation || "not loaded";
    return `<section class="workspace policy-workspace">
      <div class="ledger-head policy-head"><span class="eyebrow">Administrator workspace</span><h2>Alerting Policy YAML</h2><p>Edit routing policy, check it without sample traffic, then review an explicitly synthetic preview before publishing.</p></div>
      ${this._policyError ? `<div class="error-box" role="alert">${escapeHtml(this._policyError)}</div>` : ""}
      ${!this._policyLoaded ? `<section class="card"><div role="status">${this._policyBusy ? "Loading Alerting Policy..." : "Alerting Policy unavailable."}</div>${this._policyError ? `<button class="secondary" data-action="reload-policy">Retry policy</button>` : ""}</section>` : `
        <section class="two-pane policy-layout">
          <div class="card editor-stack"><div class="card-header"><div><h2>Policy document</h2><p>Generation ${escapeHtml(generation)}</p></div><span class="stage-badge">${escapeHtml(this._policyStage)}</span></div><textarea class="yaml-editor policy-yaml" data-policy-yaml data-focus-key="policy-yaml" spellcheck="false">${escapeHtml(this._policyContents)}</textarea></div>
          <div class="editor-stack">
            <section class="card form-card"><div><span class="eyebrow">Synthetic preview</span><h2>Synthetic Alert labels</h2><p>These JSON fixtures are sent only to simulation. They are not the live Alert Ledger.</p></div><textarea class="small-textarea mono synthetic-alerts" data-policy-alerts data-focus-key="policy-alerts" spellcheck="false">${escapeHtml(this._policySyntheticText)}</textarea></section>
            <section class="card"><h2>Check</h2>${this._policyCheck ? `<div class="ok">Policy YAML checked successfully.</div>` : `<p>Check parses and validates the candidate with no synthetic Alerts.</p>`}</section>
            <section class="card policy-preview"><span class="eyebrow">Synthetic preview</span><h2>Routing result</h2>${renderPolicyPreview(this._policyPreview)}</section>
          </div>
        </section>
        <nav class="publish-bar" aria-label="Alerting Policy draft workflow"><span>${escapeHtml(this._policyStage)}</span><button class="secondary" data-action="policy-check" ${this._policyBusy ? "disabled" : ""}>Check</button><button class="secondary" data-action="policy-review" ${this._policyBusy || !checked ? "disabled" : ""}>Review synthetic preview</button><button data-action="policy-publish" ${this._policyBusy || !reviewed ? "disabled" : ""}>Publish</button></nav>`}
    </section>`;
  }

  _renderRuleDetail() {
    const rule = this._ledgerRules().find((item) => item.id === this._selectedRuleId);
    if (!rule) return this._renderOverview();
    const targets = rule.targets.map((target) => `<div class="target-line"><div><strong>${escapeHtml(target.title)}</strong><small>${escapeHtml(target.id)}</small></div><div><span>Desired: ${escapeHtml(formatValue(target.desired))}</span><span class="${target.aligned ? "aligned" : "attention"}">${target.aligned ? "Aligned" : `Actual: ${escapeHtml(formatValue(target.actual))}`}</span></div></div>`).join("") || `<div class="empty">No durable targets. This Rule may only produce Effects.</div>`;
    const competing = rule.competing.length ? `<section><h3>Competing Intentions</h3>${rule.competing.map((item) => `<p>${escapeHtml(item.rule_id || item.id || "Another intention")}</p>`).join("")}</section>` : "";
    return `<article class="detail card"><div class="sticky-top"><button class="secondary back" data-action="back">Back</button><span>${escapeHtml(rule.title)}</span></div>
      <span class="eyebrow">${escapeHtml(rule.phaseText)}</span><h2>${escapeHtml(rule.title)}</h2><p class="summary">${escapeHtml(describeRule(rule))}</p>
      <section><h3>Why this state</h3><p>${escapeHtml(rule.reason || describeCondition(rule.form))}</p></section>
      <section><h3>Desired targets</h3>${targets}</section>${competing}
      ${rule.history.length ? `<section><h3>Recent plan history</h3>${rule.history.map((item) => `<p>${escapeHtml(item.reason || item.status || "Service plan recorded")}</p>`).join("")}</section>` : ""}
      <div class="detail-actions"><button data-action="edit-rule">Edit guided</button><button class="secondary" data-action="show-yaml-rule">View source</button></div>
    </article>`;
  }

  _renderEditor() {
    if (this._editorMode === "document") return this._renderDocumentEditor();
    if (this._editorMode === "yaml") return this._renderYamlRuleEditor();
    if (!this._selectedRuleForm) return this._renderOverview();
    return this._renderVisualEditor();
  }

  _renderVisualEditor() {
    const form = this._selectedRuleForm;
    return `
      <section class="editor-stack" data-form-root>
        ${this._renderEditBack()}
        ${this._formEdited ? `<div class="warning-box">Guided edits normalize this Rule's YAML formatting and comments when published.</div>` : ""}
        <section class="card hero-card">
          <div class="hero-row">
            <div>
               <span class="eyebrow">Rule editor · Edit studio</span>
              <h2>${escapeHtml(form.id || "New rule")}</h2>
               <p>Build a durable <code>while → intent</code> Rule in the Visual rule editor. Every change can be checked and simulated before it reaches Home Assistant storage.</p>
            </div>
            <div class="mode-switch">
              <button class="secondary small" data-action="show-yaml-rule">Edit YAML</button>
              <button class="secondary small" data-action="show-document">Document YAML</button>
            </div>
          </div>
        </section>
        <section class="card form-card">
          <h3>Name &amp; description</h3>
          <div class="form-grid">
            ${inputField("ID", "id", form.id, "new-rule", "Rule ID used for status, history, and switches")}
            ${selectField("Enabled", "enabled", String(form.enabled), [["true", "Enabled"], ["false", "Disabled"]])}
            ${inputField("Reason", "reason", form.reason, "Turn on the sofa lamp when the room is occupied", "Shown in diagnostics and explanations")}
          </div>
          <label class="field wide"><span>Notes</span><textarea class="small-textarea" data-field="notes" data-focus-key="field-notes" name="notes" placeholder="Private authoring notes">${escapeHtml(form.notes)}</textarea></label>
          <details class="advanced"><summary>Organisation</summary><div class="form-grid">${inputField("Labels", "labels", form.labels, "living-room, lighting", "Comma-separated")}${inputField("Group", "group", form.group, "living-room-lighting")}${inputField("Profile", "profile", form.profile, "settled")}</div></details>
        </section>
        <section class="card form-card">
           <div class="section-title"><div><h3>When</h3><p>Conditions that make the intent active.</p></div>${selectInline("Condition matching", "conditionMode", form.conditionMode, [["all", "All conditions"], ["any", "Any condition"], ["none", "None match"], ["not", "Not first condition"]])}</div>
          <div class="rows">${form.conditions.map((condition, index) => this._renderCondition(condition, index, "condition")).join("")}</div>
          <button class="secondary" data-action="add-condition">Add Condition</button>
           <details class="advanced"><summary>Timing</summary><div class="form-grid single">${inputField("Activate after", "after", form.after, "5m", "Optional dwell before first activation")}</div></details>
        </section>
        <section class="card form-card">
           <div class="section-title"><div><h3>Keep</h3><p>Prevent flicker by retaining an active intent after the original condition stops.</p></div>${selectInline("Keep behavior", "holdMode", form.holdMode, [["none", "No hold"], ["after", "Keep for duration"], ["until_for", "Until condition stays true"], ["while_after", "While condition, then duration"]])}</div>
          ${this._renderHold(form)}
        </section>
        <section class="card form-card">
           <div class="section-title"><div><h3>Targets</h3><p>Desired target state. Add multiple targets for one authored Rule.</p></div><button class="secondary" data-action="add-intent">Add Target</button></div>
          ${form.intents.map((intent, index) => this._renderIntent(intent, index)).join("")}
        </section>
         <details class="card form-card advanced"><summary>Advanced Effects</summary>
          <div class="section-title"><div><h3>Effects</h3><p>Optional explicit side effects. Use sparingly; durable state belongs above.</p></div><button class="secondary" data-action="add-effect">Add Effect</button></div>
          ${form.effects.length ? form.effects.map((effect, index) => this._renderEffect(effect, index)).join("") : `<div class="empty inline-empty">No side effects configured.</div>`}
         </details>
         <details class="card form-card advanced"><summary>Resolution</summary><div class="form-grid">${selectField("Authority", "authority", form.authority, [["sensor", "Sensor"], ["automation", "Automation"], ["user", "User"]])}${inputField("Confidence", "confidence", form.confidence, "0.8")}</div></details>
      </section>
    `;
  }

  _renderCondition(condition, index, prefix) {
    return `
      <div class="row condition-row" data-${prefix}-index="${index}">
        <label><span>Entity</span><input data-${prefix}-field="entity" data-focus-key="${prefix}-${index}-entity" value="${escapeHtml(condition.entity)}" list="entity-list" placeholder="binary_sensor.room_presence"></label>
        <label><span>Operator</span>${operatorSelect(`data-${prefix}-field="operator" data-focus-key="${prefix}-${index}-operator"`, condition.operator)}</label>
        <label><span>Value</span><input data-${prefix}-field="value" data-focus-key="${prefix}-${index}-value" value="${escapeHtml(condition.value)}" placeholder="on"></label>
         <button class="icon secondary" data-action="remove-${prefix}" data-index="${index}" aria-label="Remove ${prefix}">×</button>
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
           <label class="target-field"><span>Target</span><input data-intent-field="target" data-focus-key="intent-${index}-target" value="${escapeHtml(intent.target)}" list="entity-list" placeholder="light.office"></label>
           <button class="icon secondary" data-action="remove-intent" data-index="${index}" aria-label="Remove target">×</button>
        </div>
        <div class="rows compact">${intent.fields.map((field, fieldIndex) => this._renderIntentField(field, index, fieldIndex)).join("")}</div>
        <button class="secondary small" data-action="add-intent-field" data-index="${index}">Add Field</button>
        <details class="advanced"><summary>Application options</summary>
          <div class="form-grid">
            ${intentInputField("Assert transition", "transitionAssert", intent.transitionAssert, "2s", "Used when asserting an existing target", index)}
            ${intentInputField("Change transition", "transitionChange", intent.transitionChange, "5s", "Used for value changes", index)}
            ${intentInputField("Withdraw transition", "transitionWithdraw", intent.transitionWithdraw, "7s", "Used when turning off", index)}
            ${intentInputField("TTL", "ttl", intent.ttl, "30s", "", index)}
            ${intentInputField("Easing", "easing", intent.easing, "linear", "", index)}
          </div>
        </details>
      </div>
    `;
  }

  _renderIntentField(field, intentIndex, fieldIndex) {
    return `
      <div class="row field-row" data-intent-index="${intentIndex}" data-field-index="${fieldIndex}">
        <label><span>Field</span><input data-intent-field-row="name" data-focus-key="intent-${intentIndex}-field-${fieldIndex}-name" value="${escapeHtml(field.name)}" placeholder="brightness_pct"></label>
        <label><span>Mode</span>${fieldOperatorSelect(field.operator, `intent-${intentIndex}-field-${fieldIndex}-operator`)}</label>
        <label><span>Value</span><input data-intent-field-row="value" data-focus-key="intent-${intentIndex}-field-${fieldIndex}-value" value="${escapeHtml(field.value)}" placeholder="70"></label>
         <button class="icon secondary" data-action="remove-intent-field" data-intent-index="${intentIndex}" data-field-index="${fieldIndex}" aria-label="Remove field">×</button>
      </div>
    `;
  }

  _renderEffect(effect, index) {
    return `
      <div class="subcard" data-effect-index="${index}">
        <div class="subcard-head">
          <label class="target-field"><span>Service</span><input data-effect-field="service" data-focus-key="effect-${index}-service" value="${escapeHtml(effect.service)}" placeholder="notify.mobile_app_phone"></label>
           <button class="icon secondary" data-action="remove-effect" data-index="${index}" aria-label="Remove effect">×</button>
        </div>
        <div class="form-grid">
          <label class="field"><span>Target JSON</span><textarea class="small-textarea mono" data-effect-field="target" data-focus-key="effect-${index}-target" placeholder='{"entity_id":"light.office"}'>${escapeHtml(effect.target)}</textarea></label>
          <label class="field"><span>Data JSON</span><textarea class="small-textarea mono" data-effect-field="data" data-focus-key="effect-${index}-data" placeholder='{"message":"Office occupied"}'>${escapeHtml(effect.data)}</textarea></label>
        </div>
      </div>
    `;
  }

  _renderYamlRuleEditor() {
    return `
      <section class="card editor">
        ${this._renderEditBack()}
        <div class="card-header"><div><h2>Rule YAML</h2><p>Advanced escape hatch for fields the visual editor does not expose yet.</p></div><div class="actions"><button class="secondary small" data-action="show-visual-rule">Visual Editor</button><button class="secondary small" data-action="show-document">Document YAML</button></div></div>
        ${this._visualModeError ? `<div class="warning-box">${escapeHtml(this._visualModeError)} Edit this rule as YAML to avoid losing unsupported fields.</div>` : ""}
        <textarea class="yaml-editor" data-focus-key="rule-yaml" spellcheck="false">${escapeHtml(this._selectedRuleContents)}</textarea>
      </section>
    `;
  }

  _renderDocumentEditor() {
    return `
      <section class="card editor">
        ${this._renderEditBack()}
        <div class="card-header"><div><h2>Document YAML</h2><p>Bulk edit the complete storage document. Validate before saving.</p></div><button class="secondary small" data-action="new-rule">Back to Visual</button></div>
        <textarea class="yaml-editor" data-focus-key="document-yaml" spellcheck="false">${escapeHtml(this._contents)}</textarea>
      </section>
    `;
  }

  _renderEditBack() {
    const destination = this._selectedRuleId && this._selectedRuleId !== "__new__" ? "detail" : "overview";
    return `<div class="edit-back"><button class="secondary" data-action="leave-edit" data-destination="${destination}">Back to ${destination}</button>${this._dirty ? `<span>Draft preserved</span>` : ""}</div>`;
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
    if (!this._preview) return `<div class="muted">Review Changes evaluates consequences without applying services. Dry-run evaluates desired targets without applying services.</div>`;
    const summary = summarizePreview(this._preview);
    const facts = [`${summary.changing} changing`, `${summary.unchanged} already aligned`, `${summary.effects} effect${summary.effects === 1 ? "" : "s"}`];
    if (summary.withdrawals != null) facts.push(`${summary.withdrawals} withdrawal${summary.withdrawals === 1 ? "" : "s"}`);
    return `<div class="consequences"><strong>${summary.headline}</strong><p>${facts.join(" · ")}</p>${summary.errors.length ? `<div role="alert" class="error-box">${summary.errors.map(escapeHtml).join("<br>")}</div>` : ""}</div><details><summary>Raw preview JSON</summary><pre>${escapeHtml(JSON.stringify(this._preview, null, 2))}</pre></details>`;
  }

  _renderSimulation() {
    return `
      <label class="field"><span>Simulation timeline JSON</span><textarea class="small-textarea mono" data-timeline data-focus-key="simulation-timeline">${escapeHtml(this._timelineText)}</textarea></label>
      <button class="secondary small" data-action="simulate">Run Simulation</button>
      ${this._simulation ? `${renderSimulationSummary(this._simulation)}<details><summary>Raw simulation JSON</summary><pre>${escapeHtml(JSON.stringify(this._simulation, null, 2))}</pre></details>` : `<div class="muted">Use simulation for after/hold timing before installing on the live instance.</div>`}
    `;
  }

  _renderHistory() {
    if (!this._history.length) return `<div class="empty">No history yet.</div>`;
    const items = this._history.slice(0, 8).map((item) => `
       <div class="history-item"><div><strong>${escapeHtml(formatTimestamp(item.timestamp || item.created_at))}</strong><span>${escapeHtml(item.reason || "Saved document")} · ${escapeHtml(item.rule_count ?? "?")} Rules · generation ${escapeHtml((item.generation || "").slice(0, 12))}</span></div><button class="secondary small" data-rollback="${escapeHtml(item.generation)}">Review rollback</button></div>
    `).join("");
    return `${items}${this._rollbackReview ? renderRollbackReview(this._rollbackReview) : ""}`;
  }

  _renderMigration() {
    const migration = this._migration;
    const proposal = migration.proposal;
    return `<section class="card migration">
      <div class="card-header"><div><h2>Migrate HA automation</h2><p>Inspect a strict supported subset and add proposed Rules to this editor.</p></div><button class="secondary small" data-action="migration-discover">Discover</button></div>
      <div class="warning-box"><strong>Source automation stays unchanged.</strong><p>Intentional never disables, edits, or calls the source automation. Review overlap before saving.</p></div>
      ${migration.loading ? `<div role="status">${escapeHtml(capitalize(migration.loading))}ing…</div>` : ""}${migration.automations.length ? `<label class="field"><span>Loaded automation</span><select data-migration-select data-focus-key="migration-select" name="migration-select"><option value="">Select…</option>${migration.automations.map((item) => `<option value="${escapeHtml(item.entity_id)}" ${migration.selected === item.entity_id ? "selected" : ""}>${escapeHtml(item.alias || item.entity_id)}</option>`).join("")}</select></label>` : `<div class="muted">Discover loaded automations to begin.</div>`}
      ${migration.inspection ? `<div class="muted">${migration.inspection.supported ? "Supported candidate" : "Unsupported"}. ${(migration.inspection.diagnostics || []).map((item) => escapeHtml(item.message)).join(" ")}</div><button class="secondary small" data-action="migration-propose" ${migration.inspection.supported ? "" : "disabled"}>Propose</button>` : ""}
      ${proposal ? `<pre>${escapeHtml(proposal.yaml || JSON.stringify(proposal.diagnostics, null, 2))}</pre><button data-action="migration-add" ${proposal.supported && proposal.merged_validation?.valid ? "" : "disabled"}>Add to editor</button>` : ""}
    </section>`;
  }

  _render() {
    if (!this.shadowRoot) return;
    const viewState = captureViewState(this.shadowRoot);
    this._renderRuleViewModels = buildRuleViewModels(this._uniqueRules(), this._world || {}, this._hass?.states || {});
    const version = this._health?.version ? `v${this._health.version}` : "";
    const generation = this._document?.generation ? this._document.generation.slice(0, 12) : "not loaded";
    this.shadowRoot.innerHTML = `
      <style>${styles}</style>
         <main class="${this._narrow ? "narrow" : ""}" aria-busy="${this._busy || this._alertsBusy || this._policyBusy}">
         <header>
           <div><h1>Intentional</h1><p>${escapeHtml(version)} · generation ${escapeHtml(generation)}</p></div>
           <div class="actions"><button class="secondary" data-action="reload" ${this._busy ? "disabled" : ""}>Reload</button></div>
          </header>
          <nav class="workspace-tabs" aria-label="Intentional workspaces">
            <button class="${this._workspace === "intent" ? "selected" : ""}" data-action="switch-workspace" data-workspace="intent" ${this._workspace === "intent" ? 'aria-current="page"' : ""}>Intent</button>
            <button class="${this._workspace === "alert" ? "selected" : ""}" data-action="switch-workspace" data-workspace="alert" ${this._workspace === "alert" ? 'aria-current="page"' : ""}>Alert</button>
            <button class="${this._workspace === "policy" ? "selected" : ""}" data-action="switch-workspace" data-workspace="policy" ${this._workspace === "policy" ? 'aria-current="page"' : ""}>Alerting Policy</button>
          </nav>
          <div class="sr-status" aria-live="polite">${escapeHtml(this._workspace === "policy" ? this._policyStage : this._stage)}${this._busy || this._alertsBusy || this._policyBusy ? ", loading" : ""}</div>${this._workspace === "intent" && this._error ? `<div class="banner" role="alert">${escapeHtml(this._error)}</div>` : ""}
          <datalist id="entity-list">${this._entityOptions()}</datalist>
          ${this._workspace === "alert" ? this._renderAlertWorkspace() : this._workspace === "policy" ? this._renderPolicyWorkspace() : this._screen === "overview" ? this._renderOverview() : this._screen === "detail" ? `<section class="two-pane"><aside class="card rules">${this._renderRules()}</aside>${this._renderRuleDetail()}</section>` : `<section class="two-pane edit-layout"><aside class="card rules">${this._renderRules()}</aside><div>${this._renderEditor()}<details class="card inspector"><summary>Checks, preview, simulation &amp; history</summary><h2>Validation</h2>${this._renderValidation()}<h2>Preview</h2>${this._renderPreview()}<h2>Simulation</h2>${this._renderSimulation()}<h2>History</h2>${this._renderHistory()}</details></div></section>`}
          ${this._workspace === "intent" && this._screen === "edit" ? `<nav class="publish-bar" aria-label="Draft workflow"><span>${escapeHtml(this._stage)}</span><button class="secondary" data-action="validate" ${this._busy ? "disabled" : ""}>Check</button><button class="secondary" data-action="review" ${this._busy || this._stage === "Draft" ? "disabled" : ""}>Review Changes</button><button data-action="save" ${this._busy || !this._dirty || this._stage !== "Reviewed" || this._reviewedFingerprint !== candidateFingerprint(this._candidateContents()) ? "disabled" : ""}>Publish</button></nav>` : ""}
       </main>
    `;
    this._bindEvents();
    restoreViewState(this.shadowRoot, viewState);
  }

  _bindEvents() {
    this.shadowRoot.querySelector("textarea.yaml-editor")?.addEventListener("input", (event) => {
      if (this._editorMode === "document") this._contents = event.target.value;
      else this._selectedRuleContents = event.target.value;
      this._markDirty();
    });
    this.shadowRoot.querySelector("[data-timeline]")?.addEventListener("input", (event) => { this._timelineText = event.target.value; });
    this.shadowRoot.querySelector("[data-policy-yaml]")?.addEventListener("input", (event) => {
      this._policyContents = event.target.value;
      this._policyStage = "Draft";
      this._policyCheckedFingerprint = "";
      this._policyReviewedFingerprint = "";
      this._policyCheck = null;
      this._policyPreview = null;
      this._render();
    });
    this.shadowRoot.querySelector("[data-policy-alerts]")?.addEventListener("input", (event) => {
      this._policySyntheticText = event.target.value;
      if (this._policyStage === "Reviewed") this._policyStage = "Checked";
      this._policyReviewedFingerprint = "";
      this._policyPreview = null;
      this._render();
    });
    this.shadowRoot.querySelector("[data-form-root]")?.addEventListener("input", (event) => this._onFormInput(event));
    this.shadowRoot.querySelector("[data-form-root]")?.addEventListener("change", (event) => this._onFormInput(event));
    this.shadowRoot.querySelectorAll("[data-action]").forEach((button) => button.addEventListener("click", () => this._handleAction(button)));
    this.shadowRoot.querySelectorAll("[data-rule-id]").forEach((button) => button.addEventListener("click", () => this._selectRule(button.dataset.ruleId)));
    this.shadowRoot.querySelectorAll("[data-rollback]").forEach((button) => button.addEventListener("click", () => this._reviewRollback(button.dataset.rollback)));
    this.shadowRoot.querySelector("[data-migration-select]")?.addEventListener("change", (event) => this._inspectMigration(event.target.value));
  }

  _onFormInput(event) {
    const form = this._selectedRuleForm;
    if (!form) return;
    const target = event.target;
    this._formEdited = true;
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
    this._previewRequest += 1;
    this._dirty = true;
    this._validation = null;
    this._preview = null;
    this._simulation = null;
    this._stage = "Draft";
    this._reviewedFingerprint = "";
    this._queueValidate();
    const save = this.shadowRoot.querySelector('[data-action="save"]');
    if (save) save.disabled = true;
  }

  _handleAction(button) {
    const action = button.dataset.action;
    if (action === "switch-workspace") {
      this._workspace = button.dataset.workspace;
      this._render();
      return;
    }
    if (action === "open-alert-rule") {
      this._workspace = "intent";
      this._selectRule(button.dataset.ruleId);
      return;
    }
    if (action === "reload" && (!this._dirty || confirm("Discard unsaved editor changes and reload?"))) this._load();
    if (action === "reload-alerts") this._loadAlerts();
    if (action === "reload-policy") this._loadPolicy();
    if (action === "policy-check") this._checkPolicy();
    if (action === "policy-review") this._reviewPolicy();
    if (action === "policy-publish") this._publishPolicy();
    if (action === "save") this._save();
    if (action === "validate") this._validate();
    if (action === "review") this._review();
    if (action === "simulate") this._simulate();
    if (action === "migration-discover") this._discoverMigrations();
    if (action === "migration-propose") this._proposeMigration();
    if (action === "migration-add") this._addMigrationProposal();
    if (action === "rollback-apply") this._applyRollback();
    if (action === "new-rule") this._newRule();
    if (action === "show-document") this._showDocument();
    if (action === "show-yaml-rule") this._showYamlRule();
    if (action === "show-visual-rule") this._showVisualRule();
    if (action === "leave-edit" && (!this._dirty || confirm("Leave the editor? Your unsaved draft will be preserved."))) { this._screen = button.dataset.destination; this._render(); }
    if (action === "back") { this._screen = "overview"; this._render(); }
    if (action === "edit-rule") { this._screen = "edit"; this._render(); }
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
    this._formEdited = true;
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
      block,
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
  if (String(block || "").split("\n").some(hasUnquotedInlineComment)) return "Visual mode is unavailable to prevent data loss: YAML scalar inline comments cannot be preserved by the visual editor. Edit this rule as YAML.";
  return "";
}

function hasUnquotedInlineComment(line) {
  if (/^\s*#/.test(line)) return false;
  let quote = "";
  let escaped = false;
  for (let index = 0; index < line.length; index += 1) {
    const character = line[index];
    if (quote) {
      if (quote === '"' && character === "\\" && !escaped) { escaped = true; continue; }
      if (character === quote && !escaped) quote = "";
      escaped = false;
      continue;
    }
    if (character === "'" || character === '"') { quote = character; continue; }
    if (character === "#" && /\s/.test(line[index - 1] || "")) return true;
  }
  return false;
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
  return `<label class="field"><span>${escapeHtml(label)}</span><input data-field="${field}" data-focus-key="field-${field}" name="${field}" value="${escapeHtml(value)}" placeholder="${escapeHtml(placeholder)}">${help ? `<small>${escapeHtml(help)}</small>` : ""}</label>`;
}

function intentInputField(label, field, value, placeholder = "", help = "", index = 0) {
  return `<label class="field"><span>${escapeHtml(label)}</span><input data-intent-field="${field}" data-focus-key="intent-${index}-${field}" name="${field}" value="${escapeHtml(value)}" placeholder="${escapeHtml(placeholder)}">${help ? `<small>${escapeHtml(help)}</small>` : ""}</label>`;
}

function selectField(label, field, value, options) {
  return `<label class="field"><span>${escapeHtml(label)}</span><select data-field="${field}" data-focus-key="field-${field}" name="${field}">${options.map(([optionValue, text]) => `<option value="${optionValue}" ${String(value) === optionValue ? "selected" : ""}>${escapeHtml(text)}</option>`).join("")}</select></label>`;
}

function selectInline(label, field, value, options) {
  return `<label class="inline-select"><span>${escapeHtml(label)}</span><select data-field="${field}">${options.map(([optionValue, text]) => `<option value="${optionValue}" ${String(value) === optionValue ? "selected" : ""}>${escapeHtml(text)}</option>`).join("")}</select></label>`;
}

function operatorSelect(attribute, selected) {
  const operators = ["is", "is_not", "lt", "lte", "gt", "gte", "in", "not_in", "contains", "exists"];
  return `<select ${attribute}>${operators.map((op) => `<option value="${op}" ${op === selected ? "selected" : ""}>${op}</option>`).join("")}</select>`;
}

function fieldOperatorSelect(selected, focusKey = "") {
  const operators = [["value", "Set"], ["min", "Minimum"], ["max", "Maximum"], ["offset", "Offset"], ["multiply", "Multiply"]];
  return `<select data-intent-field-row="operator"${focusKey ? ` data-focus-key="${focusKey}"` : ""}>${operators.map(([op, label]) => `<option value="${op}" ${op === selected ? "selected" : ""}>${label}</option>`).join("")}</select>`;
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

function classifyRulePhase(rule) {
  if (rule.enabled === false || rule.paused) return { section: "disabled", text: rule.paused ? "Paused" : "Disabled" };
  if (rule.error || rule.blocked_by?.length || rule.drift || rule.projectionIssue) return { section: "attention", text: rule.drift ? "Needs alignment" : rule.blocked_by?.length ? "Blocked" : "Needs attention" };
  if (rule.active || rule.active_intent_count || ["active", "held", "lingering"].includes(rule.phase)) return { section: "active", text: rule.phase === "held" || rule.phase === "lingering" ? "Keeping desired state" : "Active now" };
  if (rule.phase === "pending" || rule.for_remaining_ms != null) return { section: "waiting", text: "Waiting for conditions to settle" };
  return { section: "waiting", text: "Waiting for its situation" };
}

function describeCondition(rule) {
  const form = rule?.form || rule;
  const conditions = form?.conditions || [];
  if (!conditions.length) return "Whenever this Rule is enabled";
  const joiner = form.conditionMode === "any" ? " or " : " and ";
  return conditions.map((item) => `${item.entity} ${item.operator === "is" ? "is" : item.operator} ${item.value}`).join(joiner);
}

function describeIntent(rule) {
  const intents = rule?.form?.intents || rule?.intents || [];
  if (!intents.length) return "run its Effects";
  return intents.map((intent) => `${intent.target} should ${intent.fields.map((field) => `${field.name} ${field.operator === "value" ? "be" : field.operator} ${field.value}`).join(" and ")}`).join("; ");
}

function describeRule(rule) {
  return `${describeCondition(rule)}, keep ${describeIntent(rule)}.`;
}

function buildRuleViewModels(rules, world, states) {
  const statuses = new Map((world.authored_rules || []).map((item) => [item.rule_id || item.id, item]));
  const records = world.desired_records || [];
  const projections = world.targets || [];
  const recordsByTarget = new Map(records.map((item) => [item.target, item]));
  const projectionsByTarget = new Map(projections.map((item) => [item.target, item]));
  return rules.map((rule) => {
    const form = parseRuleForm(extractRuleBlock(rule.block || "", rule.id) || rule.block || "", rule);
    const status = statuses.get(rule.id) || {};
    const targetIds = [...new Set([...(status.targets || []), ...form.intents.map((item) => item.target).filter(Boolean)])];
    const targets = targetIds.map((id) => {
      const candidateRecord = recordsByTarget.get(id) || {};
      const record = !candidateRecord.rule_id || candidateRecord.rule_id === rule.id ? candidateRecord : {};
      const projection = projectionsByTarget.get(id) || {};
      const actual = record.actual || projection.actual || states[id]?.state;
      const desired = projection.desired || record.desired || Object.fromEntries((form.intents.find((item) => item.target === id)?.fields || []).map((field) => [field.name, field.value]));
      return { id, title: states[id]?.attributes?.friendly_name || id, desired, actual, aligned: projection.plan_match === "match" };
    });
    const targetProjections = targetIds.map((id) => projectionsByTarget.get(id)).filter(Boolean);
    const projectionIssue = targetProjections.some(hasProjectionIssue);
    const phase = classifyRulePhase({ ...rule, ...status, projectionIssue });
    return { ...rule, ...status, form, title: rule.reason || rule.id, reason: rule.reason || status.reason || "", group: rule.group || status.group || form.group, profile: rule.profile || status.profile || form.profile, targets, targetCount: targetIds.length, competing: targetProjections.flatMap((item) => item.active_intents || item.rules || []).filter((item) => item.rule_id && item.rule_id !== rule.id), history: targetProjections.flatMap((item) => item.recent_attempts || item.attempts || []).filter((item) => !item.rule_id || item.rule_id === rule.id).slice(0, 5), section: phase.section, phaseText: phase.text };
  });
}

function summarizePreview(result) {
  const targets = result?.preview || result?.targets || [];
  const nowChanges = targets.filter((item) => Object.values(item.changes || {}).some((change) => change?.changed === true));
  const nowTargets = new Set(nowChanges.map((item) => item.target).filter(Boolean));
  const laterTargets = new Set();
  for (const phase of result?.phases || []) {
    if (Number(phase.horizon_ms || 0) <= 0) continue;
    for (const plan of phase.service_plans || []) {
      const target = plan.target || plan.data?.entity_id || plan.data?.target?.entity_id;
      for (const id of Array.isArray(target) ? target : [target]) if (id && !nowTargets.has(id)) laterTargets.add(id);
    }
  }
  const changing = nowChanges.length + laterTargets.size;
  const effectKeys = new Set();
  for (const effect of result?.effects || []) effectKeys.add(JSON.stringify(effect));
  for (const phase of result?.phases || []) for (const effect of phase.effects || []) effectKeys.add(JSON.stringify(effect));
  const effects = effectKeys.size;
  const withdrawalEvents = result?.withdrawals || result?.reconciliation?.withdrawals || result?.events?.filter((item) => item.type === "withdrawal");
  const withdrawals = withdrawalEvents ? withdrawalEvents.length : null;
  const errors = result?.errors || [];
  const unchanged = targets.filter((item) => item.target ? !nowTargets.has(item.target) && !laterTargets.has(item.target) : !Object.values(item.changes || {}).some((change) => change?.changed === true)).length;
  const timing = nowChanges.length && laterTargets.size ? `${nowChanges.length} now, ${laterTargets.size} later` : laterTargets.size ? `${laterTargets.size} later` : nowChanges.length ? `${nowChanges.length} now` : "";
  return { changing, nowChanging: nowChanges.length, laterChanging: laterTargets.size, unchanged, effects, withdrawals, errors, headline: errors.length ? "Review found problems" : changing ? `${changing} target${changing === 1 ? "" : "s"} would change${timing ? ` (${timing})` : ""}` : "No target changes needed" };
}

function hasProjectionIssue(projection) {
  const reconciliation = projection?.reconciliation || {};
  return projection?.plan_match === "mismatch" || Boolean(projection?.policy_denial || projection?.retry || projection?.drift || projection?.manual_override || reconciliation.policy_denial || reconciliation.retry || reconciliation.drift || reconciliation.manual_override);
}

function renderRollbackReview(snapshot) {
  const generation = snapshot.generation || "";
  const source = snapshot.contents || snapshot.snapshot || snapshot.source || "";
  return `<section class="rollback-review"><h3>Rollback review</h3><p>${escapeHtml(snapshot.reason || "Saved document")} · generation ${escapeHtml(generation.slice(0, 12))}</p><pre>${escapeHtml(source || JSON.stringify(snapshot, null, 2))}</pre><button data-action="rollback-apply">Apply this rollback</button></section>`;
}

function draftStageTransition(stage, event) {
  if (event === "edit") return "Draft";
  if (event === "validate-valid") return "Checked";
  if (event === "review") return stage === "Checked" || stage === "Reviewed" ? "Reviewed" : stage;
  if (event === "publish") return stage === "Reviewed" ? "Published" : stage;
  return stage;
}

function candidateFingerprint(contents) { let hash = 2166136261; for (const char of String(contents)) { hash ^= char.charCodeAt(0); hash = Math.imul(hash, 16777619); } return (hash >>> 0).toString(16); }
function formatValue(value) { return value && typeof value === "object" ? Object.entries(value).map(([key, item]) => `${key}: ${typeof item === "object" ? JSON.stringify(item) : item}`).join(", ") : String(value ?? "unknown"); }
function formatTimestamp(value) { if (!value) return "Previous version"; const date = new Date(value); return Number.isNaN(date.valueOf()) ? String(value) : date.toLocaleString(); }
function renderSimulationSummary(result) { const steps = result?.steps || []; const active = steps.reduce((count, step) => count + (step.active_rules?.length || 0), 0); return `<div class="consequences"><strong>${steps.length} timeline step${steps.length === 1 ? "" : "s"}</strong><p>${active} active Rule observations across the timeline.</p></div>`; }

function sortAlerts(alerts) {
  const stateRank = { firing: 0, pending: 1, inactive: 2 };
  const severityRank = { critical: 0, warning: 1, info: 2 };
  return [...(alerts || [])].sort((left, right) => {
    const ranked = (stateRank[left?.state] ?? 3) - (stateRank[right?.state] ?? 3)
      || (severityRank[left?.severity] ?? 3) - (severityRank[right?.severity] ?? 3);
    if (ranked) return ranked;
    for (const key of ["rule_id", "name", "instance_id", "summary", "evaluation_status"]) {
      const a = String(left?.[key] ?? "");
      const b = String(right?.[key] ?? "");
      if (a < b) return -1;
      if (a > b) return 1;
    }
    return 0;
  });
}

function renderPolicyPreview(preview) {
  if (!preview) return `<div class="muted">Run Review synthetic preview after the candidate is checked.</div>`;
  if (preview.valid === false || preview.errors?.length) return `<div class="error-box" role="alert">${(preview.errors || ["Policy simulation failed"]).map(escapeHtml).join("<br>")}</div>`;
  const alerts = preview.alerts || [];
  const generation = preview.candidate_generation ? `<p>Candidate generation ${escapeHtml(preview.candidate_generation)}</p>` : "";
  const generalWarnings = (preview.warnings || []).map((warning) => `<li>${escapeHtml(formatValue(warning))}</li>`).join("");
  if (!alerts.length) return `<div class="ok">Synthetic preview is valid. No synthetic Alerts were supplied.</div>${generation}${generalWarnings ? `<h3>Warnings</h3><ul class="attention">${generalWarnings}</ul>` : ""}`;
  return `${generation}${generalWarnings ? `<h3>Warnings</h3><ul class="attention">${generalWarnings}</ul>` : ""}<div class="policy-results">${alerts.map((item, index) => {
    const labels = Object.entries(item.labels || {}).map(([key, value]) => `<span><strong>${escapeHtml(key)}</strong>=${escapeHtml(value)}</span>`).join("");
    const routes = (item.routes || []).map((route) => `<div class="policy-route"><strong>${escapeHtml(route.route_id || "route")} &rarr; ${escapeHtml(route.receiver || "no receiver")}</strong><span>Group keys: ${escapeHtml((route.group_by || []).join(", ") || "none")}</span><span>Group key: ${escapeHtml(formatValue(route.group_key ?? "none"))}</span>${route.suppression ? `<span class="attention">Suppression: ${escapeHtml(formatValue(route.suppression))}</span>` : `<span>Suppression: none</span>`}</div>`).join("") || `<div class="muted">No routes matched.</div>`;
    const fanout = (item.fanout || []).map((entry) => `<li><strong>${escapeHtml(entry.receiver || "receiver")}</strong> via ${escapeHtml(formatValue(entry.destination || "unknown destination"))}</li>`).join("") || `<li>No Receiver destinations.</li>`;
    const warnings = (item.warnings || []).map((warning) => `<li>${escapeHtml(formatValue(warning))}</li>`).join("");
    return `<article class="policy-result"><h3>Synthetic Alert ${index + 1}</h3><div class="policy-labels">${labels || `<span>No labels</span>`}</div><h3>Routes</h3>${routes}<h3>Fanout</h3><ul>${fanout}</ul><h3>Warnings</h3>${warnings ? `<ul class="attention">${warnings}</ul>` : `<p>None</p>`}</article>`;
  }).join("")}</div>`;
}

function captureViewState(root) {
  const active = root?.activeElement;
  const key = active?.dataset?.focusKey || active?.getAttribute?.("name") || active?.id;
  const path = key ? `[data-focus-key="${selectorEscape(key)}"], [name="${selectorEscape(key)}"], #${selectorEscape(key)}` : active?.dataset?.action ? `[data-action="${selectorEscape(active.dataset.action)}"]` : active?.classList?.contains("yaml-editor") ? ".yaml-editor" : "";
  return { path, start: active?.selectionStart, end: active?.selectionEnd, scrollTop: active?.scrollTop, scrollLeft: active?.scrollLeft, pageScroll: root?.querySelector("main")?.scrollTop || 0, open: [...(root?.querySelectorAll("details[open]") || [])].map((item) => item.dataset.focusKey || [...root.querySelectorAll("details")].indexOf(item)) };
}
function restoreViewState(root, state) { if (!state) return; (state.open || []).forEach((key) => (typeof key === "number" ? root.querySelectorAll("details")[key] : root.querySelector(`[data-focus-key="${selectorEscape(key)}"]`))?.setAttribute("open", "")); const active = state.path && root.querySelector(state.path); if (active) { active.focus(); if (state.start != null && active.setSelectionRange) active.setSelectionRange(state.start, state.end); active.scrollTop = state.scrollTop || 0; active.scrollLeft = state.scrollLeft || 0; } const main = root.querySelector("main"); if (main) main.scrollTop = state.pageScroll || 0; }
function selectorEscape(value) { return globalThis.CSS?.escape ? globalThis.CSS.escape(value) : String(value).replace(/[^a-zA-Z0-9_-]/g, "\\$&"); }

const styles = `
  :host { display: block; color: var(--primary-text-color); background: var(--primary-background-color); }
  main { padding: 24px; box-sizing: border-box; min-height: 100vh; }
  header { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; margin-bottom: 18px; }
  h1, h2, h3 { margin: 0; font-weight: 600; }
  h1 { font-size: 28px; } h2 { font-size: 18px; } h3 { font-size: 16px; }
  p { color: var(--secondary-text-color); margin: 6px 0 0; }
  code { background: var(--secondary-background-color); border-radius: 5px; padding: 1px 5px; }
  button, select, input, textarea { font: inherit; }
  button { border: 0; border-radius: 10px; padding: 10px 14px; min-height: 44px; background: var(--primary-color); color: var(--text-primary-color); cursor: pointer; }
  button:focus-visible, input:focus-visible, select:focus-visible, textarea:focus-visible, summary:focus-visible { outline: 3px solid var(--primary-color); outline-offset: 2px; }
  button:disabled { opacity: .45; cursor: default; }
  button.secondary { background: var(--secondary-background-color); color: var(--primary-text-color); }
  button.small { padding: 6px 10px; font-size: 13px; }
  button.icon { min-width: 38px; padding: 8px 10px; font-size: 18px; }
  .actions, .mode-switch { display: flex; gap: 8px; flex-wrap: wrap; }
  .workspace-tabs { display: flex; gap: 6px; margin: 0 0 22px; border-bottom: 1px solid var(--divider-color); overflow-x: auto; }
  .workspace-tabs button { flex: 0 0 auto; border-radius: 10px 10px 0 0; background: transparent; color: var(--secondary-text-color); }
  .workspace-tabs button.selected { color: var(--primary-color); box-shadow: inset 0 -3px var(--primary-color); }
  .two-pane { display: grid; grid-template-columns: minmax(260px, 340px) minmax(0, 1fr); gap: 16px; align-items: start; }
  .narrow .two-pane { grid-template-columns: 1fr; } .narrow header { flex-direction: column; }
  .card { background: var(--card-background-color); border-radius: 16px; box-shadow: var(--ha-card-box-shadow, 0 2px 8px rgba(0,0,0,.15)); padding: 16px; }
  .card-header, .section-title, .hero-row, .subcard-head, .history-item { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
  .rules, .inspector, .editor-stack { display: flex; flex-direction: column; gap: 12px; }
  .rule { text-align: left; background: var(--secondary-background-color); color: var(--primary-text-color); display: flex; flex-direction: column; gap: 3px; }
  .rule.selected { outline: 2px solid var(--primary-color); background: color-mix(in srgb, var(--primary-color) 12%, var(--secondary-background-color)); }
  .rule-title { font-weight: 600; } .rule-meta, .rule-reason, .muted, .empty, small { color: var(--secondary-text-color); font-size: 13px; }
  .hero-card { border-top: 4px solid var(--primary-color); }
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
  .overview { max-width: 920px; margin: auto; } .ledger-head { border-left: 5px solid var(--success-color, #43a047); padding: 8px 18px; margin-bottom: 22px; }
  .workspace { max-width: 1000px; margin: auto; display: flex; flex-direction: column; gap: 14px; }
  .alert-head { border-left-color: var(--warning-color, #ffa000); }
  .policy-head { border-left-color: var(--primary-color); }
  .alert-ledger { display: flex; flex-direction: column; gap: 10px; }
  .alert-row { background: var(--card-background-color); border-radius: 14px; border-left: 5px solid var(--divider-color); box-shadow: var(--ha-card-box-shadow, 0 2px 8px rgba(0,0,0,.15)); padding: 16px; }
  .alert-row.firing { border-left-color: var(--error-color); } .alert-row.pending { border-left-color: var(--warning-color, #ffa000); }
  .alert-row.stale { outline: 2px dashed var(--warning-color, #ffa000); outline-offset: -2px; }
  .alert-row-head, .alert-badges, .alert-meta, .policy-labels { display: flex; align-items: center; justify-content: space-between; gap: 8px; flex-wrap: wrap; }
  .alert-badges, .alert-meta, .policy-labels { justify-content: flex-start; }
  .alert-meta { margin-top: 12px; color: var(--secondary-text-color); font-size: 13px; }
  .state-badge, .stale-badge, .stage-badge { border-radius: 999px; padding: 5px 9px; background: var(--secondary-background-color); font-size: 12px; font-weight: 700; }
  .stale-badge { background: color-mix(in srgb, var(--warning-color, #ffa000) 25%, transparent); }
  .link-button { min-height: 0; padding: 0; border-radius: 0; background: transparent; color: var(--primary-color); text-decoration: underline; }
  .policy-workspace { padding-bottom: 70px; } .policy-layout { grid-template-columns: minmax(300px, 1fr) minmax(320px, 1fr); }
  .policy-yaml { min-height: 62vh; } .synthetic-alerts { min-height: 120px; }
  .policy-results, .policy-result { display: flex; flex-direction: column; gap: 12px; } .policy-result { padding-top: 14px; border-top: 1px solid var(--divider-color); }
  .policy-route { display: flex; flex-direction: column; gap: 4px; padding: 10px; border-radius: 10px; background: var(--secondary-background-color); font-size: 13px; }
  .ledger { display: grid; gap: 22px; } .ledger-section h2 { display: flex; gap: 8px; align-items: center; margin-bottom: 8px; } .ledger-section h2 span { font-size: 12px; color: var(--secondary-text-color); }
  .ledger-section .rule { width: 100%; margin-bottom: 7px; border-left: 4px solid var(--divider-color); } .ledger-section:first-child .rule { border-left-color: var(--warning-color, #ffa000); }
  .phase { font-size: 12px; color: var(--primary-color); } .create { margin-top: 28px; } summary { min-height: 44px; display: flex; align-items: center; cursor: pointer; }
  .detail { max-width: 860px; } .detail > section { padding: 18px 0; border-top: 1px solid var(--divider-color); } .summary { font-size: 18px; line-height: 1.5; margin-bottom: 22px; }
  .target-line { display: flex; justify-content: space-between; gap: 20px; padding: 12px 0; } .target-line small, .target-line span { display: block; } .aligned { color: var(--success-color, #43a047); } .attention { color: var(--warning-color, #b26a00); }
  .sticky-top { display: none; } .publish-bar { position: sticky; bottom: 0; z-index: 3; margin-top: 16px; padding: 10px; display: flex; justify-content: flex-end; align-items: center; gap: 8px; background: var(--card-background-color); border-top: 1px solid var(--divider-color); }
  .edit-back { display: none; }
  .inline-select { display: flex; flex-direction: column; gap: 3px; } .inline-select span, .sr-status { font-size: 12px; color: var(--secondary-text-color); } .sr-status { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); }
  @media (max-width: 700px) { main { padding: 10px; padding-bottom: 72px; } .two-pane, .form-grid, .row { grid-template-columns: 1fr; } .two-pane > .rules { display: none; } .card-header, .section-title, .hero-row, header, .target-line, .alert-row-head { flex-direction: column; align-items: stretch; } .sticky-top, .edit-back { position: sticky; top: 0; z-index: 2; display: flex; align-items: center; gap: 10px; padding: 8px; background: var(--card-background-color); } .edit-back span { color: var(--secondary-text-color); font-size: 12px; } .publish-bar { position: fixed; left: 0; right: 0; margin: 0; } .publish-bar span { display: none; } .publish-bar button { flex: 1; } }
`;

customElements.define("intentional-panel", IntentionalPanel);
