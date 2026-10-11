"use strict";

// ---------------------------------------------------------------- Library tab
// Your past searches as topics (same text across tabs = one topic), saved results, watched
// topics, suggestions and insights. Uses app.js helpers: $, esc, num, ago, clip, toast, S,
// KINDS, TAB_NAME, showTab, prefill, openFromLibrary, markBtns, toggleMark.

const KIND_ICON = { github: "⌥", arxiv: "📄", youtube: "▶", blog: "✎" };
const EVERY = [[1, "daily"], [3, "every 3 days"], [7, "weekly"], [14, "every 2 weeks"], [30, "monthly"]];
const LIB = { data: null, topics: new Map(), q: "", filter: "all", showHidden: false, loadSeq: 0, searchSeq: 0, panel: null };

const inDays = (iso) => {
  if (!iso) return "?";
  const d = (new Date(iso).getTime() - Date.now()) / 864e5;
  if (d <= 0) return "due now";
  return d < 1 ? `in ${Math.max(1, Math.round(d * 24))}h` : `in ${Math.round(d)}d`;
};
const money = (x) => x == null ? "?" : x < 10 ? `$${x.toFixed(2)}` : `$${Math.round(x)}`;
const post = (url, body, method = "POST") => fetch(url, {
  method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body ?? {}),
}).then(async (r) => r.ok ? r.json() : Promise.reject(new Error((await r.json().catch(() => ({}))).detail || r.statusText)));

function libInit() {
  const p = document.createElement("section");
  p.id = "panel-library";
  p.className = "panel lib";
  p.hidden = true;
  p.setAttribute("role", "tabpanel");
  const filters = ["all", "topics", ...Object.keys(KINDS)].map((f) =>
    `<button type="button" data-f="${f}" aria-pressed="${f === "all"}">${f === "all" ? "All" : f === "topics" ? "Topics" : TAB_NAME[f]}</button>`).join("");
  p.innerHTML = `
    <div class="lib-search card">
      <input type="search" class="lib-q" placeholder="Search your library: topics, keywords, tags, notes and every result you got" aria-label="Search your library">
      <div class="lib-filters" hidden>${filters}</div>
    </div>
    <div class="lib-stats"></div>
    <div class="lib-found" hidden></div>
    <div class="lib-grid">
      <div class="lib-main">
        <div class="lib-h"><h2>Topics</h2><span class="grow"></span><label class="lib-hidden-toggle" hidden><input type="checkbox"> show hidden</label></div>
        <div class="lib-topics"></div>
        <div class="lib-h"><h2>Saved</h2><span class="muted small">★ on any result card</span></div>
        <div class="lib-saved card"></div>
      </div>
      <aside class="lib-side">
        <section class="card lib-box"><div class="lib-h"><h2>Ideas from Claude</h2></div><div class="lib-ideas"></div></section>
        <section class="card lib-box"><div class="lib-h"><h2>Suggested next</h2><span class="muted small">free, from your own results</span></div><div class="lib-sugg"></div></section>
        <section class="card lib-box"><div class="lib-h"><h2>Insights</h2></div><div class="lib-insights"></div></section>
      </aside>
    </div>`;
  $("#panels").appendChild(p);
  LIB.panel = p;

  let t;
  $(".lib-q", p).addEventListener("input", (ev) => { clearTimeout(t); t = setTimeout(() => libSearch(ev.target.value), 250); });
  $(".lib-filters", p).addEventListener("click", (ev) => {
    const b = ev.target.closest("[data-f]");
    if (!b) return;
    LIB.filter = b.dataset.f;
    p.querySelectorAll(".lib-filters button").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
    libSearch($(".lib-q", p).value);
  });
  $(".lib-hidden-toggle input", p).addEventListener("change", (ev) => { LIB.showHidden = ev.target.checked; libRender(); });
  p.addEventListener("click", libClick);
  p.addEventListener("change", libChange);
  p.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && ev.target.classList.contains("tag-in")) {
      ev.preventDefault();
      const tp = LIB.topics.get(+ev.target.dataset.tid), v = ev.target.value.trim().replace(/^#/, "");
      if (tp && v) patchTopic(tp.id, { tags: [...tp.tags, v] });
    }
  });
}

async function libLoad() {
  const seq = ++LIB.loadSeq;
  const data = await fetch("/api/library").then((r) => r.json()).catch(() => null);
  if (!data || seq !== LIB.loadSeq) return;
  LIB.data = data;
  LIB.topics = new Map(data.topics.map((t) => [t.id, t]));
  libRender();
  if (LIB.q) libSearch(LIB.q);
}

function libRender() {
  const p = LIB.panel, d = LIB.data;
  if (!d) return;
  renderStats(d.stats, d.watch_running);
  const toggle = $(".lib-hidden-toggle", p);
  toggle.hidden = !d.stats.hidden;
  toggle.lastChild.textContent = ` show hidden (${d.stats.hidden})`;
  const topics = d.topics.filter((t) => LIB.showHidden || !t.hidden);
  $(".lib-topics", p).innerHTML = topics.length ? topics.map(topicCard).join("")
    : `<div class="placeholder card">No searches yet. Every search you run in the other tabs is collected here.</div>`;
  $(".lib-saved", p).innerHTML = d.saved.length ? d.saved.map((it) => itemRow(it)).join("")
    : `<p class="muted pad">Nothing saved yet. Press ☆ on a result card to keep it here.</p>`;
  renderIdeas(d.ideas);
  renderSuggestions(d.suggestions);
  renderInsights(d.insights);
}

function renderStats(s, running) {
  const tile = (label, value, sub = "", title = "") =>
    `<div class="stat card"${title ? ` title="${esc(title)}"` : ""}><div class="stat-l">${label}</div><div class="stat-v">${value}</div>${sub ? `<div class="stat-s">${sub}</div>` : ""}</div>`;
  const by = Object.entries(s.runs_by_kind || {}).map(([k, n]) => `${TAB_NAME[k]} ${n}`).join(" · ");
  $(".lib-stats", LIB.panel).innerHTML = [
    tile("Topics", num(s.topics), s.kw_pending ? "labelling new topics…" : ""),
    tile("Searches", num(s.runs), esc(by)),
    tile("Results found", num(s.items), "unique repos, papers, videos & posts"),
    tile("Saved", num(s.saved)),
    tile("Watched", num(s.watching), running ? `checking ${TAB_NAME[running.kind]} now…` : s.unseen ? `${s.unseen} with new results` : "",
      "A watched topic+tab is re-run on a schedule while Scout is running; only results you have not seen are listed."),
    tile("Spent this month", money(s.cost_month_usd), `${money(s.cost_usd)} in total`,
      "API-equivalent cost reported by Claude Code: searches, watch checks, keywords and ideas. Subscription plans are not billed per run."),
  ].join("");
}

function tabPills(t) {
  return Object.keys(KINDS).map((k) => {
    const v = t.kinds[k], w = t.watches[k];
    if (!v) return `<button type="button" class="tpill missing" data-prefill="${k}" data-tid="${t.id}" title="Put this topic in the ${TAB_NAME[k]} search box">+ ${TAB_NAME[k]}</button>`;
    const on = !!(w && w.enabled);
    const fresh = w && w.unseen && w.last_new
      ? `<button type="button" class="newb" data-open-kind="${k}" data-run="${esc(w.last_run)}" title="Open the latest watch check">${w.last_new} new</button>` : "";
    const res = v.status === "done" ? `${v.n_results ?? 0}` : v.status;
    return `<span class="tpill-wrap"><button type="button" class="tpill" data-open-kind="${k}" data-run="${esc(v.last_run)}" title="Open the latest ${TAB_NAME[k]} run${v.runs > 1 ? ` (${v.runs} runs)` : ""}">${KIND_ICON[k]} ${TAB_NAME[k]} · ${esc(res)} · ${ago(v.last_at)}</button>${fresh}` +
      `<button type="button" class="watch-toggle" data-kind="${k}" data-tid="${t.id}" aria-pressed="${on}" title="${on ? "Watched: re-checked for new results. Click to stop watching." : "Watch: re-check this topic in " + TAB_NAME[k] + " on a schedule and list only new results"}">👁</button></span>`;
  }).join("");
}

function topicCard(t) {
  const label = t.label || clip(t.query, 90);
  const kws = t.keywords.map((k) => `<button type="button" class="kw" data-q="${esc(k)}" title="Search your library for this">${esc(k)}</button>`).join("");
  const purpose = t.purpose ? `<span class="purpose" title="Purpose, from your request">🎯 ${esc(t.purpose)}</span>` : "";
  const pending = t.kw_status == null ? `<span class="muted small">labelling…</span>` : "";
  const watched = Object.entries(t.watches).filter(([, w]) => w.enabled);
  let watch = "";
  if (watched.length) {
    const every = watched[0][1].every_days;
    const next = watched.map(([, w]) => w.next_due).sort()[0];
    const notes = watched.filter(([, w]) => w.note).map(([k, w]) => `${TAB_NAME[k]}: ${esc(w.note)}`).join(" · ");
    watch = `<div class="t-watch">👁 Watching ${watched.map(([k]) => TAB_NAME[k]).join(", ")} ·
      <select class="every" data-tid="${t.id}" aria-label="How often to check">${EVERY.map(([n, l]) => `<option value="${n}"${n === every ? " selected" : ""}>${l}</option>`).join("")}</select>
      · next check ${inDays(next)} <button type="button" class="link check-now" data-tid="${t.id}" title="Each check is one search run (about $0.15–0.20 with Sonnet)">check now</button>
      ${notes ? `<span class="warn-txt"> · ${notes}</span>` : ""}</div>`;
  }
  const tags = t.tags.map((g) => `<span class="utag">#${esc(g)}<button type="button" class="tag-x" data-tid="${t.id}" data-tag="${esc(g)}" aria-label="Remove tag ${esc(g)}">×</button></span>`).join("");
  const related = (t.related || []).map((id) => LIB.topics.get(id)).filter(Boolean)
    .map((r) => `<button type="button" class="link rel" data-goto="${r.id}">${esc(r.label || clip(r.query, 50))}</button>`).join(", ");
  const finds = t.finds.map((f) => `<button type="button" class="link" data-open-kind="${f.kind}" data-run="${esc(f.id)}" title="${esc(f.query)}">${KIND_ICON[f.kind]} ${f.kind === "youtube" ? "videos" : "posts"} on ${esc(clip(findSubject(f.query), 50))}</button>`).join(" · ");
  return `<article class="topic card${t.hidden ? " is-hidden" : ""}" id="topic-${t.id}">
    <div class="t-head">
      <h3 class="t-label" title="${esc(t.query)}">${esc(label)}</h3>
      <button type="button" class="ghost edit-label" data-tid="${t.id}" title="Rename">✎</button>
      <span class="grow"></span>
      <span class="muted small" title="${esc(t.last_at || "")}">${ago(t.last_at || t.created_at)}</span>
      <button type="button" class="ghost hide-topic" data-tid="${t.id}" title="${t.hidden ? "Show this topic again" : "Hide this topic (its runs stay in History)"}">${t.hidden ? "unhide" : "hide"}</button>
    </div>
    ${t.label ? `<div class="t-query">“${esc(clip(t.query, 240))}”</div>` : ""}
    <div class="t-kw">${purpose}${kws}${pending}</div>
    <div class="t-tabs">${tabPills(t)}</div>
    ${watch}
    ${finds ? `<div class="t-line">↳ ${finds}</div>` : ""}
    ${related ? `<div class="t-line muted">Related: ${related}</div>` : ""}
    <div class="t-tags">${tags}<input class="tag-in" data-tid="${t.id}" placeholder="+ tag" aria-label="Add a tag" size="8"></div>
    <details class="t-notes"${t.notes ? " open" : ""}><summary>Notes</summary>
      <textarea class="notes" data-tid="${t.id}" rows="2" placeholder="What is it for? What did you conclude? (saved when you click away)">${esc(t.notes)}</textarea></details>
  </article>`;
}

// What a "Find videos / Find blog posts" query was about: the repo, paper or video it names.
function findSubject(q) {
  const m = q.match(/the GitHub project (\S+?)(?=[\s(:]|$)/) || q.match(/the paper "(.+?)" \(arXiv/) || q.match(/the YouTube video "(.+?)"/);
  return m ? m[1] : q;
}

function itemRow(it, reason = false) {
  const from = it.topic ? ` · <button type="button" class="link" data-open-kind="${it.kind}" data-run="${esc(it.run)}" title="Open the run that found it">from “${esc(clip(it.topic, 50))}”</button>` : "";
  return `<div class="lib-item" data-kind="${it.kind}" data-id="${esc(it.id)}">
    <span class="li-icon" title="${TAB_NAME[it.kind]}">${KIND_ICON[it.kind]}</span>
    <div class="li-body">
      <a class="li-title" href="${esc(it.url)}" target="_blank" rel="noopener">${esc(it.title)}</a>
      <div class="li-sub">${esc(it.sub || "")}${from}${it.runs > 1 ? ` · in ${it.runs} runs` : ""}</div>
      ${reason && it.reason ? `<div class="li-reason">${esc(clip(it.reason, 240))}</div>` : ""}
    </div>
    ${markBtns(it.star, it.vote)}
  </div>`;
}

function renderIdeas(ideas) {
  $(".lib-ideas", LIB.panel).innerHTML = `
    <p class="muted small">Claude (Haiku) reads your topics, keywords, tags, notes and ★/👍/👎, then proposes next searches. About 1¢ a click.</p>
    <button type="button" class="btn ask-ideas">✨ Suggest searches</button>
    <ol class="ideas">${ideas.map((i) => `<li>
      <div class="idea-q">${esc(i.query)}</div>
      ${i.why ? `<div class="idea-why">${esc(i.why)}</div>` : ""}
      <div class="idea-act"><button type="button" class="badge find" data-prefill="${i.kind}" data-q="${esc(i.query)}">Search in ${TAB_NAME[i.kind]} ▸</button>
        <button type="button" class="link idea-dismiss" data-id="${i.id}">dismiss</button></div></li>`).join("")}</ol>`;
}

function renderSuggestions(list) {
  const linked = list.filter((s) => s.type === "paper" || s.type === "repo");
  const tabs = list.filter((s) => s.type === "tabs");
  const kws = list.filter((s) => s.type === "keyword");
  const by = (s) => s.by.map((b) => `${KIND_ICON[b.kind]} ${esc(clip(b.title, 40))}`).join("; ");
  const parts = [];
  if (linked.length) {
    parts.push(`<h3>Linked from your results, not opened yet</h3><ul class="sugg">${linked.map((s) => {
      const q = s.type === "paper"
        ? `The paper "${s.title}" (arXiv:${s.id}) and the closest related work: follow-ups, alternatives and benchmarks.`
        : `The GitHub project ${s.id} and similar projects: alternatives and how they compare.`;
      const k = s.type === "paper" ? "arxiv" : "github";
      return `<li><a href="${esc(s.url)}" target="_blank" rel="noopener">${KIND_ICON[k]} ${esc(clip(s.title, 90))}</a>
        <div class="muted small">linked by ${s.n}: ${by(s)}</div>
        <button type="button" class="badge find" data-prefill="${k}" data-q="${esc(q)}">Search ${TAB_NAME[k]} ▸</button></li>`;
    }).join("")}</ul>`);
  }
  if (tabs.length) {
    parts.push(`<h3>Not tried in every tab</h3><ul class="sugg">${tabs.map((s) =>
      `<li><span>${esc(clip(s.title, 60))}</span> → ${s.missing.map((k) => `<button type="button" class="badge find" data-prefill="${k}" data-tid="${s.topic_id}">${TAB_NAME[k]}</button>`).join(" ")}</li>`).join("")}</ul>`);
  }
  if (kws.length) {
    parts.push(`<h3>Tags on your results you never searched</h3><ul class="sugg">${kws.map((s) =>
      `<li><button type="button" class="kw" data-q="${esc(s.title)}" title="Find these results in your library">${esc(s.title)}</button>
        <span class="muted small">on ${s.n} results</span>
        <span class="minis">${Object.keys(KINDS).map((k) => `<button type="button" class="mini" data-prefill="${k}" data-q="${esc(s.title)}" title="Search ${TAB_NAME[k]} for “${esc(s.title)}”" aria-label="Search ${TAB_NAME[k]}">${KIND_ICON[k]}</button>`).join("")}</span></li>`).join("")}</ul>`);
  }
  $(".lib-sugg", LIB.panel).innerHTML = parts.join("") || `<p class="muted small">Suggestions appear after a few searches.</p>`;
}

// Single-series charts in the accent hue: columns = searches per week, bars = tag counts.
function renderInsights(ins) {
  const weeks = ins.activity;
  const max = Math.max(1, ...weeks.map((w) => Object.keys(KINDS).reduce((a, k) => a + w[k], 0)));
  const fmtWeek = (iso) => new Date(iso + "T00:00").toLocaleDateString("en", { month: "short", day: "numeric" });
  const cols = weeks.map((w) => {
    const total = Object.keys(KINDS).reduce((a, k) => a + w[k], 0);
    const parts = Object.keys(KINDS).filter((k) => w[k]).map((k) => `${TAB_NAME[k]} ${w[k]}`).join(", ");
    const tip = `Week of ${fmtWeek(w.week)}: ${total} search${total === 1 ? "" : "es"}${parts ? ` (${parts})` : ""}`;
    return `<div class="col" tabindex="0" aria-label="${esc(tip)}">
      <span class="cap">${total || ""}</span><span class="bar" style="height:${(total / max) * 100}%"></span>
      <span class="tip" role="tooltip">${esc(tip)}</span></div>`;
  }).join("");
  const ticks = weeks.map((w, i) => `<span>${i % 2 === weeks.length % 2 ? "" : esc(fmtWeek(w.week))}</span>`).join("");
  const tags = ins.result_tags.slice(0, 10), tmax = Math.max(1, ...tags.map(([, n]) => n));
  const bars = tags.map(([t, n]) => `<div class="hbar" title="${n} of your results carry this tag">
    <button type="button" class="kw plain" data-q="${esc(t.replace(/-/g, " "))}">${esc(t)}</button>
    <span class="track"><span class="fill" style="width:${(n / tmax) * 100}%"></span></span><span class="val">${n}</span></div>`).join("");
  const kws = ins.keywords.map(([k, n]) => `<button type="button" class="kw" data-q="${esc(k)}">${esc(k)}${n > 1 ? ` <b>×${n}</b>` : ""}</button>`).join("");
  $(".lib-insights", LIB.panel).innerHTML = `
    <h3>Searches per week</h3>
    <div class="cols" role="group" aria-label="Searches per week">${cols}</div>
    <div class="cols-x" aria-hidden="true">${ticks}</div>
    <details class="small"><summary>As a table</summary><table class="tbl"><thead><tr><th>Week of</th>${Object.keys(KINDS).map((k) => `<th>${TAB_NAME[k]}</th>`).join("")}</tr></thead>
      <tbody>${weeks.map((w) => `<tr><td>${esc(fmtWeek(w.week))}</td>${Object.keys(KINDS).map((k) => `<td>${w[k]}</td>`).join("")}</tr>`).join("")}</tbody></table></details>
    ${kws ? `<h3>Your topics' keywords</h3><div class="t-kw">${kws}</div>` : ""}
    ${bars ? `<h3>Most common tags on your results</h3><div class="hbars">${bars}</div>` : ""}`;
}

// ---------------------------------------------------------------- search
async function libSearch(q) {
  const p = LIB.panel, found = $(".lib-found", p);
  LIB.q = q.trim();
  const on = !!LIB.q;
  $(".lib-filters", p).hidden = !on;
  $(".lib-grid", p).hidden = on;
  $(".lib-stats", p).hidden = on;
  found.hidden = !on;
  if (!on) return;
  const kind = KINDS[LIB.filter] ? LIB.filter : "";
  const seq = ++LIB.searchSeq;
  const r = await fetch(`/api/library/search?q=${encodeURIComponent(LIB.q)}${kind ? `&kind=${kind}` : ""}`).then((x) => x.json()).catch(() => null);
  if (!r || seq !== LIB.searchSeq) return;
  const topics = LIB.filter === "all" || LIB.filter === "topics" ? r.topics : [];
  const items = LIB.filter === "topics" ? [] : r.items;
  const n = topics.length + items.length;
  found.innerHTML = `<div class="lib-h"><h2>${n} match${n === 1 ? "" : "es"} for “${esc(LIB.q)}”</h2>${r.mode === "any word" ? `<span class="muted small">no result had every word, so these match any of them</span>` : ""}</div>
    ${topics.length ? `<h3>Topics</h3>${topics.map((t) => `<div class="topic-row card"><button type="button" class="link t-label" data-goto="${t.id}">${esc(t.label || clip(t.query, 90))}</button><div class="t-tabs">${tabPills(t)}</div></div>`).join("")}` : ""}
    ${items.length ? `<h3>Results</h3><div class="card">${items.map((it) => itemRow(it, true)).join("")}</div>` : ""}
    ${n ? "" : `<div class="placeholder card">Nothing in your library matches. Try fewer words, or search for it in a tab.</div>`}`;
}

// ---------------------------------------------------------------- actions
async function patchTopic(tid, body) {
  try { await post(`/api/library/topics/${tid}`, body, "PATCH"); } catch (e) { return toast("Could not save: " + e.message); }
  libLoad();
}

async function libClick(ev) {
  const el = ev.target.closest("button, a");
  if (!el || el.tagName === "A") return;
  const d = el.dataset;
  if (el.classList.contains("mk")) {
    const row = el.closest("[data-id]");
    await toggleMark(el, row.dataset.kind, row.dataset.id);
    if (el.dataset.mk === "star" && !LIB.q) libLoad();
  } else if (d.run) {
    openFromLibrary(d.openKind, d.run);
  } else if (d.prefill) {
    prefill(d.prefill, d.q ?? LIB.topics.get(+d.tid)?.query ?? "");
  } else if (el.classList.contains("kw")) {
    const box = $(".lib-q", LIB.panel);
    box.value = d.q;
    libSearch(d.q);
    window.scrollTo({ top: 0, behavior: "smooth" });
  } else if (d.goto) {
    if (LIB.q) { $(".lib-q", LIB.panel).value = ""; await libSearch(""); }
    const card = document.getElementById(`topic-${d.goto}`);
    card?.scrollIntoView({ behavior: "smooth", block: "center" });
    card?.classList.add("flash");
    setTimeout(() => card?.classList.remove("flash"), 1600);
  } else if (el.classList.contains("watch-toggle")) {
    const on = el.getAttribute("aria-pressed") !== "true";
    try {
      const w = await post(`/api/library/topics/${d.tid}/watch`, { kind: d.kind, enabled: on }, "PUT");
      toast(on ? `Watching in ${TAB_NAME[d.kind]} · first check ${inDays(w.next_due)}` : "Stopped watching");
    } catch (e) { toast("Could not change the watch: " + e.message); }
    libLoad();
    pollWatch();
  } else if (el.classList.contains("check-now")) {
    const r = await post(`/api/library/topics/${d.tid}/check-now`).catch(() => null);
    toast(r ? `Queued ${r.queued} check${r.queued === 1 ? "" : "s"}; they run one at a time, results appear here` : "Could not queue the check");
    setTimeout(() => { libLoad(); pollWatch(); }, 1500);
  } else if (el.classList.contains("hide-topic")) {
    const tp = LIB.topics.get(+d.tid);
    patchTopic(+d.tid, { hidden: !tp.hidden });
  } else if (el.classList.contains("edit-label")) {
    const tp = LIB.topics.get(+d.tid);
    const v = prompt("Topic name (leave empty to use the request text):", tp.label || "");
    if (v !== null) patchTopic(tp.id, { label: v.trim() });
  } else if (el.classList.contains("tag-x")) {
    const tp = LIB.topics.get(+d.tid);
    patchTopic(tp.id, { tags: tp.tags.filter((g) => g !== d.tag) });
  } else if (el.classList.contains("idea-dismiss")) {
    await post(`/api/library/ideas/${d.id}/dismiss`).catch(() => null);
    libLoad();
  } else if (el.classList.contains("ask-ideas")) {
    el.disabled = true;
    el.textContent = "Claude is thinking…";
    try {
      const r = await post("/api/library/ideas");
      toast(`${r.ideas.length} ideas${r.cost_usd != null ? ` · $${r.cost_usd.toFixed(3)}` : ""}`);
    } catch (e) {
      toast("No ideas: " + e.message);
    }
    libLoad();
  }
}

function libChange(ev) {
  const el = ev.target;
  if (el.classList.contains("notes")) {
    patchTopic(+el.dataset.tid, { notes: el.value });
  } else if (el.classList.contains("every")) {
    const tp = LIB.topics.get(+el.dataset.tid);
    Promise.all(Object.entries(tp.watches).filter(([, w]) => w.enabled).map(([k]) =>
      post(`/api/library/topics/${tp.id}/watch`, { kind: k, enabled: true, every_days: +el.value }, "PUT")))
      .then(() => { toast("Schedule saved"); libLoad(); }, (e) => toast("Could not save: " + e.message));
  }
}
