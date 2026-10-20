/* mongochat dashboard — vanilla JS, no build step.
   Keys live in JS memory only: never localStorage, never cookies. */
"use strict";

/* ------------------------------------------------------------------ */
/* Small helpers                                                       */
/* ------------------------------------------------------------------ */

const $ = (sel) => document.querySelector(sel);

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function esc(text) {
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

const state = {
  sessionId: null,
  database: "",
  busy: false,
  // Credentials stay here only.
  creds: { uri: "", database: "", api_key: "", base_url: "", model: "", token: "" },
  // What the server already has configured (from /api/config), or null.
  serverConfig: null,
  pollTimer: null,
};

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
  // The key is sent per request and never persisted.
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
/* Markdown                                                            */
/* ------------------------------------------------------------------ */

function renderMarkdown(text) {
  const source = String(text || "");
  if (typeof window.marked === "undefined") {
    const pre = el("pre", null, source);
    const wrap = el("div");
    wrap.appendChild(pre);
    return wrap;
  }
  window.marked.setOptions({ breaks: true, gfm: true });
  const raw = window.marked.parse(source);
  if (window.DOMPurify) {
    const clean = window.DOMPurify.sanitize(raw, { USE_PROFILES: { html: true } });
    const holder = el("div", "md");
    holder.innerHTML = clean;
    return holder;
  }
  // No sanitizer available: fall back to escaped plain text.
  const holder = el("div", "md");
  holder.textContent = source;
  return holder;
}

/* ------------------------------------------------------------------ */
/* Syntax tinting for executed queries                                 */
/* ------------------------------------------------------------------ */

function highlightJSON(value) {
  const text = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  const re = /("(?:\\.|[^"\\])*")(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/g;
  let out = "";
  let last = 0;
  let match;
  while ((match = re.exec(text)) !== null) {
    out += esc(text.slice(last, match.index));
    if (match[1]) {
      out += match[2]
        ? '<span class="k">' + esc(match[1]) + "</span>" + esc(match[2])
        : '<span class="s">' + esc(match[1]) + "</span>";
    } else if (match[3]) {
      out += '<span class="n">' + esc(match[3]) + "</span>";
    } else {
      out += '<span class="n">' + esc(match[0]) + "</span>";
    }
    last = re.lastIndex;
  }
  out += esc(text.slice(last));
  return out;
}

/* ------------------------------------------------------------------ */
/* Messages                                                            */
/* ------------------------------------------------------------------ */

const messages = $("#messages");
const welcome = $("#welcome");

function addMessage(role, text) {
  if (welcome) welcome.remove();
  const wrap = el("div", "msg " + role);
  const bubble = el("div", "bubble");
  if (role === "assistant") {
    bubble.appendChild(renderMarkdown(text));
  } else {
    bubble.textContent = text;
  }
  wrap.appendChild(bubble);
  messages.appendChild(wrap);
  scrollDown();
  return wrap;
}

function addMeta(wrap, text) {
  wrap.appendChild(el("div", "meta", text));
}

function scrollDown() {
  messages.scrollTop = messages.scrollHeight;
}

function buildSteps(result) {
  const details = document.createElement("details");
  details.className = "steps";
  const summary = document.createElement("summary");
  const rows = result.steps.reduce((sum, s) => sum + (s.row_count || 0), 0);
  summary.textContent =
    "Steps · " + result.steps.length + " tool call" + (result.steps.length === 1 ? "" : "s") +
    " · " + rows + " rows · " + result.elapsed_ms + "ms";
  details.appendChild(summary);

  const timeline = el("div", "timeline");
  result.steps.forEach((step) => {
    const item = el("div", "step");
    const head = el("div", "step-head");
    head.appendChild(el("span", "name", (step.error ? "✗ " : "→ ") + step.tool_name));
    head.appendChild(el("span", "stat", step.row_count + " rows"));
    head.appendChild(el("span", "stat", step.elapsed_ms + "ms"));
    if (step.error) head.appendChild(el("span", "err", "error"));
    item.appendChild(head);

    if (step.arguments && Object.keys(step.arguments).length) {
      item.appendChild(el("div", "step-args", JSON.stringify(step.arguments, null, 1)));
    }
    if (step.result_summary) {
      item.appendChild(el("div", "step-args", step.result_summary));
    }
    if (step.executed_query) {
      const pre = document.createElement("div");
      pre.className = "query";
      pre.innerHTML = highlightJSON(step.executed_query);
      item.appendChild(pre);
    }
    timeline.appendChild(item);
  });
  details.appendChild(timeline);
  return details;
}

/* ------------------------------------------------------------------ */
/* Typing indicator                                                    */
/* ------------------------------------------------------------------ */

const typing = $("#typing");
const typingTool = $("#typing-tool");

function setTyping(on, tool) {
  if (!typing) return;
  typing.hidden = !on;
  typingTool.textContent = on && tool ? tool : "";
}

function startPolling(sessionId) {
  stopPolling();
  state.pollTimer = setInterval(async () => {
    try {
      const data = await api("/api/trace/" + encodeURIComponent(sessionId), {
        headers: keyHeaders(),
      });
      setTyping(true, data.tool_name || "thinking…");
    } catch (err) {
      /* trace polling is best effort */
    }
  }, 400);
}

function stopPolling() {
  if (state.pollTimer) {
    clearInterval(state.pollTimer);
    state.pollTimer = null;
  }
  setTyping(false);
}

/* ------------------------------------------------------------------ */
/* Sidebar                                                             */
/* ------------------------------------------------------------------ */

function renderCollections(index) {
  const list = $("#collection-list");
  const empty = $("#empty-collections");
  list.textContent = "";
  const collections = index.collections || [];
  empty.hidden = collections.length > 0;

  collections.forEach((coll) => {
    const box = el("div", "collection");
    const btn = document.createElement("button");
    btn.type = "button";
    const name = el("span", null, coll.name);
    const count = el("span", "count", formatCount(coll.document_count));
    btn.appendChild(name);
    btn.appendChild(count);
    btn.title = "Ask about " + coll.name;
    btn.addEventListener("click", () => {
      const input = $("#input");
      input.value = "What is in the " + coll.name + " collection?";
      input.focus();
      closeDrawer();
    });
    box.appendChild(btn);

    const paths = document.createElement("ul");
    paths.className = "paths";
    (coll.attributes || []).slice(0, 30).forEach((attr) => {
      const li = document.createElement("li");
      const type = el("span", "t", " : " + (attr.types || []).join("|"));
      const label = el("span", null, attr.path);
      li.appendChild(label);
      li.appendChild(type);
      li.title = "Insert into the question";
      li.addEventListener("click", () => {
        const input = $("#input");
        input.value = (input.value ? input.value + " " : "") + attr.path;
        input.focus();
        closeDrawer();
      });
      paths.appendChild(li);
    });
    if ((coll.attributes || []).length > 30) {
      const li = el("li", "t", "… " + (coll.attributes.length - 30) + " more");
      paths.appendChild(li);
    }
    box.appendChild(paths);
    list.appendChild(box);
  });
}

function formatCount(n) {
  if (typeof n !== "number") return "";
  return n >= 1000 ? n.toLocaleString() + " docs" : n + (n === 1 ? " doc" : " docs");
}

async function loadSchema() {
  const data = await api("/api/schema/" + encodeURIComponent(state.sessionId), {
    headers: keyHeaders(),
  });
  renderCollections(data);
  return data;
}

/* ------------------------------------------------------------------ */
/* Connect                                                             */
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

  // Anything the server already has stays optional and says where it comes from.
  $("#f-uri").required = !uriProvided;
  $("#f-db").required = !dbProvided;
  $("#f-uri").placeholder = uriProvided ? cfg.uri_hint : "mongodb://localhost:27017";
  $("#f-db").placeholder = dbProvided ? cfg.database + " (from server config)" : "";
  $("#f-key").placeholder = keyProvided ? "using the server's key" : "sk-…";
  $("#token-label").hidden = !(cfg && cfg.token_required);

  const hints = [];
  if (uriProvided) hints.push("URI comes from the server config — leave blank to use it.");
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
  $("#db-badge").textContent = data.database + " · " + (data.collections || []).length + " collections";
  closeModal();
  await loadSchema();
}

connectForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  modalError.hidden = true;

  const creds = readCreds();
  const submitBtn = connectForm.querySelector('button[type="submit"]');
  submitBtn.disabled = true;
  submitBtn.textContent = "Connecting…";
  try {
    await createSession(creds);
  } catch (err) {
    modalError.textContent = err.message;
    modalError.hidden = false;
  } finally {
    submitBtn.disabled = false;
    submitBtn.textContent = "Connect";
    input.focus();
  }
});

$("#modal-cancel").addEventListener("click", () => {
  if (state.sessionId) closeModal();
});

$("#new-session").addEventListener("click", () => openModal(false));

/* ------------------------------------------------------------------ */
/* Index actions                                                       */
/* ------------------------------------------------------------------ */

$("#refresh-index").addEventListener("click", async () => {
  if (!state.sessionId) return;
  const btn = $("#refresh-index");
  btn.disabled = true;
  try {
    const data = await api("/api/schema/" + encodeURIComponent(state.sessionId) + "/refresh", {
      method: "POST",
      headers: keyHeaders(),
    });
    $("#db-badge").textContent =
      state.database + " · " + (data.collections || []).length + " collections";
    await loadSchema();
  } catch (err) {
    notifyError(err.message);
  } finally {
    btn.disabled = false;
  }
});

$("#reset-index").addEventListener("click", async () => {
  if (!state.sessionId) return;
  if (!window.confirm("Delete the cached schema index for this database?")) return;
  const btn = $("#reset-index");
  btn.disabled = true;
  try {
    await api("/api/schema/" + encodeURIComponent(state.sessionId), {
      method: "DELETE",
      headers: keyHeaders(),
    });
    await loadSchema();
  } catch (err) {
    notifyError(err.message);
  } finally {
    btn.disabled = false;
  }
});

function notifyError(message) {
  if (welcome) welcome.remove();
  const wrap = el("div", "msg assistant");
  const bubble = el("div", "bubble error");
  bubble.textContent = message;
  wrap.appendChild(bubble);
  messages.appendChild(wrap);
  scrollDown();
}

/* ------------------------------------------------------------------ */
/* Sending                                                             */
/* ------------------------------------------------------------------ */

const form = $("#composer");
const input = $("#input");
const sendBtn = $("#send");

function autoGrow() {
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 160) + "px";
}
input.addEventListener("input", autoGrow);

input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    form.requestSubmit();
  }
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
    notifyError("Message exceeds 2000 characters.");
    return;
  }

  input.value = "";
  autoGrow();
  addMessage("user", question);
  setBusy(true);
  startPolling(state.sessionId);

  try {
    const data = await api("/api/chat", {
      method: "POST",
      headers: keyHeaders(),
      body: JSON.stringify(chatBody(question)),
    });
    const wrap = addMessage("assistant", data.answer);
    if (data.steps && data.steps.length) wrap.appendChild(buildSteps(data));
    const note = [];
    note.push(data.steps.length + " steps");
    note.push(data.row_count + " rows");
    note.push(data.elapsed_ms + "ms");
    if (data.truncated) note.push("truncated");
    addMeta(wrap, note.join(" · "));
  } catch (err) {
    if (err.code === "no_session") {
      notifyError("Session expired. Reconnect and try again.");
      openModal(false);
    } else {
      notifyError(err.message);
    }
  } finally {
    stopPolling();
    setBusy(false);
    input.focus();
  }
});

function setBusy(busy) {
  state.busy = busy;
  sendBtn.disabled = busy;
  input.disabled = busy;
}

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
  const sidebar = $("#sidebar");
  if (sidebar.classList.contains("open")) closeDrawer();
  else openDrawer();
});
$("#drawer-scrim").addEventListener("click", closeDrawer);

/* ------------------------------------------------------------------ */
/* Boot                                                                */
/* ------------------------------------------------------------------ */

(async function boot() {
  try {
    state.serverConfig = await api("/api/config");
    $("#composer-note").textContent =
      "Server config: " + state.serverConfig.model + " · " + state.serverConfig.base_url;
    $("#composer-note").hidden = false;
  } catch (err) {
    /* server unreachable; the modal will surface errors */
    state.serverConfig = null;
  }

  const cfg = state.serverConfig;
  if (cfg && cfg.ready && !cfg.token_required) {
    // Everything needed is configured server-side: connect without asking again.
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
  input.focus();
})();
