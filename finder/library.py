"""Library: every query, run and result in one SQLite file (runs/scout.db).

runs/<id>/ stays the source of truth for each run. This database indexes it (sync() adds
anything missing at startup) and holds what only lives here: the keywords Claude extracted
per topic, your tags and notes, ★/👍/👎 marks, watched topics and Claude's search ideas.

A topic is one request text: the same words searched in several tabs are one topic, and a
"Find videos / Find blog posts" run belongs to the topic of the run it was started from.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import threading
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import date, datetime, timedelta

from . import llm
from .config import DEFAULT_MODEL, KINDS, RUNS_DIR

DB_PATH = RUNS_DIR / "scout.db"
RUN_ID_RE = re.compile(r"^\d{8}-\d{6}-(github|arxiv|youtube|blog)-[0-9a-f]{4}$")
WATCH_COUNT = 5          # a watch run lists only what is new, so a short list is enough
WATCH_EVERY_DAYS = 7
WATCH_MIN_WINDOW_DAYS = 30  # search APIs index late, so a check always looks a month back
RUN_ONLY_FIELDS = ("rank", "score", "reason", "start_s", "start_label", "quote")

SCHEMA = """
CREATE TABLE IF NOT EXISTS topics(
  id INTEGER PRIMARY KEY, norm TEXT UNIQUE NOT NULL, query TEXT NOT NULL,
  label TEXT, keywords TEXT NOT NULL DEFAULT '[]', purpose TEXT, kw_status TEXT,
  tags TEXT NOT NULL DEFAULT '[]', notes TEXT NOT NULL DEFAULT '', hidden INTEGER NOT NULL DEFAULT 0,
  created_at TEXT, last_at TEXT);
CREATE TABLE IF NOT EXISTS runs(
  id TEXT PRIMARY KEY, topic_id INTEGER, kind TEXT, query TEXT, origin TEXT NOT NULL DEFAULT 'search',
  parent_run TEXT, status TEXT, started_at TEXT, model TEXT, since_key TEXT, since TEXT, count INTEGER,
  n_results INTEGER, n_new INTEGER, cost_usd REAL);
CREATE INDEX IF NOT EXISTS runs_topic ON runs(topic_id, kind);
CREATE TABLE IF NOT EXISTS items(
  kind TEXT, key TEXT, id TEXT, title TEXT, url TEXT, sub TEXT, data TEXT,
  first_run TEXT, first_at TEXT, last_at TEXT, PRIMARY KEY(kind, key));
CREATE TABLE IF NOT EXISTS run_items(
  run_id TEXT, kind TEXT, key TEXT, rank INTEGER, score INTEGER, reason TEXT, is_new INTEGER,
  PRIMARY KEY(run_id, kind, key));
CREATE INDEX IF NOT EXISTS run_items_key ON run_items(kind, key);
CREATE TABLE IF NOT EXISTS marks(
  kind TEXT, key TEXT, star INTEGER NOT NULL DEFAULT 0, vote INTEGER NOT NULL DEFAULT 0, updated_at TEXT,
  PRIMARY KEY(kind, key));
CREATE TABLE IF NOT EXISTS watches(
  topic_id INTEGER, kind TEXT, enabled INTEGER NOT NULL DEFAULT 1, every_days INTEGER, model TEXT,
  count INTEGER, next_due TEXT, last_run TEXT, last_new INTEGER, unseen INTEGER NOT NULL DEFAULT 0,
  note TEXT, PRIMARY KEY(topic_id, kind));
CREATE TABLE IF NOT EXISTS ideas(
  id INTEGER PRIMARY KEY, created_at TEXT, kind TEXT, query TEXT, why TEXT, builds_on TEXT,
  status TEXT NOT NULL DEFAULT 'new', run_id TEXT);
CREATE TABLE IF NOT EXISTS llm_calls(ts TEXT, purpose TEXT, model TEXT, cost_usd REAL);
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(ref UNINDEXED, title, body,
  tokenize='porter unicode61 remove_diacritics 2');
"""

_lock = threading.RLock()  # one writer at a time; every call is a few ms
_kw_busy = threading.Lock()


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@contextmanager
def _db():
    with _lock:
        con = sqlite3.connect(DB_PATH, timeout=10)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        finally:
            con.close()


def init() -> None:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    with _db() as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(SCHEMA)
        con.execute("UPDATE topics SET kw_status=NULL WHERE kw_status='error'")  # retry once per start


def _log(msg: str) -> None:
    print(f"[library] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------- text helpers

def norm_query(q: str) -> str:
    return " ".join(q.lower().split()).strip(" .!?")


def item_key(kind: str, item_id: str) -> str:
    return item_id.strip().lower() if kind == "github" else item_id.strip()


_WORD = re.compile(r"[^\W_][\w+#.-]*", re.U)
_STOP = set("""a an and or the of for in on to with without by from at as is are be it its this that
these those i me my we our you your will would can could should use using used want need like such
about into via vs best good great top new find show give get some any all more most how what which who
why when where do does make build ideally e.g eg etc""".split())


def tokens(s: str) -> set[str]:
    out = set()
    for w in _WORD.findall((s or "").lower()):
        w = w.strip(".-")
        if len(w) > 1 and w not in _STOP:
            out.add(w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w)
    return out


def _j(s: str | None, default):
    try:
        return json.loads(s) if s else default
    except ValueError:
        return default


def _clip(s: str | None, n: int) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _human(n) -> str:
    if n is None:
        return "?"
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.1f}k" if n >= 1000 else str(n)


def _item_view(kind: str, it: dict) -> tuple[str, str, str]:
    """(title, one-line facts, searchable text) for a result item."""
    tags = " ".join(it.get("topics") or it.get("tags") or [])
    if kind == "github":
        sub = " · ".join(x for x in [f"★ {_human(it.get('stars'))}", it.get("language"),
                                     f"pushed {(it.get('pushed_at') or '')[:10]}"] if x)
        return it["id"], sub, " ".join(filter(None, [it.get("description"), tags, it.get("language")]))
    if kind == "arxiv":
        cites = f"cites {it['citations']}" if it.get("citations") is not None else ""
        sub = " · ".join(x for x in [(it.get("published") or "")[:10], it.get("primary_category"), cites] if x)
        authors = ", ".join(it.get("authors") or [])
        return it.get("title") or it["id"], sub, " ".join(filter(None, [authors, it.get("abstract"),
                                                                     " ".join(it.get("categories") or [])]))
    if kind == "youtube":
        dur = it.get("duration_s") or 0
        sub = " · ".join(x for x in [it.get("channel"), f"{_human(it.get('views'))} views",
                                     f"{dur // 60} min" if dur else "", (it.get("published") or "")[:10]] if x)
        return it.get("title") or it["id"], sub, " ".join(filter(None, [it.get("channel"),
                                                                     (it.get("description") or "")[:2000], tags]))
    sub = " · ".join(x for x in [it.get("site") or it.get("host"), it.get("published"),
                                 f"{it['read_min']} min read" if it.get("read_min") else ""] if x)
    return it.get("title") or it["id"], sub, " ".join(filter(None, [
        it.get("site") or it.get("host"), it.get("author"), it.get("description"), tags,
        " ".join(it.get("outline") or [])]))


def _fts_put(con, ref: str, title: str, body: str) -> None:
    con.execute("DELETE FROM fts WHERE ref=?", (ref,))
    con.execute("INSERT INTO fts(ref, title, body) VALUES(?,?,?)", (ref, title or "", body or ""))


def _topic_fts(con, tid: int) -> None:
    t = con.execute("SELECT * FROM topics WHERE id=?", (tid,)).fetchone()
    if t:
        body = " ".join([t["query"], " ".join(_j(t["keywords"], [])), t["purpose"] or "",
                         " ".join(_j(t["tags"], [])), t["notes"] or ""])
        _fts_put(con, f"topic:{tid}", t["label"] or t["query"], body)


# ---------------------------------------------------------------------- topics & runs

# Queries the "Find videos / Find blog posts" buttons write; used to file old runs under their parent.
_FIND_GITHUB = re.compile(r"about the GitHub project (\S+?)(?=[\s(:]|$)")
_FIND_ARXIV = re.compile(r"\(arXiv:([^)\s]+)\)")
_FIND_VIDEO = re.compile(r'the YouTube video "(.+?)"(?= by |:)')


def _guess_parent_topic(con, query: str) -> int | None:
    for kind, rx in (("github", _FIND_GITHUB), ("arxiv", _FIND_ARXIV), ("youtube", _FIND_VIDEO)):
        m = rx.search(query)
        if not m:
            continue
        col, val = ("title", m.group(1)) if kind == "youtube" else ("key", item_key(kind, m.group(1)))
        row = con.execute(
            f"SELECT r.topic_id FROM items i JOIN run_items ri ON ri.kind=i.kind AND ri.key=i.key "
            f"JOIN runs r ON r.id=ri.run_id WHERE i.kind=? AND i.{col}=? AND r.topic_id IS NOT NULL "
            f"ORDER BY r.started_at LIMIT 1", (kind, val)).fetchone()
        if row:
            return row[0]
    return None


def _topic_for(con, query: str, origin: str, parent_run: str | None, started_at: str) -> int:
    if parent_run:
        row = con.execute("SELECT topic_id FROM runs WHERE id=?", (parent_run,)).fetchone()
        if row and row[0]:
            return row[0]
    if origin == "find" and (tid := _guess_parent_topic(con, query)):
        return tid
    n = norm_query(query)
    row = con.execute("SELECT id FROM topics WHERE norm=?", (n,)).fetchone()
    if row:
        return row[0]
    cur = con.execute("INSERT INTO topics(norm, query, created_at, last_at) VALUES(?,?,?,?)",
                      (n, query.strip(), started_at, started_at))
    _topic_fts(con, cur.lastrowid)
    return cur.lastrowid


def _upsert_run(con, m: dict) -> int:
    origin = m.get("origin") or ("find" if _guess_find(m.get("query", "")) else "search")
    tid = m.get("topic_id")
    if not tid or not con.execute("SELECT 1 FROM topics WHERE id=?", (tid,)).fetchone():
        tid = _topic_for(con, m["query"], origin, m.get("parent_run"), m.get("started_at") or _now())
    stats = m.get("stats") or {}
    con.execute(
        "INSERT INTO runs(id, topic_id, kind, query, origin, parent_run, status, started_at, model, since_key, since,"
        " count, n_results, cost_usd) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET"
        " status=excluded.status, n_results=excluded.n_results, cost_usd=excluded.cost_usd, topic_id=excluded.topic_id",
        (m["id"], tid, m["kind"], m["query"], origin, m.get("parent_run"), m.get("status"), m.get("started_at"),
         m.get("model"), m.get("since_key"), m.get("since"), m.get("count"), m.get("n_results"),
         stats.get("cost_usd")))
    if origin == "search":
        con.execute("UPDATE topics SET last_at=max(coalesce(last_at,''), ?) WHERE id=?", (m.get("started_at"), tid))
    return tid


def _guess_find(query: str) -> bool:
    return bool(_FIND_GITHUB.search(query) or _FIND_ARXIV.search(query) or _FIND_VIDEO.search(query)) and \
        query.startswith(("Videos ", "Blog posts "))


def on_run_start(meta: dict) -> int:
    """Register a run that is starting; returns its topic id. Also marks a Claude idea as used
    when the run searches exactly what the idea proposed."""
    with _db() as con:
        tid = _upsert_run(con, meta)
        con.execute("UPDATE ideas SET status='used', run_id=? WHERE status='new' AND kind=? AND lower(query)=?",
                    (meta["id"], meta["kind"], meta["query"].strip().lower()))
        return tid


def ingest_run(run_id: str) -> dict | None:
    """Index a run folder (idempotent). Returns {topic_id, n_new} or None."""
    d = RUNS_DIR / run_id
    try:
        m = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    results = None
    if (d / "results.json").exists():
        try:
            results = json.loads((d / "results.json").read_text(encoding="utf-8"))
        except ValueError:
            pass
    kind = m["kind"]
    with _db() as con:
        prev = con.execute("SELECT origin, parent_run, topic_id FROM runs WHERE id=?", (run_id,)).fetchone()
        if prev:  # keep what on_run_start recorded
            m.setdefault("origin", prev["origin"])
            m.setdefault("parent_run", prev["parent_run"])
            m.setdefault("topic_id", prev["topic_id"])
        tid = _upsert_run(con, m)
        n_new = 0
        for it in (results or {}).get("items", []):
            key = item_key(kind, it["id"])
            data = {k: v for k, v in it.items() if k not in RUN_ONLY_FIELDS}
            title, sub, text = _item_view(kind, data)
            seen = con.execute(
                "SELECT 1 FROM run_items ri JOIN runs r ON r.id=ri.run_id WHERE ri.kind=? AND ri.key=? "
                "AND ri.run_id<>? AND r.started_at<? LIMIT 1", (kind, key, run_id, m.get("started_at") or "~")).fetchone()
            is_new = 0 if seen else 1
            n_new += is_new
            con.execute(
                "INSERT INTO items(kind, key, id, title, url, sub, data, first_run, first_at, last_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(kind, key) DO UPDATE SET title=excluded.title, "
                "url=excluded.url, sub=excluded.sub, data=excluded.data, last_at=max(last_at, excluded.last_at), "
                "first_at=min(first_at, excluded.first_at), "
                "first_run=CASE WHEN excluded.first_at < first_at THEN excluded.first_run ELSE first_run END",
                (kind, key, it["id"], title, it.get("url"), sub, json.dumps(data, ensure_ascii=False), run_id,
                 m.get("started_at"), m.get("started_at")))
            con.execute("INSERT OR REPLACE INTO run_items VALUES(?,?,?,?,?,?,?)",
                        (run_id, kind, key, it.get("rank"), it.get("score"), it.get("reason"), is_new))
            reasons = " ".join(r[0] or "" for r in con.execute(
                "SELECT reason FROM run_items WHERE kind=? AND key=?", (kind, key)))
            _fts_put(con, f"item:{kind}:{key}", title, f"{text} {reasons}")
        con.execute("UPDATE runs SET n_new=? WHERE id=?", (n_new if results else None, run_id))
        if m.get("origin") == "watch" or (prev and prev["origin"] == "watch"):
            con.execute("UPDATE watches SET last_run=?, last_new=?, unseen=?, note=? WHERE topic_id=? AND kind=?",
                        (run_id, n_new, 1 if n_new else 0,
                         None if m.get("status") == "done" else f"last check {m.get('status')}", tid, kind))
    return {"topic_id": tid, "n_new": n_new}


def sync(active: set[str] = frozenset()) -> int:
    """Index run folders the database is missing or has out of date. Returns how many."""
    if not RUNS_DIR.exists():
        return 0
    with _db() as con:
        known = {r["id"]: r["status"] for r in con.execute("SELECT id, status FROM runs")}
    n = 0
    for d in sorted(RUNS_DIR.iterdir()):
        if not RUN_ID_RE.match(d.name) or d.name in active:
            continue
        try:
            status = json.loads((d / "meta.json").read_text(encoding="utf-8")).get("status")
        except (OSError, ValueError):
            continue
        if known.get(d.name) == status and status != "running":
            continue
        if status == "running":  # the server stopped mid-run
            _mark_interrupted(d)
        if ingest_run(d.name):
            n += 1
    return n


def _mark_interrupted(d) -> None:
    p = d / "meta.json"
    m = json.loads(p.read_text(encoding="utf-8"))
    m["status"] = "interrupted"
    p.write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------------------------------------------------------------------- keywords (Claude)

KW_SYSTEM = "You index a researcher's search requests for their personal library. Reply with JSON only: no prose, no code fence."
KW_PROMPT = """For each search request below, return
{"topics":[{"i":<number>,"label":"2-5 word title in Title Case","keywords":["3-6 short lowercase English noun phrases: the canonical terms an expert would use for its main concepts"],"purpose":"why the person wants it, in a few words, ONLY if the request says so (e.g. 'for an AI lecture'), else null"}]}

Requests:
"""


def extract_pending_keywords() -> None:
    """Give every topic without keywords a label, keywords and purpose (Haiku, batched)."""
    if not _kw_busy.acquire(blocking=False):
        return
    try:
        for _ in range(5):
            with _db() as con:
                rows = con.execute("SELECT id, query FROM topics WHERE kw_status IS NULL ORDER BY id LIMIT 30").fetchall()
            if not rows:
                return
            listing = "\n".join(f"{i}. {_clip(r['query'], 400)}" for i, r in enumerate(rows, 1))
            try:
                out, cost = llm.claude_json(KW_PROMPT + listing, KW_SYSTEM)
            except Exception as e:
                _log(f"keyword extraction failed: {e}")
                with _db() as con:
                    con.executemany("UPDATE topics SET kw_status='error' WHERE id=?", [(r["id"],) for r in rows])
                return
            by_i = {t.get("i"): t for t in out.get("topics", []) if isinstance(t, dict)}
            with _db() as con:
                con.execute("INSERT INTO llm_calls VALUES(?,?,?,?)", (_now(), "keywords", "haiku", cost))
                for i, r in enumerate(rows, 1):
                    t = by_i.get(i) or {}
                    kws = [str(k).strip().lower() for k in t.get("keywords") or [] if str(k).strip()][:6]
                    purpose = t.get("purpose")
                    con.execute("UPDATE topics SET label=?, keywords=?, purpose=?, kw_status=? WHERE id=?",
                                (_clip(str(t.get("label") or ""), 60) or None, json.dumps(kws, ensure_ascii=False),
                                 _clip(str(purpose), 80) if purpose and str(purpose).lower() != "null" else None,
                                 "done" if t else "error", r["id"]))
                    _topic_fts(con, r["id"])
    finally:
        _kw_busy.release()


# ---------------------------------------------------------------------- user edits

def update_topic(tid: int, tags: list[str] | None = None, notes: str | None = None,
                 hidden: bool | None = None, label: str | None = None) -> bool:
    with _db() as con:
        if not con.execute("SELECT 1 FROM topics WHERE id=?", (tid,)).fetchone():
            return False
        if tags is not None:
            clean = list(dict.fromkeys(_clip(t, 40).lower() for t in tags if t.strip()))[:20]
            con.execute("UPDATE topics SET tags=? WHERE id=?", (json.dumps(clean, ensure_ascii=False), tid))
        if notes is not None:
            con.execute("UPDATE topics SET notes=? WHERE id=?", (notes[:5000], tid))
        if hidden is not None:
            con.execute("UPDATE topics SET hidden=? WHERE id=?", (int(hidden), tid))
        if label is not None:
            con.execute("UPDATE topics SET label=? WHERE id=?", (_clip(label, 60) or None, tid))
        _topic_fts(con, tid)
    return True


def set_mark(kind: str, item_id: str, star: bool | None = None, vote: int | None = None) -> dict:
    key = item_key(kind, item_id)
    with _db() as con:
        con.execute("INSERT OR IGNORE INTO marks(kind, key, updated_at) VALUES(?,?,?)", (kind, key, _now()))
        if star is not None:
            con.execute("UPDATE marks SET star=?, updated_at=? WHERE kind=? AND key=?", (int(star), _now(), kind, key))
        if vote is not None:
            con.execute("UPDATE marks SET vote=?, updated_at=? WHERE kind=? AND key=?",
                        (max(-1, min(1, vote)), _now(), kind, key))
        r = con.execute("SELECT star, vote FROM marks WHERE kind=? AND key=?", (kind, key)).fetchone()
        return {"star": bool(r["star"]), "vote": r["vote"]}


def annotate(kind: str, ids: list[str], run_id: str | None) -> dict:
    """Per result id: your mark, and whether an earlier run already showed it to you."""
    out = {}
    with _db() as con:
        r = con.execute("SELECT started_at FROM runs WHERE id=?", (run_id,)).fetchone() if run_id else None
        before = r["started_at"] if r else "~"
        for i in ids[:100]:
            key = item_key(kind, i)
            mk = con.execute("SELECT star, vote FROM marks WHERE kind=? AND key=?", (kind, key)).fetchone()
            prev = con.execute(
                "SELECT r.id, r.started_at, coalesce(t.label, t.query) AS topic FROM run_items ri "
                "JOIN runs r ON r.id=ri.run_id LEFT JOIN topics t ON t.id=r.topic_id "
                "WHERE ri.kind=? AND ri.key=? AND ri.run_id<>? AND r.started_at<? ORDER BY r.started_at",
                (kind, key, run_id or "", before)).fetchall()
            out[i] = {"star": bool(mk["star"]) if mk else False, "vote": mk["vote"] if mk else 0,
                      "seen": {"n": len(prev), "first_at": prev[0]["started_at"], "run": prev[0]["id"],
                               "topic": prev[0]["topic"]} if prev else None}
        if run_id:
            con.execute("UPDATE watches SET unseen=0 WHERE last_run=?", (run_id,))
    return out


# ---------------------------------------------------------------------- watches

def set_watch(tid: int, kind: str, enabled: bool, every_days: int | None = None) -> dict | None:
    with _db() as con:
        if not con.execute("SELECT 1 FROM topics WHERE id=?", (tid,)).fetchone():
            return None
        w = con.execute("SELECT * FROM watches WHERE topic_id=? AND kind=?", (tid, kind)).fetchone()
        every = every_days or (w["every_days"] if w else None) or WATCH_EVERY_DAYS
        if not w:
            last = con.execute("SELECT model, started_at FROM runs WHERE topic_id=? AND kind=? "
                               "ORDER BY started_at DESC LIMIT 1", (tid, kind)).fetchone()
            base = datetime.fromisoformat(last["started_at"]) if last else datetime.now().astimezone()
            due = max(base + timedelta(days=every), datetime.now().astimezone() + timedelta(minutes=1))
            con.execute("INSERT INTO watches(topic_id, kind, enabled, every_days, model, count, next_due) "
                        "VALUES(?,?,?,?,?,?,?)", (tid, kind, int(enabled), every,
                                                  (last["model"] if last else None) or DEFAULT_MODEL, WATCH_COUNT,
                                                  due.isoformat(timespec="seconds")))
        else:
            con.execute("UPDATE watches SET enabled=?, every_days=? WHERE topic_id=? AND kind=?",
                        (int(enabled), every, tid, kind))
            if every_days and w["every_days"] != every_days and w["next_due"]:  # re-plan from the last check
                last = con.execute("SELECT started_at FROM runs WHERE topic_id=? AND kind=? ORDER BY started_at "
                                   "DESC LIMIT 1", (tid, kind)).fetchone()
                if last:
                    due = max(datetime.fromisoformat(last["started_at"]) + timedelta(days=every),
                              datetime.now().astimezone() + timedelta(minutes=1))
                    con.execute("UPDATE watches SET next_due=? WHERE topic_id=? AND kind=?",
                                (due.isoformat(timespec="seconds"), tid, kind))
        return dict(con.execute("SELECT * FROM watches WHERE topic_id=? AND kind=?", (tid, kind)).fetchone())


def check_now(tid: int) -> int:
    with _db() as con:
        return con.execute("UPDATE watches SET next_due=? WHERE topic_id=? AND enabled=1",
                           (_now(), tid)).rowcount


def next_due_watch() -> dict | None:
    """The most overdue enabled watch, with what its run needs: query, recency window and the
    items this topic already showed in that tab (the run must not submit them again)."""
    with _db() as con:
        w = con.execute("SELECT w.*, t.query FROM watches w JOIN topics t ON t.id=w.topic_id "
                        "WHERE w.enabled=1 AND t.hidden=0 AND w.next_due<=? ORDER BY w.next_due LIMIT 1",
                        (_now(),)).fetchone()
        if not w:
            return None
        w = dict(w)
        last = con.execute("SELECT max(started_at) FROM runs WHERE topic_id=? AND kind=?",
                           (w["topic_id"], w["kind"])).fetchone()[0]
        since = date.today() - timedelta(days=WATCH_MIN_WINDOW_DAYS)
        if last:
            since = min(since, datetime.fromisoformat(last).date() - timedelta(days=3))
        w["since"] = since.isoformat()
        w["seen"] = [{"id": r["id"], "title": r["title"]} for r in con.execute(
            "SELECT DISTINCT i.id, i.title FROM run_items ri JOIN runs r ON r.id=ri.run_id "
            "JOIN items i ON i.kind=ri.kind AND i.key=ri.key WHERE r.topic_id=? AND ri.kind=? "
            "ORDER BY r.started_at DESC LIMIT 80", (w["topic_id"], w["kind"]))]
        return w


def watch_started(tid: int, kind: str, note: str | None = None) -> None:
    """Plan the next check; note says why this one was skipped, if it was."""
    with _db() as con:
        w = con.execute("SELECT every_days FROM watches WHERE topic_id=? AND kind=?", (tid, kind)).fetchone()
        due = datetime.now().astimezone() + timedelta(days=(w["every_days"] if w else WATCH_EVERY_DAYS))
        con.execute("UPDATE watches SET next_due=?, note=? WHERE topic_id=? AND kind=?",
                    (due.isoformat(timespec="seconds"), note, tid, kind))


# ---------------------------------------------------------------------- search & overview

def _fts_match(q: str, op: str) -> str:
    words = [w for w in re.findall(r"[^\W_][\w]*", q.lower()) if len(w) > 1 or w.isdigit()]
    return f" {op} ".join(f'"{w}"*' for w in words[:12])


def _item_rows(con, refs: list[tuple[str, str]]) -> list[dict]:
    out = []
    for kind, key in refs:
        i = con.execute("SELECT kind, key, id, title, url, sub, first_at FROM items WHERE kind=? AND key=?",
                        (kind, key)).fetchone()
        if not i:
            continue
        last = con.execute(
            "SELECT r.id, r.started_at, coalesce(t.label, t.query) AS topic, ri.reason FROM run_items ri "
            "JOIN runs r ON r.id=ri.run_id LEFT JOIN topics t ON t.id=r.topic_id "
            "WHERE ri.kind=? AND ri.key=? ORDER BY r.started_at DESC", (kind, key)).fetchall()
        mk = con.execute("SELECT star, vote FROM marks WHERE kind=? AND key=?", (kind, key)).fetchone()
        out.append({**dict(i), "star": bool(mk["star"]) if mk else False, "vote": mk["vote"] if mk else 0,
                    "runs": len(last), "run": last[0]["id"] if last else None,
                    "topic": last[0]["topic"] if last else None, "reason": last[0]["reason"] if last else None})
    return out


def search(q: str, kind: str | None = None, limit: int = 40) -> dict:
    """Full-text search over topics (query, keywords, tags, notes) and every result you got."""
    with _db() as con:
        rows, mode = [], None
        for op, label in (("AND", "all words"), ("OR", "any word")):  # any word only if all words finds nothing
            match = _fts_match(q, op)
            if not match:
                break
            try:
                rows = con.execute("SELECT ref FROM fts WHERE fts MATCH ? ORDER BY bm25(fts, 4.0, 1.0) LIMIT 200",
                                   (match,)).fetchall()
            except sqlite3.OperationalError:
                rows = []
            if rows:
                mode = label
                break
        tids, refs = [], []
        for (ref,) in rows:
            if ref.startswith("topic:"):
                tids.append(int(ref[6:]))
            else:
                _, k, key = ref.split(":", 2)
                if not kind or k == kind:
                    refs.append((k, key))
        topics = [t for t in _topics(con, include_hidden=True) if t["id"] in set(tids)]
        topics.sort(key=lambda t: tids.index(t["id"]))
        return {"topics": topics if not kind else [], "items": _item_rows(con, refs[:limit]), "mode": mode}


def similar_topics(q: str, limit: int = 3) -> list[dict]:
    """Past topics that cover most of the words of what is being typed."""
    qt = tokens(q)
    if len(qt) < 1:
        return []
    n = norm_query(q)
    with _db() as con:
        scored = []
        for t in _topics(con):
            tt = tokens(t["query"]) | {w for k in t["keywords"] for w in tokens(k)}
            s = 1.0 if t["norm"] == n else len(qt & tt) / len(qt)
            if s >= 0.6 and (len(qt) >= 2 or s == 1.0):
                scored.append((s, t["last_at"] or "", t))
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        return [{k: t[k] for k in ("id", "label", "query", "kinds", "last_at")} | {"exact": t["norm"] == n}
                for _, _, t in scored[:limit]]


def _topics(con, include_hidden: bool = False) -> list[dict]:
    runs = defaultdict(lambda: defaultdict(list))
    for r in con.execute("SELECT id, topic_id, kind, origin, status, started_at, n_results, n_new, query "
                         "FROM runs ORDER BY started_at"):
        runs[r["topic_id"]][r["kind"]].append(dict(r))
    watches = defaultdict(dict)
    for w in con.execute("SELECT * FROM watches"):
        watches[w["topic_id"]][w["kind"]] = dict(w)
    out = []
    for t in con.execute("SELECT * FROM topics ORDER BY coalesce(last_at, created_at) DESC"):
        if t["hidden"] and not include_hidden:
            continue
        kinds = {}
        for k, rs in runs[t["id"]].items():
            last = rs[-1]
            kinds[k] = {"runs": len(rs), "searched": sum(r["origin"] == "search" for r in rs),
                        "last_run": last["id"], "last_at": last["started_at"], "n_results": last["n_results"],
                        "status": last["status"]}
        out.append({"id": t["id"], "norm": t["norm"], "query": t["query"], "label": t["label"],
                    "keywords": _j(t["keywords"], []), "purpose": t["purpose"], "kw_status": t["kw_status"],
                    "tags": _j(t["tags"], []), "notes": t["notes"], "hidden": bool(t["hidden"]),
                    "created_at": t["created_at"], "last_at": t["last_at"], "kinds": kinds,
                    "watches": watches.get(t["id"], {}),
                    "finds": [r for rs in runs[t["id"]].values() for r in rs if r["origin"] == "find"]})
    return out


def _related(topics: list[dict]) -> None:
    """topic["related"] = ids of up to 3 topics sharing the most keywords/words."""
    toks = {t["id"]: {w for k in t["keywords"] for w in tokens(k)} | tokens(t["query"]) for t in topics}
    for t in topics:
        a = toks[t["id"]]
        sims = []
        for u in topics:
            if u["id"] != t["id"] and a:
                b = toks[u["id"]]
                j = len(a & b) / len(a | b)
                if j >= 0.15:
                    sims.append((j, u["id"]))
        t["related"] = [i for _, i in sorted(sims, reverse=True)[:3]]


GENERIC_TAGS = set("""ai llm llms python machine-learning deep-learning artificial-intelligence pytorch nlp
open-source opensource chatgpt gpt openai javascript typescript rust go golang docker generative-ai genai
ml data-science tutorial agent agents ai-agents llm-agent""".split())


def _norm_tag(t: str) -> str:
    return t.strip().lower().replace("_", "-")


def _suggestions(con, topics: list[dict]) -> list[dict]:
    """Free suggestions from your own data, strongest first."""
    out = []
    seen_items = {(r["kind"], r["key"]) for r in con.execute("SELECT kind, key FROM items")}
    marked = {(r["kind"], r["key"]) for r in con.execute("SELECT kind, key FROM marks WHERE star=1 OR vote=1")}
    topic_of = {}
    for r in con.execute("SELECT ri.kind, ri.key, coalesce(t.label, t.query) AS topic FROM run_items ri "
                         "JOIN runs r ON r.id=ri.run_id JOIN topics t ON t.id=r.topic_id WHERE t.hidden=0"):
        topic_of.setdefault((r["kind"], r["key"]), r["topic"])

    # 1. papers and repos your results link to that you have not looked at yet
    papers, repos = defaultdict(lambda: {"by": [], "title": None}), defaultdict(lambda: {"by": []})
    for r in con.execute("SELECT kind, key, title, data FROM items"):
        if (r["kind"], r["key"]) not in topic_of:
            continue
        d = _j(r["data"], {})
        src = {"kind": r["kind"], "title": r["title"], "liked": (r["kind"], r["key"]) in marked,
               "topic": topic_of[(r["kind"], r["key"])]}
        for p in d.get("papers") or [{"id": x} for x in d.get("paper_links") or []]:
            if p.get("id") and ("arxiv", p["id"]) not in seen_items:
                papers[p["id"]]["by"].append(src)
                papers[p["id"]]["title"] = papers[p["id"]]["title"] or p.get("title")
        links = ([d["code_url"]] if r["kind"] == "arxiv" and d.get("code_url") else []) + \
            (d.get("code_links") or [] if r["kind"] != "arxiv" else [])
        for u in links:
            m = re.match(r"https?://github\.com/([\w.-]+/[\w.-]+)", u or "")
            if m and ("github", m.group(1).lower().removesuffix(".git")) not in seen_items and \
                    not (r["kind"] == "github" and m.group(1).lower() == r["key"]):
                repos[m.group(1).removesuffix(".git")]["by"].append(src)

    def strong(v):
        n = len({(s["kind"], s["title"]) for s in v["by"]})
        return n >= 2 or any(s["liked"] for s in v["by"]) or any(s["kind"] == "arxiv" for s in v["by"])

    for pid, v in sorted(papers.items(), key=lambda kv: -len(kv[1]["by"])):
        if strong(v) and v["title"]:
            out.append({"type": "paper", "id": pid, "title": v["title"], "n": len(v["by"]),
                        "by": v["by"][:3], "url": f"https://arxiv.org/abs/{pid}"})
    for rid, v in sorted(repos.items(), key=lambda kv: -len(kv[1]["by"])):
        if strong(v):
            out.append({"type": "repo", "id": rid, "title": rid, "n": len(v["by"]), "by": v["by"][:3],
                        "url": f"https://github.com/{rid}"})
    linked = sorted((s for s in out), key=lambda s: (-s["n"], s["type"]))[:8]

    # 2. topics you searched in some tabs but not the others (a closely related topic counts)
    tabs, by_id = [], {t["id"]: t for t in topics}
    for t in topics:
        searched = [k for k, v in t["kinds"].items() if v["searched"]]
        covered = set(t["kinds"]).union(*(by_id[i]["kinds"] for i in t.get("related", []) if i in by_id))
        missing = [k for k in KINDS if k not in covered]
        if searched and missing:
            tabs.append({"type": "tabs", "topic_id": t["id"], "title": t["label"] or t["query"], "query": t["query"],
                         "missing": missing})
    tabs = tabs[:5]

    # 3. tags that keep showing up on your results but that you never searched for
    asked, compact = set(), []
    for t in topics:
        asked |= tokens(t["query"]) | {w for k in t["keywords"] for w in tokens(k)}
        compact += [re.sub(r"\W|_", "", x.lower()) for x in [t["query"], *t["keywords"]]]
    counts, where = Counter(), defaultdict(set)
    for r in con.execute("SELECT kind, key, data FROM items WHERE kind IN ('github', 'blog')"):
        if (r["kind"], r["key"]) not in topic_of:
            continue
        d = _j(r["data"], {})
        for tag in {_norm_tag(x) for x in (d.get("topics") or d.get("tags") or [])}:
            flat = re.sub(r"\W|_", "", tag)
            if tag and tag not in GENERIC_TAGS and not tokens(tag.replace("-", " ")) <= asked \
                    and not any(flat in c for c in compact):
                counts[tag] += 1
                where[tag].add(topic_of[(r["kind"], r["key"])])
    kw = [{"type": "keyword", "title": tag.replace("-", " "), "n": n, "topics": sorted(where[tag])[:3]}
          for tag, n in counts.most_common(30) if n >= 3][:10]
    return linked + tabs + kw


def _insights(con, topics: list[dict]) -> dict:
    kw = Counter(k for t in topics for k in t["keywords"])
    tags = Counter()
    for r in con.execute("SELECT data FROM items WHERE kind IN ('github', 'blog')"):
        d = _j(r["data"], {})
        tags.update({_norm_tag(x) for x in (d.get("topics") or d.get("tags") or [])} - GENERIC_TAGS - {""})
    start = date.today() - timedelta(days=55)
    start -= timedelta(days=start.weekday())  # Monday
    weeks = defaultdict(lambda: Counter())
    for r in con.execute("SELECT kind, started_at, origin FROM runs WHERE started_at>=?", (start.isoformat(),)):
        d = datetime.fromisoformat(r["started_at"]).date()
        weeks[(d - timedelta(days=d.weekday())).isoformat()][r["kind"]] += 1
    activity = []
    w = start
    while w <= date.today():
        activity.append({"week": w.isoformat(), **{k: weeks[w.isoformat()].get(k, 0) for k in KINDS}})
        w += timedelta(days=7)
    return {"keywords": kw.most_common(15), "result_tags": tags.most_common(15), "activity": activity}


def overview() -> dict:
    with _db() as con:
        topics = _topics(con, include_hidden=True)
        visible = [t for t in topics if not t["hidden"]]
        _related(visible)
        n_runs = Counter({r["kind"]: r["n"] for r in con.execute("SELECT kind, count(*) AS n FROM runs GROUP BY kind")})
        cost_runs = con.execute("SELECT coalesce(sum(cost_usd),0) FROM runs").fetchone()[0]
        cost_llm = con.execute("SELECT coalesce(sum(cost_usd),0) FROM llm_calls").fetchone()[0]
        month = date.today().replace(day=1).isoformat()
        cost_month = (con.execute("SELECT coalesce(sum(cost_usd),0) FROM runs WHERE started_at>=?", (month,)).fetchone()[0]
                      + con.execute("SELECT coalesce(sum(cost_usd),0) FROM llm_calls WHERE ts>=?", (month,)).fetchone()[0])
        watches = con.execute("SELECT count(*), coalesce(sum(unseen),0) FROM watches w JOIN topics t ON t.id=w.topic_id "
                              "WHERE w.enabled=1 AND t.hidden=0").fetchone()
        saved = [(r["kind"], r["key"]) for r in con.execute(
            "SELECT kind, key FROM marks WHERE star=1 ORDER BY updated_at DESC LIMIT 100")]
        ideas = [dict(r) for r in con.execute("SELECT * FROM ideas WHERE status='new' ORDER BY id DESC LIMIT 12")]
        return {
            "stats": {"topics": len(visible), "runs": sum(n_runs.values()), "runs_by_kind": dict(n_runs),
                      "items": con.execute("SELECT count(*) FROM items").fetchone()[0], "saved": len(saved),
                      "watching": watches[0], "unseen": watches[1], "cost_usd": round(cost_runs + cost_llm, 2),
                      "cost_month_usd": round(cost_month, 2), "hidden": len(topics) - len(visible),
                      "kw_pending": sum(t["kw_status"] is None for t in visible)},
            "topics": topics, "saved": _item_rows(con, saved), "suggestions": _suggestions(con, visible),
            "insights": _insights(con, visible), "ideas": ideas,
        }


def watch_status() -> dict:
    with _db() as con:
        r = con.execute("SELECT count(*), coalesce(sum(w.unseen),0), min(w.next_due) FROM watches w "
                        "JOIN topics t ON t.id=w.topic_id WHERE w.enabled=1 AND t.hidden=0").fetchone()
        return {"watching": r[0], "unseen": r[1], "next_due": r[2]}


# ---------------------------------------------------------------------- ideas (Claude)

IDEAS_SYSTEM = ("You suggest what a researcher should search next in Scout, a personal tool that finds GitHub "
                "repositories, arXiv papers, YouTube videos and English blog posts. Reply with JSON only: no prose, "
                "no code fence.")
IDEAS_PROMPT = """Here is what this person has searched and how they rated results (JSON):
{profile}

Propose 6 new searches that would move their work forward: adjacent techniques, prerequisites,
alternatives worth comparing, evaluation and benchmarks, or practical how-tos for the purposes they
stated. Favour topics they return to and items they starred or liked; steer away from what they
disliked. Do not repeat or lightly reword their past searches or the earlier ideas listed. Name a
specific paper, repository or product only if you are sure it exists.
Reply as {{"ideas":[{{"query":"a one-sentence request written like their own searches","kind":"github|arxiv|youtube|blog","why":"one sentence naming the topic or item it builds on","builds_on":"label of that topic"}}]}}"""


def make_ideas() -> tuple[list[dict], float | None]:
    with _db() as con:
        topics = [t for t in _topics(con)][:40]
        marks = con.execute("SELECT m.kind, m.star, m.vote, i.title FROM marks m JOIN items i "
                            "ON i.kind=m.kind AND i.key=m.key WHERE m.star=1 OR m.vote<>0 "
                            "ORDER BY m.updated_at DESC LIMIT 40").fetchall()
        earlier = [r["query"] for r in con.execute("SELECT query FROM ideas ORDER BY id DESC LIMIT 30")]
    if not topics:
        raise llm.LLMError("Search for something first: ideas build on your past searches.")
    profile = {
        "topics": [{"label": t["label"] or _clip(t["query"], 60), "request": _clip(t["query"], 300),
                    "keywords": t["keywords"], "purpose": t["purpose"], "tags": t["tags"],
                    "notes": _clip(t["notes"], 300) or None, "tabs": sorted(t["kinds"]),
                    "last": (t["last_at"] or "")[:10]} for t in topics],
        "starred": [f"[{m['kind']}] {m['title']}" for m in marks if m["star"]],
        "liked": [f"[{m['kind']}] {m['title']}" for m in marks if m["vote"] > 0],
        "disliked": [f"[{m['kind']}] {m['title']}" for m in marks if m["vote"] < 0],
        "earlier_ideas": earlier,
    }
    out, cost = llm.claude_json(IDEAS_PROMPT.format(profile=json.dumps(profile, ensure_ascii=False, indent=1)),
                                IDEAS_SYSTEM)
    rows = []
    with _db() as con:
        con.execute("INSERT INTO llm_calls VALUES(?,?,?,?)", (_now(), "ideas", "haiku", cost))
        for i in out.get("ideas", [])[:8]:
            if not isinstance(i, dict) or not str(i.get("query") or "").strip():
                continue
            kind = i.get("kind") if i.get("kind") in KINDS else "arxiv"
            cur = con.execute("INSERT INTO ideas(created_at, kind, query, why, builds_on) VALUES(?,?,?,?,?)",
                              (_now(), kind, _clip(str(i["query"]), 400), _clip(str(i.get("why") or ""), 300),
                               _clip(str(i.get("builds_on") or ""), 80) or None))
            rows.append(dict(con.execute("SELECT * FROM ideas WHERE id=?", (cur.lastrowid,)).fetchone()))
    return rows, cost


def dismiss_idea(idea_id: int) -> bool:
    with _db() as con:
        return con.execute("UPDATE ideas SET status='dismissed' WHERE id=?", (idea_id,)).rowcount > 0
