"""Thin clients for GitHub, arXiv, Semantic Scholar, Hugging Face Papers, YouTube,
Hacker News and blog pages.

All metadata shown in the UI comes from these APIs (or the article page itself), never
from the model. Every function returns plain dicts ("candidates") keyed by a stable id:
  github -> "owner/repo"      arxiv -> "2510.01234" (no version suffix)
  youtube -> "dQw4w9WgXcQ" (11-char video id)      blog -> the page URL (see canon_url)
"""
from __future__ import annotations

import html
import re
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit

import httpx

from .config import YOUTUBE_API, github_token, youtube_api_key

UA = "git-arxiv-finder/1.0 (local research tool)"
GH_API = "https://api.github.com"
ARXIV_API = "https://export.arxiv.org/api/query"
S2_BATCH = "https://api.semanticscholar.org/graph/v1/paper/batch"
HF_API = "https://huggingface.co/api/papers"

GITHUB_URL_RE = re.compile(r"https?://(?:www\.)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)", re.I)
ARXIV_LINK_RE = re.compile(
    r"(?:arxiv\.org/(?:abs|pdf|html)/|huggingface\.co/papers/|arXiv:\s?)(\d{4}\.\d{4,5})(?:v\d+)?", re.I
)
_NOT_REPO_OWNERS = {"orgs", "features", "topics", "sponsors", "marketplace", "settings", "apps", "about", "collections"}


class SourceError(RuntimeError):
    """A source failed in a way the model should hear about (rate limit, bad query...)."""


def client(timeout: float = 25) -> httpx.Client:
    return httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": UA})


# --------------------------------------------------------------------------- helpers

def parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def ago(s: str | None) -> str:
    dt = parse_dt(s)
    if not dt:
        return "?"
    days = (datetime.now(timezone.utc) - dt).days
    if days < 1:
        return "today"
    if days < 60:
        return f"{days}d ago"
    if days < 730:
        return f"{days // 30}mo ago"
    return f"{days // 365}y ago"


def human(n: int | None) -> str:
    if n is None:
        return "?"
    if n >= 1_000_000:
        return f"{n / 1e6:.1f}M".replace(".0M", "M")
    if n >= 1000:
        return f"{n / 1000:.1f}k".replace(".0k", "k")
    return str(n)


def github_links(text: str | None) -> list[str]:
    out: list[str] = []
    for owner, repo in GITHUB_URL_RE.findall(text or ""):
        repo = re.sub(r"(\.git|[.,;:)\]]+)$", "", repo)
        if owner.lower() in _NOT_REPO_OWNERS or not repo:
            continue
        url = f"https://github.com/{owner}/{repo}"
        if url.lower() not in (u.lower() for u in out):
            out.append(url)
    return out


def arxiv_ids_in(text: str | None) -> list[str]:
    seen: list[str] = []
    for i in ARXIV_LINK_RE.findall(text or ""):
        if i not in seen:
            seen.append(i)
    return seen


def norm_arxiv_id(s: str) -> str:
    s = s.strip()
    m = re.search(r"(\d{4}\.\d{4,5})", s)
    return m.group(1) if m else re.sub(r"v\d+$", "", s.removeprefix("arXiv:"))


def merge(old: dict, new: dict) -> dict:
    """Merge candidate dicts, never overwriting known values with empties."""
    for k, v in new.items():
        if isinstance(v, list) and isinstance(old.get(k), list):
            old[k] = old[k] + [x for x in v if x not in old[k]]
        elif v not in (None, "", [], {}) or k not in old:
            old[k] = v
    return old


# --------------------------------------------------------------------------- GitHub

def _gh_headers(raw: bool = False) -> dict:
    h = {
        "Accept": "application/vnd.github.raw+json" if raw else "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    tok = github_token()
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


def _gh_error(r: httpx.Response) -> SourceError:
    try:
        msg = r.json().get("message", "")
    except Exception:
        msg = r.text[:200]
    if r.status_code in (403, 429):
        reset = r.headers.get("x-ratelimit-reset")
        wait = max(0, int(reset) - int(time.time())) if reset and reset.isdigit() else "?"
        return SourceError(f"GitHub rate limit ({r.status_code}): {msg}. Resets in ~{wait}s.")
    return SourceError(f"GitHub error {r.status_code}: {msg}")


def repo_from_api(it: dict) -> dict:
    created = parse_dt(it.get("created_at"))
    months = max(1.0, (datetime.now(timezone.utc) - created).days / 30.44) if created else None
    stars = it.get("stargazers_count") or 0
    return {
        "kind": "github",
        "id": it["full_name"],
        "url": it.get("html_url"),
        "owner": (it.get("owner") or {}).get("login"),
        "avatar": (it.get("owner") or {}).get("avatar_url"),
        "description": (it.get("description") or "").strip(),
        "stars": stars,
        "forks": it.get("forks_count"),
        "open_issues": it.get("open_issues_count"),
        "language": it.get("language"),
        "topics": it.get("topics") or [],
        "license": ((it.get("license") or {}).get("spdx_id") or None),
        "created_at": it.get("created_at"),
        "pushed_at": it.get("pushed_at"),
        "archived": it.get("archived", False),
        "is_fork": it.get("fork", False),
        "homepage": it.get("homepage") or None,
        "stars_per_month": round(stars / months) if months else None,
    }


def gh_search(
    query: str,
    sort: str = "best-match",
    pushed_after: str | None = None,
    created_after: str | None = None,
    min_stars: int | None = None,
    language: str | None = None,
    limit: int = 30,
) -> tuple[int, list[dict], str]:
    quals = []
    if pushed_after:
        quals.append(f"pushed:>={pushed_after}")
    if created_after:
        quals.append(f"created:>={created_after}")
    if min_stars:
        quals.append(f"stars:>={min_stars}")
    if language:
        quals.append(f'language:"{language}"' if " " in language else f"language:{language}")
    if "archived:" not in query:
        quals.append("archived:false")
    q = " ".join([query.strip(), *quals])
    params: dict = {"q": q, "per_page": max(1, min(int(limit), 50))}
    if sort in ("stars", "updated", "forks"):
        params.update(sort=sort, order="desc")
    with client() as c:
        r = c.get(f"{GH_API}/search/repositories", params=params, headers=_gh_headers())
    if r.status_code != 200:
        raise _gh_error(r)
    data = r.json()
    return data.get("total_count", 0), [repo_from_api(it) for it in data.get("items", [])], q


_BADGE_RE = re.compile(r"\[!\[[^\]]*\]\([^)]*\)\]\([^)]*\)|!\[[^\]]*\]\([^)]*\)")
_TAG_RE = re.compile(r"<[^>]+>")


def clean_readme(text: str) -> str:
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = _BADGE_RE.sub("", text)
    text = _TAG_RE.sub("", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def gh_readme(full_name: str) -> str:
    """Raw README text (uncleaned) or raises SourceError."""
    with client() as c:
        r = c.get(f"{GH_API}/repos/{full_name}/readme", headers=_gh_headers(raw=True))
    if r.status_code == 404:
        raise SourceError(f"{full_name} has no README (or does not exist).")
    if r.status_code != 200:
        raise _gh_error(r)
    return r.text


# --------------------------------------------------------------------------- arXiv

_NS = {
    "a": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
    "os": "http://a9.com/-/spec/opensearch/1.1/",
}
_arxiv_lock = threading.Lock()
_arxiv_last = 0.0


def _arxiv_get(params: dict) -> str:
    """GET with arXiv's requested 3 s spacing between calls, one retry on 5xx/timeouts."""
    global _arxiv_last
    with _arxiv_lock:
        wait = 3.0 - (time.monotonic() - _arxiv_last)
        if wait > 0:
            time.sleep(wait)
        last_err = None
        for attempt in range(2):
            try:
                with client(timeout=40) as c:
                    r = c.get(ARXIV_API, params=params)
                _arxiv_last = time.monotonic()
                if r.status_code == 200:
                    return r.text
                last_err = SourceError(f"arXiv HTTP {r.status_code}")
            except httpx.HTTPError as e:
                _arxiv_last = time.monotonic()
                last_err = SourceError(f"arXiv request failed: {e}")
            time.sleep(3)
        raise last_err  # type: ignore[misc]


def _text(el, path: str) -> str:
    x = el.find(path, _NS)
    return " ".join((x.text or "").split()) if x is not None and x.text else ""


def parse_arxiv_feed(xml_text: str) -> tuple[int, list[dict]]:
    root = ET.fromstring(xml_text)
    total = int(_text(root, "os:totalResults") or 0)
    papers = []
    for e in root.findall("a:entry", _NS):
        raw_id = _text(e, "a:id")
        if "/api/errors" in raw_id:
            raise SourceError(f"arXiv rejected the query: {_text(e, 'a:summary')}")
        pid = norm_arxiv_id(raw_id.rsplit("/abs/", 1)[-1])
        abstract = _text(e, "a:summary")
        comment = _text(e, "arxiv:comment")
        pdf = next(
            (l.get("href") for l in e.findall("a:link", _NS) if l.get("title") == "pdf"),
            f"https://arxiv.org/pdf/{pid}",
        )
        prim = e.find("arxiv:primary_category", _NS)
        papers.append({
            "kind": "arxiv",
            "id": pid,
            "url": f"https://arxiv.org/abs/{pid}",
            "pdf": pdf,
            "title": _text(e, "a:title"),
            "authors": [_text(a, "a:name") for a in e.findall("a:author", _NS)],
            "abstract": abstract,
            "published": _text(e, "a:published"),
            "updated": _text(e, "a:updated"),
            "primary_category": prim.get("term") if prim is not None else None,
            "categories": [c.get("term") for c in e.findall("a:category", _NS)],
            "comment": comment or None,
            "journal_ref": _text(e, "arxiv:journal_ref") or None,
            "code_links": github_links(abstract + " " + comment),
            "sources": ["arxiv"],
        })
    return total, papers


_ARX_TOK = re.compile(r'\(|\)|(?:[A-Za-z_]+:)?"[^"]*"|[^\s()"]+')
_ARX_OPS = {"AND", "OR", "ANDNOT"}


def arxiv_and(query: str) -> str:
    """arXiv ORs words that have no operator between them: in a month of cs.CL+cs.AI,
    abs:"test-time scaling" matched 29 papers, abs:"test-time scaling" reasoning 1,745, and
    abs:"test-time scaling" AND abs:reasoning 13. People (and models) mean AND, so join such
    neighbours with AND, keeping an explicit OR-chain together: a OR b c -> (a OR b) AND c."""
    toks = _ARX_TOK.findall(query)
    if toks.count("(") != toks.count(")"):
        return query.strip()
    pos = 0

    def group() -> str:
        nonlocal pos
        items = []
        while pos < len(toks):
            t = toks[pos]
            pos += 1
            if t == "(":
                items.append("(" + group() + ")")
            elif t == ")":
                break
            else:
                items.append(t)
        segs = [[]]
        for t in items:
            if segs[-1] and segs[-1][-1] not in _ARX_OPS and t not in _ARX_OPS:
                segs.append([])  # two operands side by side: an implicit AND
            segs[-1].append(t)
        if len(segs) == 1:
            return " ".join(segs[0])
        return " AND ".join(f"({' '.join(s)})" if len(s) > 1 else s[0] for s in segs)

    return group()


def arxiv_search(
    query: str,
    categories: list[str] | None = None,
    submitted_after: str | None = None,
    submitted_before: str | None = None,
    sort: str = "relevance",
    limit: int = 30,
) -> tuple[int, list[dict], str]:
    parts = [f"({arxiv_and(query)})"]
    cats = [c.strip() for c in (categories or []) if c.strip()]
    if cats:
        parts.append("(" + " OR ".join(f"cat:{c}" for c in cats) + ")")
    if submitted_after or submitted_before:
        a = (submitted_after or "1991-01-01").replace("-", "") + "0000"
        b = (submitted_before or date.today().isoformat()).replace("-", "") + "2359"
        parts.append(f"submittedDate:[{a} TO {b}]")
    sq = " AND ".join(parts)
    params = {
        "search_query": sq,
        "start": 0,
        "max_results": max(1, min(int(limit), 50)),
        "sortBy": "submittedDate" if sort == "date" else "relevance",
        "sortOrder": "descending",
    }
    total, papers = parse_arxiv_feed(_arxiv_get(params))
    return total, papers, sq


def arxiv_by_ids(ids: list[str]) -> list[dict]:
    if not ids:
        return []
    _, papers = parse_arxiv_feed(_arxiv_get({"id_list": ",".join(ids), "max_results": len(ids)}))
    return papers


# --------------------------------------------------------------------------- Semantic Scholar

def s2_citations(arxiv_ids: list[str]) -> dict[str, dict]:
    """Best effort: {arxiv_id: {citations, influential, venue}}. Empty dict on failure."""
    ids = list(dict.fromkeys(arxiv_ids))[:400]
    if not ids:
        return {}
    for attempt in range(2):
        try:
            with client(timeout=12) as c:
                r = c.post(
                    S2_BATCH,
                    params={"fields": "citationCount,influentialCitationCount,venue"},
                    json={"ids": [f"arXiv:{i}" for i in ids]},
                )
            if r.status_code == 429 and attempt == 0:
                time.sleep(2)
                continue
            if r.status_code != 200:
                return {}
            out = {}
            for pid, row in zip(ids, r.json()):
                if row:
                    venue = row.get("venue") or None
                    out[pid] = {
                        "citations": row.get("citationCount"),
                        "influential": row.get("influentialCitationCount"),
                        "venue": None if (venue or "").lower() in ("arxiv.org", "arxiv") else venue,
                    }
            return out
        except (httpx.HTTPError, ValueError):
            return {}
    return {}


# --------------------------------------------------------------------------- Hugging Face Papers

def _hf_fields(p: dict) -> dict:
    return {
        "hf_upvotes": p.get("upvotes"),
        "hf_github": p.get("githubRepo") or None,
        "hf_github_stars": p.get("githubStars"),
        "ai_summary": p.get("ai_summary") or None,
        "ai_keywords": (p.get("ai_keywords") or [])[:8],
    }


def hf_search(query: str, limit: int = 20) -> list[dict]:
    """Semantic search over Hugging Face Papers (ML-heavy). Returns arXiv candidates."""
    with client(timeout=20) as c:
        r = c.get(f"{HF_API}/search", params={"q": query, "limit": max(1, min(int(limit), 50))})
    if r.status_code != 200:
        raise SourceError(f"Hugging Face search HTTP {r.status_code}")
    out = []
    for it in r.json()[:limit]:
        p = it.get("paper") or it
        pid = norm_arxiv_id(p.get("id", ""))
        if not re.fullmatch(r"\d{4}\.\d{4,5}", pid):
            continue
        abstract = " ".join((p.get("summary") or "").split())
        out.append({
            "kind": "arxiv",
            "id": pid,
            "url": f"https://arxiv.org/abs/{pid}",
            "pdf": f"https://arxiv.org/pdf/{pid}",
            "title": " ".join((p.get("title") or "").split()),
            "authors": [a.get("name") for a in p.get("authors") or [] if a.get("name")],
            "abstract": abstract,
            "published": p.get("publishedAt"),
            "code_links": github_links(abstract),
            "sources": ["hf"],
            **_hf_fields(p),
        })
    return out


def hf_paper(arxiv_id: str) -> dict | None:
    try:
        with client(timeout=10) as c:
            r = c.get(f"{HF_API}/{arxiv_id}")
        return _hf_fields(r.json()) if r.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None


# --------------------------------------------------------------------------- YouTube

YT_ID_RE = re.compile(r"(?:v=|youtu\.be/|shorts/|embed/|live/)([A-Za-z0-9_-]{11})")
_ISO_DUR_RE = re.compile(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?")
_CHAPTER_RE = re.compile(
    r"^[ \t\-•*]*[\[(]?((?:\d{1,2}:)?\d{1,2}:\d{2})[\])]?[ \t]*[-–—:|]?[ \t]*(\S[^\n]{0,99}?)[ \t]*$", re.M
)
_URL_RE = re.compile(r"https?://\S+")


def norm_video_id(s: str) -> str:
    m = YT_ID_RE.search(s)
    return m.group(1) if m else s.strip()


def hms(sec: float | int | None) -> str:
    if sec is None:
        return "?"
    h, rest = divmod(int(sec), 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _hms_to_s(t: str) -> int:
    total = 0
    for part in t.split(":"):
        total = total * 60 + int(part)
    return total


def iso_duration_s(s: str | None) -> int | None:
    """'PT1H2M3S' -> 3723. Live streams and premieres report P0D (0)."""
    m = _ISO_DUR_RE.fullmatch(s or "")
    if not s or not m:
        return None
    d, h, mi, sec = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + sec


def chapters_in(description: str, duration_s: int | None) -> list[dict]:
    """Timestamped lines in a description (YouTube chapters or a hand-written index)."""
    out: list[dict] = []
    for t, title in _CHAPTER_RE.findall(description or ""):
        start = _hms_to_s(t)
        if (duration_s and start >= duration_s) or (out and start <= out[-1]["start_s"]):
            continue
        out.append({"start_s": start, "title": title.strip(" -–—:|")})
    return out if len(out) >= 3 else []


def desc_snippet(text: str | None) -> str:
    """Description without URLs and chapter lines (both are listed separately)."""
    return _CHAPTER_RE.sub("", _URL_RE.sub("", text or ""))


def _int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _yt_get(path: str, params: dict) -> dict:
    key = youtube_api_key()
    if not key:
        raise SourceError("No YouTube API key. Add a YOUTUBE_API_KEY= line to env.txt.txt (see README).")
    try:  # key in a header, not the URL, so request logs never show it
        with client() as c:
            r = c.get(f"{YOUTUBE_API}/{path}", params=params, headers={"X-Goog-Api-Key": key})
    except httpx.HTTPError as e:
        raise SourceError(f"YouTube request failed: {type(e).__name__}") from None
    if r.status_code == 200:
        return r.json()
    try:
        err = r.json().get("error") or {}
        reason = (err.get("errors") or [{}])[0].get("reason") or err.get("status") or ""
        msg = _TAG_RE.sub("", err.get("message") or "")
    except (ValueError, AttributeError):
        reason, msg = "", r.text[:200]
    if reason in ("quotaExceeded", "dailyLimitExceeded"):
        raise SourceError("YouTube API daily quota is used up (it resets at midnight Pacific time).")
    if reason in ("keyInvalid", "API_KEY_INVALID") or "API key not valid" in msg:
        raise SourceError("The YouTube API key was rejected. Check YOUTUBE_API_KEY in env.txt.txt.")
    if reason in ("accessNotConfigured", "SERVICE_DISABLED"):
        raise SourceError("YouTube Data API v3 is not enabled in this key's Google Cloud project.")
    raise SourceError(f"YouTube API error {r.status_code} {reason}: {msg[:200]}")


def video_from_api(it: dict, chan: dict | None = None) -> dict:
    sn, cd, st = it.get("snippet") or {}, it.get("contentDetails") or {}, it.get("statistics") or {}
    chan = chan or {}
    vid = it["id"]
    desc = (sn.get("description") or "").strip()
    dur = iso_duration_s(cd.get("duration"))
    views = _int(st.get("viewCount"))
    pub = parse_dt(sn.get("publishedAt"))
    days = max(1.0, (datetime.now(timezone.utc) - pub).total_seconds() / 86400) if pub else None
    thumbs = sn.get("thumbnails") or {}
    live = sn.get("liveBroadcastContent")
    return {
        "kind": "youtube",
        "id": vid,
        "url": f"https://www.youtube.com/watch?v={vid}",
        "title": html.unescape(sn.get("title") or ""),
        "channel": sn.get("channelTitle"),
        "channel_url": f"https://www.youtube.com/channel/{sn['channelId']}" if sn.get("channelId") else None,
        "subscribers": None if chan.get("hiddenSubscriberCount") else _int(chan.get("subscriberCount")),
        "description": desc[:3000],
        "published": sn.get("publishedAt"),
        "duration_s": dur,
        "views": views,
        "likes": _int(st.get("likeCount")),  # missing when the uploader hides likes
        "comments": _int(st.get("commentCount")),
        "views_per_day": round(views / days) if views is not None and days else None,
        "thumbnail": (thumbs.get("medium") or thumbs.get("high") or thumbs.get("default") or {}).get("url"),
        "tags": (sn.get("tags") or [])[:12],
        "audio_language": sn.get("defaultAudioLanguage") or sn.get("defaultLanguage"),
        "captions": cd.get("caption") == "true",  # uploader-made captions; auto captions don't count
        "live": live if live in ("live", "upcoming") else None,
        "chapters": chapters_in(desc, dur),
        "paper_links": arxiv_ids_in(desc),
        "code_links": github_links(desc),
    }


def yt_videos(ids: list[str]) -> list[dict]:
    """Full metadata (stats, duration, full description, channel subscribers) in the given order."""
    ids = list(dict.fromkeys(ids))[:50]
    if not ids:
        return []
    data = _yt_get("videos", {"part": "snippet,contentDetails,statistics", "id": ",".join(ids), "maxResults": 50})
    by_id = {it["id"]: it for it in data.get("items", [])}
    chan_of = {vid: (it.get("snippet") or {}).get("channelId") for vid, it in by_id.items()}
    chan_ids = [c for c in dict.fromkeys(chan_of.values()) if c]
    chans: dict[str, dict] = {}
    if chan_ids:
        try:
            cdata = _yt_get("channels", {"part": "statistics", "id": ",".join(chan_ids), "maxResults": 50})
            chans = {c["id"]: c.get("statistics") or {} for c in cdata.get("items", [])}
        except SourceError:
            pass  # subscriber counts are a nice-to-have
    return [video_from_api(by_id[i], chans.get(chan_of[i])) for i in ids if i in by_id]


def yt_search(
    query: str,
    order: str = "relevance",
    published_after: str | None = None,
    duration: str = "any",
    language: str | None = None,
    limit: int = 20,
) -> tuple[int, list[dict]]:
    """search.list (100 quota units) for ids, then yt_videos (2 units) for the real metadata."""
    params: dict = {"part": "snippet", "type": "video", "q": query.strip(),
                    "maxResults": max(1, min(int(limit), 50)),
                    "order": order if order in ("relevance", "viewCount", "date") else "relevance"}
    if published_after:
        params["publishedAfter"] = f"{published_after}T00:00:00Z"
    if duration in ("short", "medium", "long"):
        params["videoDuration"] = duration
    if language:
        params["relevanceLanguage"] = language
    data = _yt_get("search", params)
    ids = [(it.get("id") or {}).get("videoId") for it in data.get("items", [])]
    return (data.get("pageInfo") or {}).get("totalResults", 0), yt_videos([i for i in ids if i])


_TRANSCRIPT_ERRORS = {
    "TranscriptsDisabled": "captions are turned off for this video",
    "NoTranscriptFound": "this video has no captions",
    "VideoUnavailable": "the video is unavailable",
    "VideoUnplayable": "the video is unplayable (private, region-locked or removed)",
    "AgeRestricted": "the video is age-restricted",
    "IpBlocked": "YouTube is blocking caption requests from this IP for now",
    "RequestBlocked": "YouTube is blocking caption requests from this IP for now",
    "PoTokenRequired": "YouTube requires a PO token for this video's captions",
}


def yt_transcript(video_id: str) -> tuple[str, list[tuple[float, str]]]:
    """(language label, [(start_s, text)]) from the video's captions: English when it exists
    (uploader-made before auto-generated), else the original language. Needs no API key."""
    from youtube_transcript_api import NoTranscriptFound, YouTubeTranscriptApi, YouTubeTranscriptApiException
    try:
        tl = YouTubeTranscriptApi().list(video_id)
        try:
            tr = tl.find_transcript(["en", "en-US", "en-GB"])
        except NoTranscriptFound:
            tr = next(iter(tl), None)
            if tr is None:
                raise
        fetched = tr.fetch()
    except YouTubeTranscriptApiException as e:
        name = type(e).__name__
        raise SourceError(_TRANSCRIPT_ERRORS.get(name, f"captions unavailable ({name})")) from None
    except Exception as e:  # network errors and the like
        raise SourceError(f"captions unavailable ({type(e).__name__})") from None
    snips = [(float(sn.start), " ".join(sn.text.split())) for sn in fetched.snippets if sn.text.strip()]
    return tr.language, snips


def transcript_windows(snips: list[tuple[float, str]], window_s: int = 20) -> list[tuple[int, str]]:
    """Merge caption lines into passages of about window_s seconds: [(start_s, text)]."""
    out: list[tuple[int, str]] = []
    start, buf = None, []
    for t, text in snips:
        if start is None:
            start = t
        elif t - start >= window_s:
            out.append((int(start), " ".join(buf)))
            start, buf = t, []
        buf.append(text)
    if buf:
        out.append((int(start or 0), " ".join(buf)))
    return out


def pick_passages(windows: list[tuple[int, str]], max_chars: int, keywords: list[str]) -> tuple[list[int], set[int]]:
    """Indices of the passages to show (in time order) and the keyword-hit subset.

    The intro comes first (it usually says what the video covers), then the passages with
    the most keyword hits (up to ~60% of the budget), then evenly spaced samples so the whole
    arc of the video stays visible."""
    cost = [len(t) + 10 for _, t in windows]
    if sum(cost) <= max_chars:
        kws = [k.lower() for k in keywords if k.strip()]
        return list(range(len(windows))), {i for i, (_, t) in enumerate(windows) if any(k in t.lower() for k in kws)}
    chosen: set[int] = set()
    left = max_chars

    def take(i: int) -> bool:
        nonlocal left
        if i in chosen or cost[i] > left:
            return False
        chosen.add(i)
        left -= cost[i]
        return True

    for i in range(min(2, len(windows))):
        take(i)
    hits: set[int] = set()
    kws = [k.lower() for k in keywords if k.strip()]
    if kws:
        stop_at = left - int(max_chars * 0.6)
        ranked = sorted(((sum(t.lower().count(k) for k in kws), i) for i, (_, t) in enumerate(windows)), reverse=True)
        for n, i in ranked:
            if n == 0 or left <= stop_at:
                break
            if i in chosen or take(i):
                hits.add(i)
    rest = [i for i in range(len(windows)) if i not in chosen]
    if rest and left > 0:
        k = max(1, int(left // (sum(cost[i] for i in rest) / len(rest))))
        for j in range(k):
            take(rest[min(len(rest) - 1, int((j + 0.5) * len(rest) / k))])
    return sorted(chosen), hits


# --------------------------------------------------------------------------- Blogs: URLs + Hacker News

HN_API = "https://hn.algolia.com/api/v1"
# Not blog posts, or covered by Scout's other tabs.
NOT_BLOG_HOSTS = {"github.com", "gist.github.com", "arxiv.org", "youtube.com", "m.youtube.com", "youtu.be",
                  "twitter.com", "x.com", "reddit.com", "old.reddit.com", "news.ycombinator.com",
                  "en.wikipedia.org", "linkedin.com", "stackoverflow.com", "quora.com"}
_TRACKING = re.compile(r"^(utm_\w+|ref|ref_src|ref_url|source|sk|gi|r|fbclid|gclid|mc_cid|mc_eid|_hsenc|_hsmi|"
                       r"triedredirect|publication_id|post_id|share)$", re.I)


def url_host(url: str) -> str:
    return (urlsplit(url.strip()).hostname or "").lower().removeprefix("www.")


def canon_url(url: str) -> str:
    """Comparable key for a page: no scheme, www., fragment, tracking params or trailing slash."""
    p = urlsplit(url.strip())
    host = url_host(url)
    if p.port and p.port not in (80, 443):
        host += f":{p.port}"
    path = re.sub(r"/(index\.html?)?$", "", re.sub(r"/{2,}", "/", p.path))
    query = urlencode([(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not _TRACKING.match(k)])
    return host + path + (f"?{query}" if query else "")


def clean_url(url: str) -> str:
    """The URL to show and link to: no fragment or tracking params."""
    p = urlsplit(url.strip())
    query = urlencode([(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not _TRACKING.match(k)])
    return p._replace(query=query, fragment="").geturl()


def is_blog_host(url: str) -> bool:
    return url_host(url) not in NOT_BLOG_HOSTS and not urlsplit(url).path.lower().endswith(".pdf")


def _hn_get(path: str, params: dict) -> dict:
    try:
        with client(timeout=15) as c:
            r = c.get(f"{HN_API}/{path}", params=params)
    except httpx.HTTPError as e:
        raise SourceError(f"Hacker News search failed: {type(e).__name__}") from None
    if r.status_code != 200:
        raise SourceError(f"Hacker News search HTTP {r.status_code}")
    return r.json()


def _hn_fields(h: dict) -> dict:
    return {"hn_id": h["objectID"], "hn_points": h.get("points") or 0, "hn_comments": h.get("num_comments") or 0,
            "hn_date": h.get("created_at"), "hn_url": f"https://news.ycombinator.com/item?id={h['objectID']}"}


def hn_search(query: str, sort: str = "relevance", since: str | None = None, min_points: int | None = None,
              limit: int = 30) -> tuple[int, list[dict], int]:
    """Hacker News stories that link somewhere (not Ask HN). Returns (total, leads, n_skipped):
    leads are blog candidates with HN points/comments, best-scoring submission per URL;
    n_skipped counts links to GitHub/arXiv/YouTube/social sites and PDFs."""
    nf = []
    if since:
        nf.append(f"created_at_i>={int(datetime.fromisoformat(since).replace(tzinfo=timezone.utc).timestamp())}")
    if min_points:
        nf.append(f"points>={int(min_points)}")
    params = {"query": query.strip(), "tags": "story", "hitsPerPage": max(1, min(int(limit), 50))}
    if nf:
        params["numericFilters"] = ",".join(nf)
    data = _hn_get("search_by_date" if sort == "date" else "search", params)
    by_key: dict[str, dict] = {}
    skipped = 0
    for h in data.get("hits", []):
        url = h.get("url")
        if not url:
            continue
        if not is_blog_host(url):
            skipped += 1
            continue
        k = canon_url(url)
        if k in by_key and by_key[k]["hn_points"] >= (h.get("points") or 0):
            continue
        by_key[k] = {"kind": "blog", "id": url, "url": url, "title": h.get("title") or "", "host": url_host(url),
                     **_hn_fields(h)}
    return data.get("nbHits", 0), list(by_key.values()), skipped


def hn_lookup(url: str) -> dict | None:
    """Best-scoring Hacker News submission of exactly this URL (+ how many times it was posted)."""
    want = canon_url(url)
    try:
        data = _hn_get("search", {"query": want, "restrictSearchableAttributes": "url", "tags": "story",
                                  "hitsPerPage": 30})
    except SourceError:
        return None
    hits = [h for h in data.get("hits", []) if h.get("url") and canon_url(h["url"]) == want]
    if not hits:
        return None
    best = max(hits, key=lambda h: h.get("points") or 0)
    return {**_hn_fields(best), "hn_posts": len(hits)}


# --------------------------------------------------------------------------- Blogs: language check

_STOPWORDS = {lang: set(words.split()) for lang, words in {
    "English": "the and of to that it for with this be by was you we not have from or but can which they will "
               "an at if our more has how what when there their also these would about into than its been were "
               "other all only like just then them does should could each because while where your is are",
    "German": "der die und den das ist nicht mit sich des auf für ein eine auch dem von wird werden sind oder wir "
              "bei nach wie aus zu noch kann über dass einer einen durch sie es im um aber",
    "French": "le la les des du et est une que qui dans pour pas sur avec sont ce cette il nous vous par plus au "
              "aux mais ou être ont ces été leur comme peut",
    "Spanish": "el los las del que y en se por con para una es al lo como más pero sus ya este porque esta entre "
               "cuando muy sin sobre también hay donde puede son ser",
    "Portuguese": "os da do das dos que em um uma para com não por mais se na no nas nos como mas ao ele isso são "
                  "foi pelo pela também você está quando",
    "Italian": "il gli di che è per un una del della dei delle non sono con si nel nella anche come più ma questo "
               "questa alla al degli essere ha può",
    "Dutch": "het een en van is dat op te zijn voor met niet aan er ook als bij maar om dan wordt worden kan naar "
             "deze wij je ze hebben nog uit door over dit",
}.items()}
_SCRIPTS = [  # (name, first, last) code point ranges for non-Latin text
    ("Korean", 0xAC00, 0xD7AF), ("Korean", 0x1100, 0x11FF), ("Korean", 0x3130, 0x318F),
    ("Japanese", 0x3040, 0x30FF), ("Chinese/Japanese", 0x4E00, 0x9FFF), ("Cyrillic", 0x0400, 0x04FF),
    ("Greek", 0x0370, 0x03FF), ("Arabic", 0x0600, 0x06FF), ("Hebrew", 0x0590, 0x05FF),
    ("Devanagari", 0x0900, 0x097F), ("Thai", 0x0E00, 0x0E7F),
]


def detect_language(text: str) -> tuple[str, bool]:
    """(language name, is_english) from the article text itself, not the page's lang attribute
    (templates often hard-code lang="en"). Non-Latin scripts are caught by script; Latin-script
    languages by how often each language's function words occur."""
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 80:
        return "too little text to tell", False
    non_latin = [c for c in letters if ord(c) >= 0x250]
    if len(non_latin) / len(letters) > 0.3:
        counts: dict[str, int] = {}
        for c in non_latin:
            o = ord(c)
            name = next((n for n, a, b in _SCRIPTS if a <= o <= b), "non-Latin script")
            counts[name] = counts.get(name, 0) + 1
        top = max(counts, key=counts.get)
        if top == "Chinese/Japanese" and counts.get("Japanese", 0) > 0.05 * len(non_latin):
            top = "Japanese"
        return ("Chinese" if top == "Chinese/Japanese" else top), False
    words = re.findall(r"[a-zà-öø-ÿ]+", text.lower())
    hits = {lang: sum(w in sw for w in words) for lang, sw in _STOPWORDS.items()}
    en = hits.pop("English")
    other, n_other = max(hits.items(), key=lambda kv: kv[1])
    n = max(1, len(words))
    if en / n >= 0.08 and en >= 2 * n_other:
        return "English", True
    if n_other > en and n_other / n >= 0.08:
        return other, False
    return "not English (language not recognised)", False


# --------------------------------------------------------------------------- Blogs: reading pages

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/140.0.0.0 Safari/537.36 git-arxiv-finder/1.0")
_PAGE_HEADERS = {"User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.9",
                 "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
MAX_PAGE_BYTES = 4_000_000
_BLOCKED = (401, 403, 429, 503)
_PAYWALL_RE = re.compile(r'"isAccessibleForFree"\s*:\s*"?false|Member-only story|"audience"\s*:\s*"only_paid"', re.I)
_EMBED_RE = re.compile(r"(?:youtube(?:-nocookie)?\.com/embed/|youtu\.be/)([A-Za-z0-9_-]{11})")
_MD_LINK_RE = re.compile(r"\]\((https?://[^)\s]+)\)")
_RSS_NS = {"content": "http://purl.org/rss/1.0/modules/content/", "dc": "http://purl.org/dc/elements/1.1/",
           "atom": "http://www.w3.org/2005/Atom"}


def _get_capped(c: httpx.Client, url: str) -> tuple[httpx.Response, bytes]:
    with c.stream("GET", url, headers=_PAGE_HEADERS) as r:
        body = b""
        for chunk in r.iter_bytes():
            body += chunk
            if len(body) > MAX_PAGE_BYTES:
                break
    return r, body


def _feed_urls(url: str) -> list[str]:
    """Feeds that may carry this post: Medium's per-author/publication feed, else the site's /feed."""
    p = urlsplit(url)
    origin = f"{p.scheme}://{p.netloc}"
    if url_host(url) == "medium.com":  # medium.com/@user/post or medium.com/publication/post
        first = next((x for x in p.path.split("/") if x), "")
        return [f"https://medium.com/feed/{first}"] if first else []
    return [f"{origin}/feed"]


def _from_feed(c: httpx.Client, url: str) -> dict | None:
    """The post's entry in its site's RSS/Atom feed: {url, title, author, date, html} or None.
    Used when the page itself blocks automated readers (Medium is behind a bot wall, but its
    feeds are public and carry the full post; they only list the 10 most recent posts)."""
    want = canon_url(url)
    m = re.search(r"-([0-9a-f]{10,12})$", urlsplit(url).path)  # Medium post id
    post_id = m.group(1) if m else None
    for feed in _feed_urls(url):
        try:
            r, body = _get_capped(c, feed)
            if r.status_code != 200:
                continue
            root = ET.fromstring(body)
        except (httpx.HTTPError, ET.ParseError):
            continue
        for it in [*root.iter("item"), *root.iter(f"{{{_RSS_NS['atom']}}}entry")]:
            link = (it.findtext("link") or "").strip()
            if not link:
                alt = it.find("atom:link", _RSS_NS)
                link = alt.get("href", "") if alt is not None else ""
            if not link or (canon_url(link) != want and not (post_id and post_id in link)):
                continue
            body_html = (it.findtext("content:encoded", namespaces=_RSS_NS)
                         or it.findtext("atom:content", namespaces=_RSS_NS)
                         or it.findtext("description") or "")
            pub = (it.findtext("pubDate") or it.findtext("atom:published", namespaces=_RSS_NS) or "").strip()
            try:  # RSS uses RFC 822 dates, Atom ISO 8601
                pub_iso = parsedate_to_datetime(pub).date().isoformat() if "," in pub else pub[:10] or None
            except (TypeError, ValueError):
                pub_iso = None
            return {"url": link, "title": " ".join((it.findtext("title") or "").split()),
                    "author": it.findtext("dc:creator", namespaces=_RSS_NS)
                    or it.findtext("atom:author/atom:name", namespaces=_RSS_NS),
                    "date": pub_iso, "html": body_html}
    return None


def _clean_tags(raw: list[str] | None) -> list[str]:
    out: list[str] = []
    for t in raw or []:
        t = re.sub(r"\s+\d[\d,]*$", "", str(t).strip())  # "ai 2,270" (post counts) -> "ai"
        for part in t.split(","):
            part = part.strip()
            if part and part.lower() not in (x.lower() for x in out):
                out.append(part)
    return out[:8]


def _extract(page: bytes | str, url: str, raw_html: str) -> tuple[dict, str]:
    import trafilatura
    common = {"url": url, "include_comments": False, "favor_precision": True}
    md = trafilatura.bare_extraction(page, with_metadata=True, include_links=True, include_formatting=True,
                                     date_extraction_params={"original_date": True, "extensive_search": True},
                                     **common)
    plain = trafilatura.bare_extraction(page, **common)
    md = md.as_dict() if hasattr(md, "as_dict") else (md or {})
    text = (getattr(plain, "text", None) if plain is not None else "") or ""
    if len(text.split()) < 40:
        raise SourceError("no article text found (a JavaScript-only page, a listing or a landing page)")
    links = [u for u in _MD_LINK_RE.findall(md.get("text") or "") if url_host(u) != url_host(url)]
    lang, english = detect_language(text)
    canonical = md.get("url") or url
    words = len(text.split())
    published = md.get("date")
    if published and published > date.today().isoformat():
        published = None
    site = re.sub(r"(\w)'S\b", r"\1's", md.get("sitename") or "") or url_host(url)
    title = " ".join((md.get("title") or "").split()) or url
    outline = [h if len(h) <= 90 else h[:89] + "…"
               for h in (_heading(l) for l in (md.get("text") or "").splitlines() if l.startswith("#"))
               if h and h != title]
    return {
        "kind": "blog", "id": url, "url": url, "host": url_host(url),
        "title": title,
        "site": site, "author": md.get("author") or None, "published": published,
        "description": " ".join((md.get("description") or "").split())[:500] or None,
        "image": urljoin(url, md["image"]) if md.get("image") else None,
        "tags": _clean_tags((md.get("categories") or []) + (md.get("tags") or [])),
        "words": words, "read_min": max(1, round(words / 230)),
        "lang": lang, "english": english,
        "paywalled": bool(_PAYWALL_RE.search(raw_html)),
        # a page whose canonical URL is on another site is a syndicated copy of that post
        "original_url": canonical if url_host(canonical) not in (url_host(url), "") and urlsplit(canonical).path.strip("/") else None,
        "outline": outline[:16],
        "paper_links": arxiv_ids_in(" ".join(links) + " " + text)[:8],
        "code_links": github_links(" ".join(links))[:8],
        "video_links": list(dict.fromkeys(YT_ID_RE.findall(" ".join(u for u in links if "youtu" in u))
                                          + _EMBED_RE.findall(raw_html)))[:6],
        "read": True,
    }, text


def _heading(line: str) -> str:
    return " ".join(re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", line.lstrip("#")).replace("**", "").replace("\\", "").split())


def fetch_article(url: str) -> tuple[dict, str]:
    """Open a blog post and return (article metadata, plain article text) or raise SourceError.

    Metadata comes from the page (title, site, author, publish date, image, tags), the language
    verdict from the text, and arXiv/GitHub/YouTube links from the article body only (not the
    site's navigation). Falls back to the site's feed when the page blocks automated readers."""
    if not re.match(r"https?://", url.strip()):
        raise SourceError("not a web URL")
    if not is_blog_host(url):
        raise SourceError(f"{url_host(url)} is not a blog (Scout has other tabs for GitHub/arXiv/YouTube)")
    try:
        with httpx.Client(timeout=20, follow_redirects=True) as c:
            r, body = _get_capped(c, url.strip())
            final = clean_url(str(r.url))
            if r.status_code in _BLOCKED:
                entry = _from_feed(c, final)
                if not entry:
                    raise SourceError(f"HTTP {r.status_code}: the site blocks automated readers and the post is "
                                      "not in its RSS feed")
                doc = (f"<html><head><title>{html.escape(entry['title'])}</title></head><body><article>"
                       f"<h1>{html.escape(entry['title'])}</h1>{entry['html']}</article></body></html>")
                art, text = _extract(doc, final, entry["html"])
                art.update(title=entry["title"] or art["title"], author=entry["author"] or art["author"],
                           published=entry["date"] or art["published"], via="RSS feed")
                return art, text
    except httpx.HTTPError as e:
        raise SourceError(f"could not load the page ({type(e).__name__})") from None
    if r.status_code != 200:
        raise SourceError(f"HTTP {r.status_code}")
    if not is_blog_host(final):
        raise SourceError(f"it redirects to {url_host(final)}, which is not a blog")
    ctype = r.headers.get("content-type", "").lower()
    if "pdf" in ctype:
        raise SourceError("it is a PDF, not a web article")
    if ctype and "html" not in ctype and "xml" not in ctype:
        raise SourceError(f"not a web page ({ctype.split(';')[0]})")
    return _extract(body, final, body.decode(r.encoding or "utf-8", errors="replace"))


def parallel(fn, items, workers: int = 8) -> list:
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(fn, items))
