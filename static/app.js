/* File Organiser — frontend.
 *
 * No build step, no framework, no CDN. One global (`window.FileOrganiser`).
 *
 * Shape of the thing, so a later React port is a lift rather than a rewrite:
 *   - `state` is the only mutable thing. Nothing else holds data.
 *   - `render*` functions are (state) -> DOM node. They read `state`, never
 *     the DOM, and never mutate. Swap `h()` for `createElement` and they port.
 *   - `do*` / `load*` functions mutate `state` and then re-render.
 *
 * Safety note: this file never touches innerHTML. Every interpolated string —
 * filenames, paths, category names, model-written descriptions and reasons —
 * becomes a text node via h(), so untrusted content can never parse as markup.
 *
 * The backend runs three passes and the UI's job is to make that legible:
 *   1. rules       — local extension/filename table, instant, decided_by:"rules"
 *   2. scheme      — one model call per run, invents this folder's categories
 *   3. classifier  — one model call per file, decided_by:"classifier"
 *
 * Pass 1 is optional: POST /scan with use_rules:false skips the table entirely
 * and sends *every* file to passes 2 and 3. Better grouping (by subject, not by
 * file type), one model call per file. A rendered plan carries `used_rules`, so
 * a plan reopened from history is always labelled with the mode that built it,
 * never with whatever the live toggle happens to say.
 */
window.FileOrganiser = (function () {
  "use strict";

  /* ------------------------------------------------------------------ *
   * DOM helpers
   * ------------------------------------------------------------------ */

  /**
   * h("div", {class: "row", onclick: fn}, "text", childNode, [more, nodes])
   * Strings become text nodes. null/false/undefined children are dropped.
   */
  function h(tag, props, ...children) {
    const el = document.createElement(tag);
    for (const [key, value] of Object.entries(props || {})) {
      if (value === null || value === undefined || value === false) continue;
      if (key === "class") el.className = value;
      else if (key === "dataset") Object.assign(el.dataset, value);
      else if (key.startsWith("on") && typeof value === "function") {
        el.addEventListener(key.slice(2).toLowerCase(), value);
      } else if (key in el && key !== "list") {
        el[key] = value;
      } else {
        el.setAttribute(key, value === true ? "" : value);
      }
    }
    appendChildren(el, children);
    return el;
  }

  function appendChildren(el, children) {
    for (const child of children) {
      if (child === null || child === undefined || child === false) continue;
      if (Array.isArray(child)) appendChildren(el, child);
      else if (child instanceof Node) el.appendChild(child);
      else el.appendChild(document.createTextNode(String(child)));
    }
  }

  const $ = (id) => document.getElementById(id);

  /**
   * Replace an element's children, dropping null/false the way h() does.
   * Calling replaceChildren(null) directly would stringify it to "null" and
   * print that on the page, so every mount point goes through here.
   */
  function mount(el, ...children) {
    if (!el) return;
    const flat = [];
    (function walk(list) {
      for (const child of list) {
        if (child === null || child === undefined || child === false) continue;
        if (Array.isArray(child)) walk(child);
        else flat.push(child instanceof Node ? child : document.createTextNode(String(child)));
      }
    })(children);
    el.replaceChildren(...flat);
  }

  const SVG_NS = "http://www.w3.org/2000/svg";

  /* Inline SVG only — no external assets, and glyph fonts render differently
   * on every platform, which would undermine "the two agents look different". */
  const ICONS = {
    // deterministic lookup table
    rules: [{ d: "M3 4.5h10M3 8h10M3 11.5h6" }],
    // a spark: the model
    classifier: [{ d: "M8 2.2l1.5 3.9 3.9 1.5-3.9 1.5L8 13l-1.5-3.9L2.6 7.6l3.9-1.5z", fill: 1 }],
    // stacked plates: a proposed scheme
    scheme: [{ d: "M8 2.5l5.5 3-5.5 3-5.5-3z" }, { d: "M2.5 10.5l5.5 3 5.5-3" }],
    none: [{ d: "M4 8h8" }],
    caret: [{ d: "M6.2 3.8L10.4 8l-4.2 4.2" }],
    check: [{ d: "M3 8.4l3.4 3.4L13 4.6" }],
    warn: [{ d: "M8 2.6l6 10.8H2z" }, { d: "M8 6.6v3.1M8 11.4v.1", w: 1.7 }],
    folder: [{ d: "M2.5 4.5h4l1.2 1.5h5.8v7H2.5z" }],
    clock: [{ d: "M8 3.2a4.8 4.8 0 100 9.6 4.8 4.8 0 000-9.6z" }, { d: "M8 5.6V8l1.8 1.2" }],
    // a hosted provider: the request leaves this machine
    cloud: [{ d: "M5.3 12.2h5.4a2.7 2.7 0 00.3-5.37 3.6 3.6 0 00-6.87.9A2.6 2.6 0 005.3 12.2z" }],
    // a shield: a local endpoint, nothing leaves the machine
    local: [{ d: "M8 2.5l4.4 1.7v3.3c0 2.6-1.8 4.5-4.4 5.4-2.6-.9-4.4-2.8-4.4-5.4V4.2z" }],
  };

  function icon(name, size) {
    const spec = ICONS[name];
    if (!spec) return null;
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", "0 0 16 16");
    svg.setAttribute("width", String(size || 12));
    svg.setAttribute("height", String(size || 12));
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    for (const part of spec) {
      const path = document.createElementNS(SVG_NS, "path");
      path.setAttribute("d", part.d);
      if (part.fill) {
        path.setAttribute("fill", "currentColor");
      } else {
        path.setAttribute("fill", "none");
        path.setAttribute("stroke", "currentColor");
        path.setAttribute("stroke-width", String(part.w || 1.5));
        path.setAttribute("stroke-linecap", "round");
        path.setAttribute("stroke-linejoin", "round");
      }
      svg.appendChild(path);
    }
    return svg;
  }

  /* ------------------------------------------------------------------ *
   * Formatting
   * ------------------------------------------------------------------ */

  function formatBytes(bytes) {
    if (typeof bytes !== "number" || !isFinite(bytes) || bytes < 0) return "";
    if (bytes < 1024) return bytes + " B";
    const units = ["KB", "MB", "GB", "TB"];
    let value = bytes / 1024;
    let i = 0;
    while (value >= 1024 && i < units.length - 1) {
      value /= 1024;
      i++;
    }
    return (value < 10 ? value.toFixed(1) : Math.round(value)) + " " + units[i];
  }

  function formatTime(iso) {
    if (!iso) return "—";
    const date = new Date(iso);
    if (isNaN(date.getTime())) return String(iso);
    return date.toLocaleString();
  }

  function plural(n, one, many) {
    return n + " " + (n === 1 ? one : many);
  }

  /** Last two segments of a path, for a compact "…/Downloads" style label. */
  function tailOf(path, segments) {
    const parts = String(path || "").split(/[/\\]/).filter(Boolean);
    const keep = parts.slice(-(segments || 2));
    return (keep.length < parts.length ? "…/" : "/") + keep.join("/");
  }

  /**
   * Split a destination into the bit that matters (the new folder) and the bit
   * that does not (the target dir the user already typed into the box).
   */
  function destinationParts(item, targetDir) {
    const dest = item.destination || "";
    if (!dest) return null;
    let rel = dest;
    let inside = false;
    if (targetDir && dest.indexOf(targetDir) === 0) {
      rel = dest.slice(targetDir.length).replace(/^[/\\]+/, "");
      inside = true;
    }
    const parts = rel.split(/[/\\]/).filter(Boolean);
    const file = parts.length ? parts[parts.length - 1] : rel;
    const folder = parts.slice(0, -1).join("/");
    return { folder, file, full: dest, inside, renamed: file !== item.filename };
  }

  /* ------------------------------------------------------------------ *
   * API
   * ------------------------------------------------------------------ */

  class ApiError extends Error {
    constructor(status, data, message) {
      super(message);
      this.status = status;
      this.data = data || {};
    }
  }

  async function request(path, options) {
    let response;
    try {
      response = await fetch(path, options);
    } catch (err) {
      throw new ApiError(0, {}, "Could not reach the backend. Is the server still running?");
    }
    let data = null;
    const raw = await response.text();
    if (raw) {
      try {
        data = JSON.parse(raw);
      } catch (err) {
        data = null;
      }
    }
    if (!response.ok) {
      const message =
        (data && (data.error || data.message)) ||
        "Request failed (HTTP " + response.status + ")";
      throw new ApiError(response.status, data, message);
    }
    return data;
  }

  function postJSON(path, body) {
    return request(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  }

  const api = {
    health: () => request("/health"),
    scan: (targetDir, maxDelegations, useRules) =>
      postJSON("/scan", {
        target_dir: targetDir,
        max_delegations: maxDelegations,
        // Older backends ignore the field; newer ones default it to true.
        use_rules: useRules !== false,
      }),
    plan: (planId) => request("/plan/" + encodeURIComponent(planId)),
    apply: (planId, approvedIds, confirmFirstApply) =>
      postJSON("/apply", {
        plan_id: planId,
        approved_ids: approvedIds,
        confirm_first_apply: !!confirmFirstApply,
      }),
    log: (limit) => request("/log?limit=" + encodeURIComponent(limit)),
    plans: (limit) => request("/plans?limit=" + encodeURIComponent(limit)),
  };

  /* ------------------------------------------------------------------ *
   * State
   * ------------------------------------------------------------------ */

  const state = {
    tab: "plan",
    health: null,
    healthError: null,
    plan: null,
    /** item_ids the user has approved for the next apply */
    approved: new Set(),
    /** item_ids already moved by an apply in this session */
    movedIds: new Set(),
    /** item_ids that failed in the last apply */
    failedIds: new Set(),
    result: null,
    /** {target_dir, approved_ids, message} while the backend wants a confirm */
    pendingConfirm: null,
    busy: null, // "scan" | "apply" | "history" | null
    banner: null, // {kind: "error"|"info"|"success", message}
    /**
     * Opt-in for the *next* scan: skip the rule table, delegate every file.
     * Deliberately never synced from a loaded plan — the toggle describes what
     * the next run will do; `plan.used_rules` describes what a plan already did.
     */
    skipRules: false,
    /** read-only config surface open? (provider, models, where each came from) */
    providerOpen: false,
    /** a broken config opens that surface once, unprompted — then never again */
    providerAutoOpened: false,
    /** which pipeline stage the running scan is believed to be in */
    scanStage: null, // "rules" | "scheme" | "assign"
    scanElapsed: 0,
    scanTimer: null,
    filter: { text: "", lens: "all" },
    density: "comfortable",
    /** category name -> open?; seeded per plan so big plans start collapsed */
    groupOpen: {},
    /** true when the plan is big enough that groups should start collapsed */
    collapseByDefault: false,
    history: { entries: [], undoLog: "", plans: [], loaded: false, error: null },
  };

  /* The backend answers /scan in a single response — there is no progress
   * stream — so these are *estimates* used only to move the highlight along
   * and name the stage most likely running. Nothing about the final plan is
   * inferred from them. */
  const STAGE_AT_MS = { scheme: 600, assign: 4500 };
  /* Above these sizes a plan opens collapsed, so 200 files is still scannable. */
  const COLLAPSE_OVER_FILES = 60;
  const COLLAPSE_OVER_GROUPS = 8;

  /* ------------------------------------------------------------------ *
   * Selectors — derived views of `state`, no mutation
   * ------------------------------------------------------------------ */

  function moveItems() {
    return state.plan ? state.plan.items.filter((i) => i.action === "move") : [];
  }

  function skipItems() {
    return state.plan ? state.plan.items.filter((i) => i.action !== "move") : [];
  }

  function reviewItems() {
    return moveItems().filter((i) => i.needs_review);
  }

  function matchesFilter(item) {
    const { text, lens } = state.filter;
    if (lens === "review" && !item.needs_review) return false;
    if (lens === "rules" && item.decided_by !== "rules") return false;
    if (lens === "classifier" && item.decided_by !== "classifier") return false;
    if (!text) return true;
    const needle = text.toLowerCase();
    return (
      String(item.filename || "").toLowerCase().includes(needle) ||
      String(item.category || "").toLowerCase().includes(needle) ||
      String(item.destination || "").toLowerCase().includes(needle)
    );
  }

  function visibleMoveItems() {
    return moveItems().filter(matchesFilter);
  }

  function filterActive() {
    return !!state.filter.text || state.filter.lens !== "all";
  }

  /** Category groups over the *visible* moves; review items live up top. */
  function categoryGroups() {
    const groups = new Map();
    for (const item of visibleMoveItems()) {
      const category = item.category || "Other";
      if (!groups.has(category)) groups.set(category, { confident: [], review: 0, agents: new Set() });
      const bucket = groups.get(category);
      bucket.agents.add(item.decided_by);
      if (item.needs_review) bucket.review += 1;
      else bucket.confident.push(item);
    }
    return [...groups.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  }

  /** How many files landed in each category, over the whole plan. */
  function categoryCounts() {
    const counts = new Map();
    for (const item of moveItems()) {
      const key = item.category || "Other";
      counts.set(key, (counts.get(key) || 0) + 1);
    }
    return counts;
  }

  /** Everything the user could still approve (already-moved rows are out). */
  function selectableIds(items) {
    return (items || moveItems())
      .filter((i) => !state.movedIds.has(i.item_id))
      .map((i) => i.item_id);
  }

  function proposedCategories() {
    return (state.plan && state.plan.proposed_categories) || [];
  }

  function classifierAvailable() {
    if (state.plan) return state.plan.classifier_available !== false;
    return healthClassifierAvailable();
  }

  /**
   * Live availability, ignoring any plan on screen. The scan bar is a decision
   * about the *next* run, so it must not read a stale plan's verdict.
   */
  function healthClassifierAvailable() {
    if (state.health && state.health.classifier) return !!state.health.classifier.available;
    return null; // unknown yet
  }

  /** Did the plan on screen skip the rule table? Older plans omit the field. */
  function planSkippedRules(plan) {
    return !!plan && plan.used_rules === false;
  }

  /**
   * The degenerate combination: no rule table *and* no delegates means nothing
   * can be classified at all, so the run would place zero files. With the rule
   * pass on, the easy files still get organised — which is why this is blocked
   * rather than merely warned about.
   */
  function scanBlocked() {
    return state.skipRules && healthClassifierAvailable() === false;
  }

  function normalisePath(value) {
    return String(value || "").trim().replace(/[/\\]+$/, "");
  }

  /**
   * What a previous run saw in this folder, or null. The only honest basis for
   * "this will make about N calls" — there is no endpoint that counts a folder,
   * so nothing is estimated unless a real plan for that exact path says so.
   * /plans rows carry a summary but not used_rules, hence `rules` (a count that
   * is always meaningful) rather than a claim about that run's mode.
   */
  function lastScanOf(dir) {
    const key = normalisePath(dir);
    if (!key) return null;
    const candidates = [];
    if (state.plan) candidates.push(state.plan);
    for (const row of (state.history && state.history.plans) || []) candidates.push(row);
    for (const plan of candidates) {
      if (normalisePath(plan.target_dir) !== key) continue;
      const summary = plan.summary || {};
      const total = summary.total_files;
      if (typeof total !== "number") continue;
      return { total, rules: (summary.by_agent && summary.by_agent.rules) || 0 };
    }
    return null;
  }

  /* --- provider / configuration ---------------------------------------- *
   * /health.provider says which model answers and which layer decided that.
   * An older backend may not send the block at all, and a *broken* config
   * sends only {name: null, error: "..."} — so nothing here may assume a
   * field exists. */

  function providerInfo() {
    return (state.health && state.health.provider) || null;
  }

  /** True when the config file itself cannot be used — a different problem,
   * with a different fix, from "no credentials". */
  function providerBroken() {
    const provider = providerInfo();
    return !!(provider && provider.error);
  }

  function providerIsLocal() {
    const provider = providerInfo();
    return !!(provider && provider.name === "compatible");
  }

  /** Where the settings live. The error payload omits config_path, so fall
   * back to the state dir, which /health always reports. */
  function configFilePath() {
    const provider = providerInfo();
    if (provider && provider.config_path) return provider.config_path;
    const dir = state.health && state.health.state_dir;
    return dir ? String(dir).replace(/[/\\]+$/, "") + "/config.json" : "";
  }

  /**
   * Why the delegates cannot run, as {kind, text}:
   *   config — the config file is unusable; no provider was resolved
   *   model  — a provider resolved but named no model for an agent
   *   detail — anything else (usually no credentials); the backend's words
   * The backend's `detail` string is always the specific reason, so it is
   * surfaced verbatim rather than guessed at.
   */
  function unavailableReason() {
    const provider = providerInfo();
    const detail = (state.health && state.health.classifier && state.health.classifier.detail) || "";
    if (provider && provider.error) return { kind: "config", text: provider.error };
    if (provider && provider.name && (!provider.scheme_model || !provider.classifier_model)) {
      return {
        kind: "model",
        text: detail || "no model is configured for provider " + provider.name,
      };
    }
    return { kind: "detail", text: detail };
  }

  /* ------------------------------------------------------------------ *
   * Selection
   * ------------------------------------------------------------------ */

  function setApproved(itemId, approved) {
    if (state.movedIds.has(itemId)) return;
    if (approved) state.approved.add(itemId);
    else state.approved.delete(itemId);
    syncSelection();
  }

  function setApprovedMany(itemIds, approved) {
    for (const id of itemIds) {
      if (state.movedIds.has(id)) continue;
      if (approved) state.approved.add(id);
      else state.approved.delete(id);
    }
    syncSelection();
  }

  /**
   * Push `state.approved` back out to the DOM without a full re-render, so
   * ticking a box never costs the user their scroll position in a long plan.
   */
  function syncSelection() {
    for (const box of document.querySelectorAll("input[data-item-id]")) {
      box.checked = state.approved.has(box.dataset.itemId);
    }
    for (const el of document.querySelectorAll("[data-count-ids]")) {
      const raw = el.dataset.countIds;
      const ids = raw ? raw.split(",").filter(Boolean) : [];
      const n = ids.filter((id) => state.approved.has(id)).length;
      el.textContent =
        el.dataset.countStyle === "short" ? n + "/" + ids.length : n + " of " + ids.length + " approved";
      el.classList.toggle("zero", n === 0);
    }
    const total = selectableIds().length;
    const approved = state.approved.size;
    const counter = $("approve-count");
    if (counter) {
      counter.textContent = approved + " of " + total + " approved";
      counter.classList.toggle("zero", approved === 0);
    }
    const applyBtn = $("apply-btn");
    if (applyBtn) {
      applyBtn.disabled = !state.plan || approved === 0 || state.busy === "apply";
    }
  }

  /** `a` toggles: if everything visible is on, turn it all off, else turn on. */
  function toggleAllVisible() {
    const ids = selectableIds(visibleMoveItems());
    if (!ids.length) return;
    const allOn = ids.every((id) => state.approved.has(id));
    setApprovedMany(ids, !allOn);
  }

  /* ------------------------------------------------------------------ *
   * Atoms
   * ------------------------------------------------------------------ */

  function badge(text, kind, title) {
    return h("span", { class: "badge" + (kind ? " badge-" + kind : ""), title: title || null }, text);
  }

  const AGENT_LABEL = {
    rules: "Rule pass",
    classifier: "Classifier agent",
    none: "No agent",
  };
  const AGENT_TITLE = {
    rules: "Rule pass — matched a known extension or filename pattern. Local, deterministic, no model call.",
    classifier: "Classifier agent — one model call placed this file into the proposed scheme.",
    none: "No agent placed this file. It stays exactly where it is.",
  };

  /** The visual identity of an agent: shape + hue + glyph, never text alone. */
  function agentMark(decidedBy) {
    const key = decidedBy === "rules" || decidedBy === "classifier" ? decidedBy : "none";
    return h(
      "span",
      { class: "mark mark-" + key, title: AGENT_TITLE[key], role: "img", "aria-label": AGENT_LABEL[key] },
      icon(key, 11),
    );
  }

  /* The two model-calling agents. They share the agent hue — they are the same
   * model provider — but keep their own glyph, exactly as the empty state and
   * the pipeline strip already draw them. */
  const DELEGATE_LABEL = { scheme: "Scheme proposer", classifier: "Classifier" };
  const DELEGATE_TITLE = {
    scheme: "Scheme proposer — one model call per run, naming this folder's categories.",
    classifier: "Classifier — one model call per file, placing it in the proposed scheme.",
  };

  function delegateMark(kind, size) {
    return h(
      "span",
      {
        class: "mark mark-classifier" + (size === "xs" ? " mark-xs" : ""),
        title: DELEGATE_TITLE[kind],
        role: "img",
        "aria-label": DELEGATE_LABEL[kind] + " agent",
      },
      icon(kind, size === "xs" ? 9 : 11),
    );
  }

  const PROVIDER_LABEL = {
    anthropic: "Anthropic",
    openai: "OpenAI",
    compatible: "Local endpoint",
  };
  const PROVIDER_NOTE = {
    anthropic: "Hosted API — filenames and short snippets are sent to Anthropic.",
    openai: "Hosted API — filenames and short snippets are sent to OpenAI.",
    compatible: "An OpenAI-protocol server you point at yourself.",
  };
  const PROVIDER_KEY_ENV = {
    anthropic: "ANTHROPIC_API_KEY",
    openai: "OPENAI_API_KEY",
    compatible: "OPENAI_API_KEY (local servers usually ignore it)",
  };

  const SOURCE_LABEL = {
    environment: "env var",
    "config file": "config file",
    default: "built-in default",
  };
  const SOURCE_KIND = { environment: "env", "config file": "file", default: "default" };

  /** Which layer won for one setting: env var, then config file, then default. */
  function sourceBadge(source) {
    if (!source) return null;
    const label = SOURCE_LABEL[source] || source;
    return badge(
      label,
      "src-" + (SOURCE_KIND[source] || "default"),
      "Came from the " + label + ". Resolution order: environment variable, then config file, then built-in default.",
    );
  }

  function confidenceMeter(confidence) {
    const level = ["high", "medium", "low"].includes(confidence) ? confidence : "na";
    return h(
      "span",
      {
        class: "conf conf-" + level,
        title: "Confidence: " + (confidence || "n/a"),
        role: "img",
        "aria-label": "confidence " + (confidence || "not applicable"),
      },
      h("i", {}),
      h("i", {}),
      h("i", {}),
    );
  }

  function pathText(value, strong) {
    return h("code", { class: "path" + (strong ? " path-strong" : ""), title: value || "" }, value || "—");
  }

  function chip(value, label, extraClass) {
    return h(
      "span",
      { class: "chip" + (extraClass ? " " + extraClass : "") },
      h("strong", {}, String(value === undefined || value === null ? 0 : value)),
      label,
    );
  }

  function selectionControls(ids, style) {
    return h(
      "div",
      { class: "select-controls" },
      h(
        "span",
        {
          class: "count muted",
          dataset: { countIds: ids.join(","), countStyle: style || "long" },
        },
        "0 of " + ids.length + " approved",
      ),
      h("button", { type: "button", class: "linkbtn", onclick: () => setApprovedMany(ids, true) }, "all"),
      h("button", { type: "button", class: "linkbtn", onclick: () => setApprovedMany(ids, false) }, "none"),
    );
  }

  /* ------------------------------------------------------------------ *
   * Render: scan bar options
   * ------------------------------------------------------------------ */

  /** The max-delegations box as a number, or null when empty or unusable. */
  function maxDelegationsValue() {
    const el = $("max-delegations");
    const raw = el ? el.value.trim() : "";
    if (raw === "") return null;
    const value = Number(raw);
    return Number.isInteger(value) && value >= 0 ? value : null;
  }

  function setSkipRules(on) {
    state.skipRules = !!on;
    renderScanOption();
    renderPipeline();
    // The empty state spells the three passes out, so it changes with the mode.
    if (state.tab === "plan" && !state.plan) renderPlanView();
  }

  function updateScanButton() {
    const btn = $("scan-btn");
    if (!btn) return;
    const blocked = scanBlocked();
    btn.disabled = state.busy === "scan" || blocked;
    if (blocked) {
      btn.title =
        "Both passes are off: the rule table is skipped and the agents are unavailable, " +
        "so this run could not classify anything.";
      // The reason, and the two ways out, are in the live region below.
      btn.setAttribute("aria-describedby", "scan-option-hint");
    } else {
      btn.removeAttribute("title");
      btn.removeAttribute("aria-describedby");
    }
  }

  /**
   * The consequences of the toggle, stated where the decision is made:
   *   off      — one quiet line, because this is the cheap default
   *   on       — how many model calls it costs, and the cap that bounds them
   *   blocked  — rules off with no delegates classifies nothing at all
   * Only the static explanation lives in index.html; everything here depends
   * on /health, the typed path and the cap, so it is mounted from state.
   */
  function renderScanOption() {
    const wrap = $("scan-option");
    const box = $("skip-rules");
    const host = $("scan-option-hint");
    if (box) {
      box.checked = state.skipRules;
      // Flipping it mid-flight would relabel a run that is already underway.
      box.disabled = state.busy === "scan";
    }
    if (wrap) {
      wrap.classList.toggle("is-on", state.skipRules);
      wrap.classList.toggle("is-blocked", scanBlocked());
    }
    updateScanButton();
    if (!host) return;

    if (!state.skipRules) {
      mount(
        host,
        h(
          "p",
          { class: "scanopt-off tiny muted" },
          "Off: the rule table places everything it recognises for free, and only the files it " +
            "cannot match need an agent at all.",
        ),
      );
      return;
    }

    if (scanBlocked()) {
      mount(host, renderRulesOffBlocked());
      return;
    }

    mount(host, renderRulesOffCost(), renderRulesOffCap());
  }

  /** Rules off + delegates unavailable: the run would do precisely nothing. */
  function renderRulesOffBlocked() {
    const reason = unavailableReason();
    return h(
      "div",
      { class: "note note-fail scanopt-note" },
      h(
        "p",
        {},
        icon("warn", 12),
        " ",
        h("strong", {}, "This would classify nothing."),
        " The agents are unavailable",
        reason.text ? " (" + reason.text + ")" : "",
        ", and with the rule table skipped there is no other pass — every file would be left " +
          "exactly where it is. Dry run stays disabled until one of these is true:",
      ),
      h(
        "ul",
        { class: "scanopt-ways" },
        h(
          "li",
          {},
          "Leave the rule pass on. It needs no model at all, and still files everything it recognises. ",
          h(
            "button",
            { type: "button", class: "linkbtn", onclick: () => setSkipRules(false) },
            "turn the rule pass back on",
          ),
        ),
        h(
          "li",
          {},
          "Or make the agents available — this is a configuration problem, not something a retry fixes. ",
          providerInfo()
            ? h(
                "button",
                { type: "button", class: "linkbtn", onclick: () => openProviderPanel() },
                reason.kind === "config" ? "fix the configuration" : "show configuration",
              )
            : null,
        ),
      ),
    );
  }

  /**
   * How expensive this run is about to be, in model calls — the one unit this
   * page can count honestly. No pricing and no token estimates: nothing here
   * knows the model's rates, and /health does not report them.
   *
   * A file count is only ever quoted from a real previous plan for this exact
   * folder. There is no endpoint that counts a folder, so without one the copy
   * says the shape of the cost and refuses to invent a number.
   */
  function renderRulesOffCost() {
    const dir = $("target-dir") ? $("target-dir").value : "";
    const seen = lastScanOf(dir);
    return h(
      "div",
      { class: "note note-agents scanopt-note" },
      h(
        "p",
        {},
        delegateMark("classifier", "xs"),
        " ",
        h("strong", {}, "One model call per file"),
        ", plus one scheme call for the run. ",
        seen
          ? [
              "A previous run on ",
              h("code", { class: "path" }, tailOf(dir)),
              " saw ",
              h("strong", {}, plural(seen.total, "file", "files")),
              " — so expect roughly ",
              h("strong", {}, plural(seen.total + 1, "model call", "model calls")),
              " if the folder still holds the same files.",
              seen.rules
                ? " The rule table placed " +
                  seen.rules +
                  " of them for free last time; with rules off those are delegated too."
                : null,
            ]
          : "Cost scales with the size of the folder, and nothing here has counted it yet — " +
            "a folder is only ever counted by scanning it.",
      ),
    );
  }

  /** The cap is the existing brake, so the toggle points straight at it. */
  function renderRulesOffCap() {
    const cap = maxDelegationsValue();
    const dir = $("target-dir") ? $("target-dir").value : "";
    const seen = lastScanOf(dir);
    const capLink = h(
      "button",
      { type: "button", class: "linkbtn", onclick: () => focusMaxDelegations() },
      "Max file calls",
    );

    if (cap === null) {
      return h(
        "p",
        { class: "scanopt-cap small muted" },
        "No ceiling on that: ",
        capLink,
        " is empty, so every file in the folder is delegated. Put a number in it and the files " +
          "past that number are left exactly where they are.",
      );
    }
    if (cap === 0) {
      return h(
        "p",
        { class: "note note-warn scanopt-note" },
        icon("warn", 12),
        " ",
        capLink,
        " is 0, so no file would be delegated — and with the rule table skipped too, this run " +
          "would leave every file in place.",
      );
    }
    return h(
      "p",
      { class: "scanopt-cap small muted" },
      capLink,
      " caps that at " + plural(cap, "delegated file", "delegated files") + ", so at most " +
        plural(cap + 1, "model call", "model calls") + " counting the scheme call",
      seen && seen.total > cap
        ? " — about " + (seen.total - cap) + " of that folder's files would then be left in place."
        : "; anything past the cap is left exactly where it is.",
    );
  }

  function focusMaxDelegations() {
    const el = $("max-delegations");
    if (!el) return;
    el.focus();
    if (el.select) el.select();
  }

  /* ------------------------------------------------------------------ *
   * Render: health strip
   * ------------------------------------------------------------------ */

  function renderHealth() {
    const el = $("health");
    if (!el) return;

    if (state.healthError) {
      mount(el, 
        h("span", { class: "health-pill health-bad" }, h("span", { class: "dot" }), "backend unreachable"),
        h("button", { type: "button", class: "linkbtn", onclick: () => loadHealth() }, "retry"),
      );
      return;
    }
    if (!state.health) {
      mount(el, h("span", { class: "muted small" }, "checking backend…"));
      return;
    }

    const classifier = state.health.classifier || {};
    const available = !!classifier.available;
    const rules = state.health.rules || {};
    const ruleCats = state.health.rule_categories || [];

    mount(el,
      h(
        "span",
        {
          class: "health-pill " + (available ? "health-ok" : "health-warn"),
          title: classifier.detail || "",
        },
        h("span", { class: "dot" }),
        available ? "agents ready" : "rules-only mode",
      ),
      renderProviderPill(),
      h(
        "span",
        {
          class: "health-pill",
          title: "The rule pass knows these extensions and fills these folders without any model call.",
        },
        icon("rules", 11),
        rules.known_extensions || 0,
        " ext · ",
        ruleCats.length,
        " rule folders",
      ),
      state.health.dynamic_categories
        ? h(
            "span",
            {
              class: "health-pill",
              title: "Everything else is grouped into categories proposed per run from your folder's actual contents.",
            },
            icon("scheme", 11),
            "scheme per run",
          )
        : null,
    );
  }

  /* ------------------------------------------------------------------ *
   * Render: provider pill + read-only config surface
   * ------------------------------------------------------------------ */

  /**
   * Which model answers, in the status strip. The two agents can run different
   * models — that is the cost lever — so both are named, each behind its own
   * agent glyph. The pill is the handle for the config surface below.
   */
  function renderProviderPill() {
    const provider = providerInfo();
    if (!provider) return null; // older backend: say nothing rather than guess
    const open = state.providerOpen;
    const caret = h("span", { class: "prov-caret" + (open ? " is-open" : "") }, icon("caret", 11));

    if (provider.error) {
      return h(
        "button",
        {
          type: "button",
          class: "health-pill prov-pill health-bad",
          "aria-expanded": open ? "true" : "false",
          "aria-controls": "provider-panel",
          title: "The config file cannot be used: " + provider.error,
          onclick: () => toggleProviderPanel(),
        },
        icon("warn", 11),
        "config error",
        caret,
      );
    }

    const local = provider.name === "compatible";
    const name = provider.name ? PROVIDER_LABEL[provider.name] || provider.name : "no provider";
    const scheme = provider.scheme_model || "";
    const classifierModel = provider.classifier_model || "";
    const sameModel = !!scheme && scheme === classifierModel;

    return h(
      "button",
      {
        type: "button",
        class: "health-pill prov-pill" + (local ? " is-local" : ""),
        "aria-expanded": open ? "true" : "false",
        "aria-controls": "provider-panel",
        title:
          name +
          (provider.base_url ? " at " + provider.base_url : "") +
          " — scheme proposer: " +
          (scheme || "no model set") +
          ", classifier: " +
          (classifierModel || "no model set") +
          ". Click for where each setting came from.",
        onclick: () => toggleProviderPanel(),
      },
      icon(local ? "local" : "cloud", 11),
      h("span", { class: "prov-name" }, local ? "local" : name),
      local && provider.base_url
        ? h("code", { class: "prov-url" }, provider.base_url)
        : null,
      h(
        "span",
        { class: "prov-models" },
        sameModel
          ? h(
              "span",
              { class: "prov-model", title: "Both agents run " + scheme },
              delegateMark("scheme", "xs"),
              delegateMark("classifier", "xs"),
              h("code", { class: "prov-model-id" }, scheme),
            )
          : [modelEntry("scheme", scheme), modelEntry("classifier", classifierModel)],
      ),
      caret,
    );
  }

  function modelEntry(kind, model) {
    return h(
      "span",
      {
        class: "prov-model",
        title: DELEGATE_TITLE[kind] + " Model: " + (model || "none configured"),
      },
      delegateMark(kind, "xs"),
      model
        ? h("code", { class: "prov-model-id" }, model)
        : h("span", { class: "prov-missing" }, "no model"),
    );
  }

  function toggleProviderPanel(force) {
    state.providerOpen = force === undefined ? !state.providerOpen : !!force;
    renderHealth();
    renderProviderPanel();
  }

  function openProviderPanel() {
    toggleProviderPanel(true);
    const panel = $("provider-panel");
    if (panel && panel.scrollIntoView) panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  /**
   * The debugging affordance: every effective setting, and which layer decided
   * it. Read-only on purpose — settings live in the config file and in env
   * vars, so this names both rather than offering a form.
   */
  function renderProviderPanel() {
    const host = $("provider-panel");
    if (!host) return;
    const provider = providerInfo();
    if (!provider || !state.providerOpen) {
      host.hidden = true;
      mount(host);
      return;
    }
    host.hidden = false;

    const sources = provider.sources || {};
    const broken = !!provider.error;
    const local = provider.name === "compatible";
    const classifier = (state.health && state.health.classifier) || {};
    const cfgPath = configFilePath();

    mount(
      host,
      h(
        "section",
        { class: "card provider-card" + (broken ? " is-broken" : local ? " is-local" : "") },
        h(
          "div",
          { class: "card-head" },
          h(
            "div",
            { class: "card-title" },
            h("h2", {}, "Model configuration"),
            h(
              "p",
              { class: "muted small" },
              "Read-only. Every value below is resolved at request time — environment variable first, " +
                "then the config file, then a built-in default.",
            ),
          ),
          h(
            "div",
            { class: "select-controls" },
            h("button", { type: "button", class: "linkbtn", onclick: () => loadHealth() }, "re-check"),
            h(
              "button",
              { type: "button", class: "linkbtn", onclick: () => toggleProviderPanel(false) },
              "close",
            ),
          ),
        ),

        broken
          ? h(
              "div",
              { class: "cfg-block cfg-error" },
              h("h3", {}, icon("warn", 13), " This config file cannot be used"),
              h("p", { class: "cfg-error-msg" }, provider.error),
              h(
                "p",
                { class: "small" },
                "No provider could be resolved, so both agents are off. This is not a missing API key — " +
                  "no key would help. Edit the file below (or set the matching environment variable), " +
                  "then press re-check; the server re-reads it on every request.",
              ),
              h(
                "p",
                { class: "cfg-env" },
                "FILE_ORGANISER_PROVIDER · FILE_ORGANISER_BASE_URL · FILE_ORGANISER_SCHEME_MODEL · FILE_ORGANISER_CLASSIFIER_MODEL",
              ),
            )
          : null,

        !broken && local
          ? h(
              "div",
              { class: "cfg-block cfg-local" },
              h("h3", {}, icon("local", 13), " Nothing leaves this machine"),
              h(
                "p",
                { class: "small" },
                "Both agents talk to the OpenAI-protocol endpoint below and to nothing else. " +
                  "No hosted API is contacted, so no filename or file snippet is sent off this machine.",
              ),
              pathText(provider.base_url || "no base_url set", true),
            )
          : null,

        !broken
          ? h(
              "div",
              { class: "cfg-table" },
              configRow({
                label: "Provider",
                value: provider.name,
                source: sources.provider,
                env: "FILE_ORGANISER_PROVIDER",
                note: provider.name ? PROVIDER_NOTE[provider.name] : null,
              }),
              local || provider.base_url
                ? configRow({
                    label: "Base URL",
                    value: provider.base_url,
                    source: sources.base_url,
                    env: "FILE_ORGANISER_BASE_URL",
                    missing: "not set",
                  })
                : null,
              configRow({
                label: "Scheme model",
                mark: delegateMark("scheme"),
                value: provider.scheme_model,
                source: sources["scheme.model"],
                env: "FILE_ORGANISER_SCHEME_MODEL",
                missing: "no model configured for this provider",
                note: "One call per run — the folder's category scheme.",
              }),
              configRow({
                label: "Classifier model",
                mark: delegateMark("classifier"),
                value: provider.classifier_model,
                source: sources["classifier.model"],
                env: "FILE_ORGANISER_CLASSIFIER_MODEL",
                missing: "no model configured for this provider",
                note: "One call per file — where cost accumulates, so a cheaper model here is deliberate.",
              }),
              renderEffortRow(sources),
            )
          : null,

        h(
          "div",
          { class: "cfg-foot" },
          h(
            "div",
            { class: "kv" },
            h("span", { class: "kv-key" }, "Config file"),
            pathText(cfgPath || "—", true),
          ),
          h(
            "p",
            { class: "tiny muted" },
            broken
              ? "Fix that file — or delete it to fall back to the built-in defaults. "
              : "That file need not exist: without one, every setting falls back to its built-in default. ",
            "API keys are never read from it: they stay in " +
              (PROVIDER_KEY_ENV[provider.name] || "the provider's own environment variable") +
              ".",
          ),
          h(
            "p",
            { class: "cfg-status " + (classifier.available ? "is-ok" : "is-off") },
            h("span", { class: "dot" }),
            classifier.available ? "Delegates ready" : "Delegates unavailable",
            classifier.detail ? h("span", { class: "cfg-detail" }, classifier.detail) : null,
          ),
        ),
      ),
    );
  }

  /**
   * /health reports where the reasoning-effort settings came from but not what
   * they resolved to, so this row shows provenance only rather than inventing
   * a value.
   */
  function renderEffortRow(sources) {
    const schemeSource = sources["scheme.effort"];
    const classifierSource = sources["classifier.effort"];
    if (!schemeSource && !classifierSource) return null;
    return h(
      "div",
      { class: "cfg-row" },
      h("span", { class: "cfg-key" }, "Reasoning effort"),
      h(
        "span",
        { class: "cfg-val" },
        h(
          "span",
          { class: "cfg-efforts" },
          h(
            "span",
            { class: "cfg-effort", title: DELEGATE_TITLE.scheme },
            delegateMark("scheme", "xs"),
            sourceBadge(schemeSource) || h("span", { class: "cfg-missing" }, "unknown"),
          ),
          h(
            "span",
            { class: "cfg-effort", title: DELEGATE_TITLE.classifier },
            delegateMark("classifier", "xs"),
            sourceBadge(classifierSource) || h("span", { class: "cfg-missing" }, "unknown"),
          ),
        ),
        h("span", { class: "cfg-env" }, "FILE_ORGANISER_SCHEME_EFFORT · FILE_ORGANISER_CLASSIFIER_EFFORT"),
        h("span", { class: "tiny muted" }, "/health reports where each was set, not the value itself."),
      ),
    );
  }

  /** One setting: what it is, what it resolved to, and which layer decided. */
  function configRow(opts) {
    const value = opts.value;
    return h(
      "div",
      { class: "cfg-row" },
      h("span", { class: "cfg-key" }, opts.mark || null, opts.label),
      h(
        "span",
        { class: "cfg-val" },
        value
          ? h("code", { class: "path path-strong cfg-value" }, String(value))
          : h("span", { class: "cfg-missing" }, opts.missing || "not set"),
        opts.env ? h("span", { class: "cfg-env" }, opts.env) : null,
        opts.note ? h("span", { class: "tiny muted" }, opts.note) : null,
      ),
      // A source is only meaningful for a value that exists: the backend still
      // reports "default" for a model that has no default, which would read as
      // a claim that something was set.
      value ? sourceBadge(opts.source) : null,
    );
  }

  /* ------------------------------------------------------------------ *
   * Render: pipeline
   * ------------------------------------------------------------------ */

  /**
   * The three stages as data. `status` is one of:
   *   idle | run | done | skip
   */
  function pipelineStages() {
    const plan = state.plan;
    const scanning = state.busy === "scan";
    const stage = state.scanStage;
    const known = classifierAvailable();
    /* A rendered plan is labelled by the mode that built it; only a run with no
     * plan yet is labelled by the live toggle. Otherwise a plan reopened from
     * history would be mislabelled the moment the toggle changed. */
    const rulesOff = plan && !scanning ? planSkippedRules(plan) : state.skipRules;

    const stages = [
      { key: "rules", name: "Rule pass", icon: "rules", note: "extension & filename table — local, instant" },
      { key: "scheme", name: "Proposed scheme", icon: "scheme", note: "one model call names this folder's categories" },
      { key: "assign", name: "Assignments", icon: "classifier", note: "one model call per remaining file" },
    ];

    if (scanning) {
      const order = ["rules", "scheme", "assign"];
      const at = order.indexOf(stage || "rules");
      for (const s of stages) {
        const idx = order.indexOf(s.key);
        if (rulesOff && s.key === "rules") {
          s.status = "skip";
          s.note = SKIPPED_RULES_NOTE.live;
        } else if (known === false && s.key !== "rules") {
          s.status = "skip";
          s.note = "classifier unavailable — skipped";
        } else if (idx < at) {
          s.status = "done";
          s.note = "done";
        } else if (idx === at) {
          s.status = "run";
          s.note = RUNNING_NOTE[s.key];
        } else {
          s.status = "idle";
          s.note = "waiting";
        }
      }
      return stages;
    }

    if (!plan) {
      for (const s of stages) s.status = "idle";
      if (rulesOff) {
        stages[0].status = "skip";
        stages[0].note = "skipped by choice — every file goes to the agents";
      }
      if (known === false) {
        stages[1].status = "skip";
        stages[1].note = "classifier unavailable";
        stages[2].status = "skip";
        stages[2].note = rulesOff
          ? "nothing would be classified at all"
          : "unmatched files will be left alone";
      }
      return stages;
    }

    const summary = plan.summary || {};
    const byAgent = summary.by_agent || {};
    const proposed = proposedCategories();
    const classified = byAgent.classifier || 0;

    if (rulesOff) {
      // Not "0 files placed": the table never ran, and a zero would read as a
      // table that ran and matched nothing.
      stages[0].status = "skip";
      stages[0].note = SKIPPED_RULES_NOTE.done;
    } else {
      stages[0].status = "done";
      stages[0].count = byAgent.rules || 0;
      stages[0].note = plural(byAgent.rules || 0, "file placed", "files placed") + " by table lookup";
    }

    if (plan.classifier_available === false) {
      stages[1].status = "skip";
      stages[1].note = "classifier unavailable — no scheme proposed";
      stages[2].status = "skip";
      stages[2].note = plural(summary.to_skip || 0, "file left alone", "files left alone") + ", never guessed";
    } else if (!proposed.length && !classified) {
      stages[1].status = "skip";
      stages[1].note = rulesOff ? "no scheme was proposed" : "not needed — the rules placed everything";
      stages[2].status = "skip";
      stages[2].note = rulesOff ? "no file was assigned" : "nothing to delegate";
    } else {
      stages[1].status = proposed.length ? "done" : "skip";
      stages[1].count = proposed.length;
      stages[1].note = proposed.length
        ? plural(proposed.length, "category proposed", "categories proposed") +
          " (" + proposed.filter((c) => c.is_new).length + " new)"
        : "no scheme could be proposed";
      stages[2].status = classified ? "done" : "skip";
      stages[2].count = classified;
      stages[2].note = classified
        ? plural(classified, "file assigned", "files assigned") + " by the classifier"
        : "no files needed assigning";
    }
    return stages;
  }

  /* The rule table did not run, as opposed to running and matching nothing. */
  const SKIPPED_RULES_NOTE = {
    live: "skipped — every file goes to the agents",
    done: "skipped — every file went to the agents",
  };

  const RUNNING_NOTE = {
    rules: "matching extensions…",
    scheme: "reading every unmatched file, proposing categories…",
    assign: "placing each remaining file…",
  };

  function renderPipeline() {
    const host = $("pipeline");
    if (!host) return;
    const stages = pipelineStages();

    const grid = h("div", { class: "pipeline" });
    stages.forEach((s, index) => {
      if (index > 0) {
        grid.appendChild(h("span", { class: "stage-link" }, icon("caret", 13)));
      }
      grid.appendChild(
        h(
          "div",
          { class: "stage stage-" + s.key + " is-" + s.status },
          h(
            "span",
            { class: "stage-ico" },
            s.status === "run" ? h("span", { class: "spinner" }) : icon(s.icon, 13),
          ),
          h(
            "span",
            { class: "stage-body" },
            h(
              "span",
              { class: "stage-name" },
              s.name,
              s.count !== undefined && s.status === "done"
                ? h("span", { class: "stage-count muted" }, " · " + s.count)
                : null,
            ),
            h("span", { class: "stage-note" }, s.note),
          ),
        ),
      );
    });

    const children = [grid];
    // Same rule as the stages: a plan is described by the mode that built it,
    // anything else by the live toggle.
    const rulesOff =
      state.plan && state.busy !== "scan" ? planSkippedRules(state.plan) : state.skipRules;

    if (rulesOff && state.plan && state.busy !== "scan") {
      // The "grouped by subject" claim only holds if something was grouped.
      const grouped = ((state.plan.summary || {}).to_move || 0) > 0;
      children.push(
        h(
          "p",
          { class: "note note-agents" },
          delegateMark("classifier", "xs"),
          " Rule table skipped for this plan — nothing was placed by extension",
          grouped
            ? [
                ", so these groups are by what the files are ",
                h("em", {}, "about"),
                ", not by file type.",
              ]
            : ".",
        ),
      );
    }
    // With no plan yet, the blocked scan bar directly above already says all of
    // this and carries the ways out — a second red block here would be noise.
    const scanBarOwnsIt = scanBlocked() && !state.plan;
    if (classifierAvailable() === false && !scanBarOwnsIt) {
      // Unavailable has several causes now — a broken config file, a provider
      // with no model named, or no credentials — and they have different
      // fixes, so the note says which one it is.
      const reason = unavailableReason();
      const configLink = providerInfo()
        ? h(
            "button",
            { type: "button", class: "linkbtn", onclick: () => openProviderPanel() },
            "show configuration",
          )
        : null;
      children.push(
        // With the rule table off as well, there is no pass left at all — a
        // strictly worse state than "rules-only", so it gets its own note.
        rulesOff
          ? h(
              "p",
              { class: "note note-fail" },
              icon("warn", 12),
              " No pass can run: the rule table is skipped and the agents are unavailable, so ",
              h("strong", {}, "every file is left exactly where it is"),
              ". Turn the rule pass back on, or fix the configuration. ",
              configLink,
              reason.text ? h("span", { class: "muted" }, " (" + reason.text + ")") : null,
            )
          : reason.kind === "config"
            ? h(
                "p",
                { class: "note note-fail" },
                icon("warn", 12),
                " Broken configuration — no provider could be resolved, so ",
                h("strong", {}, "nothing can be delegated"),
                ". This is not a missing API key; the fix is editing ",
                h("code", { class: "path" }, configFilePath()),
                ". The rule pass still runs, and everything it can't match is left alone. ",
                configLink,
                // The exact message is in the config surface the link opens;
                // repeating it here would be three copies on one screen.
              )
            : h(
                "p",
                { class: "note note-warn" },
                icon("warn", 12),
                " Rules-only mode. Files the extension table can't place are ",
                h("strong", {}, "left exactly where they are"),
                " — no category is ever guessed for them. ",
                configLink,
                reason.text ? h("span", { class: "muted" }, " (" + reason.text + ")") : null,
              ),
      );
    }
    mount(host, ...children);
  }

  /* ------------------------------------------------------------------ *
   * Render: banner
   * ------------------------------------------------------------------ */

  function renderBanner() {
    const el = $("banner");
    if (!el) return;
    if (!state.banner) {
      el.hidden = true;
      mount(el);
      return;
    }
    el.hidden = false;
    el.className = "banner banner-" + state.banner.kind;
    mount(el, 
      state.banner.kind === "error" ? icon("warn", 14) : null,
      h("span", { class: "banner-body" }, state.banner.message),
      state.banner.retry
        ? h("button", { type: "button", class: "linkbtn", onclick: state.banner.retry }, "try again")
        : null,
      h(
        "button",
        {
          type: "button",
          class: "banner-close",
          title: "Dismiss",
          "aria-label": "Dismiss message",
          onclick: () => {
            state.banner = null;
            renderBanner();
          },
        },
        "×",
      ),
    );
  }

  function setBanner(kind, message, retry) {
    state.banner = message ? { kind, message, retry: retry || null } : null;
    renderBanner();
  }

  /* ------------------------------------------------------------------ *
   * Render: plan view
   * ------------------------------------------------------------------ */

  function renderPlanView() {
    const view = $("plan-view");
    if (!view) return;
    view.className = "view" + (state.density === "compact" ? " compact" : "");

    if (state.busy === "scan") {
      mount(view, renderScanLoading());
      return;
    }
    if (!state.plan) {
      mount(view, renderNoPlan());
      return;
    }
    if (!state.plan.items.length) {
      mount(view, renderEmptyScan());
      return;
    }

    mount(view, 
      renderPlanBar(),
      state.result ? renderResults() : null,
      state.pendingConfirm ? renderConfirm() : null,
      renderSchemePanel(),
      renderToolbar(),
      renderReviewSection(),
      renderGroups(),
      renderSkipped(),
      renderApplyBar(),
    );
    syncSelection();
  }

  /* --- empty / loading states --------------------------------------- */

  function renderNoPlan() {
    const known = classifierAvailable();
    return h(
      "section",
      { class: "card empty" },
      h("h2", {}, "No plan yet"),
      h(
        "p",
        {},
        "Point the box above at a folder and press Dry run. The tool reads the folder, " +
          "writes a plan, and shows you every proposed move. It does not touch a single file " +
          "until you tick items and press Apply.",
      ),
      h(
        "ul",
        { class: "empty-steps" },
        h(
          "li",
          { class: "empty-step" + (state.skipRules ? " is-off" : "") },
          agentMark("rules"),
          state.skipRules
            ? h(
                "span",
                {},
                h("b", {}, "Rule pass"),
                " — ",
                h("strong", {}, "turned off for the next run"),
                ". Every file goes to the two agents below instead, so the grouping follows what " +
                  "the files are about rather than their extensions.",
              )
            : h(
                "span",
                {},
                h("b", {}, "Rule pass"),
                " — a local extension and filename table places the easy files. Instant, no model call.",
              ),
        ),
        h(
          "li",
          { class: "empty-step" },
          h("span", { class: "mark mark-classifier" }, icon("scheme", 11)),
          h(
            "span",
            {},
            h("b", {}, "Scheme proposer"),
            " — one agent reads everything the rules couldn't place, all at once, and invents the category names ",
            h("em", {}, "for this folder"),
            ". There is no fixed category list.",
          ),
        ),
        h(
          "li",
          { class: "empty-step" },
          agentMark("classifier"),
          h("span", {}, h("b", {}, "Classifier"), " — a second agent assigns each remaining file to one of those proposed categories."),
        ),
      ),
      // Same reason as the pipeline: when the scan bar is blocked it already
      // explains this, one screenful above, with the two ways out.
      known === false && !scanBlocked() ? renderRulesOnlyNote() : null,
    );
  }

  /** The empty state's version of "why only the rule pass can run". */
  function renderRulesOnlyNote() {
    const reason = unavailableReason();
    const broken = reason.kind === "config";
    return h(
      "p",
      { class: "note " + (broken ? "note-fail" : "note-warn") },
      broken
        ? "The configuration is broken, so no provider could be resolved and neither agent can run. "
        : "Right now only the rule pass can run. ",
      "Files it can't match will be listed as skipped and left alone.",
      reason.text ? h("span", { class: "muted" }, " (" + reason.text + ")") : null,
      providerInfo()
        ? h(
            "button",
            {
              type: "button",
              class: "linkbtn",
              style: "margin-left:8px",
              onclick: () => openProviderPanel(),
            },
            broken ? "fix the configuration" : "show configuration",
          )
        : null,
    );
  }

  function renderEmptyScan() {
    return h(
      "section",
      { class: "card empty" },
      h("h2", {}, "Nothing to organise"),
      h("p", {}, "The scan found no files in ", pathText(state.plan.target_dir, true), "."),
      h(
        "p",
        { class: "small" },
        "Sub-folders are not descended into and hidden files are left alone, so an apparently " +
          "full folder can still come back empty.",
      ),
    );
  }

  function renderScanLoading() {
    const stage = state.scanStage || "rules";
    const known = classifierAvailable();
    const rulesOff = state.skipRules;
    const title =
      known === false
        ? "Running the rule pass…"
        : { rules: "Matching extensions…", scheme: "Proposing a category scheme…", assign: "Assigning files to categories…" }[stage];
    const detail =
      known === false
        ? "Rules-only mode: no model calls will be made."
        : {
            rules: "Local table lookup — this part is instant.",
            scheme: rulesOff
              ? "One model call is reading every file in the folder, together, so the categories fit this folder."
              : "One model call is reading every file the rules could not place, together, so the categories fit this folder.",
            assign: rulesOff
              ? "One model call per file — every file, since the rule table was skipped. Larger folders take longer."
              : "One model call per remaining file. Larger folders take longer.",
          }[stage];

    return h(
      "section",
      { class: "card" },
      h(
        "div",
        { class: "loading" },
        h("span", { class: "spinner", style: "color: var(--accent)" }),
        h(
          "span",
          { class: "loading-body" },
          h("span", { class: "loading-title" }, title),
          h("span", { class: "muted small" }, detail),
          h(
            "span",
            { class: "faint tiny", id: "scan-elapsed" },
            "elapsed " + state.scanElapsed + "s · nothing is being moved",
          ),
        ),
      ),
      h(
        "div",
        { class: "skeleton-list" },
        h("div", { class: "skeleton" }),
        h("div", { class: "skeleton" }),
        h("div", { class: "skeleton" }),
        h("div", { class: "skeleton" }),
      ),
    );
  }

  /* --- sticky summary bar -------------------------------------------- */

  function renderPlanBar() {
    const plan = state.plan;
    const summary = plan.summary || {};
    const byAgent = summary.by_agent || {};
    return h(
      "div",
      { class: "planbar" },
      h(
        "div",
        { class: "planbar-target" },
        icon("folder", 13),
        h("code", { class: "path path-strong", title: plan.target_dir }, tailOf(plan.target_dir)),
        h("span", { class: "faint tiny nowrap" }, "plan " + plan.plan_id),
      ),
      h(
        "div",
        { class: "chips" },
        chip(summary.total_files, " files"),
        chip(summary.to_move, " to move"),
        // A "0 by rules" chip would read as a table that ran and matched
        // nothing. When it never ran, say that instead.
        planSkippedRules(plan)
          ? h(
              "span",
              {
                class: "chip chip-skip",
                title: "The rule table was skipped for this plan — every file went to the agents.",
              },
              icon("rules", 11),
              "rules skipped",
            )
          : chip(byAgent.rules || 0, " by rules", "chip-rules"),
        chip(byAgent.classifier || 0, " by agent", "chip-agent"),
        summary.needs_review ? chip(summary.needs_review, " need review", "chip-review") : null,
        summary.to_skip ? chip(summary.to_skip, " skipped") : null,
      ),
      h(
        "div",
        { class: "planbar-actions" },
        h(
          "button",
          {
            type: "button",
            class: "linkbtn",
            title: "Toggle row density",
            onclick: () => {
              state.density = state.density === "compact" ? "comfortable" : "compact";
              renderPlanView();
            },
          },
          state.density === "compact" ? "comfortable" : "compact",
        ),
      ),
      plan.applied_at
        ? h("p", { class: "note note-done" }, "Applied " + formatTime(plan.applied_at) + ".")
        : null,
    );
  }

  /* --- scheme panel --------------------------------------------------- */

  function renderSchemePanel() {
    const plan = state.plan;
    const proposed = proposedCategories();
    const counts = categoryCounts();

    if (!proposed.length) {
      if (plan.classifier_available === false) {
        return h(
          "section",
          { class: "card" },
          h(
            "div",
            { class: "card-title" },
            h("h2", {}, "No scheme was proposed"),
            h(
              "p",
              { class: "muted small" },
              "The scheme proposer needs the classifier delegate, which was unavailable for this scan" +
                (plan.classifier_detail ? " (" + plan.classifier_detail + ")" : "") +
                ". " +
                (planSkippedRules(plan)
                  ? "The rule table was skipped for this run too, so no pass could place anything: every file is under Skipped, untouched."
                  : "Only the rule pass ran; everything it could not match is under Skipped, untouched."),
            ),
          ),
        );
      }
      const delegated = (plan.summary && plan.summary.by_agent && plan.summary.by_agent.classifier) || 0;
      return h(
        "section",
        { class: "card" },
        h(
          "div",
          { class: "card-title" },
          h("h2", {}, "No new categories needed"),
          h(
            "p",
            { class: "muted small" },
            delegated
              ? "Every delegated file fit a folder that already existed — nothing new was invented for this run."
              : planSkippedRules(plan)
                ? "The rule table was skipped and no file reached the classifier, so no scheme was proposed."
                : "The extension rules placed every file, so no scheme proposer call was made.",
          ),
        ),
      );
    }

    const newCount = proposed.filter((c) => c.is_new).length;
    return h(
      "section",
      { class: "card scheme" },
      h(
        "div",
        { class: "card-head" },
        h(
          "div",
          { class: "card-title" },
          h("span", { class: "scheme-eyebrow" }, icon("scheme", 12), "proposed for this folder"),
          h(
            "h2",
            {},
            plural(proposed.length, "category", "categories"),
            newCount ? ", " + newCount + " newly invented" : ", all reused",
          ),
          plan.scheme_reason
            ? h("p", { class: "scheme-reason" }, plan.scheme_reason)
            : h("p", { class: "scheme-reason faint" }, "The proposer gave no rationale for this grouping."),
        ),
      ),
      h(
        "div",
        { class: "scheme-grid" },
        proposed.map((cat) => renderSchemeCard(cat, counts.get(cat.name) || 0)),
      ),
      h(
        "p",
        { class: "tiny faint" },
        "These names were written by the scheme proposer from your folder's contents — they are not " +
          "picked from a fixed list. Reused names are existing folders or rule categories.",
      ),
    );
  }

  function renderSchemeCard(cat, count) {
    const name = cat.name || "—";
    return h(
      "button",
      {
        type: "button",
        class: "scheme-card" + (cat.is_new ? " is-new" : ""),
        title: "Filter the plan to " + name,
        onclick: () => {
          state.filter = { text: state.filter.text === name ? "" : name, lens: "all" };
          renderPlanView();
        },
      },
      h(
        "span",
        { class: "scheme-card-top" },
        h("span", { class: "scheme-name" }, name),
        cat.is_new
          ? badge("new", "new", "Invented for this run — this folder does not exist yet")
          : badge("reused", "kept", "Reused: this folder already exists, or it is a rule category"),
      ),
      cat.description ? h("span", { class: "scheme-desc" }, cat.description) : null,
      h("span", { class: "scheme-count" }, count ? plural(count, "file lands here", "files land here") : "no files landed here"),
    );
  }

  /* --- filter toolbar -------------------------------------------------- */

  function renderToolbar() {
    const shown = visibleMoveItems().length;
    const total = moveItems().length;
    // "By rules" would be a lens onto an empty set for a plan the rule table
    // never ran for, so it is dropped rather than shown as a dead filter.
    const lenses = [
      ["all", "All"],
      ["review", "Needs review"],
      ["rules", "By rules"],
      ["classifier", "By agent"],
    ].filter(([key]) => !(key === "rules" && planSkippedRules(state.plan)));
    const allIds = selectableIds(visibleMoveItems());

    return h(
      "div",
      { class: "card toolbar" },
      h("input", {
        type: "search",
        id: "plan-filter",
        placeholder: "Filter by filename, category or destination…",
        value: state.filter.text,
        "aria-label": "Filter files",
        oninput: (event) => {
          state.filter.text = event.target.value;
          rerenderPlanKeepingFocus("plan-filter");
        },
      }),
      h(
        "div",
        { class: "segmented", role: "group", "aria-label": "Filter by decision" },
        lenses.map(([key, label]) =>
          h(
            "button",
            {
              type: "button",
              class: state.filter.lens === key ? "on" : "",
              "aria-pressed": state.filter.lens === key ? "true" : "false",
              onclick: () => {
                state.filter.lens = key;
                renderPlanView();
              },
            },
            label,
          ),
        ),
      ),
      filterActive()
        ? h(
            "span",
            { class: "small muted" },
            "showing " + shown + " of " + total,
            h(
              "button",
              {
                type: "button",
                class: "linkbtn",
                style: "margin-left:8px",
                onclick: () => {
                  state.filter = { text: "", lens: "all" };
                  renderPlanView();
                },
              },
              "clear",
            ),
          )
        : null,
      h(
        "div",
        { class: "select-controls", style: "margin-left:auto" },
        h("span", { class: "muted small" }, filterActive() ? "shown:" : "all:"),
        h(
          "button",
          { type: "button", class: "linkbtn", onclick: () => setApprovedMany(allIds, true) },
          "approve all",
        ),
        h(
          "button",
          { type: "button", class: "linkbtn", onclick: () => setApprovedMany(allIds, false) },
          "approve none",
        ),
      ),
    );
  }

  /** Re-render, then put the caret back where the user left it. */
  function rerenderPlanKeepingFocus(id) {
    const before = document.getElementById(id);
    const caret = before && before.selectionStart;
    renderPlanView();
    const after = document.getElementById(id);
    if (after) {
      after.focus();
      if (caret !== null && caret !== undefined && after.setSelectionRange) {
        try {
          after.setSelectionRange(caret, caret);
        } catch (err) {
          /* type=search may refuse setSelectionRange in some engines */
        }
      }
    }
  }

  /* --- rows ------------------------------------------------------------ */

  /**
   * One selectable file row. `variant` only changes styling.
   * Renders as a <label> so the whole row is a click target for the checkbox.
   */
  function renderItemRow(item, variant) {
    const moved = state.movedIds.has(item.item_id);
    const failed = state.failedIds.has(item.item_id);
    const dest = destinationParts(item, state.plan ? state.plan.target_dir : "");
    const agent = item.decided_by === "rules" || item.decided_by === "classifier" ? item.decided_by : "none";

    return h(
      "label",
      {
        class:
          "row row-" +
          agent +
          (variant ? " row-" + variant : "") +
          (moved ? " row-moved" : "") +
          (failed ? " row-failed" : ""),
      },
      h("input", {
        type: "checkbox",
        class: "row-check",
        disabled: moved,
        dataset: { itemId: item.item_id },
        onchange: (event) => setApproved(item.item_id, event.target.checked),
      }),
      h("span", { class: "row-mark" }, agentMark(item.decided_by)),
      h(
        "span",
        { class: "row-body" },
        h(
          "span",
          { class: "row-name" },
          h("span", { class: "filename", title: item.source || item.filename }, item.filename),
          h("span", { class: "size" }, formatBytes(item.size_bytes)),
          h(
            "span",
            { class: "row-flags" },
            confidenceMeter(item.confidence),
            item.needs_review ? badge("review", "review", "Low or medium confidence — approve deliberately") : null,
            moved ? badge("moved", "done") : null,
            failed ? badge("failed", "fail") : null,
          ),
        ),
        renderDestination(dest),
        item.reason ? h("span", { class: "row-reason", title: item.reason }, item.reason) : null,
      ),
    );
  }

  function renderDestination(dest) {
    if (!dest) return h("span", { class: "dest-none muted" }, "stays where it is");
    return h(
      "span",
      { class: "dest", title: dest.full },
      h("span", { class: "dest-arrow" }, "→"),
      dest.inside ? h("span", { class: "dest-dir" }, "./") : null,
      dest.folder ? h("span", { class: "dest-folder" }, dest.folder + "/") : null,
      h("span", { class: "dest-file" + (dest.renamed ? " rename" : "") }, dest.file),
      dest.renamed ? badge("renamed", "review", "The destination filename differs — a name clash was resolved") : null,
    );
  }

  /* --- review ---------------------------------------------------------- */

  function renderReviewSection() {
    const items = visibleMoveItems().filter((i) => i.needs_review);
    if (!items.length) return null;
    const ids = selectableIds(items);
    return h(
      "section",
      { class: "card review-card" },
      h(
        "div",
        { class: "card-head" },
        h(
          "div",
          { class: "card-title" },
          h("h2", { class: "review-title" }, icon("warn", 14), "Needs your review"),
          h(
            "p",
            { class: "muted small" },
            plural(items.length, "file was", "files were") +
              " placed by the classifier agent without high confidence. " +
              "These start unchecked — tick only the ones you agree with.",
          ),
        ),
        selectionControls(ids),
      ),
      h("div", { class: "rows" }, items.map((item) => renderItemRow(item, "review"))),
    );
  }

  /* --- category groups --------------------------------------------------- */

  function renderGroups() {
    const groups = categoryGroups();
    if (!groups.length) {
      return h(
        "section",
        { class: "card" },
        h(
          "p",
          { class: "placeholder" },
          filterActive() ? "No files match this filter." : "No moves proposed — every file stays put.",
        ),
      );
    }
    return h("section", { class: "groups" }, groups.map(([name, bucket]) => renderGroup(name, bucket)));
  }

  function renderGroup(name, bucket) {
    const ids = selectableIds(bucket.confident);
    const proposed = proposedCategories().find((c) => c.name === name);
    const open = groupIsOpen(name);
    const agents = [...bucket.agents];
    const agentKey = agents.length === 1 ? agents[0] : "mixed";
    const total = bucket.confident.length + bucket.review;

    return h(
      "details",
      {
        class: "card group",
        open,
        ontoggle: (event) => {
          state.groupOpen[name] = event.target.open;
        },
      },
      h(
        "summary",
        {},
        h("span", { class: "group-caret" }, icon("caret", 12)),
        agentKey === "mixed" ? h("span", { class: "mark" }, icon("folder", 11)) : agentMark(agentKey),
        h("span", { class: "group-name" }, name),
        proposed && proposed.is_new ? badge("new", "new", "Category invented for this run") : null,
        h(
          "span",
          { class: "group-meta" },
          bucket.review ? chip(bucket.review, " review", "chip-review") : null,
          h(
            "span",
            {
              class: "count muted small",
              title: "approved / approvable in this category",
              dataset: { countIds: ids.join(","), countStyle: "short" },
            },
            "0/" + ids.length,
          ),
          chip(total, total === 1 ? " file" : " files"),
        ),
      ),
      h(
        "div",
        { class: "group-body" },
        proposed && proposed.description
          ? h("p", { class: "placeholder tight small" }, proposed.description)
          : null,
        ids.length
          ? h(
              "div",
              { class: "group-tools" },
              selectionControls(ids),
              bucket.review
                ? h(
                    "span",
                    { class: "muted small" },
                    bucket.review === 1
                      ? "1 more in this category is up in Needs review"
                      : bucket.review + " more in this category are up in Needs review",
                  )
                : null,
            )
          : null,
        bucket.confident.length
          ? h("div", { class: "rows" }, bucket.confident.map((item) => renderItemRow(item)))
          : h("p", { class: "placeholder tight" }, "Every file in this category needs review — see above."),
      ),
    );
  }

  function groupIsOpen(name) {
    if (Object.prototype.hasOwnProperty.call(state.groupOpen, name)) return state.groupOpen[name];
    return !state.collapseByDefault;
  }

  /* --- skipped ---------------------------------------------------------- */

  function renderSkipped() {
    const items = skipItems();
    if (!items.length) return null;
    return h(
      "details",
      { class: "card skipped" },
      h(
        "summary",
        {},
        h("span", { class: "group-caret" }, icon("caret", 12)),
        h("strong", {}, "Skipped"),
        h("span", { class: "muted" }, plural(items.length, "file left exactly where it is", "files left exactly where they are")),
      ),
      h(
        "div",
        { class: "skipped-body rows" },
        items.map((item) =>
          h(
            "div",
            { class: "row row-static row-" + (item.decided_by === "rules" ? "rules" : "none") },
            h("span", { class: "row-mark" }, agentMark(item.decided_by)),
            h(
              "span",
              { class: "row-body" },
              h(
                "span",
                { class: "row-name" },
                h("span", { class: "filename" }, item.filename),
                h("span", { class: "size" }, formatBytes(item.size_bytes)),
              ),
              h("span", { class: "dest-none muted" }, "stays where it is"),
              item.reason ? h("span", { class: "row-reason", title: item.reason }, item.reason) : null,
            ),
          ),
        ),
      ),
    );
  }

  /* --- apply bar --------------------------------------------------------- */

  function renderApplyBar() {
    const applied = !!(state.plan && state.plan.applied_at);
    const busy = state.busy === "apply";
    return h(
      "div",
      { class: "applybar" },
      h("span", { id: "approve-count", class: "approve-count" }, "0 approved"),
      h(
        "span",
        { class: "applybar-note" },
        busy
          ? "Moving files…"
          : applied
            ? "Already applied — re-applying only touches files still in place."
            : filterActive()
              ? "Only checked items move — including any hidden by the current filter."
              : "Only checked items move.",
      ),
      h(
        "button",
        {
          type: "button",
          id: "apply-btn",
          class: "btn btn-primary",
          disabled: true,
          onclick: () => doApply(false),
        },
        busy ? "Applying…" : "Apply approved moves",
      ),
    );
  }

  /* --- first-apply confirm ------------------------------------------------ */

  function renderConfirm() {
    const pending = state.pendingConfirm;
    return h(
      "section",
      { class: "card confirm-card" },
      h("h2", {}, "First apply to this folder"),
      h(
        "p",
        {},
        "This tool has never moved anything inside ",
        pathText(pending.target_dir, true),
        ". Confirming will create the category sub-folders there and move ",
        plural(pending.approved_ids.length, "approved file", "approved files"),
        " into them. Every move is written to the undo log first.",
      ),
      pending.message ? h("p", { class: "muted small" }, pending.message) : null,
      h(
        "div",
        { class: "confirm-actions" },
        h(
          "button",
          { type: "button", class: "btn btn-primary", onclick: () => doApply(true) },
          "Yes, apply to this folder",
        ),
        h(
          "button",
          {
            type: "button",
            class: "btn btn-ghost",
            onclick: () => {
              state.pendingConfirm = null;
              renderPlanView();
            },
          },
          "Cancel",
        ),
      ),
    );
  }

  /* --- results ------------------------------------------------------------ */

  function renderResults() {
    const result = state.result;
    const counts = result.counts || {};
    const failed = result.failed || [];
    const moved = result.moved || [];
    const skipped = result.skipped || [];

    return h(
      "section",
      { class: "card results-card" + (failed.length ? " has-failures" : "") },
      h(
        "div",
        { class: "card-head" },
        h("h2", {}, failed.length ? "Applied with failures" : "Applied"),
        h(
          "div",
          { class: "chips" },
          chip(counts.moved !== undefined ? counts.moved : moved.length, " moved", "chip-ok"),
          chip(counts.skipped !== undefined ? counts.skipped : skipped.length, " skipped"),
          chip(
            counts.failed !== undefined ? counts.failed : failed.length,
            " failed",
            failed.length ? "chip-fail" : null,
          ),
        ),
      ),
      failed.length
        ? h(
            "div",
            { class: "result-block block-fail" },
            h("h3", {}, icon("warn", 12), " Failed — these files did not move"),
            failed.map((row) =>
              h(
                "div",
                { class: "result-row" },
                h("span", { class: "filename" }, row.filename),
                h("span", { class: "result-detail" }, row.error || "unknown error"),
              ),
            ),
            h("p", { class: "tiny muted" }, "They are still selected, so pressing Apply again retries just these."),
          )
        : null,
      moved.length
        ? h(
            "details",
            { class: "result-block", open: failed.length === 0 },
            h("summary", {}, plural(moved.length, "file moved", "files moved")),
            moved.map((row) =>
              h(
                "div",
                { class: "result-row" },
                h("span", { class: "filename" }, row.filename),
                h("span", { class: "dest-arrow mono" }, "→"),
                pathText(row.destination),
                row.category ? badge(row.category, "kept") : null,
              ),
            ),
          )
        : null,
      skipped.length
        ? h(
            "details",
            { class: "result-block" },
            h("summary", {}, plural(skipped.length, "file skipped", "files skipped")),
            skipped.map((row) =>
              h(
                "div",
                { class: "result-row" },
                h("span", { class: "filename" }, row.filename),
                h("span", { class: "muted" }, row.reason || ""),
              ),
            ),
          )
        : null,
      h("p", { class: "tiny muted" }, "Every move is recorded in the undo log — the History tab has the path."),
    );
  }

  /* ------------------------------------------------------------------ *
   * Render: history view
   * ------------------------------------------------------------------ */

  function renderHistoryView() {
    const view = $("history-view");
    if (!view) return;
    if (!state.history.loaded && state.busy === "history") {
      mount(view, 
        h(
          "section",
          { class: "card" },
          h(
            "div",
            { class: "loading" },
            h("span", { class: "spinner", style: "color: var(--accent)" }),
            h("span", { class: "loading-body" }, h("span", { class: "loading-title" }, "Loading history…")),
          ),
          h(
            "div",
            { class: "skeleton-list" },
            h("div", { class: "skeleton" }),
            h("div", { class: "skeleton" }),
            h("div", { class: "skeleton" }),
          ),
        ),
      );
      return;
    }
    if (state.history.error) {
      mount(view, 
        h(
          "section",
          { class: "card empty" },
          h("h2", {}, "History unavailable"),
          h("p", {}, state.history.error),
          h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: () => loadHistory(true) }, "Retry"),
        ),
      );
      return;
    }
    mount(view, renderUndoCard(), renderRecentPlans(), renderLogEntries());
  }

  function renderUndoCard() {
    const undoLog = state.history.undoLog || (state.health && state.health.undo_log) || "";
    const stateDir = state.health ? state.health.state_dir : "";
    return h(
      "section",
      { class: "card" },
      h(
        "div",
        { class: "card-head" },
        h(
          "div",
          { class: "card-title" },
          h("h2", {}, "Reversing a move"),
          h(
            "p",
            { class: "muted small" },
            "Every applied move is appended to the undo log as one JSON line. To reverse one by " +
              "hand, read the log and move the file from its destination back to its source.",
          ),
        ),
        h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: () => loadHistory(true) }, "Refresh"),
      ),
      h("div", { class: "kv" }, h("span", { class: "kv-key" }, "Undo log"), pathText(undoLog)),
      stateDir ? h("div", { class: "kv" }, h("span", { class: "kv-key" }, "State dir"), pathText(stateDir)) : null,
    );
  }

  function renderRecentPlans() {
    const plans = state.history.plans || [];
    return h(
      "section",
      { class: "card" },
      h("h2", {}, "Recent runs"),
      plans.length
        ? h(
            "div",
            { class: "table-scroll" },
            h(
              "table",
              { class: "table" },
              h(
                "thead",
                {},
                h(
                  "tr",
                  {},
                  h("th", {}, "Scanned"),
                  h("th", {}, "Folder"),
                  h("th", {}, "Outcome"),
                  h("th", {}, "Applied"),
                  h("th", {}, ""),
                ),
              ),
              h(
                "tbody",
                {},
                plans.map((plan) => {
                  const summary = plan.summary || {};
                  return h(
                    "tr",
                    {},
                    h("td", { class: "nowrap" }, formatTime(plan.created_at)),
                    h("td", {}, h("code", { class: "path", title: plan.target_dir }, tailOf(plan.target_dir))),
                    h(
                      "td",
                      { class: "nowrap" },
                      (summary.to_move || 0) +
                        " to move · " +
                        (summary.needs_review || 0) +
                        " review · " +
                        (summary.to_skip || 0) +
                        " skipped",
                    ),
                    h("td", { class: "nowrap" }, plan.applied_at ? formatTime(plan.applied_at) : "not applied"),
                    h(
                      "td",
                      {},
                      h(
                        "button",
                        { type: "button", class: "linkbtn", onclick: () => loadPlan(plan.plan_id) },
                        "open",
                      ),
                    ),
                  );
                }),
              ),
            ),
          )
        : h("p", { class: "placeholder" }, "No runs recorded yet. Run a dry run and it will show up here."),
    );
  }

  function renderLogEntries() {
    const entries = state.history.entries || [];
    return h(
      "section",
      { class: "card" },
      h("h2", {}, "Move log"),
      entries.length
        ? h(
            "div",
            { class: "table-scroll" },
            h(
              "table",
              { class: "table" },
              h(
                "thead",
                {},
                h(
                  "tr",
                  {},
                  h("th", {}, "When"),
                  h("th", {}, "Decided by"),
                  h("th", {}, "File"),
                  h("th", {}, "Moved to"),
                  h("th", {}, "Conf."),
                ),
              ),
              h(
                "tbody",
                {},
                entries.map((entry) =>
                  h(
                    "tr",
                    {},
                    h("td", { class: "nowrap" }, formatTime(entry.timestamp)),
                    h(
                      "td",
                      {},
                      h(
                        "span",
                        { class: "legend-item" },
                        agentMark(entry.decided_by),
                        h("span", { class: "small muted" }, AGENT_LABEL[entry.decided_by] || entry.decided_by || "—"),
                      ),
                    ),
                    h("td", {}, h("code", { class: "path", title: entry.source }, tailOf(entry.source, 1))),
                    h(
                      "td",
                      {},
                      h("span", { class: "dest-folder mono small" }, (entry.category || "—") + "/"),
                      h("code", { class: "path", title: entry.destination }, tailOf(entry.destination, 1)),
                    ),
                    h("td", {}, confidenceMeter(entry.confidence)),
                  ),
                ),
              ),
            ),
          )
        : h("p", { class: "placeholder" }, "Nothing has been moved yet."),
    );
  }

  /* ------------------------------------------------------------------ *
   * Actions
   * ------------------------------------------------------------------ */

  async function loadHealth() {
    state.healthError = null;
    renderHealth();
    try {
      state.health = await api.health();
    } catch (err) {
      state.health = null;
      state.healthError = err.message;
      setBanner("error", err.message, () => loadHealth());
    }
    // A broken config file is the one state worth opening unprompted: nothing
    // can be delegated until a file on disk is edited. Only ever once, so a
    // re-check the user asked for doesn't fight them.
    if (providerBroken() && !state.providerAutoOpened) {
      state.providerAutoOpened = true;
      state.providerOpen = true;
    }
    renderHealth();
    renderProviderPanel();
    // Availability decides whether a rules-off run is even worth starting.
    renderScanOption();
    renderPipeline();
    if (state.tab === "plan") renderPlanView();
  }

  function resetApplyState() {
    state.result = null;
    state.pendingConfirm = null;
    state.movedIds = new Set();
    state.failedIds = new Set();
  }

  /** Confident items start checked; review items start unchecked. */
  function seedPlanUi() {
    state.approved = new Set(moveItems().filter((i) => !i.needs_review).map((i) => i.item_id));
    state.filter = { text: "", lens: "all" };
    state.groupOpen = {};
    const groupCount = new Set(moveItems().map((i) => i.category || "Other")).size;
    state.collapseByDefault =
      moveItems().length > COLLAPSE_OVER_FILES || groupCount > COLLAPSE_OVER_GROUPS;
  }

  /** With the rule table skipped there is no rules stage to sit in. */
  function stageForElapsed(ms) {
    if (ms >= STAGE_AT_MS.assign) return "assign";
    if (ms >= STAGE_AT_MS.scheme || state.skipRules) return "scheme";
    return "rules";
  }

  function startScanClock() {
    stopScanClock();
    const startedAt = Date.now();
    state.scanStage = stageForElapsed(0);
    state.scanElapsed = 0;
    state.scanTimer = setInterval(() => {
      const ms = Date.now() - startedAt;
      state.scanElapsed = Math.floor(ms / 1000);
      const next = stageForElapsed(ms);
      const changed = next !== state.scanStage;
      state.scanStage = next;
      renderPipeline();
      if (changed) {
        renderPlanView();
      } else {
        const el = $("scan-elapsed");
        if (el) el.textContent = "elapsed " + state.scanElapsed + "s · nothing is being moved";
      }
    }, 500);
  }

  function stopScanClock() {
    if (state.scanTimer) clearInterval(state.scanTimer);
    state.scanTimer = null;
    state.scanStage = null;
  }

  async function doScan(event) {
    if (event) event.preventDefault();
    const targetInput = $("target-dir");
    const targetDir = targetInput.value.trim();
    if (!targetDir) {
      setBanner("error", "Enter the folder you want organised.");
      targetInput.focus();
      return;
    }
    const rawMax = $("max-delegations").value.trim();
    const maxDelegations = rawMax === "" ? null : Number(rawMax);
    if (maxDelegations !== null && (!Number.isInteger(maxDelegations) || maxDelegations < 0)) {
      setBanner("error", "Max file calls must be a whole number of zero or more.");
      return;
    }
    // The button is disabled in this state, but a keyboard submit can still
    // reach here — and this run could not classify a single file.
    if (scanBlocked()) {
      setBanner(
        "error",
        "The agents are unavailable, so with the rule table skipped this run would classify " +
          "nothing and leave every file in place. Untick “Let the agents classify every file”, " +
          "or fix the model configuration.",
      );
      renderScanOption();
      return;
    }
    const useRules = !state.skipRules;

    state.busy = "scan";
    setBanner(null, null);
    renderScanOption(); // disables the button and freezes the mode for this run
    switchTab("plan");
    startScanClock();
    renderPipeline();
    renderPlanView();

    try {
      const plan = await api.scan(targetDir, maxDelegations, useRules);
      state.plan = plan;
      resetApplyState();
      seedPlanUi();
      announceScan(plan);
    } catch (err) {
      state.plan = null;
      setBanner("error", err.message, () => doScan());
    } finally {
      state.busy = null;
      stopScanClock();
      renderScanOption(); // re-enables the button, and re-costs the next run
      renderPipeline();
      renderPlanView();
    }
  }

  function announceScan(plan) {
    const summary = plan.summary || {};
    if (!summary.total_files) {
      setBanner("info", "Dry run complete — that folder has no files to organise.");
      return;
    }
    const review = summary.needs_review || 0;
    const newCats = proposedCategories().filter((c) => c.is_new).length;
    const bits = ["Dry run complete — nothing has been moved."];
    if (planSkippedRules(plan)) {
      const delegated = (summary.by_agent && summary.by_agent.classifier) || 0;
      bits.push(
        delegated
          ? "The rule table was skipped, so all " +
            plural(delegated, "placement is", "placements are") +
            " the agents' own."
          : "The rule table was skipped, and the agents placed nothing.",
      );
    }
    if (newCats) bits.push(plural(newCats, "new category was", "new categories were") + " proposed for this folder.");
    bits.push(
      review
        ? plural(review, "file needs", "files need") + " your review before it can be approved."
        : "All proposed moves are high confidence.",
    );
    setBanner("info", bits.join(" "));
  }

  async function loadPlan(planId) {
    switchTab("plan");
    state.busy = "scan";
    startScanClock();
    renderPlanView();
    try {
      state.plan = await api.plan(planId);
      resetApplyState();
      seedPlanUi();
      const target = $("target-dir");
      if (target) target.value = state.plan.target_dir || "";
      setBanner("info", "Loaded plan " + planId + " — it is inert until you apply it.");
    } catch (err) {
      setBanner("error", err.message);
    } finally {
      state.busy = null;
      stopScanClock();
      // The target box now holds this plan's folder, so the estimate moves too.
      renderScanOption();
      renderPipeline();
      renderPlanView();
    }
  }

  async function doApply(confirmFirstApply) {
    if (!state.plan || state.busy === "apply") return;
    const approvedIds = [...state.approved];
    if (!approvedIds.length) return;

    state.busy = "apply";
    setBanner(null, null);
    renderPlanView();
    try {
      const result = await api.apply(state.plan.plan_id, approvedIds, confirmFirstApply);
      state.pendingConfirm = null;
      state.result = result;
      for (const row of result.moved || []) state.movedIds.add(row.item_id);
      state.failedIds = new Set((result.failed || []).map((row) => row.item_id));
      // Leave only the failures selected, so a retry is one click.
      state.approved = new Set(state.failedIds);

      const counts = result.counts || {};
      if (counts.failed) {
        setBanner("error", plural(counts.failed, "move failed", "moves failed") + " — see the results panel.");
      } else {
        setBanner("success", plural(counts.moved || 0, "file moved", "files moved") + " successfully.");
      }
      refreshPlanQuietly();
      loadHistory(true);
    } catch (err) {
      if (err.status === 409 && err.data && err.data.requires_confirmation) {
        // Never auto-confirm: park the request and let the user say yes.
        state.pendingConfirm = {
          target_dir: err.data.target_dir || state.plan.target_dir,
          approved_ids: approvedIds,
          message: err.data.error || "",
        };
      } else {
        setBanner("error", err.message);
      }
    } finally {
      state.busy = null;
      renderPlanView();
      if (state.pendingConfirm) {
        const card = document.querySelector(".confirm-card");
        if (card && card.scrollIntoView) card.scrollIntoView({ behavior: "smooth", block: "center" });
      }
    }
  }

  /** Pick up applied_at without disturbing the results panel. */
  async function refreshPlanQuietly() {
    if (!state.plan) return;
    const planId = state.plan.plan_id;
    try {
      const fresh = await api.plan(planId);
      if (state.plan && fresh.plan_id === state.plan.plan_id) {
        state.plan = fresh;
        renderPlanView();
      }
    } catch (err) {
      /* non-fatal: the results panel is still the source of truth */
    }
  }

  async function loadHistory(force) {
    if (state.history.loaded && !force) return;
    if (!state.history.loaded) state.busy = state.busy || "history";
    if (state.tab === "history") renderHistoryView();
    try {
      const [log, plans] = await Promise.all([api.log(100), api.plans(20)]);
      state.history = {
        entries: (log && log.entries) || [],
        undoLog: (log && log.undo_log) || "",
        plans: (plans && plans.plans) || [],
        loaded: true,
        error: null,
      };
    } catch (err) {
      state.history = Object.assign({}, state.history, { error: err.message, loaded: true });
    } finally {
      if (state.busy === "history") state.busy = null;
      if (state.tab === "history") renderHistoryView();
      // Past runs are the only source of a file count for the cost estimate.
      renderScanOption();
    }
  }

  function switchTab(tab) {
    state.tab = tab;
    for (const button of document.querySelectorAll(".tab")) {
      const active = button.dataset.tab === tab;
      button.setAttribute("aria-selected", active ? "true" : "false");
      button.classList.toggle("active", active);
    }
    $("plan-view").hidden = tab !== "plan";
    $("history-view").hidden = tab !== "history";
    if (tab === "history") {
      renderHistoryView();
      loadHistory(false);
    } else {
      renderPlanView();
    }
  }

  /* ------------------------------------------------------------------ *
   * Keyboard
   * ------------------------------------------------------------------ */

  function isTyping(target) {
    if (!target) return false;
    const tag = (target.tagName || "").toLowerCase();
    return tag === "input" || tag === "textarea" || tag === "select" || target.isContentEditable;
  }

  function onKeyDown(event) {
    if (event.metaKey || event.ctrlKey) {
      if (event.key === "Enter" && state.plan && state.approved.size && !state.busy) {
        event.preventDefault();
        doApply(false);
      }
      return;
    }
    if (event.altKey) return;
    if (isTyping(event.target)) {
      if (event.key === "Escape") event.target.blur();
      return;
    }
    if (event.key === "Escape" && state.providerOpen) {
      event.preventDefault();
      toggleProviderPanel(false);
    } else if (event.key === "/") {
      event.preventDefault();
      const box = $("target-dir");
      if (box) {
        box.focus();
        box.select();
      }
    } else if (event.key === "a" || event.key === "A") {
      if (!state.plan || state.busy) return;
      event.preventDefault();
      toggleAllVisible();
    } else if (event.key === "f" || event.key === "F") {
      const box = $("plan-filter");
      if (box) {
        event.preventDefault();
        box.focus();
      }
    }
  }

  /* ------------------------------------------------------------------ *
   * Init
   * ------------------------------------------------------------------ */

  function init() {
    const form = $("scan-form");
    if (form) form.addEventListener("submit", doScan);
    const skipBox = $("skip-rules");
    if (skipBox) skipBox.addEventListener("change", (event) => setSkipRules(event.target.checked));
    // Both inputs feed the cost estimate. Only the hint is re-mounted, so the
    // caret never leaves the box being typed in.
    for (const id of ["target-dir", "max-delegations"]) {
      const box = $(id);
      if (box) box.addEventListener("input", () => renderScanOption());
    }
    for (const button of document.querySelectorAll(".tab")) {
      button.addEventListener("click", () => switchTab(button.dataset.tab));
    }
    document.addEventListener("keydown", onKeyDown);
    renderScanOption();
    renderPipeline();
    renderPlanView();
    loadHealth();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  /* Exposed for console debugging and for headless render tests: everything
   * else stays inside the closure. */
  return {
    state,
    api,
    init,
    loadPlan,
    loadHistory,
    switchTab,
    setPlanForTest(plan, health) {
      if (health !== undefined) state.health = health;
      state.plan = plan;
      resetApplyState();
      seedPlanUi();
      renderHealth();
      renderProviderPanel();
      renderScanOption();
      renderPipeline();
      renderPlanView();
    },
    toggleProviderPanel,
    setSkipRules,
    render: {
      plan: renderPlanView,
      pipeline: renderPipeline,
      health: renderHealth,
      provider: renderProviderPanel,
      history: renderHistoryView,
      scanOption: renderScanOption,
    },
  };
})();
