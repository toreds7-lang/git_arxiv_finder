"""FastAPI app: serves the UI, starts runs, streams their timeline over SSE."""
from __future__ import annotations

import asyncio
import json
import subprocess
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import library, llm, runner
from .config import (COUNT_OPTIONS, DEFAULT_COUNTS, DEFAULT_MODEL, DEFAULT_SINCE, MODELS, SINCE_OPTIONS,
                     STATIC_DIR, claude_bin, github_token, youtube_api_key)

Kind = Literal["github", "arxiv", "youtube", "blog"]
RUN_ID = r"^\d{8}-\d{6}-(github|arxiv|youtube|blog)-[0-9a-f]{4}$"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    library.init()

    async def catch_up():  # index runs made before the Library existed, then label new topics
        await asyncio.to_thread(library.sync, set(runner.ACTIVE))
        await asyncio.to_thread(library.extract_pending_keywords)

    tasks = [asyncio.create_task(catch_up()), asyncio.create_task(runner.watch_loop())]
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="Scout · GitHub, arXiv, YouTube & blog finder", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

_claude_version: str | None = None


def claude_version() -> str | None:
    global _claude_version
    if _claude_version is None:
        try:
            out = subprocess.run([claude_bin(), "--version"], capture_output=True, text=True, timeout=20,
                                 creationflags=runner.NO_WINDOW)
            _claude_version = out.stdout.strip() or None
        except (OSError, subprocess.TimeoutExpired):
            _claude_version = None
    return _claude_version


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/config")
async def config():
    return {
        "counts": DEFAULT_COUNTS,
        "count_options": COUNT_OPTIONS,
        "since_options": list(SINCE_OPTIONS),
        "default_since": DEFAULT_SINCE,
        "models": MODELS,
        "default_model": DEFAULT_MODEL,
        "claude_version": await asyncio.to_thread(claude_version),
        "github_token": bool(github_token()),
        "youtube_key": bool(youtube_api_key()),
        "active_runs": list(runner.ACTIVE),
    }


class SearchReq(BaseModel):
    kind: Kind
    query: str = Field(min_length=2, max_length=2000)
    count: int = Field(ge=1, le=50)
    since: str = DEFAULT_SINCE
    model: str = DEFAULT_MODEL
    origin: Literal["search", "find"] = "search"
    parent_run: str | None = Field(default=None, pattern=RUN_ID)  # the run whose card's Find button started this


@app.post("/api/search")
async def search(req: SearchReq):
    if req.since not in SINCE_OPTIONS:
        raise HTTPException(400, f"since must be one of {list(SINCE_OPTIONS)}")
    if req.model not in MODELS:
        raise HTTPException(400, f"model must be one of {MODELS}")
    if req.kind == "youtube" and not youtube_api_key():  # fail before Claude spends anything
        raise HTTPException(400, "No YouTube API key. Add a YOUTUBE_API_KEY= line to env.txt.txt and search again.")
    run = runner.Run(req.kind, req.query, req.count, req.since, req.model, origin=req.origin,
                     parent_run=req.parent_run)
    runner.ACTIVE[run.id] = run
    await run.start()
    return {"run_id": run.id}


def _sse(ev: dict) -> str:
    return f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"


@app.get("/api/runs/{run_id}/events")
async def events(run_id: str, request: Request):
    run = runner.ACTIVE.get(run_id)
    if not run and not runner.load_run(run_id):
        raise HTTPException(404, "unknown run")

    async def gen():
        if not run:  # finished: replay from disk
            for ev in runner.load_run(run_id)["events"]:
                yield _sse(ev)
            yield _sse({"type": "eof"})
            return
        snapshot, q = run.subscribe()
        try:
            for ev in snapshot:
                yield _sse(ev)
            if snapshot and snapshot[-1]["type"] == "end":
                return
            while not await request.is_disconnected():
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                yield _sse(ev)
                if ev["type"] == "end":
                    break
        finally:
            run.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/runs/{run_id}/cancel")
async def cancel(run_id: str):
    run = runner.ACTIVE.get(run_id)
    if not run:
        raise HTTPException(404, "run is not active")
    return {"cancelled": run.cancel()}


@app.get("/api/history")
async def history(kind: Kind | None = None):
    return await asyncio.to_thread(runner.list_history, kind)


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str):
    data = await asyncio.to_thread(runner.load_run, run_id)
    if not data:
        raise HTTPException(404, "unknown run")
    data["active"] = run_id in runner.ACTIVE
    return data


# ---------------------------------------------------------------------- Library

@app.get("/api/library")
async def library_overview():
    data = await asyncio.to_thread(library.overview)
    w = runner.watch_running()
    data["watch_running"] = {"run_id": w.id, "kind": w.kind, "topic_id": w.topic_id} if w else None
    if data["stats"]["kw_pending"]:
        asyncio.create_task(asyncio.to_thread(library.extract_pending_keywords))
    return data


@app.get("/api/library/status")
async def library_status():
    data = await asyncio.to_thread(library.watch_status)
    w = runner.watch_running()
    data["running"] = {"run_id": w.id, "kind": w.kind, "query": w.query} if w else None
    return data


@app.get("/api/library/search")
async def library_search(q: str = Query(min_length=1, max_length=300), kind: Kind | None = None):
    return await asyncio.to_thread(library.search, q, kind)


@app.get("/api/library/similar")
async def library_similar(q: str):
    return await asyncio.to_thread(library.similar_topics, q[:2000])


class MarkReq(BaseModel):
    kind: Kind
    id: str = Field(min_length=1, max_length=2000)
    star: bool | None = None
    vote: Literal[-1, 0, 1] | None = None


@app.post("/api/library/mark")
async def library_mark(req: MarkReq):
    return await asyncio.to_thread(library.set_mark, req.kind, req.id, req.star, req.vote)


class AnnotateReq(BaseModel):
    kind: Kind
    ids: list[str] = Field(max_length=100)
    run_id: str | None = Field(default=None, pattern=RUN_ID)


@app.post("/api/library/annotate")
async def library_annotate(req: AnnotateReq):
    return await asyncio.to_thread(library.annotate, req.kind, req.ids, req.run_id)


class TopicReq(BaseModel):
    tags: list[str] | None = Field(default=None, max_length=20)
    notes: str | None = Field(default=None, max_length=5000)
    hidden: bool | None = None
    label: str | None = Field(default=None, max_length=80)


@app.patch("/api/library/topics/{tid}")
async def library_topic(tid: int, req: TopicReq):
    if not await asyncio.to_thread(library.update_topic, tid, req.tags, req.notes, req.hidden, req.label):
        raise HTTPException(404, "unknown topic")
    return {"ok": True}


class WatchReq(BaseModel):
    kind: Kind
    enabled: bool
    every_days: int | None = Field(default=None, ge=1, le=90)


@app.put("/api/library/topics/{tid}/watch")
async def library_watch(tid: int, req: WatchReq):
    w = await asyncio.to_thread(library.set_watch, tid, req.kind, req.enabled, req.every_days)
    if not w:
        raise HTTPException(404, "unknown topic")
    return w


@app.post("/api/library/topics/{tid}/check-now")
async def library_check_now(tid: int):
    return {"queued": await asyncio.to_thread(library.check_now, tid)}


@app.post("/api/library/ideas")
async def library_ideas():
    try:
        ideas, cost = await asyncio.to_thread(library.make_ideas)
    except llm.LLMError as e:
        raise HTTPException(502, str(e))
    return {"ideas": ideas, "cost_usd": cost}


@app.post("/api/library/ideas/{idea_id}/dismiss")
async def library_dismiss_idea(idea_id: int):
    return {"ok": await asyncio.to_thread(library.dismiss_idea, idea_id)}
