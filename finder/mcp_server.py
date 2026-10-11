"""MCP stdio server giving Claude Code its ONLY tools for a search run.

Spawned by Claude Code (via --mcp-config) once per run. Every candidate a tool
returns is saved to <run-dir>/candidates.json, and submit_results only accepts
ids that are in there, so Claude can rank results but cannot invent them.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Annotated, Literal

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp.server.mcpserver import MCPServer  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from finder import sources as S  # noqa: E402
from finder.config import (KINDS, MAX_ARTICLE_READS, MAX_READMES, MAX_SEARCHES, MAX_TRANSCRIPTS,  # noqa: E402
                           MAX_YT_SEARCHES)

ap = argparse.ArgumentParser()
ap.add_argument("--run-dir", required=True)
ap.add_argument("--kind", choices=KINDS, required=True)
ap.add_argument("--since", default="")  # YYYY-MM-DD or ""
ap.add_argument("--count", type=int, default=10)
ap.add_argument("--seen-file", default="")  # watch checks: ids the user already saw (JSON list)
ARGS = ap.parse_args()

RUN_DIR = Path(ARGS.run_dir)
SINCE = ARGS.since or None
CANDS: dict[str, dict] = {}  # canonical key -> candidate
budget = {"search": 0, "readme": 0, "transcript": 0, "yt_units": 0, "read": 0}
CAPTION_STARTS: dict[str, list[int]] = {}  # video key -> caption line starts (s), for timestamp checks
ARTICLE_TEXT: dict[str, str] = {}  # blog key -> normalised article text, for quote checks
ALIASES: dict[str, str] = {}  # blog key of a URL that redirected -> key of where it landed

mcp = MCPServer("finder")


def _seen_keys() -> set[str]:
    if not ARGS.seen_file:
        return set()
    try:
        return {_key(i) for i in json.loads(Path(ARGS.seen_file).read_text(encoding="utf-8"))}
    except (OSError, ValueError):
        return set()


def _key(cid: str) -> str:
    cid = cid.strip()
    if ARGS.kind == "arxiv":
        return S.norm_arxiv_id(cid)
    if ARGS.kind == "youtube":
        return S.norm_video_id(cid)  # video ids are case-sensitive
    if ARGS.kind == "blog":
        k = S.canon_url(cid)
        return ALIASES.get(k, k)
    cid = cid.removeprefix("https://").removeprefix("github.com/").strip("/")
    return cid.lower()


def _save() -> None:
    tmp = RUN_DIR / "candidates.json.tmp"
    tmp.write_text(json.dumps(CANDS, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, RUN_DIR / "candidates.json")


def _remember(items: list[dict]) -> list[tuple[dict, bool]]:
    """Store candidates; return [(candidate, is_new)]."""
    out = []
    for it in items:
        k = _key(it["id"])
        new = k not in CANDS
        CANDS[k] = S.merge(CANDS.get(k, {}), it)
        out.append((CANDS[k], new))
    _save()
    return out


def _spend_search() -> str | None:
    budget["search"] += 1
    limit = MAX_YT_SEARCHES if ARGS.kind == "youtube" else MAX_SEARCHES
    if budget["search"] > limit:
        return (f"Search budget reached ({limit} searches). Rank what you already have and "
                "call submit_results now.")
    return None


def _clip(s: str | None, n: int) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


# =========================================================================== GitHub tools

def _fmt_repo(i: int, r: dict, new: bool) -> str:
    if not new:
        return f"[{i}] {r['id']} (already seen above)"
    bits = [f"★{S.human(r.get('stars'))}"]
    if r.get("stars_per_month"):
        bits[0] += f" (+{S.human(r['stars_per_month'])}/mo)"
    bits += [r.get("language") or "-", f"pushed {S.ago(r.get('pushed_at'))}",
             f"created {(r.get('created_at') or '')[:7]}", r.get("license") or "no license"]
    line = f"[{i}] {r['id']} " + " · ".join(bits)
    if r.get("is_fork"):
        line += " · FORK"
    line += f"\n    {_clip(r.get('description'), 220) or '(no description)'}"
    if r.get("topics"):
        line += f"\n    topics: {', '.join(r['topics'][:8])}"
    return line


def search_github(
    query: Annotated[str, Field(description=(
        "GitHub repository search keywords. Short is better (2-4 words); terms are ANDed. "
        "Qualifiers allowed: topic:llm, in:name,description,readme, user:org, OR between terms. "
        "To check a project you already know of, use repo:owner/name."))],
    sort: Annotated[Literal["best-match", "stars", "updated"], Field(description=(
        "best-match = GitHub relevance; stars = established projects; updated = freshest activity"))] = "best-match",
    pushed_after: Annotated[str | None, Field(description="YYYY-MM-DD: only repos with commits since then")] = None,
    created_after: Annotated[str | None, Field(description="YYYY-MM-DD: only repos created since then")] = None,
    min_stars: Annotated[int | None, Field(description="minimum stars, e.g. 50 to skip toy projects")] = None,
    language: Annotated[str | None, Field(description="primary language, e.g. Python, TypeScript")] = None,
    limit: Annotated[int, Field(ge=5, le=50, description="results to fetch (default 30)")] = 30,
) -> str:
    """Search GitHub repositories. Returns compact lines: stars (+stars/month momentum), language,
    last push, creation month, license, description, topics."""
    if msg := _spend_search():
        return msg
    note = ""
    if SINCE and (not pushed_after or pushed_after < SINCE):
        pushed_after = SINCE
        note = f" Recency filter pushed>={SINCE} applied automatically."
    try:
        total, repos, q = S.gh_search(query, sort, pushed_after, created_after, min_stars, language, limit)
    except S.SourceError as e:
        return f"Search failed: {e}"
    rows = _remember(repos)
    n_new = sum(new for _, new in rows)
    head = (f"Found {total:,} repos for `{q}` (showing {len(rows)}, sort={sort}, {n_new} new this session).{note}")
    if not rows:
        return head + "\nNo results: try fewer or broader keywords, synonyms, or drop qualifiers."
    return head + "\n" + "\n".join(_fmt_repo(i, r, new) for i, (r, new) in enumerate(rows, 1))


def get_readme(
    full_name: Annotated[str, Field(description="owner/repo exactly as listed in search results")],
    max_chars: Annotated[int, Field(ge=500, le=6000)] = 2500,
) -> str:
    """Read the start of a repository's README to judge what it really does and how mature it is.
    Use only for a few candidates whose relevance is unclear."""
    budget["readme"] += 1
    if budget["readme"] > MAX_READMES:
        return f"README budget reached ({MAX_READMES}). Decide from descriptions and submit."
    try:
        raw = S.gh_readme(full_name)
    except S.SourceError as e:
        return f"README unavailable: {e}"
    papers = S.arxiv_ids_in(raw)
    k = _key(full_name)
    if k in CANDS:
        CANDS[k] = S.merge(CANDS[k], {"paper_links": papers, "readme_scanned": True})
        _save()
    text = S.clean_readme(raw)
    head = f"README of {full_name} ({len(text):,} chars, showing {min(len(text), max_chars):,})"
    if papers:
        head += f"; links arXiv papers: {', '.join(papers[:5])}"
    return head + "\n" + text[:max_chars]


# =========================================================================== arXiv tools

def _fmt_paper(i: int, p: dict, new: bool) -> str:
    if not new:
        return f"[{i}] {p['id']} (already seen above)"
    bits = [(p.get("published") or "")[:10]]
    if p.get("primary_category"):
        bits.append(p["primary_category"])
    if p.get("citations") is not None:
        bits.append(f"cites {p['citations']} ({p.get('influential') or 0} influential)")
    if p.get("hf_upvotes"):
        bits.append(f"HF ▲{p['hf_upvotes']}")
    if p.get("code_links") or p.get("hf_github"):
        bits.append("code ✓")
    if p.get("journal_ref") or p.get("venue"):
        bits.append(_clip(p.get("journal_ref") or p.get("venue"), 40))
    authors = p.get("authors") or []
    au = ", ".join(authors[:3]) + (" et al." if len(authors) > 3 else "")
    return (f"[{i}] {p['id']} · " + " · ".join(bits) +
            f"\n    {p.get('title')}\n    {au}\n    {_clip(p.get('abstract'), 380)}")


def _with_citations(papers: list[dict]) -> list[dict]:
    need = [p["id"] for p in papers if _key(p["id"]) not in CANDS or CANDS[_key(p["id"])].get("citations") is None]
    cites = S.s2_citations(need)
    for p in papers:
        if p["id"] in cites:
            p.update(cites[p["id"]])
    return papers


def _paper_listing(head: str, papers: list[dict]) -> str:
    rows = _remember(_with_citations(papers))
    n_new = sum(new for _, new in rows)
    head = head.replace("{new}", str(n_new))
    if not rows:
        return head + "\nNo results: broaden (OR synonyms, abs: instead of ti:, fewer AND terms)."
    return head + "\n" + "\n".join(_fmt_paper(i, p, new) for i, (p, new) in enumerate(rows, 1))


def search_arxiv(
    query: Annotated[str, Field(description=(
        "arXiv query syntax. Fields: ti: (title), abs: (abstract), au: (author), all:. "
        "Operators: AND, OR, ANDNOT, parentheses; quote phrases. Terms with no operator between them "
        "are joined with AND (an OR-chain stays one group: a OR b c -> (a OR b) AND c). "
        'Example: abs:"retrieval augmented generation" AND (abs:evaluation OR abs:benchmark)'))],
    categories: Annotated[list[str] | None, Field(description="e.g. [\"cs.CL\", \"cs.AI\"]; ORed together")] = None,
    submitted_after: Annotated[str | None, Field(description="YYYY-MM-DD")] = None,
    submitted_before: Annotated[str | None, Field(description="YYYY-MM-DD")] = None,
    sort: Annotated[Literal["relevance", "date"], Field(description="relevance or newest first")] = "relevance",
    limit: Annotated[int, Field(ge=5, le=50)] = 30,
) -> str:
    """Search arXiv (keyword/boolean, not semantic). Returns id, date, category, citation counts
    (Semantic Scholar), code availability, title, authors and a clipped abstract."""
    if msg := _spend_search():
        return msg
    note = ""
    if SINCE and (not submitted_after or submitted_after < SINCE):
        submitted_after = SINCE
        note = f" Recency filter submitted>={SINCE} applied automatically."
    try:
        total, papers, sq = S.arxiv_search(query, categories, submitted_after, submitted_before, sort, limit)
    except S.SourceError as e:
        return f"Search failed: {e}"
    head = f"Found {total:,} arXiv papers for `{sq}` (showing {len(papers)}, sort={sort}, {{new}} new).{note}"
    return _paper_listing(head, papers)


def search_hf_papers(
    query: Annotated[str, Field(description="natural-language description; this search is semantic")],
    limit: Annotated[int, Field(ge=5, le=50)] = 20,
) -> str:
    """Semantic search over Hugging Face Papers (mostly ML/AI arXiv papers) with community upvotes
    and linked GitHub code. Complements search_arxiv's keyword matching."""
    if msg := _spend_search():
        return msg
    try:
        papers = S.hf_search(query, limit)
    except S.SourceError as e:
        return f"Search failed: {e}"
    dropped = 0
    if SINCE:
        kept = [p for p in papers if (p.get("published") or "")[:10] >= SINCE]
        dropped = len(papers) - len(kept)
        papers = kept
    head = f"Hugging Face Papers: {len(papers)} results for \"{query}\" ({{new}} new)"
    if dropped:
        head += f"; {dropped} older than {SINCE} dropped"
    return _paper_listing(head + ".", papers)


def get_abstracts(
    ids: Annotated[list[str], Field(description="up to 8 arXiv ids already seen in search results")],
) -> str:
    """Full abstracts (plus author comments) for candidates whose relevance is unclear. No network cost."""
    out = []
    for i in ids[:8]:
        p = CANDS.get(_key(i))
        if not p:
            out.append(f"{i}: not in results yet; search for it first.")
            continue
        extra = f"\n  comment: {p['comment']}" if p.get("comment") else ""
        out.append(f"{p['id']} — {p.get('title')}\n  {p.get('abstract')}{extra}")
    return "\n\n".join(out)


# =========================================================================== YouTube tools

def _fmt_video(i: int, v: dict, new: bool) -> str:
    if not new:
        return f"[{i}] {v['id']} (already seen above)"
    chan = v.get("channel") or "?"
    if v.get("subscribers") is not None:
        chan += f" ({S.human(v['subscribers'])} subs)"
    views = f"{S.human(v.get('views'))} views"
    if v.get("views_per_day"):
        views += f" (+{S.human(v['views_per_day'])}/day)"
    bits = [chan, S.hms(v["duration_s"]) if v.get("duration_s") else "length ?", views]
    if v.get("likes") is not None:
        bits.append(f"{S.human(v['likes'])} likes")
    bits.append((v.get("published") or "")[:10])
    if v.get("captions"):
        bits.append("CC")
    if v.get("live"):
        bits.append("LIVE NOW" if v["live"] == "live" else "UPCOMING (not aired yet)")
    lines = [f'[{i}] {v["id"]} "{v.get("title")}"', "    " + " · ".join(bits)]
    if desc := _clip(S.desc_snippet(v.get("description")), 240):
        lines.append(f"    {desc}")
    if ch := v.get("chapters"):
        shown = " · ".join(f"{S.hms(c['start_s'])} {_clip(c['title'], 40)}" for c in ch[:10])
        lines.append(f"    chapters ({len(ch)}): {shown}" + (" …" if len(ch) > 10 else ""))
    links = [f"arXiv {p}" for p in (v.get("paper_links") or [])[:3]]
    links += [u.removeprefix("https://") for u in (v.get("code_links") or [])[:3]]
    if links:
        lines.append("    links: " + " · ".join(links))
    return "\n".join(lines)


def search_youtube(
    query: Annotated[str, Field(description=(
        "What to look for in natural words (YouTube matching is fuzzy, not a strict AND), e.g. "
        "'transformer attention explained', 'vLLM tutorial', an exact paper, project or talk title. "
        "Words like tutorial, lecture, talk, explained, demo, walkthrough steer the format."))],
    order: Annotated[Literal["relevance", "viewCount", "date"], Field(description=(
        "relevance = YouTube's ranking; viewCount = most watched (established explainers); date = newest"))] = "relevance",
    duration: Annotated[Literal["any", "medium", "long", "short"], Field(description=(
        "any; medium = 4-20 min; long = over 20 min (lectures, talks, deep dives); short = under 4 min"))] = "any",
    published_after: Annotated[str | None, Field(description="YYYY-MM-DD: only videos uploaded since then")] = None,
    language: Annotated[str | None, Field(description="ISO 639-1 code to favour, e.g. en or ko")] = None,
    limit: Annotated[int, Field(ge=5, le=50, description="results to fetch (default 20)")] = 20,
) -> str:
    """Search YouTube videos (about 100 of the 10,000 daily API quota units per call). Per video:
    title, channel (+subscribers), length, views (+views/day), likes, upload date, a description
    snippet, chapters, and arXiv/GitHub links found in the description."""
    if msg := _spend_search():
        return msg
    note = ""
    if SINCE and (not published_after or published_after < SINCE):
        published_after = SINCE
        note = f" Recency filter published>={SINCE} applied automatically."
    try:
        total, videos = S.yt_search(query, order, published_after, duration, language, limit)
    except S.SourceError as e:
        return f"Search failed: {e}"
    budget["yt_units"] += 102  # search.list 100 + videos.list 1 + channels.list 1
    rows = _remember(videos)
    n_new = sum(new for _, new in rows)
    head = (f'Found ~{total:,} videos for "{query}" (showing {len(rows)}, order={order}, duration={duration}, '
            f"{n_new} new; ~{budget['yt_units']} API quota units used this run).{note}")
    if not rows:
        return head + "\nNo results: try fewer or more common words, or drop the duration/language filter."
    return head + "\n" + "\n".join(_fmt_video(i, v, new) for i, (v, new) in enumerate(rows, 1))


def get_transcript(
    video_id: Annotated[str, Field(description="video id exactly as listed in search results")],
    keywords: Annotated[list[str] | None, Field(description=(
        "1-5 short terms from the request, e.g. [\"kv cache\", \"paged attention\"]. Passages that "
        "mention them are marked ★, and each term's hit count over the whole video is reported."))] = None,
    max_chars: Annotated[int, Field(ge=1000, le=8000)] = 5000,
) -> str:
    """Read a video's captions to check what it really covers and WHERE ([m:ss] timestamps).
    Shows the intro, the passages that match your keywords, and evenly spaced samples of the
    rest. Use it for candidates whose fit is unclear or to find the moment worth linking to."""
    k = _key(video_id)
    v = CANDS.get(k)
    if not v:
        return f"Transcript unavailable: {video_id} is not in the results yet; search for it first."
    budget["transcript"] += 1
    if budget["transcript"] > MAX_TRANSCRIPTS:
        return f"Transcript budget reached ({MAX_TRANSCRIPTS}). Decide from what you have and submit."
    try:
        lang, snips = S.yt_transcript(v["id"])
    except S.SourceError as e:
        return f"Transcript unavailable for {v['id']}: {e}."
    if not snips:
        return f"Transcript unavailable for {v['id']}: the captions are empty."
    CAPTION_STARTS[k] = [int(t) for t, _ in snips]
    CANDS[k] = S.merge(CANDS[k], {"transcript_read": True})
    _save()
    kws = [w.strip() for w in keywords or [] if w.strip()][:5]
    windows = S.transcript_windows(snips)
    idx, hits = S.pick_passages(windows, max_chars, kws)
    head = (f'Transcript of {v["id"]} "{_clip(v.get("title"), 80)}" ({lang}, captions run to '
            f"{S.hms(snips[-1][0])}; showing {len(idx)} of {len(windows)} passages)")
    if kws:
        full = " ".join(t for _, t in snips).lower()
        head += "; keyword hits: " + ", ".join(f"'{w}' {full.count(w.lower())}" for w in kws)
    out, prev = [head], None
    for i in idx:
        if prev is not None and i != prev + 1:
            out.append("…")
        start, text = windows[i]
        out.append(f"[{S.hms(start)}]{' ★' if i in hits else ''} {text}")
        prev = i
    return "\n".join(out)


def _moment(k: str, start: int) -> dict | None:
    """Ground a jump point in data Claude actually saw: snap it to a caption line of a
    transcript it read, or to a chapter within 20 s. None if neither backs it."""
    v = CANDS[k]
    chapters = v.get("chapters") or []
    if v.get("duration_s") and start >= v["duration_s"]:
        return None
    snap = None
    if k in CAPTION_STARTS:
        snap = max((t for t in CAPTION_STARTS[k] if t <= start + 2), default=None)
    elif chapters:
        near = min(chapters, key=lambda c: abs(c["start_s"] - start))
        snap = near["start_s"] if abs(near["start_s"] - start) <= 20 else None
    if snap is None:
        return None
    label = next((c["title"] for c in reversed(chapters) if c["start_s"] <= snap), None)
    return {"start_s": snap, "start_label": label}


# =========================================================================== Blog tools

def _remember_leads(leads: list[dict]) -> list[tuple[dict, bool]]:
    """Like _remember, but a page already read keeps its own title and the best-voted HN
    submission of a URL wins."""
    out = []
    for b in leads:
        k = _key(b["id"])
        old = CANDS.get(k)
        if old is None:
            CANDS[k] = b
        elif (old.get("hn_points") or 0) < b["hn_points"]:
            old.update({f: v for f, v in b.items() if f.startswith("hn_")})
        out.append((CANDS[k], old is None))
    _save()
    return out


def _fmt_lead(i: int, b: dict, new: bool) -> str:
    if not new:
        return f"[{i}] {b['url']} (already seen above)"
    return (f"[{i}] {b['url']}\n    \"{_clip(b.get('title'), 140)}\" · HN ▲{b.get('hn_points', 0)} · "
            f"{b.get('hn_comments', 0)} comments · posted to HN {(b.get('hn_date') or '')[:10]}")


def search_hn(
    query: Annotated[str, Field(description=(
        "2-4 words; Hacker News search matches submission titles and URLs, e.g. 'paged attention', "
        "'prompt caching', 'postgres vector search'"))],
    sort: Annotated[Literal["relevance", "date"], Field(description=(
        "relevance (favours well-voted stories) or date (newest first)"))] = "relevance",
    min_points: Annotated[int | None, Field(description="only stories with at least this many points, e.g. 10")] = None,
    limit: Annotated[int, Field(ge=5, le=50, description="stories to fetch (default 30)")] = 30,
) -> str:
    """Search Hacker News for blog posts and articles people submitted there. Community-vetted:
    points and comment counts show how much readers valued a post. Returns leads to open with
    read_articles (links to GitHub, arXiv, YouTube, social sites and PDFs are left out)."""
    if msg := _spend_search():
        return msg
    try:
        total, leads, skipped = S.hn_search(query, sort, SINCE, min_points, limit)
    except S.SourceError as e:
        return f"Search failed: {e}"
    rows = _remember_leads(leads)
    n_new = sum(new for _, new in rows)
    head = f'Found {total:,} Hacker News stories for "{query}" (showing {len(rows)} article links, sort={sort}, {n_new} new'
    if skipped:
        head += f"; {skipped} GitHub/arXiv/YouTube/social/PDF links left out"
    head += ")."
    if SINCE:
        head += f" Only stories posted since {SINCE}; read_articles checks each post's own date."
    if not rows:
        return head + "\nNo results: try fewer or more common words."
    return head + "\n" + "\n".join(_fmt_lead(i, b, new) for i, (b, new) in enumerate(rows, 1))


_QUOTE_MAP = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-", " ": " "})


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.translate(_QUOTE_MAP).lower()).strip()


def _chunks(text: str, size: int = 280) -> list[tuple[int, str]]:
    """Article paragraphs merged into passages of about `size` chars: [(index, text)]."""
    out, buf = [], ""
    for p in (x.strip() for x in text.split("\n")):
        if not p:
            continue
        buf = f"{buf} {p}" if buf else p
        if len(buf) >= size:
            out.append(_clip(buf, 700))
            buf = ""
    if buf:
        out.append(buf)
    return list(enumerate(out))


def _too_old(a: dict) -> bool:
    return bool(SINCE and a.get("published") and a["published"] < SINCE)


def _fmt_article(i: int, a: dict, text: str, kws: list[str], max_chars: int) -> str:
    title = _clip(a.get("title"), 160)
    if not a.get("english"):
        return f"[{i}] ✗ NOT ENGLISH ({a.get('lang')}) · {a['url']}\n    \"{title}\" cannot be submitted."
    if _too_old(a):
        return (f"[{i}] ✗ TOO OLD (published {a['published']}, before {SINCE}) · {a['url']}\n"
                f"    \"{title}\" cannot be submitted.")
    bits = [a.get("site") or a.get("host")]
    if a.get("author"):
        bits.append(f"by {_clip(a['author'], 60)}")
    bits.append(f"published {a['published']}" if a.get("published") else "date unknown")
    bits.append(f"{a['words']:,} words (~{a['read_min']} min)")
    if a.get("hn_points") is not None:
        bits.append(f"HN ▲{a['hn_points']}")
    if a.get("paywalled"):
        bits.append("PAYWALLED (text may be a preview)")
    if a.get("via"):
        bits.append(f"read via {a['via']}")
    lines = [f"[{i}] ✓ English · {a['url']}", f'    "{title}"', "    " + " · ".join(bits)]
    if a.get("original_url"):
        lines.append(f"    syndicated copy; the original is at {a['original_url']}")
    links = ([f"arXiv {p}" for p in a.get("paper_links", [])[:4]]
             + [u.removeprefix("https://") for u in a.get("code_links", [])[:4]]
             + [f"youtube {v}" for v in a.get("video_links", [])[:3]])
    if links:
        lines.append("    links: " + " · ".join(links))
    if a.get("outline"):
        lines.append("    outline: " + " · ".join(_clip(h, 60) for h in a["outline"][:12]))
    windows = _chunks(text)
    idx, hits = S.pick_passages(windows, max_chars, kws)
    if kws:
        low = text.lower()
        lines.append("    keyword hits: " + ", ".join(f"'{w}' {low.count(w.lower())}" for w in kws))
    lines.append("    text:")
    prev = None
    for j in idx:
        if prev is not None and j != prev + 1:
            lines.append("      …")
        lines.append(f"      {'★ ' if j in hits else ''}{windows[j][1]}")
        prev = j
    return "\n".join(lines)


def _read_one(url: str) -> tuple[str, dict | None, str, str | None]:
    try:
        art, text = S.fetch_article(url)
        return url, art, text, None
    except S.SourceError as e:
        return url, None, "", str(e)
    except Exception as e:  # a parser bug on one odd page must not sink the whole batch
        return url, None, "", f"could not parse the page ({type(e).__name__})"


def read_articles(
    urls: Annotated[list[str], Field(description=(
        "1-8 page URLs to open: from WebSearch results, search_hn leads, or links you saw"))],
    keywords: Annotated[list[str] | None, Field(description=(
        "1-5 short terms from the request, e.g. [\"prefix caching\", \"kv cache\"]. Passages that "
        "mention them are shown and marked ★, and each term's hit count is reported."))] = None,
    max_chars_each: Annotated[int, Field(ge=400, le=5000, description="text excerpt per article")] = 1200,
) -> str:
    """Open blog posts/articles and check them: whether they are written in English, title, site,
    author, publish date, length, HN points, outline (headings), arXiv/GitHub/YouTube links in the
    article, and an excerpt (intro, passages matching your keywords, samples of the rest).
    Only posts marked ✓ here can be submitted."""
    urls = list(dict.fromkeys(u.strip() for u in urls if u.strip()))
    note = ""
    if len(urls) > 8:
        note += f" Only the first 8 of {len(urls)} URLs were opened; pass the rest in another call."
        urls = urls[:8]
    fresh = [u for u in urls if not CANDS.get(_key(u), {}).get("read")]
    left = MAX_ARTICLE_READS - budget["read"]
    if fresh and left <= 0:
        return f"Read budget reached ({MAX_ARTICLE_READS} pages). Rank what you have read and submit."
    if len(fresh) > left:
        note += f" Page budget: only {left} more could be opened; the rest were skipped."
        skipped = set(fresh[left:])
        urls = [u for u in urls if u not in skipped]
        fresh = fresh[:left]
    budget["read"] += len(fresh)
    fetched = {u: (art, text, err) for u, art, text, err in S.parallel(_read_one, fresh)}
    kws = [w.strip() for w in keywords or [] if w.strip()][:5]
    out, tally = [], {"ok": 0, "lang": 0, "old": 0, "fail": 0}
    for i, u in enumerate(urls, 1):
        if u not in fetched:  # opened earlier in this run
            c = CANDS[_key(u)]
            out.append(f"[{i}] {c['url']} (already read above: "
                       f"{'✓ English' if c.get('english') and not _too_old(c) else '✗ cannot be submitted'})")
            continue
        art, text, err = fetched[u]
        if err:
            tally["fail"] += 1
            out.append(f"[{i}] ✗ UNREADABLE · {u}: {err}. Cannot be submitted.")
            continue
        k_req, k = S.canon_url(u), S.canon_url(art["url"])
        if k_req != k:  # redirected: carry over what an HN lead knew about the old URL
            ALIASES[k_req] = k
            if k_req in CANDS:
                CANDS[k] = S.merge(CANDS.pop(k_req), CANDS.get(k, {}))
        CANDS[k] = S.merge(CANDS.get(k, {}), art)
        ARTICLE_TEXT[k] = _norm(text)
        a = CANDS[k]
        tally["ok" if a["english"] and not _too_old(a) else "lang" if not a["english"] else "old"] += 1
        out.append(_fmt_article(i, a, text, kws, max_chars_each))
    _save()
    head = (f"Opened {len(fresh)} new pages: {tally['ok']} English ✓, {tally['lang']} not English, "
            f"{tally['old']} too old, {tally['fail']} unreadable")
    if len(urls) > len(fresh):
        head += f"; {len(urls) - len(fresh)} already read"
    head += f" ({budget['read']}/{MAX_ARTICLE_READS} page budget used).{note}"
    return head + "\n\n" + "\n\n".join(out)


def _verified_quote(k: str, quote: str) -> str | None:
    """The quote if it appears verbatim in the article text (case, quote marks, dashes and
    whitespace aside; '...' may join fragments that appear in order), else None."""
    q = " ".join(quote.split()).strip(" \"'“”‘’")
    text = ARTICLE_TEXT.get(k)
    if not text or not 15 <= len(q) <= 300:
        return None
    pos = 0
    for frag in re.split(r"\s*(?:\.\.\.|…)\s*", q):
        f = _norm(frag).strip(" \"'.,;:")
        if not f:
            continue
        at = text.find(f, pos)
        if at < 0:
            return None
        pos = at + len(f)
    return q


# =========================================================================== submit

class RankedItem(BaseModel):
    id: str = Field(description="exact id from search results: owner/repo or arXiv id like 2510.01234")
    score: int = Field(ge=0, le=100, description="combined relevance x usefulness, 0-100")
    reason: str = Field(description="1-2 concrete sentences: what it is and why it fits the request")


class VideoItem(RankedItem):
    id: str = Field(description="exact video id from search results, e.g. dQw4w9WgXcQ")
    start_s: int | None = Field(default=None, ge=0, description=(
        "optional: second where the part that matters for THIS request begins, taken from a transcript "
        "you read or the video's chapters. Omit when it is the beginning or you did not check."))


class BlogItem(RankedItem):
    id: str = Field(description="the post's URL exactly as read_articles listed it")
    quote: str | None = Field(default=None, description=(
        "one short sentence (under 200 characters) copied VERBATIM from the article text read_articles "
        "showed you: the one that best shows why this post fits the request. It is checked against the "
        "article; never paraphrase. Omit if no sentence fits."))


def _blog_problems(items: list[RankedItem]) -> list[str]:
    out = []
    for it in items:
        c = CANDS[_key(it.id)]
        if not c.get("read"):
            out.append(f"{it.id}: not opened yet (call read_articles on it first)")
        elif not c.get("english"):
            out.append(f"{it.id}: not written in English ({c.get('lang')})")
        elif _too_old(c):
            out.append(f"{it.id}: published {c['published']}, before {SINCE}")
    return out


def _submit(summary: str, items: list[RankedItem]) -> str:
    unknown = [it.id for it in items if _key(it.id) not in CANDS]
    if unknown:
        return ("ERROR: these ids never appeared in a search result this session: "
                + ", ".join(unknown) + ". Remove them (or search for them first) and call submit_results again.")
    if ARGS.kind == "blog" and (problems := _blog_problems(items)):
        return ("ERROR: these posts cannot be submitted:\n" + "\n".join(problems)
                + "\nRemove them (or open them with read_articles first) and call submit_results again.")
    seen, final, dropped, dropped_q, already = set(), [], 0, 0, 0
    old = _seen_keys()
    for it in items:
        k = _key(it.id)
        if k in seen:
            continue
        if k in old:
            already += 1
            continue
        seen.add(k)
        row = {"key": k, "id": CANDS[k]["id"], "score": it.score, "reason": it.reason.strip()}
        start = getattr(it, "start_s", None)
        if start is not None and start >= 15:  # no jump link for "from the beginning"
            if moment := _moment(k, start):
                row.update(moment)
            else:
                dropped += 1
        if quote := getattr(it, "quote", None):
            if checked := _verified_quote(k, quote):
                row["quote"] = checked
            else:
                dropped_q += 1
        final.append(row)
    note = ""
    if len(final) > ARGS.count:
        note = f" Trimmed to the requested {ARGS.count}."
        final = final[: ARGS.count]
    if dropped:
        note += f" Dropped {dropped} timestamp(s) not backed by a transcript you read or the chapters."
    if dropped_q:
        note += f" Dropped {dropped_q} quote(s) not found verbatim in the article text."
    if already:
        note += f" Skipped {already} result(s) the user had already seen."
    (RUN_DIR / "final.json").write_text(
        json.dumps({"summary": summary.strip(), "items": final}, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return f"Accepted {len(final)} results.{note} Reply with the single word: done"


_SUMMARY = Annotated[str, Field(description="2-4 sentences on the landscape: main approaches, standouts, gaps")]


def submit_results(summary: _SUMMARY, items: Annotated[list[RankedItem], Field(description="best first")]) -> str:
    """Submit the final ranked list. Call exactly once at the end (again only if it returns an error)."""
    return _submit(summary, items)


def submit_video_results(summary: _SUMMARY, items: Annotated[list[VideoItem], Field(description="best first")]) -> str:
    """Submit the final ranked list. Call exactly once at the end (again only if it returns an error)."""
    return _submit(summary, items)


def submit_blog_results(summary: _SUMMARY, items: Annotated[list[BlogItem], Field(description="best first")]) -> str:
    """Submit the final ranked list. Call exactly once at the end (again only if it returns an error).
    Every item must be a post read_articles marked ✓."""
    return _submit(summary, items)


TOOLS = {
    "github": [search_github, get_readme, submit_results],
    "arxiv": [search_arxiv, search_hf_papers, get_abstracts, submit_results],
    "youtube": [search_youtube, get_transcript, submit_video_results],
    "blog": [search_hn, read_articles, submit_blog_results],
}
for fn in TOOLS[ARGS.kind]:
    name = "submit_results" if fn in (submit_video_results, submit_blog_results) else fn.__name__
    mcp.tool(name=name, structured_output=False)(fn)

if __name__ == "__main__":
    mcp.run()
