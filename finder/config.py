"""Paths, defaults and secrets for git_arxiv_finder."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = ROOT / "runs"  # search history: one folder per run
STATIC_DIR = ROOT / "static"
# Claude Code runs in a throwaway cwd outside the project so no CLAUDE.md /
# project settings leak into the scout's context.
WORK_ROOT = Path(os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp") / "git_arxiv_finder"

KINDS = ("github", "arxiv", "youtube", "blog")

# Recommended result counts: ~10 repos is what people actually evaluate in one
# sitting (each needs a README look); papers are skimmed by title/abstract, so
# 15 gives broader coverage without becoming a dump. Videos cost minutes each to
# watch, so 10 is plenty; blog posts take 5-20 minutes each to read, likewise.
# Claude may return fewer.
DEFAULT_COUNTS = {"github": 10, "arxiv": 15, "youtube": 10, "blog": 10}
COUNT_OPTIONS = [5, 10, 15, 20, 30]

SINCE_OPTIONS = {  # label -> days back (None = no limit)
    "1m": 30,
    "3m": 91,
    "6m": 182,
    "1y": 365,
    "2y": 730,
    "any": None,
}
DEFAULT_SINCE = "1y"

MODELS = ["sonnet", "opus", "haiku", "fable"]
DEFAULT_MODEL = "sonnet"

RUN_TIMEOUT_S = int(os.environ.get("FINDER_TIMEOUT_S", "420"))
MAX_SEARCHES = 10  # per run, enforced by the MCP server
MAX_READMES = 4
MAX_TRANSCRIPTS = 4
# A YouTube search.list call costs 100 of the 10,000 free daily quota units, so
# video runs get a smaller search budget than GitHub/arXiv.
MAX_YT_SEARCHES = 6
# Blog runs: pages opened by read_articles (every submitted post must be one of
# them), at most 8 per call. WebSearch calls are Claude Code's own tool, so the
# prompt, not this server, keeps their number down.
MAX_ARTICLE_READS = 30
YOUTUBE_API = os.environ.get("YOUTUBE_API_URL", "https://www.googleapis.com/youtube/v3")


def _read_key_from_file(path: Path, keys: tuple[str, ...]) -> str | None:
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
        k, sep, v = line.partition("=")
        if sep and k.strip().lower().removeprefix("export ").strip() in keys:
            v = v.strip().strip('"').strip("'")
            if v:
                return v
    return None


def _secret(env_names: tuple[str, ...], file_keys: tuple[str, ...]) -> str | None:
    """First env var that is set, else a KEY= line in .env (or env.txt.txt)."""
    for name in env_names:
        if os.environ.get(name):
            return os.environ[name].strip()
    for name in (".env", "env.txt.txt", "env.txt"):
        val = _read_key_from_file(ROOT / name, file_keys)
        if val:
            return val
    return None


def github_token() -> str | None:
    """GITHUB_TOKEN env var, else a GITHUB_TOKEN= line in .env (or env.txt.txt)."""
    return _secret(("GITHUB_TOKEN", "GH_TOKEN"), ("github_token", "gh_token"))


def youtube_api_key() -> str | None:
    """YOUTUBE_API_KEY env var, else a YOUTUBE_API_KEY= line in .env (or env.txt.txt)."""
    return _secret(("YOUTUBE_API_KEY", "YT_API_KEY"), ("youtube_api_key", "yt_api_key"))


def claude_bin() -> str:
    return (
        os.environ.get("CLAUDE_BIN")
        or shutil.which("claude")
        or str(Path.home() / ".local" / "bin" / ("claude.exe" if os.name == "nt" else "claude"))
    )
