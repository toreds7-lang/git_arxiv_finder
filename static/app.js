"use strict";

// ---------------------------------------------------------------- helpers
const $ = (sel, el = document) => el.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const num = (n) => n == null ? "?" : n >= 1e6 ? (n / 1e6).toFixed(1).replace(/\.0$/, "") + "M" : n >= 1000 ? (n / 1000).toFixed(n >= 1e4 ? 0 : 1).replace(/\.0$/, "") + "k" : String(n);
const ago = (iso) => {
  if (!iso) return "?";
  const d = (Date.now() - new Date(iso).getTime()) / 864e5;
  if (d < 1) return "today";
  if (d < 60) return `${Math.floor(d)}d ago`;
  if (d < 730) return `${Math.floor(d / 30)}mo ago`;
  return `${Math.floor(d / 365)}y ago`;
};
const hms = (sec) => {
  if (sec == null) return "?";
  const s = Math.floor(sec), h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), pad = (x) => String(x).padStart(2, "0");
  return h ? `${h}:${pad(m)}:${pad(s % 60)}` : `${m}:${pad(s % 60)}`;
};
const store = {
  get(k, d) { try { const v = localStorage.getItem("scout:" + k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem("scout:" + k, JSON.stringify(v)); } catch { /* private mode */ } },
};
function toast(msg) {
  const t = $("#toast"); t.textContent = msg; t.hidden = false;
  clearTimeout(toast._t); toast._t = setTimeout(() => (t.hidden = true), 1800);
}

const KINDS = {
  github: {
    placeholder: "Describe what you're looking for, in any language. e.g. \"Open-source tools that give LLM agents long-term memory, ideally Python, self-hostable\"",
    examples: ["Long-term memory for LLM agents", "Local LLM inference that runs on a laptop", "Open-source coding agents like Claude Code", "Python PDF table extraction"],
    sorts: { rank: "Claude's ranking", stars: "Stars", momentum: "Momentum (★/mo)", pushed: "Recently pushed", created: "Newest" },
  },
  arxiv: {
    placeholder: "Describe the research topic, in any language. e.g. \"Test-time compute scaling for LLM reasoning: methods and benchmarks\"",
    examples: ["Long-term memory for LLM agents", "Test-time compute scaling for reasoning", "Low-resource ASR fine-tuning", "Evaluating RAG systems"],
    sorts: { rank: "Claude's ranking", date: "Newest", citations: "Citations", upvotes: "HF upvotes" },
  },
  youtube: {
    placeholder: "Describe the videos you want, in any language. e.g. \"Clear visual explanations of how attention works in transformers, for beginners\"",
    examples: ["How attention in transformers works", "vLLM tutorial for serving LLMs", "Conference talks on LLM agents", "Fine-tuning Whisper for a new language"],
    sorts: { rank: "Claude's ranking", views: "Views", trending: "Views per day", date: "Newest", likes: "Likes", short: "Shortest" },
  },
  blog: {
    placeholder: "Describe the blog posts you want, in any language (results are English-only). e.g. \"Engineering write-ups on running LLM agents in production: failures, evals, lessons learned\"",
    examples: ["How vLLM's paged attention works", "Lessons learned running LLM agents in production", "Prompt caching tips and pitfalls", "Postgres as a vector database: experience reports"],
    sorts: { rank: "Claude's ranking", date: "Newest", hn: "HN points", read: "Shortest read" },
  },
};
const KIND_LABEL = { github: "GitHub repos", arxiv: "arXiv papers", youtube: "YouTube videos", blog: "Blog posts" };
const TAB_NAME = { github: "GitHub", arxiv: "arXiv", youtube: "YouTube", blog: "Blogs" };
const SINCE_LABEL = { "1m": "Past month", "3m": "Past 3 months", "6m": "Past 6 months", "1y": "Past year", "2y": "Past 2 years", any: "Any time", custom: "Since last check" };
const ICON = {
  start: "▶", info: "⚙", ready: "✓", thinking: "💭", say: "💬", tool_call: "🔧", tool_result: "📦",
  submit: "📝", accepted: "✅", enrich: "🔗", enriched: "🔗", results: "🏁", done: "✅",
  warn: "⚠", error: "✖", cancelled: "⏹",
};

let CFG = null;
const S = {}; // per-kind state

// ---------------------------------------------------------------- setup
async function init() {
  CFG = await fetch("/api/config").then((r) => r.json());
  renderHealth();
  const tpl = $("#panel-tpl");
  for (const kind of Object.keys(KINDS)) {
    const panel = tpl.content.firstElementChild.cloneNode(true);
    panel.id = `panel-${kind}`;
    panel.dataset.kind = kind;
    $("#panels").appendChild(panel);
    S[kind] = { panel, es: null, runId: null, lastSeq: -1, running: false, t0: null, timer: null, results: null, sort: "rank" };
    setupPanel(kind);
    refreshHistory(kind);
  }
  libInit();
  document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
  const linked = (location.hash.match(/run=([\w-]+)/) || [])[1];
  const linkedKind = linked && (linked.match(/-(github|arxiv|youtube|blog)-/) || [])[1];
  if (linkedKind) {
    showTab(linkedKind);
    openRun(linkedKind, linked);
  } else {
    showTab(location.hash === "#library" ? "library" : store.get("tab", "github"));
  }
  pollWatch();
  setInterval(pollWatch, 60000);
}

// Header chip + Library tab dot for watched topics: how many, how many new results unseen.
async function pollWatch() {
  const w = await fetch("/api/library/status").then((r) => r.json()).catch(() => null);
  const chip = $("#watch-chip");
  if (!w || !chip) return;
  chip.hidden = !w.watching && !w.running;
  chip.textContent = `👁 ${w.watching} watched` + (w.unseen ? ` · ${w.unseen} with new results` : "") +
    (w.running ? ` · checking ${TAB_NAME[w.running.kind]}…` : "");
  chip.classList.toggle("ok", !!w.unseen);
  document.querySelector('.tabs button[data-tab="library"] .dot').hidden = !w.unseen && !w.running;
}

function setHash(runId) {
  try { history.replaceState(null, "", runId ? `#run=${runId}` : location.pathname); } catch { /* ignore */ }
}

function renderHealth() {
  const h = $("#health");
  const c = CFG.claude_version;
  h.innerHTML =
    `<span class="chip ${c ? "ok" : "bad"}" title="Claude Code CLI">${c ? "● Claude Code " + esc(c.split(" ")[0]) : "✖ Claude Code CLI not found"}</span>` +
    `<span class="chip ${CFG.github_token ? "ok" : "bad"}" title="GitHub API auth">${CFG.github_token ? "● GitHub token" : "✖ no GitHub token (10 searches/min)"}</span>` +
    `<span class="chip ${CFG.youtube_key ? "ok" : "bad"}" title="YouTube Data API v3 key">${CFG.youtube_key ? "● YouTube key" : "✖ no YouTube key"}</span>` +
    `<button type="button" class="chip" id="watch-chip" title="Watched topics are re-checked on a schedule while Scout runs. Open the Library." hidden></button>`;
  $("#watch-chip").addEventListener("click", () => showTab("library"));
}

function showTab(kind) {
  if (!S[kind] && kind !== "library") kind = "github";
  document.querySelectorAll(".tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === kind)));
  for (const k of Object.keys(S)) S[k].panel.hidden = k !== kind;
  $("#panel-library").hidden = kind !== "library";
  store.set("tab", kind);
  if (kind === "library") {
    try { history.replaceState(null, "", "#library"); } catch { /* ignore */ }
    libLoad();
  } else {
    setHash(S[kind].runId);
  }
}

// Put a query into a tab's search box without running it (runs cost money; the user presses Search).
function prefill(kind, query) {
  const st = S[kind], form = $("form", st.panel);
  showTab(kind);
  window.scrollTo({ top: 0, behavior: "smooth" });
  if (st.running) return toast(`A ${KIND_LABEL[kind]} search is already running`);
  form.query.value = query;
  form.query.focus();
  $(".prior", st.panel).hidden = true;
  toast("Ready: press Search (or Ctrl+Enter) to run it");
}

function setupPanel(kind) {
  const st = S[kind], p = st.panel, form = $("form", p), K = KINDS[kind];
  const prefs = store.get("prefs:" + kind, {});
  form.query.placeholder = K.placeholder;
  form.count.innerHTML = CFG.count_options.map((n) =>
    `<option value="${n}">${n}${n === CFG.counts[kind] ? " (recommended)" : ""}</option>`).join("");
  form.count.value = prefs.count || CFG.counts[kind];
  form.since.innerHTML = CFG.since_options.map((s) =>
    `<option value="${s}">${SINCE_LABEL[s] || s}${s === CFG.default_since ? " (default)" : ""}</option>`).join("");
  form.since.value = prefs.since || CFG.default_since;
  form.model.innerHTML = CFG.models.map((m) =>
    `<option value="${m}">${m[0].toUpperCase() + m.slice(1)}${m === CFG.default_model ? " (recommended)" : ""}</option>`).join("");
  form.model.value = prefs.model || CFG.default_model;

  $(".examples", p).innerHTML = K.examples.map((e) => `<button type="button">${esc(e)}</button>`).join("");
  $(".examples", p).addEventListener("click", (ev) => {
    if (ev.target.tagName === "BUTTON") { form.query.value = ev.target.textContent; form.query.focus(); }
  });
  form.query.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && (ev.ctrlKey || ev.metaKey)) form.requestSubmit();
  });
  let priorTimer;
  form.query.addEventListener("input", () => { clearTimeout(priorTimer); priorTimer = setTimeout(() => showPrior(kind), 350); });
  $(".prior", p).addEventListener("click", (ev) => {
    const b = ev.target.closest("[data-run]");
    if (b) openFromLibrary(b.dataset.openKind, b.dataset.run);
  });
  form.addEventListener("submit", (ev) => { ev.preventDefault(); startSearch(kind); });
  $(".cancel", p).addEventListener("click", () => cancelRun(kind));

  const sortSel = $(".sort", p);
  sortSel.innerHTML = Object.entries(K.sorts).map(([v, l]) => `<option value="${v}">${l}</option>`).join("");
  sortSel.addEventListener("change", () => { st.sort = sortSel.value; renderResults(kind, st.results); });
  $(".copy-md", p).addEventListener("click", () => copyMarkdown(kind));
  $(".history", p).addEventListener("change", (ev) => { if (ev.target.value) openRun(kind, ev.target.value); });
  $(".results", p).addEventListener("click", async (ev) => {
    const mk = ev.target.closest(".mk");
    if (mk) {
      const id = mk.closest("[data-id]").dataset.id;
      const r = await toggleMark(mk, kind, id);
      if (r && st.ann) st.ann[id] = { ...(st.ann[id] || {}), ...r };
      return;
    }
    const b = ev.target.closest(".find");
    if (b) findIn(b.dataset.kind, b.dataset.q, st.runId);
  });
  if (kind === "youtube" && !CFG.youtube_key) {
    const n = $(".notice", p);
    n.hidden = false;
    n.innerHTML = `<b>YouTube needs a free API key.</b> In <a href="https://console.cloud.google.com/apis/library/youtube.googleapis.com" target="_blank" rel="noopener">Google Cloud Console</a>, enable <i>YouTube Data API v3</i>, create an API key under <i>Credentials</i>, add the line <code>YOUTUBE_API_KEY=your-key</code> to <code>env.txt.txt</code>, then reload this page.`;
  }
}

// "Find videos" / "Find blog posts" on a result card: a targeted run in that tab. Any date,
// because videos and posts about older work are often older too; 5 results, because few
// cover one particular paper or repo.
function findIn(kind, query, parentRun) {
  const st = S[kind], form = $("form", st.panel);
  showTab(kind);
  window.scrollTo({ top: 0, behavior: "smooth" });
  if (st.running) return toast(`A ${KIND_LABEL[kind]} search is already running`);
  form.query.value = query;
  setSelect(form.since, "any");
  setSelect(form.count, 5);
  // parent_run files this run under the topic of the card's run in the Library
  startSearch(kind, { remember: false, origin: "find", parentRun });
}

// "You searched this before" under the search box, as the user types.
async function showPrior(kind) {
  const st = S[kind], form = $("form", st.panel), box = $(".prior", st.panel), q = form.query.value.trim();
  if (q.length < 3 || st.running) { box.hidden = true; return; }
  const rows = await fetch(`/api/library/similar?q=${encodeURIComponent(q)}`).then((r) => r.json()).catch(() => []);
  if (form.query.value.trim() !== q) return;
  box.hidden = !rows.length;
  box.innerHTML = rows.map((t) => {
    const runs = Object.entries(t.kinds).map(([k, v]) =>
      `<button type="button" class="link" data-open-kind="${k}" data-run="${esc(v.last_run)}" title="Open that run">${TAB_NAME[k]} ${ago(v.last_at)}</button>`).join(" · ");
    const here = t.kinds[kind] ? "" : ` <span class="muted">· not yet searched in ${TAB_NAME[kind]}</span>`;
    return `<div>📚 ${t.exact ? "You searched exactly this before" : "Similar past search"}: <b title="${esc(t.query)}">${esc(t.label || clip(t.query, 70))}</b> · ${runs}${here}</div>`;
  }).join("");
}

function openFromLibrary(kind, runId) {
  showTab(kind);
  openRun(kind, runId);
  window.scrollTo({ top: 0, behavior: "smooth" });
}

// ★ save / 👍 / 👎 on a result; stored per item, so it shows on every run that lists it.
function markBtns(star, vote) {
  return `<span class="marks">` +
    `<button type="button" class="mk" data-mk="star" aria-pressed="${!!star}" title="${star ? "Saved in your Library (click to unsave)" : "Save to your Library"}">${star ? "★" : "☆"}</button>` +
    `<button type="button" class="mk" data-mk="up" aria-pressed="${vote === 1}" title="Useful (helps Claude's ideas)">👍</button>` +
    `<button type="button" class="mk" data-mk="down" aria-pressed="${vote === -1}" title="Not useful (helps Claude's ideas)">👎</button></span>`;
}

async function toggleMark(btn, kind, id) {
  const box = btn.closest(".marks"), pressed = btn.getAttribute("aria-pressed") === "true";
  const body = { kind, id };
  if (btn.dataset.mk === "star") body.star = !pressed;
  else body.vote = pressed ? 0 : btn.dataset.mk === "up" ? 1 : -1;
  const r = await fetch("/api/library/mark", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
    .then((x) => x.ok ? x.json() : null).catch(() => null);
  if (!r) { toast("Could not save that"); return null; }
  box.outerHTML = markBtns(r.star, r.vote);
  if (body.star !== undefined) toast(r.star ? "Saved to your Library" : "Removed from Saved");
  return r;
}

async function annotate(kind) {
  const st = S[kind], res = st.results, runId = st.runId;
  if (!res || !res.items.length) return;
  const ann = await fetch("/api/library/annotate", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ kind, ids: res.items.map((i) => i.id), run_id: runId }),
  }).then((r) => r.ok ? r.json() : {}).catch(() => ({}));
  if (st.results !== res) return;
  st.ann = ann;
  applyAnn(kind);
}

function applyAnn(kind) {
  const st = S[kind], ann = st.ann || {};
  for (const art of $(".results", st.panel).querySelectorAll(".res[data-id]")) {
    const a = ann[art.dataset.id] || {}, head = $(".res-head", art);
    head.querySelector(".marks")?.remove();
    head.querySelector(".seen")?.remove();
    const s = a.seen;
    const seen = s ? `<span class="badge seen" title="First shown ${esc((s.first_at || "").slice(0, 10))} for “${esc(s.topic || "")}”${s.n > 1 ? `, in ${s.n} earlier runs` : ""}">seen ${ago(s.first_at)}</span>` : "";
    head.insertAdjacentHTML("beforeend", seen + markBtns(a.star, a.vote));
  }
}

// ---------------------------------------------------------------- runs
async function startSearch(kind, { remember = true, origin = "search", parentRun = null } = {}) {
  const st = S[kind], form = $("form", st.panel);
  const body = {
    kind, query: form.query.value.trim(), count: +form.count.value, since: form.since.value, model: form.model.value, origin,
  };
  if (parentRun) body.parent_run = parentRun;
  if (body.query.length < 2) return;
  if (body.since === "custom") body.since = CFG.default_since;  // "since last check" belongs to watch runs
  $(".prior", st.panel).hidden = true;
  if (remember) store.set("prefs:" + kind, { count: body.count, since: body.since, model: body.model });
  resetView(kind);
  setRunning(kind, true);
  try {
    const r = await fetch("/api/search", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
    const { run_id } = await r.json();
    attach(kind, run_id);
    refreshHistory(kind, run_id);
  } catch (e) {
    addEvent(kind, { type: "error", t: 0, title: "Could not start: " + (e.message || e) });
    setRunning(kind, false, "error");
  }
}

function attach(kind, runId) {
  const st = S[kind];
  st.es?.close();
  st.runId = runId;
  st.lastSeq = -1;
  setHash(runId);
  const es = new EventSource(`/api/runs/${runId}/events`);
  st.es = es;
  es.onmessage = (m) => { if (st.es === es) handleEvent(kind, JSON.parse(m.data)); };
  es.onerror = () => { if (!st.running) es.close(); };
}

async function cancelRun(kind) {
  const st = S[kind];
  if (!st.runId) return;
  $(".cancel", st.panel).disabled = true;
  await fetch(`/api/runs/${st.runId}/cancel`, { method: "POST" }).catch(() => {});
}

async function openRun(kind, runId) {
  const st = S[kind];
  const data = await fetch(`/api/runs/${runId}`).then((r) => r.ok ? r.json() : null);
  if (!data) return toast("Could not load that run");
  const form = $("form", st.panel), m = data.meta;
  form.query.value = m.query;
  setSelect(form.count, m.count);
  setSelect(form.since, m.since_key);
  setSelect(form.model, m.model);
  $(".prior", st.panel).hidden = true;
  resetView(kind);
  if (data.active) {
    setRunning(kind, true);
    attach(kind, runId);
    return;
  }
  st.runId = runId;
  st.lastSeq = -1;
  setHash(runId);
  $(".history", st.panel).value = runId;
  for (const ev of data.events) handleEvent(kind, ev, true);
  if (!data.events.some((e) => e.type === "end")) setRunning(kind, false, m.status);
}

function setSelect(sel, value) {
  if (value == null || value === "") return;
  const text = (sel.name === "since" && SINCE_LABEL[value]) || String(value);
  if (![...sel.options].some((o) => o.value === String(value))) sel.add(new Option(text, value));
  sel.value = value;
}

function handleEvent(kind, ev, replay = false) {
  const st = S[kind];
  if (ev.type === "eof") { st.es?.close(); if (st.running) setRunning(kind, false); return; }
  if (ev.seq != null) { if (ev.seq <= st.lastSeq) return; st.lastSeq = ev.seq; }
  switch (ev.type) {
    case "stats": updateStats(kind, ev.stats); return;
    case "start":
      if (!replay) st.t0 = Date.now() - (ev.t || 0) * 1000;
      break;
    case "results":
      renderResults(kind, ev.results);
      annotate(kind);
      break;
    case "end":
      st.es?.close();
      updateStats(kind, ev.stats, ev.elapsed);
      setRunning(kind, false, ev.status);
      if (!replay) refreshHistory(kind, st.runId);
      return;
  }
  addEvent(kind, ev);
}

// ---------------------------------------------------------------- timeline
function resetView(kind) {
  const st = S[kind], p = st.panel;
  st.results = null;
  st.ann = null;
  st.lastSeq = -1;
  $(".timeline", p).innerHTML = "";
  $(".stats", p).innerHTML = "";
  $(".results", p).innerHTML = "";
  $(".results-head", p).hidden = true;
  $(".sort", p).value = st.sort = "rank";
}

function setRunning(kind, running, status) {
  const st = S[kind], p = st.panel;
  st.running = running;
  $("button[type=submit]", p).disabled = running;
  $("button[type=submit]", p).textContent = running ? "Searching…" : "Search";
  const cancel = $(".cancel", p);
  cancel.hidden = !running;
  cancel.disabled = false;
  const pill = $(".status-pill", p);
  pill.dataset.status = running ? "running" : (status || "idle");
  pill.textContent = running ? "running" : (status || "idle");
  document.querySelector(`.tabs button[data-tab="${kind}"] .dot`).hidden = !running;
  clearInterval(st.timer);
  const tl = $(".timeline", p);
  tl.querySelector(".pending")?.remove();
  if (running) {
    st.t0 = st.t0 || Date.now();
    st.timer = setInterval(() => tickElapsed(kind), 250);
    const li = document.createElement("li");
    li.className = "ev pending";
    li.innerHTML = `<span class="ev-time"></span><span class="ev-icon">⋯</span><div class="ev-body"><div class="ev-title">Claude is working…</div></div>`;
    tl.appendChild(li);
  } else {
    st.t0 = null;
  }
}

function tickElapsed(kind) {
  const st = S[kind];
  if (!st.t0) return;
  const s = (Date.now() - st.t0) / 1000;
  const el = $(".stats .elapsed", st.panel);
  if (el) el.textContent = `⏱ ${s.toFixed(1)}s`;
  const pend = $(".timeline .pending .ev-time", st.panel);
  if (pend) pend.textContent = `+${s.toFixed(0)}s`;
}

function addEvent(kind, ev) {
  const st = S[kind], tl = $(".timeline", st.panel);
  const nearBottom = tl.scrollHeight - tl.scrollTop - tl.clientHeight < 60;
  const li = document.createElement("li");
  li.className = `ev ev-${ev.type}`;
  const clock = ev.ts ? ev.ts.slice(11, 19) : "";
  let html = `<span class="ev-time" title="${esc(ev.ts || "")}">+${(ev.t || 0).toFixed(1)}s</span>` +
    `<span class="ev-icon">${ICON[ev.type] || "•"}</span>` +
    `<div class="ev-body"><div class="ev-title">${esc(ev.title).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")}` +
    (ev.dur != null ? `<span class="ev-dur">${ev.dur}s</span>` : "") +
    `<span class="ev-clock">${clock}</span></div>`;
  if (ev.detail) {
    const open = ev.type === "start" ? " open" : "";
    const label = ev.type === "tool_result" ? "show results" : ev.type === "tool_call" ? "arguments" : "details";
    html += `<details${open}><summary>${label}</summary><pre>${esc(ev.detail)}</pre></details>`;
  }
  li.innerHTML = html + "</div>";
  const pending = tl.querySelector(".pending");
  tl.insertBefore(li, pending);
  if (nearBottom) tl.scrollTop = tl.scrollHeight;
}

function updateStats(kind, s, elapsed) {
  if (!s) return;
  const st = S[kind], box = $(".stats", st.panel);
  const chips = [];
  const el = elapsed != null ? `⏱ ${elapsed}s` : st.t0 ? `⏱ ${((Date.now() - st.t0) / 1000).toFixed(1)}s` : "";
  if (el) chips.push(`<span class="chip elapsed" title="elapsed">${el}</span>`);
  if (s.model) chips.push(`<span class="chip" title="model">${esc(s.model.replace(/^claude-/, ""))}</span>`);
  chips.push(`<span class="chip" title="assistant turns">turns ${s.turns || 0}</span>`);
  chips.push(`<span class="chip" title="tool calls">tools ${s.tool_calls || 0}</span>`);
  if (s.context_tokens) chips.push(`<span class="chip" title="tokens in the latest request's context">ctx ${num(s.context_tokens)}</span>`);
  if (s.output_tokens != null) chips.push(`<span class="chip" title="total input / output tokens">in ${num(s.input_tokens)} · out ${num(s.output_tokens)}</span>`);
  for (const [w, u] of Object.entries(s.rate_limits || {})) {
    if (u != null) chips.push(`<span class="chip" title="share of your Claude plan's ${esc(w)} usage window">${esc(w.replace("_", " "))} ${Math.round(u * 100)}%</span>`);
  }
  if (s.cost_usd != null) chips.push(`<span class="chip" title="API-equivalent cost reported by Claude Code (subscription plans are not billed per run)">$${s.cost_usd.toFixed(3)}</span>`);
  box.innerHTML = chips.join("");
}

// ---------------------------------------------------------------- results
function sorted(kind, items) {
  const by = S[kind].sort, a = [...items];
  const t = (x) => new Date(x || 0).getTime();
  const f = {
    rank: (x, y) => x.rank - y.rank,
    stars: (x, y) => (y.stars || 0) - (x.stars || 0),
    momentum: (x, y) => (y.stars_per_month || 0) - (x.stars_per_month || 0),
    pushed: (x, y) => t(y.pushed_at) - t(x.pushed_at),
    created: (x, y) => t(y.created_at) - t(x.created_at),
    date: (x, y) => t(y.published) - t(x.published),
    citations: (x, y) => (y.citations || 0) - (x.citations || 0),
    upvotes: (x, y) => (y.hf_upvotes || 0) - (x.hf_upvotes || 0),
    views: (x, y) => (y.views || 0) - (x.views || 0),
    trending: (x, y) => (y.views_per_day || 0) - (x.views_per_day || 0),
    likes: (x, y) => (y.likes || 0) - (x.likes || 0),
    short: (x, y) => (x.duration_s || 1e9) - (y.duration_s || 1e9),
    hn: (x, y) => (y.hn_points || 0) - (x.hn_points || 0),
    read: (x, y) => (x.read_min || 1e9) - (y.read_min || 1e9),
  }[by];
  return f ? a.sort(f) : a;
}

function renderResults(kind, res) {
  const st = S[kind], p = st.panel;
  st.results = res;
  if (!res) return;
  $(".results-head", p).hidden = false;
  $(".summary", p).innerHTML = `<h3>Landscape</h3><p>${esc(res.summary || "")}</p>`;
  $(".count", p).textContent = `${res.items.length} results · picked from ${res.candidates_seen ?? "?"} candidates`;
  const box = $(".results", p);
  if (!res.items.length) {
    box.innerHTML = `<div class="placeholder card">Claude found nothing relevant enough. Try a broader description or a longer time range.</div>`;
    return;
  }
  const items = sorted(kind, res.items);
  box.innerHTML = items.map({ github: repoCard, arxiv: paperCard, youtube: videoCard, blog: blogCard }[kind]).join("");
  [...box.children].forEach((el, i) => { el.dataset.id = items[i].id; });
  if (st.ann) applyAnn(kind);
}

function scoreHtml(s) {
  return `<span class="score" title="Claude's relevance × usefulness score"><span class="bar"><i style="width:${Math.max(0, Math.min(100, s))}%"></i></span>${s}</span>`;
}

function repoCard(r) {
  const [owner, name] = r.id.split("/");
  const meta = [
    `<span><strong>★ ${num(r.stars)}</strong></span>`,
    r.stars_per_month ? `<span title="average stars per month since creation">+${num(r.stars_per_month)}★/mo</span>` : "",
    `<span>⑂ ${num(r.forks)}</span>`,
    r.language ? `<span>${esc(r.language)}</span>` : "",
    r.license && r.license !== "NOASSERTION" ? `<span>${esc(r.license)}</span>` : "",
    `<span title="${esc(r.pushed_at)}">pushed ${ago(r.pushed_at)}</span>`,
    `<span title="${esc(r.created_at)}">created ${esc((r.created_at || "").slice(0, 7))}</span>`,
  ].join("");
  const papers = paperBadges(r.papers, "the README");
  const home = r.homepage ? `<a class="badge" href="${esc(r.homepage)}" target="_blank" rel="noopener">🏠 homepage</a>` : "";
  const about = `the GitHub project ${r.id}${r.description ? ` (${clip(r.description, 120)})` : ""}`;
  const find = findBtn("youtube", `Videos about ${about}: tutorials, demos or talks.`) +
    findBtn("blog", `Blog posts about ${about}: tutorials, write-ups, comparisons or experience reports.`);
  return `<article class="res card">
    <div class="res-head">
      <span class="rank">#${r.rank}</span>
      ${r.avatar ? `<img class="avatar" src="${esc(r.avatar)}&s=48" alt="" loading="lazy">` : ""}
      <a class="res-title" href="${esc(r.url)}" target="_blank" rel="noopener"><span class="owner">${esc(owner)}/</span>${esc(name)}</a>
      ${scoreHtml(r.score)}
    </div>
    ${r.description ? `<p class="desc">${esc(r.description)}</p>` : ""}
    <p class="reason"><b>Why</b>${esc(r.reason)}</p>
    <div class="meta">${meta}</div>
    ${(r.topics || []).length ? `<div class="tags">${r.topics.slice(0, 10).map((t) => `<span class="tag">${esc(t)}</span>`).join("")}</div>` : ""}
    <div class="tags">${papers}${home}${find}</div>
  </article>`;
}

function clip(s, n) { return s.length > n ? s.slice(0, n - 1) + "…" : s; }

function paperBadges(papers, where) {
  return (papers || []).map((pp) =>
    `<a class="badge" href="https://arxiv.org/abs/${esc(pp.id)}" target="_blank" rel="noopener" title="arXiv paper linked from ${where}: ${esc(pp.title || pp.id)}">📄 ${esc(pp.title ? clip(pp.title, 60) : "arXiv:" + pp.id)}</a>`).join("");
}

function repoBadges(urls, where) {
  return (urls || []).slice(0, 4).map((u) =>
    `<a class="badge code" href="${esc(u)}" target="_blank" rel="noopener" title="GitHub repo linked in ${where}">⌥ ${esc(u.replace("https://github.com/", ""))}</a>`).join("");
}

function findBtn(kind, query) {
  const [label, what] = kind === "youtube" ? ["▶ Find videos", "a YouTube search for videos"] : ["✎ Find blog posts", "a search for English blog posts"];
  return `<button type="button" class="badge find" data-kind="${kind}" data-q="${esc(query)}" title="Start ${what} about this">${label}</button>`;
}

function paperCard(p) {
  const au = p.authors || [];
  const authors = au.slice(0, 4).join(", ") + (au.length > 4 ? ` et al. (${au.length})` : "");
  const cats = (p.categories || []).slice(0, 4).join(" · ") || p.primary_category || "";
  const badges = [
    `<a class="badge" href="${esc(p.pdf || `https://arxiv.org/pdf/${p.id}`)}" target="_blank" rel="noopener">PDF</a>`,
    p.code_url ? `<a class="badge code" href="${esc(p.code_url)}" target="_blank" rel="noopener">⌥ Code${p.hf_github_stars ? " ★" + num(p.hf_github_stars) : ""}</a>` : "",
    p.citations != null ? `<span class="badge" title="Semantic Scholar">Cites ${num(p.citations)}${p.influential ? ` (${p.influential} infl.)` : ""}</span>` : "",
    p.hf_upvotes ? `<a class="badge" href="https://huggingface.co/papers/${esc(p.id)}" target="_blank" rel="noopener" title="Hugging Face community upvotes">▲ ${p.hf_upvotes} HF</a>` : "",
    p.venue || p.journal_ref ? `<span class="badge">${esc(clip(p.venue || p.journal_ref, 50))}</span>` : "",
    findBtn("youtube", `Videos that explain or present the paper "${p.title}" (arXiv:${p.id}): author talks, paper walkthroughs or tutorials.`),
    findBtn("blog", `Blog posts that explain or discuss the paper "${p.title}" (arXiv:${p.id}): explainers, walkthroughs or implementation notes.`),
  ].join("");
  return `<article class="res card">
    <div class="res-head">
      <span class="rank">#${p.rank}</span>
      <a class="res-title" href="${esc(p.url)}" target="_blank" rel="noopener">${esc(p.title)}</a>
      ${scoreHtml(p.score)}
    </div>
    <div class="sub">${esc(authors)}</div>
    <div class="sub">${esc((p.published || "").slice(0, 10))} · arXiv:${esc(p.id)}${cats ? " · " + esc(cats) : ""}</div>
    <p class="reason"><b>Why</b>${esc(p.reason)}</p>
    <div class="tags">${badges}</div>
    <details><summary>Abstract${p.ai_summary ? " & TL;DR" : ""}</summary>
      ${p.ai_summary ? `<p><b>TL;DR (HF):</b> ${esc(p.ai_summary)}</p>` : ""}
      <p>${esc(p.abstract)}</p>
      ${p.comment ? `<p><i>${esc(p.comment)}</i></p>` : ""}
    </details>
  </article>`;
}

const videoAt = (v, s) => `${v.url}&t=${s}s`;

function videoCard(v) {
  const meta = [
    `<span><strong>${num(v.views)}</strong> views</span>`,
    v.views_per_day ? `<span title="average views per day since upload">+${num(v.views_per_day)}/day</span>` : "",
    v.likes != null ? `<span>👍 ${num(v.likes)}</span>` : "",
    `<span title="${esc(v.published)}">uploaded ${ago(v.published)}</span>`,
    v.captions ? `<span title="the uploader added captions">CC</span>` : "",
    v.live ? `<span class="live">${v.live === "live" ? "● live now" : "upcoming"}</span>` : "",
  ].join("");
  const jump = v.start_s != null
    ? `<a class="badge jump" href="${esc(videoAt(v, v.start_s))}" target="_blank" rel="noopener" title="Where the part that matters for your request starts (checked against the captions or chapters)">▶ Jump to ${hms(v.start_s)}${v.start_label ? " · " + esc(clip(v.start_label, 50)) : ""}</a>`
    : "";
  const find = findBtn("blog", `Blog posts covering the same topic as the YouTube video "${v.title}"${v.channel ? ` by ${v.channel}` : ""}: written explainers or tutorials.`);
  const links = jump + paperBadges(v.papers, "the description") + repoBadges(v.code_links, "the description") + find;
  const chapters = (v.chapters || []).map((c) =>
    `<li><a href="${esc(videoAt(v, c.start_s))}" target="_blank" rel="noopener">${hms(c.start_s)}</a>${esc(c.title)}</li>`).join("");
  return `<article class="res card video">
    <a class="thumb" href="${esc(v.start_s != null ? videoAt(v, v.start_s) : v.url)}" target="_blank" rel="noopener" tabindex="-1" aria-hidden="true">
      ${v.thumbnail ? `<img src="${esc(v.thumbnail)}" alt="" loading="lazy">` : ""}
      <span class="dur">${v.duration_s ? hms(v.duration_s) : ""}</span>
    </a>
    <div class="vbody">
      <div class="res-head">
        <span class="rank">#${v.rank}</span>
        <a class="res-title" href="${esc(v.url)}" target="_blank" rel="noopener">${esc(v.title)}</a>
        ${scoreHtml(v.score)}
      </div>
      <div class="sub">${v.channel_url ? `<a href="${esc(v.channel_url)}" target="_blank" rel="noopener">${esc(v.channel)}</a>` : esc(v.channel)}${v.subscribers != null ? ` · ${num(v.subscribers)} subscribers` : ""}</div>
      <p class="reason"><b>Why</b>${esc(v.reason)}</p>
      <div class="meta">${meta}</div>
      <div class="tags">${links}</div>
      ${v.description || chapters ? `<details><summary>${chapters ? "Chapters & description" : "Description"}</summary>
        ${chapters ? `<ol class="chapters">${chapters}</ol>` : ""}
        ${v.description ? `<p class="vdesc">${esc(clip(v.description, 1500))}</p>` : ""}
      </details>` : ""}
    </div>
  </article>`;
}

function blogCard(b) {
  const by = [
    `<span title="${esc(b.host)}">${esc(b.site || b.host)}</span>`,
    b.author ? `<span>by ${esc(clip(b.author, 60))}</span>` : "",
    `<span title="publish date read from the page">${b.published ? esc(b.published) : "date unknown"}</span>`,
    `<span>~${b.read_min} min read (${num(b.words)} words)</span>`,
  ].filter(Boolean).join(" · ");
  const meta = [
    b.hn_id ? `<a href="${esc(b.hn_url)}" target="_blank" rel="noopener" title="Hacker News discussion${b.hn_posts > 1 ? ` (the best-voted of ${b.hn_posts} submissions)` : ""}"><strong>▲ ${num(b.hn_points)}</strong> · ${num(b.hn_comments)} comments on HN</a>` : "",
    b.paywalled ? `<span class="warn-txt" title="the page marks this post as members-only; you may only see a preview">🔒 paywalled</span>` : "",
    b.via ? `<span title="the page blocks automated readers, so Scout read the post from the site's feed">read via ${esc(b.via)}</span>` : "",
    b.original_url ? `<a href="${esc(b.original_url)}" target="_blank" rel="noopener" title="this page is a syndicated copy of a post published elsewhere">original post ↗</a>` : "",
  ].filter(Boolean).join("");
  const videos = (b.videos || []).map((v) =>
    `<a class="badge" href="https://www.youtube.com/watch?v=${esc(v.id)}" target="_blank" rel="noopener" title="YouTube video linked or embedded in the post">▶ ${esc(v.title ? clip(v.title, 50) : v.id)}</a>`).join("");
  const links = paperBadges(b.papers, "the post") + repoBadges(b.code_links, "the post") + videos;
  const outline = (b.outline || []).map((h) => `<li>${esc(h)}</li>`).join("");
  const thumb = b.image
    ? `<a class="thumb" href="${esc(b.url)}" target="_blank" rel="noopener" tabindex="-1" aria-hidden="true"><img src="${esc(b.image)}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.closest('.res').classList.remove('has-thumb');this.parentElement.remove()"></a>`
    : "";
  return `<article class="res card blog${b.image ? " has-thumb" : ""}">
    ${thumb}
    <div class="vbody">
      <div class="res-head">
        <span class="rank">#${b.rank}</span>
        <a class="res-title" href="${esc(b.url)}" target="_blank" rel="noopener">${esc(b.title)}</a>
        ${scoreHtml(b.score)}
      </div>
      <div class="sub">${by}</div>
      <p class="reason"><b>Why</b>${esc(b.reason)}</p>
      ${b.quote ? `<blockquote class="quote" title="Quoted verbatim from the article (Scout checked it against the page text)">${esc(b.quote)}</blockquote>` : ""}
      ${meta ? `<div class="meta">${meta}</div>` : ""}
      ${links ? `<div class="tags">${links}</div>` : ""}
      ${(b.tags || []).length ? `<div class="tags">${b.tags.map((t) => `<span class="tag">${esc(t)}</span>`).join("")}</div>` : ""}
      ${outline || b.description ? `<details><summary>${outline ? "Outline" : "Description"}${outline && b.description ? " & description" : ""}</summary>
        ${b.description ? `<p class="vdesc">${esc(b.description)}</p>` : ""}
        ${outline ? `<ol class="outline">${outline}</ol>` : ""}
      </details>` : ""}
    </div>
  </article>`;
}

function copyMarkdown(kind) {
  const st = S[kind], res = st.results;
  if (!res) return;
  const q = $("form", st.panel).query.value.trim();
  const lines = [`## ${KIND_LABEL[kind]}: ${q}`, "", res.summary, ""];
  for (const it of sorted(kind, res.items)) {
    if (kind === "github") {
      lines.push(`${it.rank}. **[${it.id}](${it.url})** ★${num(it.stars)} · ${it.language || "-"} · pushed ${ago(it.pushed_at)}  `,
        `   ${it.reason}`);
    } else if (kind === "youtube") {
      const jump = it.start_s != null ? ` · [jump to ${hms(it.start_s)}](${videoAt(it, it.start_s)})` : "";
      lines.push(`${it.rank}. **[${it.title}](${it.url})** · ${it.channel} · ${hms(it.duration_s)} · ${num(it.views)} views${jump}  `,
        `   ${it.reason}`);
    } else if (kind === "blog") {
      const hn = it.hn_id ? ` · ▲${it.hn_points} on [HN](${it.hn_url})` : "";
      lines.push(`${it.rank}. **[${it.title}](${it.url})** · ${it.site || it.host} · ${it.published || "undated"} · ~${it.read_min} min${hn}  `,
        `   ${it.reason}`);
      if (it.quote) lines.push(`   > ${it.quote}`);
    } else {
      lines.push(`${it.rank}. **[${it.title}](${it.url})** (${(it.published || "").slice(0, 10)})${it.code_url ? ` · [code](${it.code_url})` : ""}  `,
        `   ${it.reason}`);
    }
  }
  const text = lines.join("\n");
  (navigator.clipboard?.writeText(text) || Promise.reject()).then(() => toast("Copied as Markdown"), () => {
    const ta = document.createElement("textarea"); ta.value = text; document.body.appendChild(ta); ta.select();
    document.execCommand("copy"); ta.remove(); toast("Copied as Markdown");
  });
}

// ---------------------------------------------------------------- history
async function refreshHistory(kind, selectId) {
  const rows = await fetch(`/api/history?kind=${kind}`).then((r) => r.json()).catch(() => []);
  const sel = $(".history", S[kind].panel);
  sel.innerHTML = `<option value="">— past searches (${rows.length}) —</option>` + rows.map((m) => {
    const when = (m.started_at || "").slice(0, 16).replace("T", " ");
    const tail = m.status === "done" ? `${m.n_results} results` : m.status;
    const how = { watch: "👁 watch · ", find: "↳ " }[m.origin] || "";
    return `<option value="${esc(m.id)}">${esc(when)} · ${how}${esc(clip(m.query, 70))} · ${esc(tail)}</option>`;
  }).join("");
  sel.value = selectId || S[kind].runId || "";
  if (sel.selectedIndex < 0) sel.value = "";
}

window.addEventListener("DOMContentLoaded", init);  // after library.js has loaded too
