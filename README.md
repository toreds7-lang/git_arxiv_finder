# Scout: GitHub, arXiv, YouTube & blog finder driven by Claude Code

Describe what you're looking for in plain language. Claude Code (the CLI you have installed
globally) searches GitHub, arXiv, YouTube or the web for English blog posts, refines its searches,
ranks the candidates, and explains each pick. A live timeline shows every step as it happens.

## Run it

```powershell
.\start.ps1          # then open http://127.0.0.1:8000
```

First-time setup (already done in this folder):

```powershell
py -3.11 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

GitHub token: the app reads `GITHUB_TOKEN` from the environment, then from a `GITHUB_TOKEN=`
(or `Github_Token=`) line in `.env` or `env.txt.txt`. Without a token GitHub allows only 10
searches per minute.

YouTube API key (needed for the YouTube tab, free):

1. Open <https://console.cloud.google.com/apis/library/youtube.googleapis.com>, create or pick a
   project, and click **Enable** for *YouTube Data API v3*.
2. Go to **APIs & Services → Credentials → Create credentials → API key**. Optionally restrict it
   to *YouTube Data API v3*.
3. Add a `YOUTUBE_API_KEY=...` line to `env.txt.txt` (or set the `YOUTUBE_API_KEY` env var) and
   reload the page. No restart is needed.

The free quota is 10,000 units per day. Each YouTube search costs about 102 units, and a run
makes 3–5 searches, so you get about 20–30 runs a day. The quota resets at midnight Pacific
time. Transcripts come from YouTube's captions directly (`youtube-transcript-api`) and cost no
quota.

The Blogs tab needs no key. It uses Claude Code's built-in WebSearch (part of your Claude plan)
and Hacker News search (free). It reads pages with `trafilatura`, which is in `requirements.txt`.

## How it works

```
Browser ──POST /api/search──▶ FastAPI ──spawns──▶ claude -p --output-format stream-json
   ▲                            │                      │  --tools ""            (no Bash/edit/web;
                                                       │                         Blogs: WebSearch only)
   └──── SSE timeline ◀─────────┘                      │  --mcp-config finder   (only our tools)
                                                       ▼
                                   finder/mcp_server.py (MCP over stdio)
                                   ├─ search_github / get_readme        → GitHub REST API
                                   ├─ search_arxiv / search_hf_papers   → arXiv API, Hugging Face Papers
                                   │   (+ Semantic Scholar citations)
                                   ├─ search_youtube / get_transcript   → YouTube Data API v3, captions
                                   ├─ search_hn / read_articles         → Hacker News (Algolia), the post pages
                                   └─ submit_results  (rejects ids no tool returned)
```

* **Claude's only tools are the search tools.** Built-in tools are turned off, and your user
  hooks, plugins and CLAUDE.md are not loaded (`--setting-sources project`, run from a temp
  directory).
* **No made-up results.** Every candidate a tool returns is saved to
  `runs/<id>/candidates.json`. `submit_results` accepts only ids from that file, so Claude ranks
  and explains, while stars, views, dates, authors and abstracts always come from the APIs.
* **Videos are checked, not just matched by title.** `get_transcript` shows Claude the intro,
  the caption passages that mention the request's keywords (with `[m:ss]` timestamps), and
  evenly spaced samples of the rest. A video card's **▶ Jump to m:ss** link is accepted only
  when it matches a caption line Claude read or a chapter in the description. Otherwise
  `submit_results` drops it.
* **Blog posts are opened, not just found.** WebSearch and Hacker News only supply leads.
  `submit_results` accepts a post only after `read_articles` has opened the page and confirmed
  that it is written in English, judging from the article text itself, not from the page's
  `lang` attribute. It must also fall inside the recency window when the page shows a date.
  Title, site, author, publish date, length, outline and links all come from the page. When a
  page blocks automated readers (Medium does), the post is read from the site's RSS feed; Medium
  feeds hold only an author's 10 most recent posts, so older Medium posts can't be used. Each
  card's quote is kept only if it appears verbatim in the article.
* **Timeline.** Each line of `stream-json` (session init, thinking, narration, tool calls, tool
  results, usage limits, final cost) becomes a timestamped event.
* **Extra signals after ranking.** GitHub: each README is scanned for linked arXiv papers. arXiv:
  Hugging Face code links and upvotes, plus Semantic Scholar citation counts. YouTube: arXiv
  papers and GitHub repos linked in the description become badges. Blogs: every post is looked
  up on Hacker News by URL (points, comments, discussion link), and the arXiv papers, GitHub
  repos and YouTube videos the article links to become badges.
* **Find videos / Find blog posts.** GitHub and arXiv cards have **▶ Find videos** and
  **✎ Find blog posts** buttons, and video cards have **✎ Find blog posts**. Each starts a
  targeted run in that tab for that repo, paper or video: 5 results, any date.
* **History.** Each run is saved under `runs/<id>/` (`events.jsonl`, `results.json`, the raw
  `stream.jsonl`). Reopen it from the History dropdown or via `http://127.0.0.1:8000/#run=<id>`.

## Library: your searches as a research memory

The **📚 Library** tab collects everything you have searched. It lives in `runs/scout.db` (SQLite
with full-text search). On startup, runs made before the Library existed are indexed automatically.

* **Topics.** One topic per request text: the same words searched in several tabs are one topic,
  and **Find videos / Find blog posts** runs are filed under the topic they came from. Claude
  (Haiku, thinking off, about 0.3¢ per batch) gives each new topic a short name, 3-6 keywords and
  the purpose, if the request states one (e.g. *for an AI lecture*). You can rename a topic, add
  `#tags` and notes, or hide it.
* **Retrieve.** Search box over topics, keywords, tags, notes and every repo, paper, video and
  post you were shown, with Claude's reason for each. While you type in a search tab, Scout shows
  *"You searched this before"* with links to those runs.
* **Marks.** ☆ save, 👍 and 👎 on every result card. Marks belong to the item, so they show up in
  every run that lists it; saved items form the **Saved** list. A *seen 2d ago* badge marks
  results an earlier run already showed you.
* **Watch (tracking).** The 👁 next to a topic's tab re-runs that search on a schedule (weekly by
  default), **only while Scout is running**. Missed checks run after a restart, one at a time. A
  check looks back about a month, is told what you have already seen (`submit_results` drops
  those), and the topic shows **N new**. Each check is a normal search run (about $0.15–0.20
  with Sonnet; YouTube checks also use API quota).
* **Suggestions, free:** papers and repos that several of your results link to but you have not
  opened, topics not yet tried in every tab, and tags that keep appearing on your results but that
  you never searched. **Ideas from Claude** (about 1¢ a click) reads your topics, tags, notes and
  marks and proposes next searches. Suggestions only fill in a search box; nothing runs until you
  press Search.
* **Insights:** searches per week, your topics' keywords, the most common tags on your results,
  and the API-equivalent spend this month.

arXiv note: arXiv ORs words that have no operator between them. `abs:"test-time scaling" reasoning`
matched 1,745 papers in a month, but 13 with `AND`. `search_arxiv` therefore joins such
neighbours with AND (an OR-chain stays one group). The query shown in the timeline is the one
actually sent.

## Defaults (recommended)

| | Results | Why |
|---|---|---|
| GitHub | **10** | About as many repos as you'll actually evaluate, since each needs a README look. |
| arXiv | **15** | Papers are skimmed by title and abstract, so a wider net still stays readable. |
| YouTube | **10** | Each video costs minutes to watch, so 10 is plenty. |
| Blogs | **10** | Each post takes 5–20 minutes to read. |

Claude returns fewer when fewer are truly relevant. The default recency is the **past year**:
for repos it means "pushed in the last year", for papers "submitted in the last year", for
videos "uploaded in the last year", for blog posts "published in the last year" (posts that show
no date are allowed), and the tools enforce it. The default model is **Sonnet**: about 45–70 s and about $0.15–0.20 of
API-equivalent usage per search. Haiku is about twice as fast and cheaper but misses well-known
leaders more often. Opus is the most thorough.

## Files

```
finder/config.py      defaults, token/key lookup, claude binary lookup
finder/sources.py     GitHub / arXiv / Semantic Scholar / Hugging Face / YouTube / Hacker News
                      clients, article extraction, English check
finder/mcp_server.py  the MCP tools Claude gets
finder/prompts.py     system prompts (search strategy and ranking rules)
finder/runner.py      spawns Claude Code, maps stream-json to timeline events, enrichment, history,
                      the watch scheduler
finder/library.py     Library: SQLite index of topics/runs/results, marks, tags, watches, suggestions
finder/llm.py         one-shot Claude calls without tools (topic keywords, search ideas)
finder/main.py        FastAPI routes + SSE
static/               index.html, app.js, style.css, library.js, library.css (no build step)
```
