/* mongochat dashboard: vanilla JS, no build step.
   Keys live in JS memory only: never localStorage, never cookies. */
"use strict";

const $ = (sel) => document.querySelector(sel);
const SVG_NS = "http://www.w3.org/2000/svg";
const compactNumber = new Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 1 });

/* ------------------------------------------------------------------ */
/* DOM helpers                                                         */
/* ------------------------------------------------------------------ */

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function icon(name, className) {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("class", "icon" + (className ? " " + className : ""));
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS(SVG_NS, "use");
  use.setAttribute("href", "#i-" + name);
  svg.appendChild(use);
  return svg;
}

function avatar(name, tone) {
  const node = el("div", "avatar" + (tone ? " " + tone : ""));
  node.appendChild(icon(name));
  return node;
}

/* ------------------------------------------------------------------ */
/* State                                                               */
/* ------------------------------------------------------------------ */

const state = {
  sessionId: null,
  database: "",
  busy: false,
  // Credentials stay in memory only.
  creds: { uri: "", database: "", api_key: "", base_url: "", model: "", token: "" },
  serverConfig: null,
};

let pollTimer = null;
let polling = false;
let activeThinking = null;

/* ------------------------------------------------------------------ */
/* API                                                                 */
/* ------------------------------------------------------------------ */

async function api(path, options = {}) {
  const headers = Object.assign({ "Content-Type": "application/json" }, options.headers || {});
  const res = await fetch(path, Object.assign({}, options, { headers }));
  let payload = null;
  try {
    payload = await res.json();
  } catch (err) {
    payload = null;
  }
  if (!res.ok) {
    const message =
      (payload && payload.error && payload.error.message) ||
      (payload && payload.detail) ||
      `Request failed (HTTP ${res.status})`;
    const error = new Error(message);
    error.code = payload && payload.error ? payload.error.code : "http";
    throw error;
  }
  return payload;
}

function headersFor(token) {
  return token ? { Authorization: "Bearer " + token } : {};
}

function keyHeaders() {
  return headersFor(state.creds.token);
}

function chatBody(message) {
  return {
    session_id: state.sessionId,
    message: message,
    api_key: state.creds.api_key || null,
    base_url: state.creds.base_url || null,
    model: state.creds.model || null,
  };
}

/* ------------------------------------------------------------------ */
/* Status, toast, connect label                                        */
/* ------------------------------------------------------------------ */

function setStatus(stateName, text) {
  const status = $("#status");
  status.dataset.state = stateName;
  $("#status-text").textContent = text;
}

let toastTimer = null;
function showToast(message, tone = "error") {
  const toast = $("#toast");
  const holder = $("#toast-icon");
  holder.textContent = "";
  holder.appendChild(icon(tone === "ok" ? "check" : "alert"));
  toast.classList.toggle("ok", tone === "ok");
  $("#toast-text").textContent = message;
  toast.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("show"), 3600);
}

function setConnectLabel(text) {
  $("#new-session .btn-label").textContent = text;
}

/* ------------------------------------------------------------------ */
/* Markdown and syntax tinting                                         */
/* ------------------------------------------------------------------ */

function renderMarkdown(text) {
  const source = String(text || "");
  const holder = el("div", "md");
  if (typeof window.marked === "undefined" || !window.DOMPurify) {
    holder.textContent = source;
    return holder;
  }
  window.marked.setOptions({ breaks: true, gfm: true });
  holder.innerHTML = window.DOMPurify.sanitize(window.marked.parse(source), {
    USE_PROFILES: { html: true },
  });
  holder.querySelectorAll("a").forEach((a) => {
    a.target = "_blank";
    a.rel = "noopener noreferrer";
  });
  return holder;
}

const TOKEN_RE = /("(?:\\.|[^"\\])*")(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/g;

function span(className, text) {
  return el("span", className, text);
}

function highlightInto(node, text) {
  const re = new RegExp(TOKEN_RE.source, "g");
  let last = 0;
  let match;
  while ((match = re.exec(text)) !== null) {
    if (match.index > last) node.append(text.slice(last, match.index));
    if (match[1] !== undefined) {
      node.appendChild(span(match[2] ? "k" : "s", match[1]));
      if (match[2]) node.append(match[2]);
    } else {
      node.appendChild(span("n", match[0]));
    }
    last = re.lastIndex;
  }
  node.append(text.slice(last));
}

/* ------------------------------------------------------------------ */
/* Thread                                                              */
/* ------------------------------------------------------------------ */

const messagesEl = $("#messages");

function clearWelcome() {
  const welcome = $("#welcome");
  if (welcome) welcome.remove();
}

function scrollDown() {
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

function addUserMessage(text) {
  clearWelcome();
  const wrap = el("div", "msg user");
  wrap.appendChild(el("div", "bubble", text));
  messagesEl.appendChild(wrap);
  scrollDown();
}

function addAssistantMessage(text) {
  clearWelcome();
  const wrap = el("div", "msg assistant");
  const body = el("div", "assistant-body");
  body.appendChild(renderMarkdown(text));
  wrap.append(avatar("sparkle"), body);
  messagesEl.appendChild(wrap);
  scrollDown();
  return body;
}

function addErrorCard(message) {
  clearWelcome();
  const wrap = el("div", "msg assistant");
  const card = el("div", "error-card");
  card.append(icon("alert"), el("span", null, message));
  wrap.append(avatar("alert", "danger"), card);
  messagesEl.appendChild(wrap);
  scrollDown();
}

function showThinking() {
  const wrap = el("div", "msg assistant thinking-msg");
  const body = el("div", "assistant-body");
  const row = el("div", "thinking");
  const dots = el("span", "dots");
  for (let i = 0; i < 3; i += 1) dots.appendChild(el("span"));
  const label = el("span", "shimmer", "Thinking");
  const tool = el("span", "thinking-tool");
  tool.hidden = true;
  row.append(dots, label, tool);
  body.appendChild(row);
  wrap.append(avatar("sparkle"), body);
  messagesEl.appendChild(wrap);
  scrollDown();
  return wrap;
}

function setThinkingTool(name) {
  if (!activeThinking) return;
  const tool = activeThinking.querySelector(".thinking-tool");
  const label = activeThinking.querySelector(".shimmer");
  if (name) {
    tool.hidden = false;
    tool.textContent = "→ " + name;
    label.textContent = "Running";
  } else {
    tool.hidden = true;
    label.textContent = "Thinking";
  }
}

function startPolling(sessionId, thinkingWrap) {
  stopPolling();
  activeThinking = thinkingWrap;
  pollTimer = setInterval(async () => {
    if (polling) return;
    polling = true;
    try {
      const data = await api("/api/trace/" + encodeURIComponent(sessionId), {
        headers: keyHeaders(),
      });
      setThinkingTool(data.tool_name);
    } catch (err) {
      /* trace polling is best effort */
    } finally {
      polling = false;
    }
  }, 400);
}

function stopPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = null;
  activeThinking = null;
}

function metaChip(text, extra) {
  return el("span", extra || "", text);
}

function buildMeta(data) {
  const steps = data.steps || [];
  const row = el("div", "meta");
  row.append(
    metaChip(steps.length + (steps.length === 1 ? " step" : " steps")),
    metaChip(data.row_count + " rows"),
    metaChip(data.elapsed_ms + " ms")
  );
  if (data.truncated) row.appendChild(metaChip("truncated", "truncated"));
  return row;
}

function buildSteps(result) {
  const steps = result.steps || [];
  const rows = steps.reduce((sum, s) => sum + (s.row_count || 0), 0);

  const details = el("details", "steps");
  const summary = document.createElement("summary");
  summary.append(icon("steps"), el("span", null, "Steps"));
  summary.appendChild(
    el(
      "span",
      "summary-count",
      steps.length + (steps.length === 1 ? " call" : " calls") + " · " + rows + " rows · " + result.elapsed_ms + " ms"
    )
  );
  summary.appendChild(icon("chevron", "chev"));
  details.appendChild(summary);

  const timeline = el("div", "timeline");
  steps.forEach((step) => {
    const item = el("div", "step" + (step.error ? " err" : ""));
    const head = el("div", "step-head");
    head.append(
      el("span", "tool-name", step.tool_name),
      el("span", "step-stat", step.row_count + " rows"),
      el("span", "step-stat", step.elapsed_ms + " ms")
    );
    item.appendChild(head);

    if (step.arguments && Object.keys(step.arguments).length) {
      item.appendChild(el("div", "step-args", JSON.stringify(step.arguments, null, 1)));
    }
    if (step.result_summary) {
      item.appendChild(el("div", "step-result", step.result_summary));
    }
    if (step.executed_query) {
      item.appendChild(buildQueryBlock(JSON.stringify(step.executed_query, null, 2)));
    }
    timeline.appendChild(item);
  });
  details.appendChild(timeline);
  return details;
}

function buildQueryBlock(text) {
  const block = el("div", "query");
  highlightInto(block, text);

  const copy = document.createElement("button");
  copy.type = "button";
  copy.className = "copy-btn";
  copy.title = "Copy query";
  copy.setAttribute("aria-label", "Copy query");
  copy.appendChild(icon("copy"));
  copy.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(text);
      copy.textContent = "";
      copy.appendChild(icon("check"));
      copy.classList.add("done");
      setTimeout(() => {
        copy.textContent = "";
        copy.appendChild(icon("copy"));
        copy.classList.remove("done");
      }, 1400);
    } catch (err) {
      showToast("Could not copy to clipboard");
    }
  });
  block.appendChild(copy);
  return block;
}

/* ------------------------------------------------------------------ */
/* Sidebar                                                             */
/* ------------------------------------------------------------------ */

function formatCount(n) {
  if (typeof n !== "number") return "";
  return compactNumber.format(n) + (n === 1 ? " doc" : " docs");
}

function collectionCard(coll) {
  const card = el("div", "collection");

  const row = el("div", "collection-row");
  const ask = document.createElement("button");
  ask.type = "button";
  ask.className = "collection-ask";
  ask.title = "Ask about " + coll.name;
  ask.append(icon("table"), el("span", "collection-name", coll.name), el("span", "count-pill", formatCount(coll.document_count)));
  ask.addEventListener("click", () => {
    setComposer("What is in the " + coll.name + " collection?");
    closeDrawer();
  });

  const toggle = document.createElement("button");
  toggle.type = "button";
  toggle.className = "toggle";
  toggle.setAttribute("aria-expanded", "false");
  toggle.setAttribute("aria-label", "Show fields");
  toggle.appendChild(icon("chevron"));
  toggle.addEventListener("click", () => {
    const open = card.classList.toggle("open");
    toggle.setAttribute("aria-expanded", String(open));
  });

  row.append(ask, toggle);
  card.appendChild(row);

  const wrap = el("div", "paths-wrap");
  const list = el("ul", "paths");
  const attrs = coll.attributes || [];
  attrs.slice(0, 30).forEach((attr) => {
    const li = document.createElement("li");
    li.title = "Add to question";
    li.append(el("span", "path-name", attr.path), el("span", "type-chip", (attr.types || []).join(" | ")));
    li.addEventListener("click", () => {
      const input = $("#input");
      setComposer((input.value ? input.value + " " : "") + attr.path);
      closeDrawer();
    });
    list.appendChild(li);
  });
  if (attrs.length > 30) {
    list.appendChild(el("li", "more", "+ " + (attrs.length - 30) + " more fields"));
  }
  wrap.appendChild(list);
  card.appendChild(wrap);
  return card;
}

function renderCollections(index) {
  const list = $("#collection-list");
  list.textContent = "";
  const collections = index.collections || [];
  $("#empty-collections").hidden = collections.length > 0;
  $("#side-idle").hidden = true;
  collections.forEach((coll) => list.appendChild(collectionCard(coll)));
}

async function loadSchema() {
  const data = await api("/api/schema/" + encodeURIComponent(state.sessionId), {
    headers: keyHeaders(),
  });
  renderCollections(data);
  return data;
}

/* ------------------------------------------------------------------ */
/* Composer                                                            */
/* ------------------------------------------------------------------ */

const form = $("#composer");
const input = $("#input");
const sendBtn = $("#send");

function autoGrow() {
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 180) + "px";
}

function updateComposerState() {
  form.classList.toggle("has-text", input.value.trim().length > 0);
}

function setComposer(text) {
  input.value = text;
  autoGrow();
  updateComposerState();
  input.focus();
}

function setBusy(busy) {
  state.busy = busy;
  sendBtn.disabled = busy;
  input.disabled = busy;
  form.classList.toggle("busy", busy);
}

input.addEventListener("input", () => {
  autoGrow();
  updateComposerState();
});

input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    form.requestSubmit();
  }
});

document.querySelectorAll(".chip").forEach((chip) => {
  chip.addEventListener("click", () => {
    setComposer(chip.dataset.q || "");
    form.requestSubmit();
  });
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const question = input.value.trim();
  if (!question || state.busy) return;
  if (!state.sessionId) {
    openModal(true);
    return;
  }
  if (question.length > 2000) {
    showToast("Messages are limited to 2,000 characters.");
    return;
  }

  input.value = "";
  autoGrow();
  updateComposerState();
  addUserMessage(question);
  setBusy(true);
  setStatus("busy", "Thinking");
  const thinking = showThinking();
  startPolling(state.sessionId, thinking);

  try {
    const data = await api("/api/chat", {
      method: "POST",
      headers: keyHeaders(),
      body: JSON.stringify(chatBody(question)),
    });
    thinking.remove();
    const body = addAssistantMessage(data.answer);
    if (data.steps && data.steps.length) body.appendChild(buildSteps(data));
    body.appendChild(buildMeta(data));
  } catch (err) {
    thinking.remove();
    if (err.code === "no_session") {
      addErrorCard("Your session expired. Reconnect to continue.");
      openModal(false);
    } else {
      addErrorCard(err.message);
    }
  } finally {
    stopPolling();
    setBusy(false);
    setStatus("ok", "Connected");
    input.focus();
  }
});

/* ------------------------------------------------------------------ */
/* Connect modal                                                       */
/* ------------------------------------------------------------------ */

const backdrop = $("#modal-backdrop");
const modalError = $("#modal-error");
const connectForm = $("#connect-form");
const serverHint = $("#server-hint");

function openModal(isFirstLoad) {
  const cfg = state.serverConfig;
  const uriProvided = Boolean(cfg && cfg.mongodb_uri_set);
  const dbProvided = Boolean(cfg && cfg.database);
  const keyProvided = Boolean(cfg && cfg.api_key_set);

  backdrop.hidden = false;
  $("#modal-cancel").hidden = isFirstLoad;
  modalError.hidden = true;

  $("#f-uri").required = !uriProvided;
  $("#f-db").required = !dbProvided;
  $("#f-uri").placeholder = uriProvided ? cfg.uri_hint : "mongodb://localhost:27017";
  $("#f-db").placeholder = dbProvided ? cfg.database + " (from server config)" : "";
  $("#f-key").placeholder = keyProvided ? "using the server's key" : "sk-…";
  $("#token-label").hidden = !(cfg && cfg.token_required);

  const hints = [];
  if (uriProvided) hints.push("URI comes from the server config. Leave it blank to use it.");
  if (dbProvided) hints.push("Database comes from the server config.");
  if (keyProvided) hints.push("API key comes from the server config.");
  serverHint.textContent = hints.join(" ");
  serverHint.hidden = hints.length === 0;

  $("#f-uri").value = state.creds.uri || "";
  $("#f-db").value = state.creds.database || "";
  $("#f-key").value = state.creds.api_key || "";
  $("#f-base").value = state.creds.base_url || (cfg && cfg.base_url) || "https://api.openai.com/v1";
  $("#f-model").value = state.creds.model || (cfg && cfg.model) || "gpt-4o-mini";
  $("#f-token").value = state.creds.token || "";

  setTimeout(() => $("#f-uri").focus(), 60);
}

function closeModal() {
  backdrop.hidden = true;
}

function readCreds() {
  return {
    uri: $("#f-uri").value.trim(),
    database: $("#f-db").value.trim(),
    api_key: $("#f-key").value.trim(),
    base_url: $("#f-base").value.trim(),
    model: $("#f-model").value.trim(),
    token: $("#f-token").value.trim(),
  };
}

async function createSession(creds) {
  const data = await api("/api/session", {
    method: "POST",
    headers: headersFor(creds.token),
    body: JSON.stringify({
      uri: creds.uri,
      database: creds.database,
      api_key: creds.api_key || null,
      base_url: creds.base_url || null,
      model: creds.model || null,
    }),
  });

  if (state.sessionId) {
    await api("/api/session/" + encodeURIComponent(state.sessionId), {
      method: "DELETE",
      headers: keyHeaders(),
    }).catch(() => {});
  }

  state.creds = creds;
  state.sessionId = data.session_id;
  state.database = data.database;
  $("#db-badge").textContent = data.database;
  setStatus("ok", "Connected");
  setConnectLabel("Reconnect");
  closeModal();
  await loadSchema();
  $("#side-idle").hidden = true;
  showToast("Connected to " + data.database, "ok");
}

connectForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  modalError.hidden = true;

  const submitBtn = connectForm.querySelector('button[type="submit"]');
  const label = submitBtn.querySelector(".btn-label");
  submitBtn.disabled = true;
  label.textContent = "Connecting…";
  try {
    await createSession(readCreds());
  } catch (err) {
    modalError.textContent = err.message;
    modalError.hidden = false;
    setStatus("error", "Connection failed");
  } finally {
    submitBtn.disabled = false;
    label.textContent = "Connect";
  }
});

$("#modal-cancel").addEventListener("click", () => {
  if (state.sessionId) closeModal();
});
$("#modal-close").addEventListener("click", () => {
  if (state.sessionId) closeModal();
});
$("#new-session").addEventListener("click", () => openModal(false));

/* ------------------------------------------------------------------ */
/* Index actions                                                       */
/* ------------------------------------------------------------------ */

$("#refresh-index").addEventListener("click", async () => {
  if (!state.sessionId) {
    showToast("Connect to a database first.");
    return;
  }
  const btn = $("#refresh-index");
  btn.disabled = true;
  btn.classList.add("spin");
  try {
    await api("/api/schema/" + encodeURIComponent(state.sessionId) + "/refresh", {
      method: "POST",
      headers: keyHeaders(),
    });
    await loadSchema();
    showToast("Index refreshed", "ok");
  } catch (err) {
    showToast(err.message);
  } finally {
    btn.disabled = false;
    btn.classList.remove("spin");
  }
});

$("#reset-index").addEventListener("click", async () => {
  if (!state.sessionId) {
    showToast("Connect to a database first.");
    return;
  }
  if (!window.confirm("Delete the cached schema index for this database?")) return;
  const btn = $("#reset-index");
  btn.disabled = true;
  try {
    await api("/api/schema/" + encodeURIComponent(state.sessionId), {
      method: "DELETE",
      headers: keyHeaders(),
    });
    await loadSchema();
    showToast("Index reset", "ok");
  } catch (err) {
    showToast(err.message);
  } finally {
    btn.disabled = false;
  }
});

/* ------------------------------------------------------------------ */
/* Drawer (mobile)                                                     */
/* ------------------------------------------------------------------ */

function openDrawer() {
  $("#sidebar").classList.add("open");
  $("#drawer-scrim").hidden = false;
}

function closeDrawer() {
  $("#sidebar").classList.remove("open");
  $("#drawer-scrim").hidden = true;
}

$("#drawer-toggle").addEventListener("click", () => {
  if ($("#sidebar").classList.contains("open")) closeDrawer();
  else openDrawer();
});
$("#drawer-scrim").addEventListener("click", closeDrawer);

/* ------------------------------------------------------------------ */
/* Keyboard                                                            */
/* ------------------------------------------------------------------ */

document.addEventListener("keydown", (event) => {
  const typing = event.target instanceof HTMLElement && /INPUT|TEXTAREA/.test(event.target.tagName);
  if (event.key === "Escape") {
    if (!backdrop.hidden && state.sessionId) closeModal();
    else closeDrawer();
  }
  if (event.key === "/" && !typing && backdrop.hidden) {
    event.preventDefault();
    input.focus();
  }
});

/* ------------------------------------------------------------------ */
/* Boot                                                                */
/* ------------------------------------------------------------------ */

(async function boot() {
  try {
    state.serverConfig = await api("/api/config");
    const note = $("#composer-note");
    note.textContent = "Using " + state.serverConfig.model + " via " + state.serverConfig.base_url;
    note.hidden = false;
  } catch (err) {
    state.serverConfig = null;
  }

  const cfg = state.serverConfig;
  if (cfg && cfg.ready && !cfg.token_required) {
    try {
      await createSession({
        uri: "",
        database: "",
        api_key: "",
        base_url: cfg.base_url,
        model: cfg.model,
        token: "",
      });
      input.focus();
      return;
    } catch (err) {
      openModal(true);
      modalError.textContent = err.message;
      modalError.hidden = false;
      return;
    }
  }

  openModal(true);
})();