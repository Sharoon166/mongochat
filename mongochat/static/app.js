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
  if (!graphOverlay.hidden) loadGraph();
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
    if (!graphOverlay.hidden) loadGraph();
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
/* Graph explorer                                                      */
/* ------------------------------------------------------------------ */

const graph = {
  nodes: [],
  edges: [],
  rafId: null,
  alpha: 0,
  dragging: null,
  justDragged: false,
  width: 0,
  height: 0,
};

const graphOverlay = $("#graph-overlay");
const graphSvg = $("#graph-svg");

function openGraph() {
  if (!state.sessionId) {
    showToast("Connect to a database first.");
    return;
  }
  graphOverlay.removeAttribute("hidden");
  graphOverlay.style.display = "flex";
  $("#graph-stats").textContent = "Loading…";
  requestAnimationFrame(() => {
    const body = graphOverlay.querySelector(".graph-body");
    graph.width = body.clientWidth;
    graph.height = body.clientHeight;
    loadGraph();
  });
}

function closeGraph() {
  graphOverlay.setAttribute("hidden", "");
  graphOverlay.style.display = "";
  stopSimulation();
  graphSvg.textContent = "";
  hideDetail();
  graph.nodes = [];
  graph.edges = [];
  graph.alpha = 0;
}

async function loadGraph() {
  try {
    const data = await api("/api/graph/" + encodeURIComponent(state.sessionId), {
      headers: keyHeaders(),
    });
    buildGraph(data);
  } catch (err) {
    closeGraph();
    showToast("Graph error: " + err.message);
  }
}

function buildGraph(data) {
  const collections = (data.index && data.index.collections) || [];
  const relations = data.relations || [];

  graph.nodes = collections.map((c) => ({
    name: c.name,
    docCount: c.document_count,
    avgSize: c.avg_doc_size || 0,
    attributes: c.attributes || [],
    x: 0, y: 0, vx: 0, vy: 0, fx: null, fy: null,
    width: 0, height: 40,
    element: null,
  }));

  const nodeMap = new Map(graph.nodes.map((n, i) => [n.name, i]));
  graph.edges = relations
    .map((r) => {
      const si = nodeMap.get(r.source_collection);
      const ti = nodeMap.get(r.target_collection);
      if (si === undefined || ti === undefined) return null;
      return {
        source: si,
        target: ti,
        sourceField: r.source_field,
        targetField: r.target_field,
        confidence: r.confidence,
        selfRef: r.self_reference,
        element: null,
      };
    })
    .filter(Boolean);

  $("#graph-stats").textContent =
    graph.nodes.length + " coll · " + graph.edges.length + " rel";
  $("#graph-empty").hidden = graph.nodes.length > 0;

  if (graph.nodes.length === 0) {
    graphSvg.textContent = "";
    return;
  }

  // If graph loaded but no edges, show a hint
  if (graph.edges.length === 0) {
    $("#graph-stats").textContent =
      graph.nodes.length + " coll · no inferred relations (try refreshing the schema index)";
  }

  graph.nodes.forEach((n) => {
    n.width = Math.max(110, n.name.length * 7.5 + 44);
  });

  var cx = graph.width / 2;
  var cy = graph.height / 2;
  var radius = Math.min(graph.width, graph.height) * 0.32;
  graph.nodes.forEach((n, i) => {
    var a = (i / graph.nodes.length) * Math.PI * 2;
    n.x = cx + radius * Math.cos(a) + (Math.random() - 0.5) * 12;
    n.y = cy + radius * Math.sin(a) + (Math.random() - 0.5) * 12;
    n.vx = 0;
    n.vy = 0;
  });

  createGraphElements();
  graph.alpha = 1;
  startSimulation();
}

function createMarker(id, color) {
  var marker = document.createElementNS(SVG_NS, "marker");
  marker.setAttribute("id", id);
  marker.setAttribute("viewBox", "0 0 10 10");
  marker.setAttribute("refX", "8");
  marker.setAttribute("refY", "5");
  marker.setAttribute("markerWidth", "7");
  marker.setAttribute("markerHeight", "7");
  marker.setAttribute("orient", "auto");
  var arrow = document.createElementNS(SVG_NS, "path");
  arrow.setAttribute("d", "M0,1 L9,5 L0,9 Z");
  arrow.setAttribute("fill", color);
  marker.appendChild(arrow);
  return marker;
}

function createGraphElements() {
  var svg = graphSvg;
  svg.textContent = "";
  svg.setAttribute("width", graph.width);
  svg.setAttribute("height", graph.height);

  var defs = document.createElementNS(SVG_NS, "defs");
  defs.appendChild(createMarker("graph-arrow", "var(--muted)"));
  defs.appendChild(createMarker("graph-arrow-hi", "var(--text-2)"));
  defs.appendChild(createMarker("graph-arrow-sel", "var(--accent)"));
  svg.appendChild(defs);

  var bg = document.createElementNS(SVG_NS, "rect");
  bg.setAttribute("width", graph.width);
  bg.setAttribute("height", graph.height);
  bg.setAttribute("fill", "transparent");
  bg.addEventListener("click", hideDetail);
  svg.appendChild(bg);

  var edgeLayer = document.createElementNS(SVG_NS, "g");
  edgeLayer.setAttribute("class", "graph-edges");
  graph.edges.forEach(function (e) {
    var path = document.createElementNS(SVG_NS, "path");
    path.setAttribute(
      "class",
      "graph-edge conf-" + e.confidence + (e.selfRef ? " self-ref" : "")
    );
    path.addEventListener("click", function (ev) {
      ev.stopPropagation();
      showEdgeDetail(e);
    });
    e.element = path;
    edgeLayer.appendChild(path);
  });
  svg.appendChild(edgeLayer);

  var nodeLayer = document.createElementNS(SVG_NS, "g");
  nodeLayer.setAttribute("class", "graph-nodes");
  graph.nodes.forEach(function (n) {
    var g = document.createElementNS(SVG_NS, "g");
    g.setAttribute("class", "graph-node");
    g.dataset.name = n.name;

    var rect = document.createElementNS(SVG_NS, "rect");
    rect.setAttribute("width", n.width);
    rect.setAttribute("height", n.height);
    rect.setAttribute("rx", 8);
    g.appendChild(rect);

    var label = document.createElementNS(SVG_NS, "text");
    label.setAttribute("x", n.width / 2);
    label.setAttribute("y", n.height / 2 - 4);
    label.setAttribute("text-anchor", "middle");
    label.setAttribute("dominant-baseline", "central");
    label.setAttribute("class", "graph-node-label");
    label.textContent = n.name;
    g.appendChild(label);

    var count = document.createElementNS(SVG_NS, "text");
    count.setAttribute("x", n.width / 2);
    count.setAttribute("y", n.height / 2 + 12);
    count.setAttribute("text-anchor", "middle");
    count.setAttribute("dominant-baseline", "central");
    count.setAttribute("class", "graph-node-count");
    count.textContent = formatCount(n.docCount);
    g.appendChild(count);

    g.addEventListener("pointerdown", function (ev) {
      startDrag(ev, n);
    });
    g.addEventListener("click", function () {
      if (graph.justDragged) {
        graph.justDragged = false;
        return;
      }
      showNodeDetail(n);
    });

    n.element = g;
    nodeLayer.appendChild(g);
  });
  svg.appendChild(nodeLayer);
}

/* Simulation */

function startSimulation() {
  stopSimulation();
  function frame() {
    if (graphOverlay.hasAttribute("hidden")) return;
    if (graph.alpha > 0.005) {
      tickSimulation();
      graph.alpha *= 0.985;
    }
    updatePositions();
    if (graph.alpha > 0.005 || graph.dragging) {
      graph.rafId = requestAnimationFrame(frame);
    }
  }
  graph.rafId = requestAnimationFrame(frame);
}

function stopSimulation() {
  if (graph.rafId) cancelAnimationFrame(graph.rafId);
  graph.rafId = null;
}

function tickSimulation() {
  var nodes = graph.nodes;
  var edges = graph.edges;
  var a = graph.alpha;

  for (var i = 0; i < nodes.length; i++) {
    for (var j = i + 1; j < nodes.length; j++) {
      var dx = nodes[j].x - nodes[i].x;
      var dy = nodes[j].y - nodes[i].y;
      var d2 = dx * dx + dy * dy;
      if (d2 < 1) d2 = 1;
      var d = Math.sqrt(d2);
      var f = (14000 / d2) * a;
      var fx = (dx / d) * f;
      var fy = (dy / d) * f;
      nodes[i].vx -= fx;
      nodes[i].vy -= fy;
      nodes[j].vx += fx;
      nodes[j].vy += fy;
    }
  }

  edges.forEach(function (e) {
    if (e.selfRef) return;
    var s = nodes[e.source];
    var t = nodes[e.target];
    var dx = t.x - s.x;
    var dy = t.y - s.y;
    var d = Math.sqrt(dx * dx + dy * dy) || 1;
    var f = (d - 150) * 0.05 * a;
    var fx = (dx / d) * f;
    var fy = (dy / d) * f;
    s.vx += fx;
    s.vy += fy;
    t.vx -= fx;
    t.vy -= fy;
  });

  var cx = graph.width / 2;
  var cy = graph.height / 2;
  nodes.forEach(function (n) {
    if (n.fx !== null) {
      n.x = n.fx;
      n.vx = 0;
    } else {
      n.vx += (cx - n.x) * 0.015 * a;
      n.x += n.vx;
      n.vx *= 0.82;
    }
    if (n.fy !== null) {
      n.y = n.fy;
      n.vy = 0;
    } else {
      n.vy += (cy - n.y) * 0.015 * a;
      n.y += n.vy;
      n.vy *= 0.82;
    }
    n.x = Math.max(n.width / 2 + 8, Math.min(graph.width - n.width / 2 - 8, n.x));
    n.y = Math.max(n.height / 2 + 8, Math.min(graph.height - n.height / 2 - 8, n.y));
  });
}

function rectEdgePoint(node, tx, ty) {
  var dx = tx - node.x;
  var dy = ty - node.y;
  var hw = node.width / 2;
  var hh = node.height / 2;
  if (dx === 0 && dy === 0) return { x: node.x, y: node.y };
  var s = Math.min(hw / Math.abs(dx), hh / Math.abs(dy));
  return { x: node.x + dx * s, y: node.y + dy * s };
}

function updatePositions() {
  graph.edges.forEach(function (e) {
    var s = graph.nodes[e.source];
    var t = graph.nodes[e.target];
    if (e.selfRef) {
      var top = s.y - s.height / 2;
      var cx = s.x;
      e.element.setAttribute(
        "d",
        "M " + (cx - 8) + "," + top + " C " + (cx - 36) + "," + (top - 44) + " " + (cx + 36) + "," + (top - 44) + " " + (cx + 8) + "," + top
      );
    } else {
      var sp = rectEdgePoint(s, t.x, t.y);
      var tp = rectEdgePoint(t, s.x, s.y);
      var dx = tp.x - sp.x;
      var dy = tp.y - sp.y;
      var d = Math.sqrt(dx * dx + dy * dy) || 1;
      tp.x -= (dx / d) * 10;
      tp.y -= (dy / d) * 10;
      e.element.setAttribute("d", "M " + sp.x + "," + sp.y + " L " + tp.x + "," + tp.y);
    }
  });
  graph.nodes.forEach(function (n) {
    n.element.setAttribute(
      "transform",
      "translate(" + (n.x - n.width / 2) + ", " + (n.y - n.height / 2) + ")"
    );
  });
}

/* Drag */

function startDrag(ev, node) {
  ev.preventDefault();
  var rect = graphSvg.getBoundingClientRect();
  graph.dragging = {
    node: node,
    offsetX: ev.clientX - rect.left - node.x,
    offsetY: ev.clientY - rect.top - node.y,
    moved: false,
  };
  node.fx = node.x;
  node.fy = node.y;
  graph.alpha = Math.max(graph.alpha, 0.3);
  startSimulation();
}

function onDragMove(ev) {
  if (!graph.dragging) return;
  var rect = graphSvg.getBoundingClientRect();
  var n = graph.dragging.node;
  n.fx = ev.clientX - rect.left - graph.dragging.offsetX;
  n.fy = ev.clientY - rect.top - graph.dragging.offsetY;
  graph.dragging.moved = true;
}

function onDragEnd() {
  if (!graph.dragging) return;
  graph.dragging.node.fx = null;
  graph.dragging.node.fy = null;
  if (graph.dragging.moved) graph.justDragged = true;
  graph.dragging = null;
}

window.addEventListener("pointermove", onDragMove);
window.addEventListener("pointerup", onDragEnd);

/* Detail panel */

function confidenceLabel(c) {
  return (
    { exact: "Exact", singular_plural: "Singular↔plural", bare: "Name match" }[c] || c
  );
}

function detailStat(label, value) {
  var row = el("div", "detail-stat");
  row.append(
    el("span", "detail-label", label),
    el("span", "detail-value", value)
  );
  return row;
}

function detailSection(title, items) {
  var sec = el("div", "detail-section");
  sec.appendChild(el("h4", null, title));
  var list = el("div", "detail-list");
  items.forEach(function (item) {
    list.appendChild(item);
  });
  sec.appendChild(list);
  return sec;
}

function clearSelection() {
  graph.nodes.forEach(function (n) {
    if (n.element) n.element.classList.remove("selected");
  });
  graph.edges.forEach(function (e) {
    if (e.element) e.element.classList.remove("selected", "highlighted");
  });
}

function showNodeDetail(node) {
  clearSelection();
  node.element.classList.add("selected");
  graph.edges.forEach(function (e) {
    if (graph.nodes[e.source] === node || graph.nodes[e.target] === node) {
      e.element.classList.add("highlighted");
    }
  });

  var body = $("#graph-detail-body");
  body.textContent = "";
  $("#graph-detail-title").textContent = node.name;

  var stats = el("div", "detail-stats");
  stats.append(
    detailStat("Documents", compactNumber.format(node.docCount)),
    detailStat("Avg size", node.avgSize ? node.avgSize + " B" : "—"),
    detailStat("Fields", String(node.attributes.length))
  );
  body.appendChild(stats);

  if (node.attributes.length) {
    body.appendChild(
      detailSection(
        "Fields",
        node.attributes.slice(0, 30).map(function (a) {
          var row = el("div", "detail-field-row");
          row.append(
            el("span", "detail-field-name", a.path),
            el("span", "type-chip", (a.types || []).join(" | "))
          );
          return row;
        })
      )
    );
  }

  var outgoing = graph.edges.filter(function (e) {
    return graph.nodes[e.source] === node;
  });
  if (outgoing.length) {
    body.appendChild(
      detailSection(
        "References",
        outgoing.map(function (e) {
          var target = graph.nodes[e.target];
          return el(
            "div",
            "detail-rel-row",
            e.sourceField + " → " + target.name + (e.selfRef ? " (self)" : "")
          );
        })
      )
    );
  }

  var incoming = graph.edges.filter(function (e) {
    return graph.nodes[e.target] === node && !e.selfRef;
  });
  if (incoming.length) {
    body.appendChild(
      detailSection(
        "Referenced by",
        incoming.map(function (e) {
          var source = graph.nodes[e.source];
          return el("div", "detail-rel-row", source.name + "." + e.sourceField);
        })
      )
    );
  }

  $("#graph-detail").hidden = false;
}

function showEdgeDetail(edge) {
  clearSelection();
  edge.element.classList.add("selected");
  var source = graph.nodes[edge.source];
  var target = graph.nodes[edge.target];

  var body = $("#graph-detail-body");
  body.textContent = "";
  $("#graph-detail-title").textContent =
    source.name + " → " + target.name;

  var stats = el("div", "detail-stats");
  stats.append(
    detailStat("Source field", edge.sourceField),
    detailStat("Target field", edge.targetField),
    detailStat("Confidence", confidenceLabel(edge.confidence)),
    detailStat("Self-reference", edge.selfRef ? "Yes" : "No")
  );
  body.appendChild(stats);

  $("#graph-detail").hidden = false;
}

function hideDetail() {
  $("#graph-detail").hidden = true;
  clearSelection();
}

/* Graph event handlers */

$("#graph-toggle").addEventListener("click", function () {
  if (!graphOverlay.hidden) closeGraph();
  else openGraph();
});

$("#graph-close").addEventListener("click", closeGraph);
$("#graph-detail-close").addEventListener("click", hideDetail);

$("#graph-relayout").addEventListener("click", function () {
  if (graph.nodes.length === 0) return;
  graph.nodes.forEach(function (n) {
    n.fx = null;
    n.fy = null;
  });
  var cx = graph.width / 2;
  var cy = graph.height / 2;
  var radius = Math.min(graph.width, graph.height) * 0.32;
  graph.nodes.forEach(function (n, i) {
    var a = (i / graph.nodes.length) * Math.PI * 2;
    n.x = cx + radius * Math.cos(a) + (Math.random() - 0.5) * 20;
    n.y = cy + radius * Math.sin(a) + (Math.random() - 0.5) * 20;
    n.vx = 0;
    n.vy = 0;
  });
  graph.alpha = 1;
  startSimulation();
});

var graphResizeTimer = null;
window.addEventListener("resize", function () {
  if (graphOverlay.hidden) return;
  clearTimeout(graphResizeTimer);
  graphResizeTimer = setTimeout(function () {
    var body = graphOverlay.querySelector(".graph-body");
    graph.width = body.clientWidth;
    graph.height = body.clientHeight;
    graphSvg.setAttribute("width", graph.width);
    graphSvg.setAttribute("height", graph.height);
    graph.alpha = Math.max(graph.alpha, 0.3);
    startSimulation();
  }, 200);
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
    if (!graphOverlay.hidden) {
      if (!$("#graph-detail").hidden) hideDetail();
      else closeGraph();
    } else if (!backdrop.hidden && state.sessionId) closeModal();
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