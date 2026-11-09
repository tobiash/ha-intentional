/* Intentional's status-first Lovelace card. No external runtime dependencies. */
const WORDS = {
  en: {
    automation: "Automation", loading: "Loading…", unavailable: "Unavailable", stale: "Status unavailable — last snapshot is stale",
    global_off: "Automation globally off", degraded: "Runtime needs attention", rules: "rules", intents: "active intents", manual: "manual overrides",
    empty: "No rules in this scope", settings: "Settings", back: "Back to status", close: "Close", refresh: "Refresh",
    scope: "Scope", all: "All", shared: "Shared rule", local: "Within this scope", outside: "Also affects", non_target: "Includes effects beyond these targets",
    description: "Purpose", request: "Requested", live_request: "Live request", desired: "Composed demand", actual: "Observed", why: "Current explanation", targets: "Targets",
    condition: "Condition evaluates", yes: "true", no: "false", blocked: "Blocked", blocked_by: "Blocked by", waiting: "Waiting", idle: "Idle", inactive: "Inactive",
    active: "Active intent", held: "Retained", lingering: "Retained", disabled: "Disabled", paused: "Paused", mixed: "Mixed states",
    contributing: "Contributes to demand", competing: "Other intents take precedence", modifier_no_change: "Modifier present; no output change", modifier_unknown: "Modifier present; animated impact unknown",
    baseline: "Baseline fields", modifiers: "Modifier fields", changed: "Output fields changed without this rule", precedence: "Manual precedence",
    match: "Observed state matches demand", mismatch: "Observed state differs from demand", unknown: "Unknown", missing: "Entity missing",
    no_demand: "No current demand", comparison: "Observation comparison — not proof of an applied service call",
    recovery: "Manual recovery", timed: "Timed", until_vacant: "Until vacant", until_resumed: "Until resumed", remaining: "remaining",
    vacancy: "Vacancy sensor", after_resume: "Demand after removing this override", resume: "Resume automation", resume_all: "Resume listed overrides",
    resume_help: "Only these target overrides are cleared. Rule enablement is unchanged.", enable: "Enable rule", disable: "Disable rule",
    enable_help: "Enablement changes the entire rule, including targets outside this card.", admin: "Rule settings require an administrator.",
    open_panel: "Open in Intentional", failed: "Action failed", busy: "Working…", dispatch: "Dispatch evidence", retry: "Retry pending", policy: "Policy blocks dispatch",
    last_attempt: "Last service attempt", no_attempt: "No recorded service attempt", retained: "Intent remains after the condition stops",
    title: "Title", area: "Area", domain: "Domain", group: "Rule group", display: "Display", popup: "Compact launcher + local dialog", inline: "Expanded card / popup content",
    language: "Language", auto: "Automatic", refresh_seconds: "Refresh interval (5–60 seconds)", enabled: "Enabled", phase: "Lifecycle",
    names: "Rule names follow their Home Assistant switch names; descriptions come from the rules.", seconds: "s", no_description: "No rule description",
    state: "Power", on: "On", off: "Off", brightness_pct: "Brightness", brightness: "Brightness", cap: "At most", floor: "At least", offset: "Offset", multiply: "Multiply", set: "Request", manual_owner: "Manual/user intent owns", unconfirmed: "Change not confirmed; check the current status.", unavailable_targets: "targets unavailable", snapshot: "Snapshot",
  },
  de: {
    automation: "Automatik", loading: "Wird geladen…", unavailable: "Nicht verfügbar", stale: "Status nicht verfügbar — letzter Stand ist veraltet",
    global_off: "Automatik global ausgeschaltet", degraded: "Laufzeit prüfen", rules: "Regeln", intents: "aktive Intents", manual: "manuelle Eingriffe",
    empty: "Keine Regeln in diesem Bereich", settings: "Einstellungen", back: "Zurück zum Status", close: "Schließen", refresh: "Aktualisieren",
    scope: "Bereich", all: "Alle", shared: "Gemeinsame Regel", local: "Innerhalb dieses Bereichs", outside: "Wirkt außerdem auf", non_target: "Enthält weitere Effekte außerhalb dieser Ziele",
    description: "Zweck", request: "Angefordert", live_request: "Aktuelle Anforderung", desired: "Zusammengesetzter Sollwert", actual: "Beobachtet", why: "Aktuelle Erklärung", targets: "Ziele",
    condition: "Bedingung ergibt", yes: "wahr", no: "falsch", blocked: "Blockiert", blocked_by: "Blockiert durch", waiting: "Wartet", idle: "Inaktiv", inactive: "Inaktiv",
    active: "Aktiver Intent", held: "Nachlauf", lingering: "Nachlauf", disabled: "Deaktiviert", paused: "Pausiert", mixed: "Gemischter Status",
    contributing: "Trägt zum Sollwert bei", competing: "Andere Intents haben Vorrang", modifier_no_change: "Modifikator vorhanden; keine Wertänderung", modifier_unknown: "Modifikator vorhanden; animierte Wirkung unbekannt",
    baseline: "Basisfelder", modifiers: "Modifikatorfelder", changed: "Ohne diese Regel veränderte Felder", precedence: "Manueller Vorrang",
    match: "Beobachtung entspricht dem Sollwert", mismatch: "Beobachtung weicht vom Sollwert ab", unknown: "Unbekannt", missing: "Entität fehlt",
    no_demand: "Kein aktueller Sollwert", comparison: "Beobachtungsvergleich — kein Nachweis eines ausgeführten Dienstaufrufs",
    recovery: "Manuelle Steuerung freigeben", timed: "Zeitlich begrenzt", until_vacant: "Bis der Raum frei ist", until_resumed: "Bis zur Freigabe", remaining: "verbleibend",
    vacancy: "Anwesenheitssensor", after_resume: "Sollwert nach Entfernen dieses Eingriffs", resume: "Automatik fortsetzen", resume_all: "Aufgelistete Eingriffe freigeben",
    resume_help: "Nur die Eingriffe dieser Ziele werden freigegeben. Regeln bleiben unverändert aktiviert.", enable: "Regel aktivieren", disable: "Regel deaktivieren",
    enable_help: "Die Änderung gilt für die gesamte Regel, auch für Ziele außerhalb dieser Karte.", admin: "Regeleinstellungen benötigen Administratorrechte.",
    open_panel: "In Intentional öffnen", failed: "Aktion fehlgeschlagen", busy: "Wird ausgeführt…", dispatch: "Ausführungsnachweis", retry: "Erneuter Versuch ausstehend", policy: "Richtlinie verhindert Ausführung",
    last_attempt: "Letzter Dienstversuch", no_attempt: "Kein Dienstversuch aufgezeichnet", retained: "Intent bleibt nach Ende der Bedingung bestehen",
    title: "Titel", area: "Raum", domain: "Domäne", group: "Regelgruppe", display: "Anzeige", popup: "Kompakte Schaltfläche + lokaler Dialog", inline: "Erweiterte Karte / Popup-Inhalt",
    language: "Sprache", auto: "Automatisch", refresh_seconds: "Aktualisierung (5–60 Sekunden)", enabled: "Aktiviert", phase: "Lebenszyklus",
    names: "Regelnamen folgen ihren Home-Assistant-Schaltern; Beschreibungen stammen aus den Regeln.", seconds: "s", no_description: "Keine Regelbeschreibung",
    state: "Zustand", on: "An", off: "Aus", brightness_pct: "Helligkeit", brightness: "Helligkeit", cap: "Höchstens", floor: "Mindestens", offset: "Verschiebung", multiply: "Multiplikator", set: "Anforderung", manual_owner: "Manueller Intent steuert", unconfirmed: "Änderung nicht bestätigt; aktuellen Status prüfen.", unavailable_targets: "Ziele nicht verfügbar", snapshot: "Stand",
  },
};
const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"}[c]));
const textValue = value => value == null ? "—" : typeof value === "object" ? JSON.stringify(value) : String(value);
const format = value => esc(textValue(value));
const language = (config, hass) => (config?.language === "auto" || !config?.language ? hass?.language : config.language)?.startsWith("de") ? "de" : "en";
const CSS = `
  :host { display:block; color:var(--primary-text-color,#202124); font:var(--paper-font-body1_-_font-size,14px) var(--paper-font-body1_-_font-family,system-ui); }
  * { box-sizing:border-box; } button, summary, a { -webkit-tap-highlight-color:transparent; }
  button { font:inherit; cursor:pointer; border:1px solid var(--divider-color,#ddd); border-radius:10px; background:var(--card-background-color,#fff); color:var(--primary-text-color,#202124); padding:10px 14px; min-height:44px; }
  button:hover { background:var(--secondary-background-color,#eee); } button:disabled { opacity:.55; cursor:default; }
  button:focus-visible, summary:focus-visible, a:focus-visible { outline:3px solid var(--primary-color,#0288d1); outline-offset:2px; }
  .launcher { width:100%; display:flex; align-items:center; text-align:start; gap:14px; border:0; padding:18px; background:transparent; }
  .icon { font-size:26px; color:var(--primary-color,#0288d1); } .grow { flex:1; min-width:0; } strong,h2 { font-weight:600; } h2 { font-size:20px; margin:0; }
  small,.muted { color:var(--secondary-text-color,#666); } .sub { display:block; margin-top:4px; } .content { padding:16px; }
  .toolbar { display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin-bottom:14px; } .toolbar h2 { flex:1; }
  .scope { margin:0 0 16px; color:var(--secondary-text-color,#666); overflow-wrap:anywhere; }
  .pill { display:inline-block; padding:3px 8px; margin:2px 5px 2px 0; background:var(--secondary-background-color,#eee); border-radius:20px; font-size:12px; }
  .warn { color:var(--warning-color,#b36b00); } .error { color:var(--error-color,#b00020); margin:10px 0; } .note { font-size:13px; line-height:1.5; }
  details.rule, .target, .setting { border-top:1px solid var(--divider-color,#ddd); padding:14px 0; }
  summary { cursor:pointer; line-height:1.6; overflow-wrap:anywhere; } summary .purpose { display:block; color:var(--secondary-text-color,#666); margin-top:5px; }
  .rule-body { padding:8px 0 0 12px; } p { margin:8px 0; line-height:1.5; overflow-wrap:anywhere; } dl { margin:10px 0; display:grid; grid-template-columns:minmax(90px,1fr) minmax(0,2fr); gap:6px 12px; }
  dt { color:var(--secondary-text-color,#666); } dd { margin:0; overflow-wrap:anywhere; white-space:pre-wrap; } code { font-size:12px; overflow-wrap:anywhere; } a { color:var(--primary-color,#0288d1); }
  dialog { color:var(--primary-text-color,#202124); background:var(--card-background-color,#fff); border:1px solid var(--divider-color,#ddd); border-radius:18px; padding:0; width:min(680px,calc(100vw - 24px)); max-height:calc(100dvh - 32px); }
  dialog::backdrop { background:rgba(0,0,0,.45); } dialog .content { padding:20px; } .setting button { margin:8px 8px 0 0; }
  @media(max-width:450px) { dialog .content { padding:14px; } dl { grid-template-columns:1fr; gap:4px; } dd { margin-bottom:6px; } }
`;

class IntentionalCard extends HTMLElement {
  constructor() {
    super(); this.attachShadow({mode:"open"}); this._epoch = 0; this._expanded = new Set();
    this._visibility = () => { clearTimeout(this._timer); if (!document.hidden) void this._refresh(); };
    this.shadowRoot.innerHTML = `<style>${CSS}</style><ha-card><div id="overview"></div><div id="inline"></div></ha-card><dialog aria-label="Automation"><div class="content" id="dialog-content"></div></dialog>`;
    this._dialog = this.shadowRoot.querySelector("dialog");
    this._dialog.addEventListener("close", () => { this._settings = false; this._render(); this.shadowRoot.querySelector('[data-action="open"]')?.focus(); });
    this.shadowRoot.addEventListener("click", event => { const button = event.target.closest("[data-action]"); if (button) void this._action(button); });
    this.shadowRoot.addEventListener("toggle", event => {
      const key = event.target.dataset?.detail;
      if (key) event.target.open ? this._expanded.add(key) : this._expanded.delete(key);
    }, true);
  }
  setConfig(config) {
    if (!config || ![undefined,"popup","inline"].includes(config.display)) throw new Error("display must be popup or inline");
    for (const key of ["area","domain","group","title"]) if (config[key] != null && typeof config[key] !== "string") throw new Error(`${key} must be a string`);
    if (config.language && !["auto","en","de"].includes(config.language)) throw new Error("language must be auto, en or de");
    const seconds = config.refresh_seconds ?? 10;
    if (!Number.isFinite(seconds) || seconds < 5 || seconds > 60) throw new Error("refresh_seconds must be between 5 and 60");
    this._config = {...config, refresh_seconds:seconds}; this._epoch++; this._data = null; this._error = ""; this._settings = false; this._expanded.clear();
    this.shadowRoot.querySelector("#inline").replaceChildren(); this.shadowRoot.querySelector("#dialog-content").replaceChildren();
    clearTimeout(this._timer); this._render(); void this._refresh();
  }
  set hass(hass) { this._hass = hass; if (!this._data && !this._pending && !this._timer) void this._refresh(); }
  connectedCallback() { document.addEventListener("visibilitychange", this._visibility); void this._refresh(); }
  disconnectedCallback() { document.removeEventListener("visibilitychange", this._visibility); clearTimeout(this._timer); clearTimeout(this._watchdog); this._timer = null; this._epoch++; this._dialog.close(); }
  static getConfigElement() { return document.createElement("intentional-card-editor"); }
  static getStubConfig() { return {display:"popup", refresh_seconds:10}; }
  getCardSize() { return this._config?.display === "inline" ? 6 : 2; }
  getGridOptions() { return {columns:12, rows:this._config?.display === "inline" ? "auto" : 2, min_columns:6}; }
  _t(key) { return WORDS[language(this._config, this._hass)][key] || key; }
  _query() { const params = new URLSearchParams(); for (const key of ["area","domain","group"]) if (this._config[key]) params.set(key,this._config[key]); return params.toString(); }
  _value(value) {
    if (!value || typeof value !== "object") return format(value);
    return Object.entries(value).map(([field,v]) => `${esc(this._t(field))}: ${field === "state" ? esc(this._t(String(v))) : format(v)}${field === "brightness_pct" ? "%" : field === "brightness" ? "/255" : ""}`).join(" · ");
  }
  _requestText(request) { return Object.entries(request).map(([op,values]) => `${esc(this._t(op))}: ${this._value(values)}`).join("; "); }
  async _refresh() {
    if (!this.isConnected || !this._config || !this._hass || this._pending || document.hidden) return;
    clearTimeout(this._timer); this._timer = null; this._pending = true; const epoch = this._epoch;
    this._watchdog = setTimeout(() => { if (this.isConnected && this._pending) { this._error = this._t("stale"); this._render(); } }, 15000);
    try {
      this._request = this._hass.callApi("GET", `intentional/presentation?${this._query()}`);
      const data = await this._request;
      if (epoch !== this._epoch || !this.isConnected) return;
      if (data.schema_version !== 1 || !Array.isArray(data.rules) || !Array.isArray(data.targets)) throw new Error("Unsupported presentation response");
      this._data = data; this._error = "";
    } catch (_error) { if (epoch === this._epoch) this._error = this._t("stale"); }
    finally {
      clearTimeout(this._watchdog);
      this._pending = false;
      if (this.isConnected) { this._render(); this._timer = setTimeout(() => { this._timer = null; void this._refresh(); }, epoch === this._epoch ? this._config.refresh_seconds * 1000 : 0); }
    }
  }
  _summary() {
    if (this._error) return this._t("unavailable");
    if (!this._data) return this._t("loading");
    if (this._data.runtime_status !== "ok") return this._t("degraded");
    if (!this._data.global_enabled) return this._t("global_off");
    if (!this._data.rules.length && !this._data.targets.length) return this._t("empty");
    const unavailable = this._data.targets.filter(t => t.availability !== "available").length;
    return `${this._data.rules.length} ${this._t("rules")} · ${this._data.rules.reduce((sum,r) => sum+r.scoped_active_intent_count,0)} ${this._t("intents")} · ${this._data.capabilities.resume_targets.length} ${this._t("manual")}${unavailable ? ` · ${unavailable} ${this._t("unavailable_targets")}` : ""}`;
  }
  _scope(row) {
    return `<p><span class="pill ${row.scope.shared ? "warn" : ""}">${esc(this._t(row.scope.shared ? "shared" : "local"))}</span><code>${esc(row.scope.targets.join(", "))}</code></p>` +
      (row.scope.outside_targets.length ? `<p class="note warn">${esc(this._t("outside"))}: ${esc(row.scope.outside_targets.join(", "))}</p>` : "") +
      (row.scope.non_target_effects ? `<p class="note warn">${esc(this._t("non_target"))}</p>` : "");
  }
  _details() {
    if (!this._data) return `<p role="status">${esc(this._t(this._error ? "unavailable" : "loading"))}</p>`;
    return this._data.rules.map(row => `<details class="rule" data-detail="${esc(row.rule_id)}" ${this._expanded.has(row.rule_id) ? "open" : ""}>
      <summary><strong>${esc(row.name || row.rule_id)}</strong> <span class="pill">${esc(this._t(row.presentation_state))}</span>${row.scope.shared ? `<span class="pill warn">${esc(this._t("shared"))}</span>` : ""}
      <span class="purpose">${esc(row.reason || this._t("no_description"))}</span><small>${[...new Set(row.variants.flatMap(v => v.requests.filter(r => row.scope.targets.includes(r.target)).map(r => this._requestText(r.desired))))].join(" · ")}</small></summary>
      <div class="rule-body">${this._scope(row)}${row.notes ? `<p>${esc(row.notes)}</p>` : ""}
      ${row.variants.filter(v => v.requests.some(r => row.scope.targets.includes(r.target)) || !v.requests.length).map(v => `<dl><dt>${esc(this._t("condition"))}</dt><dd>${esc(this._t(v.condition_firing ? "yes" : "no"))}</dd><dt>${esc(this._t("phase"))}</dt><dd>${esc(this._t(v.presentation_state))}${["held","lingering"].includes(v.phase) ? ` — ${esc(this._t("retained"))}` : ""}</dd>
      ${v.blocked_by.length ? `<dt>${esc(this._t("blocked_by"))}</dt><dd>${esc(v.blocked_by.join(", "))}</dd>` : ""}
      ${v.for_remaining_ms != null || v.hold_after?.remaining_ms != null ? `<dt>${esc(this._t("remaining"))}</dt><dd>${Math.ceil((v.for_remaining_ms ?? v.hold_after.remaining_ms)/1000)} ${esc(this._t("seconds"))}</dd>` : ""}
      ${v.requests.filter(r => row.scope.targets.includes(r.target)).map(r => `<dt>${esc(this._t("request"))} · ${esc(r.target)}</dt><dd>${this._requestText(r.desired)}</dd>`).join("")}</dl>`).join("")}
      ${row.contributions.map(c => `<p><strong>${esc(c.target)}</strong> — ${esc(this._t(c.state))}</p>${c.active_requests.map(r => `<p class="note">${esc(this._t("live_request"))}: ${this._requestText(r)}</p>`).join("")}<dl>${["baseline_fields","modifier_fields","changed_fields_without_rule","manual_precedence_fields"].map((key,i) => c[key]?.length ? `<dt>${esc(this._t(["baseline","modifiers","changed","precedence"][i]))}</dt><dd>${esc(c[key].map(f => this._t(f)).join(", "))}</dd>` : "").join("")}</dl>`).join("")}
      ${this._data.capabilities.configure ? `<a href="/intentional?rule=${encodeURIComponent(row.rule_id)}">${esc(this._t("open_panel"))}</a>` : ""}</div></details>`).join("") +
      `<h3>${esc(this._t("targets"))}</h3>${this._data.targets.map(t => this._target(t)).join("")}`;
  }
  _target(target) {
    const manual = target.manual_override;
    const rec = target.reconciliation || {};
    const attempt = rec.recent_history?.at(-1);
    return `<section class="target"><strong>${esc(target.name || target.target)}</strong><small class="sub">${esc(target.target)}${target.area ? ` · ${esc(target.area.name)}` : ""}</small>
      <dl><dt>${esc(this._t("desired"))}</dt><dd>${target.desired ? this._value(target.desired) : esc(this._t("no_demand"))}</dd><dt>${esc(this._t("actual"))}</dt><dd>${target.availability === "available" ? this._value({state:target.actual.state,...target.actual.attributes}) : esc(this._t(target.availability))}</dd></dl>
      <p class="note">${esc(this._t(target.availability === "available" ? target.plan_match : target.availability))}</p><small>${esc(this._t("comparison"))}</small>
      ${target.policy_denial ? `<p class="warn">${esc(this._t("policy"))}: ${format(target.policy_denial)}</p>` : ""}${rec.retry ? `<p class="warn">${esc(this._t("retry"))}: ${Math.ceil(rec.retry.remaining_ms/1000)} s</p>` : ""}
      ${manual ? `<p><strong>${esc(this._t("manual_owner"))}</strong>: ${esc((manual.fields || []).join(", ") || "—")}</p><p><strong>${esc(this._t("recovery"))}</strong>: ${esc(this._t(manual.resume))}${manual.remaining_ms == null ? "" : ` · ${Math.ceil(manual.remaining_ms/1000)} s ${esc(this._t("remaining"))}`}</p>${manual.vacancy_entity ? `<p>${esc(this._t("vacancy"))}: ${esc(manual.vacancy_entity)} · ${manual.vacancy_for_ms/1000} s</p>` : ""}<p class="note">${esc(this._t("after_resume"))}: ${this._value(manual.revealed_after_withdrawal?.value)}</p>` : ""}
      <details><summary>${esc(this._t("dispatch"))}</summary><p class="note">${esc(this._t(attempt ? "last_attempt" : "no_attempt"))}${attempt ? `: ${format(attempt)}` : ""}</p></details></section>`;
  }
  _settingsContent() {
    if (!this._data) return "";
    const disabled = this._busy || this._error || this._data.runtime_status !== "ok";
    const targets = this._data.capabilities.resume_targets;
    return `<p class="note">${esc(this._t("enable_help"))}</p>${!this._data.capabilities.configure ? `<p>${esc(this._t("admin"))}</p>` : this._data.rules.map(row => {
      const enabled = row.variants.every(v => v.enabled);
      return `<section class="setting"><strong>${esc(row.name || row.rule_id)}</strong>${this._scope(row)}<button data-action="enable" data-rule="${esc(row.rule_id)}" data-enabled="${!enabled}" ${disabled || !row.enable_entity_id ? "disabled" : ""}>${esc(this._t(enabled ? "disable" : "enable"))}</button></section>`;
    }).join("")}<h3>${esc(this._t("recovery"))}</h3><p class="note">${esc(this._t("resume_help"))}</p><p>${esc(targets.join(", ") || "—")}</p>
    <button data-action="resume" ${disabled || !targets.length ? "disabled" : ""}>${esc(this._t("resume_all"))}</button>`;
  }
  _render() {
    if (!this._config) return;
    for (const detail of this.shadowRoot.querySelectorAll("details[data-detail]")) {
      detail.open ? this._expanded.add(detail.dataset.detail) : this._expanded.delete(detail.dataset.detail);
    }
    const focused = this.shadowRoot.activeElement;
    const focusAction = focused?.dataset.action, focusRule = focused?.dataset.rule;
    const focusDetail = focused?.tagName === "SUMMARY" ? focused.parentElement.dataset.detail : null;
    const scroll = this._dialog.scrollTop;
    const title = this._config.title || this._t("automation");
    this._dialog.setAttribute("aria-label",title);
    const inline = this._config.display === "inline";
    this.shadowRoot.querySelector("#overview").innerHTML = inline ? "" : `<button class="launcher" data-action="open" aria-haspopup="dialog"><span class="icon" aria-hidden="true">◎</span><span class="grow"><strong>${esc(title)}</strong><small class="sub">${esc(this._summary())}</small></span><span aria-hidden="true">›</span></button>`;
    const content = `<div class="toolbar"><h2>${esc(title)}</h2><button data-action="settings">${esc(this._t(this._settings ? "back" : "settings"))}</button>${inline ? "" : `<button data-action="close" aria-label="${esc(this._t("close"))}">✕</button>`}</div>
      <p class="scope">${esc(this._t("scope"))}: ${esc([this._config.area,this._config.domain,this._config.group].filter(Boolean).join(" · ") || this._t("all"))}</p><p role="status">${esc(this._summary())}</p>
      ${this._error ? `<p class="error" role="alert">${esc(this._error)} <button data-action="refresh">${esc(this._t("refresh"))}</button></p>` : ""}${this._actionError ? `<p class="error" role="alert">${esc(this._actionError)}</p>` : ""}${this._busy ? `<p role="status">${esc(this._t("busy"))}</p>` : ""}
      ${this._settings ? this._settingsContent() : this._details()}${this._data?.generated_at ? `<small>${esc(this._t("snapshot"))}: ${esc(new Date(this._data.generated_at).toLocaleTimeString(this._hass?.language))}</small>` : ""}`;
    this.shadowRoot.querySelector("#inline").innerHTML = inline ? `<div class="content">${content}</div>` : "";
    this.shadowRoot.querySelector("#dialog-content").innerHTML = inline ? "" : content;
    if (focusAction) [...this.shadowRoot.querySelectorAll("[data-action]")].find(el => el.dataset.action === focusAction && el.dataset.rule === focusRule)?.focus({preventScroll:true});
    if (focusDetail) [...this.shadowRoot.querySelectorAll("details[data-detail]")].find(el => el.dataset.detail === focusDetail)?.querySelector("summary")?.focus({preventScroll:true});
    this._dialog.scrollTop = scroll;
  }
  async _action(button) {
    const action = button.dataset.action;
    if (action === "open") { this._render(); this._dialog.showModal(); return; }
    if (action === "close") { this._dialog.close(); return; }
    if (action === "settings") { this._settings = !this._settings; this._actionError = ""; this._render(); return; }
    if (action === "refresh") { await this._refresh(); return; }
    if (!this._settings || this._busy || this._error || this._data?.runtime_status !== "ok") return;
    this._busy = true; this._actionError = "";
    // Freeze explicit displayed targets; never send an unscoped clear call.
    const targets = [...this._data.capabilities.resume_targets];
    const row = this._data.rules.find(r => r.rule_id === button.dataset.rule);
    this._render();
    try {
      if (action === "resume") for (const target of targets) await this._hass.callService("intentional", "clear", {target});
      if (action === "enable" && this._data.capabilities.configure && row?.enable_entity_id) {
        await this._hass.callService("switch", button.dataset.enabled === "true" ? "turn_on" : "turn_off", {entity_id:row.enable_entity_id});
      }
      if (this._pending) await this._request.catch(() => {});
      await this._refresh();
      if (action === "enable" && !this._error) {
        const updated = this._data.rules.find(r => r.rule_id === row?.rule_id);
        if (!updated || !updated.variants.every(v => v.enabled === (button.dataset.enabled === "true"))) this._actionError = this._t("unconfirmed");
      }
    } catch (error) { this._actionError = `${this._t("failed")}: ${error.message || String(error)}`; }
    finally { this._busy = false; this._render(); }
  }
}

class IntentionalCardEditor extends HTMLElement {
  constructor() { super(); this.attachShadow({mode:"open"}); this.shadowRoot.addEventListener("change", e => {
    const key = e.target.dataset.key; if (!key) return;
    const value = key === "refresh_seconds" ? Number(e.target.value) : e.target.value;
    this._config = {...this._config, [key]:value};
    if (value === "") delete this._config[key];
    this.dispatchEvent(new CustomEvent("config-changed", {bubbles:true, composed:true, detail:{config:this._config}}));
  }); }
  setConfig(config) { this._config = {...config}; this._render(); }
  set hass(hass) { const first = !this._hass; this._hass = hass; if (first) this._render(); }
  _render() {
    if (!this._config) return;
    const words = WORDS[language(this._config,this._hass)];
    const input = (key, type="text", extra="") => `<label>${esc(words[key])}<input data-key="${key}" type="${type}" value="${esc(this._config[key] ?? (key === "refresh_seconds" ? 10 : ""))}" ${extra}></label>`;
    const select = (key, options, fallback) => `<label>${esc(words[key])}<select data-key="${key}">${options.map(([value,label]) => `<option value="${value}" ${(this._config[key] || fallback) === value ? "selected" : ""}>${esc(label)}</option>`).join("")}</select></label>`;
    const groups = [...new Set(Object.values(this._hass?.states || {}).map(s => s.attributes?.group).filter(Boolean))];
    this.shadowRoot.innerHTML = `<style>:host{display:block}label{display:block;margin:12px 0}input,select{display:block;box-sizing:border-box;width:100%;padding:10px;font:inherit;color:var(--primary-text-color);background:var(--card-background-color);border:1px solid var(--divider-color);border-radius:6px}p{color:var(--secondary-text-color)}</style>
      ${input("title")}${input("area","text",'list="areas"')}${input("domain","text",'list="domains"')}${input("group","text",'list="groups"')}
      <datalist id="areas">${Object.entries(this._hass?.areas || {}).map(([id,a]) => `<option value="${esc(id)}">${esc(a.name)}</option>`).join("")}</datalist>
      <datalist id="domains">${["light","switch","cover","climate","fan","media_player","lock","alarm_control_panel"].map(d => `<option value="${d}"></option>`).join("")}</datalist>
      <datalist id="groups">${groups.map(g => `<option value="${esc(g)}"></option>`).join("")}</datalist>
      ${select("display",[["popup",words.popup],["inline",words.inline]],"popup")}${select("language",[["auto",words.auto],["en","English"],["de","Deutsch"]],"auto")}${input("refresh_seconds","number",'min="5" max="60"')}<p>${esc(words.names)}</p>`;
  }
}
if (!customElements.get("intentional-card")) customElements.define("intentional-card",IntentionalCard);
if (!customElements.get("intentional-card-editor")) customElements.define("intentional-card-editor",IntentionalCardEditor);
window.customCards = window.customCards || [];
if (!window.customCards.some(c => c.type === "intentional-card")) window.customCards.push({type:"intentional-card",name:"Intentional Automation",description:"Scoped automation status, explanations and deliberate settings",preview:true});
