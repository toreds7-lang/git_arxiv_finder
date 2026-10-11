"""Runs the Claude Code CLI as a search agent and turns its stream-json into a timeline.

One Run = one `claude -p` process with only our MCP tools enabled. Its stdout is read
on a thread (works with any Windows event loop) and every line is handled on the
asyncio loop, where it becomes zero or more timeline events. Events are appended to
runs/<id>/events.jsonl and pushed to SSE subscribers.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from . import library
from . import sources as S
from .config import RUN_TIMEOUT_S, RUNS_DIR, SINCE_OPTIONS, WORK_ROOT, claude_bin, youtube_api_key
from .prompts import system_prompt, user_prompt

MCP_SCRIPT = Path(__file__).resolve().parent / "mcp_server.py"
RUN_ID_RE = re.compile(r"^\d{8}-\d{6}-(github|arxiv|youtube|blog)-[0-9a-f]{4}$")
# Built-in Claude Code tools a run may use besides the finder MCP tools. Blogs have no
# search API, so Blog Scout discovers posts with WebSearch; read_articles must still open
# every post before submit_results accepts it.
BUILTIN_TOOLS = {"blog": ["WebSearch"]}
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

ACTIVE: dict[str, Run] = {}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _first_line(s: str, n: int) -> str:
    line = s.strip().split("\n", 1)[0].strip()
    return line if len(line) <= n else line[: n - 1] + "…"


def _tool_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
    return ""


def _web_search_view(txt: str) -> tuple[str, str]:
    """WebSearch returns 'Web search results for query: "q"\\n\\nLinks: [json]\\n\\n<notes>'.
    Show the hits as readable lines: (title, detail)."""
    m = re.search(r"^Links: (\[.*?\])\s*$", txt, re.M)
    try:
        links = json.loads(m.group(1)) if m else []
    except ValueError:
        links = []
    if not links:
        first, _, rest = txt.partition("\n")
        return first, rest
    hosts = list(dict.fromkeys(S.url_host(l.get("url", "")) for l in links))
    title = f"{len(links)} web results: " + ", ".join(hosts[:4]) + (" …" if len(hosts) > 4 else "")
    lines = [f"{i}. {l.get('title', '')}\n   {l.get('url', '')}" for i, l in enumerate(links, 1)]
    notes = txt[m.end():].strip()
    return title, "\n".join(lines) + (f"\n\n{notes}" if notes else "")


def _fmt_call(name: str, inp: dict) -> str:
    """search_github("agent memory", sort=stars) style one-liner."""
    args = []
    for k, v in inp.items():
        if k == "urls" and isinstance(v, list):
            args.insert(0, f"{len(v)} pages: " + ", ".join(S.url_host(u) for u in v))
        elif k in ("query", "full_name", "ids", "video_id") or len(inp) == 1:
            args.insert(0, json.dumps(v, ensure_ascii=False))
        elif v not in (None, "", []):
            args.append(f"{k}={','.join(map(str, v)) if isinstance(v, list) else v}")
    return f"{name}({', '.join(args)})"


class Run:
    def __init__(self, kind: str, query: str, count: int, since_key: str, model: str, *, origin: str = "search",
                 parent_run: str | None = None, topic_id: int | None = None, since: str | None = None,
                 seen: list[dict] | None = None):
        """origin: "search" (typed), "find" (a Find videos/blog posts button on a card of run
        parent_run) or "watch" (a scheduled check of topic_id; since and seen come from the watch)."""
        self.id = f"{datetime.now():%Y%m%d-%H%M%S}-{kind}-{secrets.token_hex(2)}"
        self.kind, self.query, self.count, self.since_key, self.model = kind, query.strip(), count, since_key, model
        self.origin, self.parent_run, self.topic_id, self.seen = origin, parent_run, topic_id, seen or []
        days = SINCE_OPTIONS.get(since_key)
        self.since = since or ((date.today() - timedelta(days=days)).isoformat() if days else None)
        self.dir = RUNS_DIR / self.id
        self.workdir = WORK_ROOT / self.id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.workdir.mkdir(parents=True, exist_ok=True)

        self.loop = asyncio.get_running_loop()
        self.t0 = time.monotonic()
        self.started_at = _now()
        self.status = "running"
        self.events: list[dict] = []
        self.subscribers: set[asyncio.Queue] = set()
        self.proc: subprocess.Popen | None = None
        self.cancel_reason: str | None = None
        self.stderr_tail: list[str] = []
        self.n_results = 0
        # stream-json bookkeeping
        self._msg_ids: set[str] = set()
        self._calls: dict[str, tuple[str, float]] = {}
        self._thinking_est = 0
        self._submitted = False
        self._result_text = ""
        self.stats: dict = {"model": None, "turns": 0, "tool_calls": 0, "context_tokens": 0,
                            "rate_limits": {}, "cost_usd": None, "duration_s": None,
                            "input_tokens": None, "output_tokens": None}
        self._write_meta()

    # ------------------------------------------------------------------ events
    def emit(self, type_: str, title: str = "", detail: str | None = None, **extra) -> dict:
        ev = {"seq": len(self.events), "t": round(time.monotonic() - self.t0, 2), "ts": _now(),
              "type": type_, "title": title}
        if detail:
            ev["detail"] = detail
        ev.update(extra)
        self.events.append(ev)
        with open(self.dir / "events.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        for q in list(self.subscribers):
            q.put_nowait(ev)
        return ev

    def _emit_stats(self) -> None:
        self.emit("stats", stats=dict(self.stats))

    def subscribe(self) -> tuple[list[dict], asyncio.Queue]:
        q: asyncio.Queue = asyncio.Queue()
        self.subscribers.add(q)
        return list(self.events), q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)

    def _meta(self) -> dict:
        return {"id": self.id, "kind": self.kind, "query": self.query, "count": self.count,
                "since_key": self.since_key, "since": self.since, "model": self.model,
                "status": self.status, "started_at": self.started_at, "n_results": self.n_results,
                "elapsed_s": round(time.monotonic() - self.t0, 1), "stats": self.stats,
                "origin": self.origin, "parent_run": self.parent_run, "topic_id": self.topic_id}

    def _write_meta(self) -> None:
        (self.dir / "meta.json").write_text(json.dumps(self._meta(), ensure_ascii=False, indent=1), encoding="utf-8")

    # ------------------------------------------------------------------ launch
    async def start(self) -> None:
        since_txt = f"since {self.since}" if self.since else "any date"
        what = "Watch check started" if self.origin == "watch" else "Run started"
        self.emit("start", f"{what} · up to {self.count} {self.kind} results · {since_txt} · model {self.model}",
                  detail=self.query)
        try:
            self.topic_id = library.on_run_start(self._meta())
            self._write_meta()
            asyncio.create_task(asyncio.to_thread(library.extract_pending_keywords))  # a new topic gets keywords
        except Exception as ex:  # the Library is a convenience; never block a search on it
            self.emit("warn", f"Library: could not record this run ({type(ex).__name__}: {ex})")
        seen_args = []
        if self.seen:
            (self.dir / "seen.json").write_text(json.dumps([s["id"] for s in self.seen]), encoding="utf-8")
            seen_args = ["--seen-file", str(self.dir / "seen.json")]
            self.emit("info", f"Watch: skipping {len(self.seen)} results you have already seen for this topic",
                      detail="\n".join(f"{s['id']}  {s['title'] or ''}" for s in self.seen))
        sys_file = self.workdir / "system_prompt.txt"
        sys_file.write_text(system_prompt(self.kind), encoding="utf-8")
        mcp_file = self.workdir / "mcp.json"
        mcp_file.write_text(json.dumps({"mcpServers": {"finder": {
            "command": sys.executable,
            "args": [str(MCP_SCRIPT), "--run-dir", str(self.dir), "--kind", self.kind,
                     "--since", self.since or "", "--count", str(self.count), *seen_args],
            "env": {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
        }}}), encoding="utf-8")
        builtin = BUILTIN_TOOLS.get(self.kind, [])
        cmd = [
            claude_bin(), "-p",
            "--output-format", "stream-json", "--verbose",
            "--model", self.model,
            "--tools", ",".join(builtin),       # no other built-in tools (no Bash, no file edits, no WebFetch)
            "--mcp-config", str(mcp_file), "--strict-mcp-config",
            "--allowedTools", ",".join(["mcp__finder", *builtin]),  # ...only our search tools
            "--permission-mode", "dontAsk",
            "--setting-sources", "project",     # skip user hooks/plugins; cwd has no project settings
            "--disable-slash-commands",
            "--no-session-persistence",
            "--system-prompt-file", str(sys_file),
        ]
        shown = " ".join(f'"{c}"' if (" " in c or not c) else c for c in cmd[1:])
        enabled = "only the finder MCP tools" + (f" + built-in {', '.join(builtin)}" if builtin else "")
        self.emit("info", f"Launching Claude Code CLI ({enabled} enabled)", detail=f"claude {shown}")
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        try:
            self.proc = subprocess.Popen(
                cmd, cwd=self.workdir, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", bufsize=1,
                creationflags=NO_WINDOW,
            )
        except OSError as e:
            self.emit("error", f"Could not start Claude Code: {e}", detail=f"Looked for: {cmd[0]}")
            await self._finish("error")
            return
        prompt = user_prompt(self.kind, self.query, self.count, self.since, seen=self.seen)
        threading.Thread(target=self._pump, args=(prompt,), daemon=True).start()
        asyncio.create_task(self._watchdog())

    def _pump(self, prompt: str) -> None:
        p = self.proc
        assert p and p.stdin and p.stdout and p.stderr
        try:
            p.stdin.write(prompt)
            p.stdin.close()
        except OSError:
            pass

        def drain_stderr():
            for line in p.stderr:
                self.stderr_tail = (self.stderr_tail + [line.rstrip()])[-40:]

        t_err = threading.Thread(target=drain_stderr, daemon=True)
        t_err.start()
        with open(self.dir / "stream.jsonl", "w", encoding="utf-8") as raw:
            for line in p.stdout:
                raw.write(line)
                if line.strip():
                    self.loop.call_soon_threadsafe(self._on_line, line.strip())
        rc = p.wait()
        t_err.join(timeout=2)
        asyncio.run_coroutine_threadsafe(self._on_exit(rc), self.loop)

    async def _watchdog(self) -> None:
        while self.proc and self.proc.poll() is None:
            await asyncio.sleep(2)
            if time.monotonic() - self.t0 > RUN_TIMEOUT_S:
                self.emit("warn", f"Time limit of {RUN_TIMEOUT_S}s reached, stopping Claude")
                self.cancel(f"timed out after {RUN_TIMEOUT_S}s")
                return

    def cancel(self, reason: str = "cancelled by user") -> bool:
        if not self.proc or self.proc.poll() is not None:
            return False
        self.cancel_reason = reason
        if os.name == "nt":  # kill the whole tree: claude.exe + the MCP python child
            subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"],
                           capture_output=True, creationflags=NO_WINDOW)
        else:
            self.proc.kill()
        return True

    # ------------------------------------------------------------------ stream-json -> timeline
    def _on_line(self, line: str) -> None:
        try:
            e = json.loads(line)
        except ValueError:
            return
        handler = {
            "system": self._on_system, "assistant": self._on_assistant, "user": self._on_user,
            "result": self._on_result, "rate_limit_event": self._on_rate,
        }.get(e.get("type"))
        if handler:
            handler(e)

    def _on_system(self, e: dict) -> None:
        st = e.get("subtype") or ""
        if st == "init":
            self.stats["model"] = e.get("model")
            tools = [t.removeprefix("mcp__finder__") for t in e.get("tools") or []]
            servers = e.get("mcp_servers") or []
            self.emit("ready", f"Claude Code {e.get('claude_code_version', '')} session ready · {e.get('model')}",
                      detail="Tools: " + ", ".join(tools) + "\nMCP: " +
                             ", ".join(f"{s.get('name')}={s.get('status')}" for s in servers))
            for s in servers:
                if s.get("status") != "connected":
                    self.emit("error", f"MCP server '{s.get('name')}' is {s.get('status')}",
                              detail="Check that the venv has 'mcp' installed and finder/mcp_server.py runs.")
            self._emit_stats()
        elif st == "thinking_tokens":
            self._thinking_est = e.get("estimated_tokens") or self._thinking_est
        elif "retry" in st or "error" in st:
            self.emit("warn", f"Claude Code: {st.replace('_', ' ')}", detail=json.dumps(e, ensure_ascii=False)[:1500])

    def _on_assistant(self, e: dict) -> None:
        if e.get("parent_tool_use_id") or self._submitted:  # the closing "done" turn is noise
            return
        m = e.get("message") or {}
        if m.get("id") and m["id"] not in self._msg_ids:
            self._msg_ids.add(m["id"])
            self.stats["turns"] = len(self._msg_ids)
        u = m.get("usage") or {}
        ctx = sum(u.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
        if ctx:
            self.stats["context_tokens"] = ctx
        for c in m.get("content") or []:
            ct = c.get("type")
            if ct in ("thinking", "redacted_thinking"):
                txt = (c.get("thinking") or "").strip()
                est, self._thinking_est = self._thinking_est, 0
                title = "Thinking" + (f" (≈{est:,} tokens)" if est else "…")
                if txt:
                    title += f": {_first_line(txt, 160)}"
                self.emit("thinking", title, detail=txt if len(txt) > 160 else None)
            elif ct == "text":
                txt = (c.get("text") or "").strip()
                if txt and txt.lower().strip(".!` ") != "done":
                    multi = len(txt) > 220 or "\n" in txt
                    self.emit("say", _first_line(txt, 220), detail=txt if multi else None)
            elif ct == "tool_use":
                name = (c.get("name") or "").removeprefix("mcp__finder__")
                inp = c.get("input") or {}
                self._calls[c.get("id", "")] = (name, time.monotonic())
                self.stats["tool_calls"] += 1
                if name == "submit_results":
                    items = inp.get("items") or []
                    det = (inp.get("summary") or "") + "\n\n" + "\n".join(
                        f"{i}. {it.get('id')} [{it.get('score')}] {it.get('reason')}" for i, it in enumerate(items, 1))
                    self.emit("submit", f"Submitting {len(items)} ranked results", detail=det)
                else:
                    self.emit("tool_call", _fmt_call(name, inp), tool=name,
                              detail=json.dumps(inp, ensure_ascii=False, indent=1))
        self._emit_stats()

    def _on_user(self, e: dict) -> None:
        content = (e.get("message") or {}).get("content")
        if e.get("parent_tool_use_id") or not isinstance(content, list):
            return
        for c in content:
            if not isinstance(c, dict) or c.get("type") != "tool_result":
                continue
            name, t_start = self._calls.get(c.get("tool_use_id", ""), ("tool", time.monotonic()))
            txt = _tool_text(c.get("content")).strip()
            dur = round(time.monotonic() - t_start, 1)
            failed = c.get("is_error") or txt.startswith(("Search failed", "ERROR", "README unavailable", "Search budget",
                                                          "Transcript unavailable", "Read budget"))
            first, _, rest = txt.partition("\n")
            if name == "WebSearch" and not failed:
                first, rest = _web_search_view(txt)
            if name == "submit_results":
                self._submitted = not failed
                shown = first.split(" Reply with")[0]  # that part is an instruction for Claude
                self.emit("warn" if failed else "accepted", _first_line(shown, 240), dur=dur, tool=name)
            else:
                self.emit("warn" if failed else "tool_result", _first_line(first, 260), dur=dur, tool=name,
                          detail=rest[:8000] or None)

    def _on_result(self, e: dict) -> None:
        u = e.get("usage") or {}
        self._result_text = str(e.get("result") or "")
        self.stats.update(
            cost_usd=e.get("total_cost_usd"),
            duration_s=round((e.get("duration_ms") or 0) / 1000, 1),
            turns=e.get("num_turns") or self.stats["turns"],
            input_tokens=sum(u.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens",
                                                     "cache_creation_input_tokens")),
            output_tokens=u.get("output_tokens"),
        )
        if e.get("is_error") or e.get("subtype") != "success":
            self.emit("error", f"Claude stopped: {e.get('subtype')} {e.get('terminal_reason') or ''}".strip(),
                      detail=self._result_text[:2000] or None)
        self._emit_stats()

    def _on_rate(self, e: dict) -> None:
        info = e.get("rate_limit_info") or {}
        wins = info.get("unifiedWindows") or {}
        self.stats["rate_limits"] = {k: v.get("utilization") for k, v in wins.items() if isinstance(v, dict)}
        if info.get("status") not in (None, "allowed"):
            self.emit("warn", f"Usage limit status: {info.get('status')} ({info.get('rateLimitType')})")
        self._emit_stats()

    # ------------------------------------------------------------------ finish
    async def _on_exit(self, rc: int) -> None:
        try:
            await self._handle_exit(rc)
        except Exception as ex:  # never leave a run hanging in "running"
            self.emit("error", f"Internal error while finishing: {type(ex).__name__}: {ex}")
            await self._finish("error")

    async def _handle_exit(self, rc: int) -> None:
        final_path = self.dir / "final.json"
        if final_path.exists():
            if self.cancel_reason:
                self.emit("warn", f"Stopped ({self.cancel_reason}), but results were already submitted")
            await self._finalize(json.loads(final_path.read_text(encoding="utf-8")))
        elif self.cancel_reason:
            self.emit("cancelled", f"Stopped: {self.cancel_reason}")
            await self._finish("cancelled")
        else:
            tail = "\n".join(self.stderr_tail[-15:])
            why = f"Claude Code exited with code {rc}" if rc else "Claude finished without calling submit_results"
            self.emit("error", why, detail=(tail or self._result_text[:2000]) or None)
            await self._finish("error")

    async def _finalize(self, final: dict) -> None:
        cpath = self.dir / "candidates.json"
        cands = json.loads(cpath.read_text(encoding="utf-8")) if cpath.exists() else {}
        items = []
        for it in final.get("items", []):
            c = cands.get(it["key"])
            if c:
                extra = {k: it[k] for k in ("start_s", "start_label", "quote") if it.get(k) is not None}
                items.append({**c, "rank": len(items) + 1, "score": it["score"], "reason": it["reason"], **extra})
        self.emit("enrich", "Adding extra signals: " + {
            "github": "scanning READMEs for linked arXiv papers",
            "arxiv": "Hugging Face code links/upvotes + Semantic Scholar citations",
            "youtube": "titles of arXiv papers linked in video descriptions",
            "blog": "Hacker News points for each post + titles of linked papers and videos",
        }[self.kind])
        try:
            msg = await asyncio.wait_for(asyncio.to_thread(enrich, self.kind, items), timeout=45)
            self.emit("enriched", msg)
        except Exception as ex:  # enrichment is best effort
            self.emit("warn", f"Extra signals skipped: {type(ex).__name__}")
        results = {"summary": final.get("summary", ""), "items": items, "candidates_seen": len(cands)}
        (self.dir / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
        self.n_results = len(items)
        self.emit("results", f"{len(items)} results selected from {len(cands)} candidates", results=results)
        await self._finish("done")

    async def _finish(self, status: str) -> None:
        self.status = status
        elapsed = round(time.monotonic() - self.t0, 1)
        if status == "done":
            s = self.stats
            cost = f" · ${s['cost_usd']:.3f}" if s.get("cost_usd") is not None else ""
            self.emit("done", f"Done in {elapsed}s · {self.n_results} results · {s['turns']} turns · "
                              f"{s['tool_calls']} tool calls{cost}")
        self._write_meta()
        try:
            lib = await asyncio.to_thread(library.ingest_run, self.id)
            if lib and status == "done" and self.n_results:
                self.emit("info", f"Saved to Library · {lib['n_new']} of {self.n_results} results are new to you")
        except Exception as ex:
            self.emit("warn", f"Library: could not save this run ({type(ex).__name__}: {ex})")
        self.emit("end", status, status=status, elapsed=elapsed, stats=dict(self.stats))
        self._write_meta()
        ACTIVE.pop(self.id, None)
        shutil.rmtree(self.workdir, ignore_errors=True)


# ---------------------------------------------------------------------- enrichment

def _attach_paper_titles(items: list[dict]) -> None:
    """items[i]["papers"] = [{id, title}] for the arXiv ids in items[i]["paper_links"]."""
    ids = list(dict.fromkeys(pid for it in items for pid in it.get("paper_links") or []))[:40]
    titles = {}
    if ids:
        try:
            titles = {p["id"]: p["title"] for p in S.arxiv_by_ids(ids)}
        except Exception:
            pass
    for it in items:
        it["papers"] = [{"id": pid, "title": titles.get(pid)} for pid in it.get("paper_links") or []]


def _attach_video_titles(items: list[dict]) -> None:
    """items[i]["videos"] = [{id, title}] for items[i]["video_links"]; titles need a YouTube key."""
    ids = list(dict.fromkeys(v for it in items for v in it.get("video_links") or []))[:50]
    titles = {}
    if ids and youtube_api_key():
        try:
            titles = {v["id"]: v["title"] for v in S.yt_videos(ids)}  # ~2 quota units
        except Exception:
            pass
    for it in items:
        it["videos"] = [{"id": v, "title": titles.get(v)} for v in it.get("video_links") or []]


def enrich(kind: str, items: list[dict]) -> str:
    if kind == "blog":
        found = S.parallel(lambda it: S.hn_lookup(it["url"]), items)
        for it, hn in zip(items, found):
            if hn and hn["hn_points"] >= (it.get("hn_points") or 0):
                it.update(hn)
        _attach_paper_titles(items)
        _attach_video_titles(items)
        n_hn = sum(it.get("hn_id") is not None for it in items)
        n_p = sum(bool(it["papers"]) for it in items)
        n_c = sum(bool(it.get("code_links")) for it in items)
        n_v = sum(bool(it["videos"]) for it in items)
        n_q = sum(bool(it.get("quote")) for it in items)
        return (f"{n_hn} of {len(items)} posts were on Hacker News · {n_p} link arXiv papers · {n_c} link GitHub "
                f"repos · {n_v} link videos · {n_q} verified quotes")

    if kind == "youtube":
        _attach_paper_titles(items)
        n_p = sum(bool(it["papers"]) for it in items)
        n_c = sum(bool(it.get("code_links")) for it in items)
        n_t = sum(it.get("start_s") is not None for it in items)
        return (f"{n_p} of {len(items)} videos link arXiv papers · {n_c} link GitHub repos · "
                f"{n_t} have a jump-to timestamp")

    if kind == "github":
        todo = [it for it in items if not it.get("readme_scanned")]

        def scan(it):
            try:
                raw = S.gh_readme(it["id"])
            except Exception:
                return
            it["paper_links"] = list(dict.fromkeys((it.get("paper_links") or []) + S.arxiv_ids_in(raw)))[:6]
            it["readme_scanned"] = True

        S.parallel(scan, todo)
        _attach_paper_titles(items)
        n = sum(bool(it["papers"]) for it in items)
        return f"Scanned {len(todo)} READMEs → {n} of {len(items)} repos link to arXiv papers"

    hf = S.parallel(lambda it: S.hf_paper(it["id"]), items)
    for it, h in zip(items, hf):
        if h:
            S.merge(it, h)
    missing = [it["id"] for it in items if it.get("citations") is None]
    cites = S.s2_citations(missing)
    for it in items:
        if it["id"] in cites:
            S.merge(it, cites[it["id"]])
        it["code_url"] = it.get("hf_github") or next(iter(it.get("code_links") or []), None)
    n_hf = sum(h is not None for h in hf)
    n_code = sum(bool(it["code_url"]) for it in items)
    n_cit = sum(it.get("citations") is not None for it in items)
    return f"Hugging Face: {n_hf}/{len(items)} papers listed · code for {n_code} · citations for {n_cit}"


# ---------------------------------------------------------------------- history

def list_history(kind: str | None = None, limit: int = 60) -> list[dict]:
    rows = []
    if not RUNS_DIR.exists():
        return rows
    for d in sorted(RUNS_DIR.iterdir(), reverse=True):
        if not RUN_ID_RE.match(d.name) or (kind and f"-{kind}-" not in d.name):
            continue
        try:
            m = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if m.get("status") == "running" and m["id"] not in ACTIVE:
            m["status"] = "interrupted"
        m.pop("stats", None)
        rows.append(m)
        if len(rows) >= limit:
            break
    return rows


def load_run(run_id: str) -> dict | None:
    if not RUN_ID_RE.match(run_id):
        return None
    d = RUNS_DIR / run_id
    if not (d / "meta.json").exists():
        return None
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    if meta.get("status") == "running" and run_id not in ACTIVE:
        meta["status"] = "interrupted"
    events = []
    if (d / "events.jsonl").exists():
        for line in (d / "events.jsonl").read_text(encoding="utf-8").splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                pass
    results = json.loads((d / "results.json").read_text(encoding="utf-8")) if (d / "results.json").exists() else None
    return {"meta": meta, "events": events, "results": results}


# ---------------------------------------------------------------------- watches

def watch_running() -> Run | None:
    return next((r for r in ACTIVE.values() if r.origin == "watch"), None)


async def start_watch(w: dict) -> Run | None:
    """Start the scheduled check of one watched topic+tab (see library.next_due_watch)."""
    if w["kind"] == "youtube" and not youtube_api_key():
        await asyncio.to_thread(library.watch_started, w["topic_id"], w["kind"], "skipped: no YouTube API key")
        return None
    run = Run(w["kind"], w["query"], w["count"] or library.WATCH_COUNT, "custom", w["model"] or "sonnet",
              origin="watch", topic_id=w["topic_id"], since=w["since"], seen=w["seen"])
    ACTIVE[run.id] = run
    await asyncio.to_thread(library.watch_started, w["topic_id"], w["kind"])
    await run.start()
    return run


async def watch_loop() -> None:
    """Every 30 s: unless a check is already running, start the most overdue watch. Checks run
    one at a time and only while Scout is running; overdue ones catch up after a restart."""
    while True:
        try:
            if not watch_running():
                w = await asyncio.to_thread(library.next_due_watch)
                if w:
                    await start_watch(w)
        except Exception as ex:
            print(f"[watch] {type(ex).__name__}: {ex}", file=sys.stderr, flush=True)
        await asyncio.sleep(30)
