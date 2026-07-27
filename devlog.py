"""What you and your collaborators shipped since the last briefing.

Pulls commits from GitHub and edited pages from Notion, then hands the raw
material to the local model for a couple of sentences per person.

Standard library only, like the rest of the app. Two conventions from
elsewhere in the repo are followed deliberately:

  State      Per-source `last_checked` timestamps live in devlog_state.json,
             the same shape events_store.py uses for its manual events -
             a small JSON file, loaded defensively, saved whole.

  Failure    Nothing here raises at the caller. Every network path returns
             entries plus a list of human-readable problems, exactly as
             stocks.py returns a row carrying its own error. A dead token
             should cost you this one section, not the briefing.

On re-runs: the digest is cached per day and accumulated rather than
replaced. Advancing `last_checked` on every run would otherwise mean that
previewing the briefing at nine consumed the morning's commits and left the
section empty for the rest of the day.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE = HERE / "devlog_state.json"

GITHUB_API = "https://api.github.com"
NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
UA = "morning-brief/1.0"
TIMEOUT = 15

# How far back to look the very first time, before any state exists.
DEFAULT_LOOKBACK_HOURS = 24
MAX_COMMITS = 50
MAX_PAGES = 25
MAX_BLOCKS = 8


# ------------------------------------------------------------------ state

def _load_state() -> dict:
    try:
        data = json.loads(STATE.read_text(encoding="utf-8-sig"))
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("last_checked", {})
    data.setdefault("digest", {})
    return data


def _save_state(data: dict) -> None:
    try:
        STATE.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                         encoding="utf-8")
    except OSError:
        pass          # a read-only install shouldn't break the briefing


def _utc(moment: dt.datetime) -> str:
    """ISO 8601 in UTC, which is what both APIs want."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def last_checked(source: str, lookback_hours: int = DEFAULT_LOOKBACK_HOURS) -> str:
    """When this source was last read, as an ISO 8601 UTC string.

    First run has no state, so it looks back a fixed window instead of
    fetching the entire history of every repo.
    """
    stored = _load_state()["last_checked"].get(source)
    if isinstance(stored, str) and stored.strip():
        return stored.strip()
    return _utc(dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=lookback_hours))


def mark_checked(source: str, when: dt.datetime | None = None) -> None:
    """Record a successful read. Only called when the fetch actually worked -
    marking a failed call would silently skip whatever it failed to fetch."""
    state = _load_state()
    state["last_checked"][source] = _utc(when or dt.datetime.now(dt.timezone.utc))
    _save_state(state)


def reset_state() -> int:
    """Forget every timestamp, so the next run looks back the default window."""
    state = _load_state()
    count = len(state.get("last_checked", {}))
    state["last_checked"] = {}
    _save_state(state)
    return count


# ------------------------------------------------------------------ http

def _get_json(url: str, headers: dict, body: dict | None = None,
              timeout: int = TIMEOUT) -> tuple[object | None, str]:
    """(payload, problem). Never raises.

    The problem string is written for a person, because it ends up in the
    briefing: "GitHub token was rejected" is actionable, "HTTPError 401" is
    not.
    """
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data,
                                     method="POST" if body is not None else "GET")
    request.add_header("User-Agent", UA)
    request.add_header("Accept", "application/json")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    for key, value in headers.items():
        request.add_header(key, value)

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
        return (json.loads(raw) if raw.strip() else {}), ""
    except urllib.error.HTTPError as exc:
        remaining = exc.headers.get("X-RateLimit-Remaining") if exc.headers else None
        if exc.code in (401, 403) and remaining == "0":
            reset = (exc.headers or {}).get("X-RateLimit-Reset", "")
            when = ""
            if str(reset).isdigit():
                when = dt.datetime.fromtimestamp(int(reset)).strftime(" (resets %H:%M)")
            return None, f"rate limit reached{when}"
        if exc.code == 429:
            return None, "rate limit reached"
        if exc.code == 401:
            return None, "token was rejected - check it hasn't expired"
        if exc.code == 403:
            return None, "access refused - the token may not cover this"
        if exc.code == 404:
            return None, "not found, or not shared with the integration"
        return None, f"HTTP {exc.code}"
    except urllib.error.URLError as exc:
        return None, f"couldn't reach the API ({exc.reason})"
    except Exception as exc:
        return None, f"unexpected {type(exc).__name__}"


def _token(source_cfg: dict, *env_names: str) -> str:
    """Token from config, falling back to the environment.

    A config value of "env:NAME" reads that variable, which lets the token
    stay out of config.json entirely.
    """
    raw = str(source_cfg.get("token") or "").strip()
    if raw.startswith("env:"):
        return os.environ.get(raw[4:].strip(), "").strip()
    if raw:
        return raw
    for name in env_names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


# ---------------------------------------------------------------- github

def github_activity(cfg: dict, now: dt.datetime | None = None) -> dict:
    """Commits pushed to the tracked repos since each was last read."""
    opts = ((cfg.get("devlog") or {}).get("github") or {})
    repos = [r for r in (opts.get("repos") or []) if str(r).strip()]
    if not opts.get("enabled", True) or not repos:
        return {"entries": [], "problems": [], "checked": []}

    token = _token(opts, "MORNING_BRIEF_GITHUB_TOKEN", "GITHUB_TOKEN")
    if not token:
        return {"entries": [], "checked": [],
                "problems": ["GitHub: no token set - see the README"]}

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    me = str(opts.get("username") or "").strip().lower()
    lookback = int(opts.get("lookback_hours", DEFAULT_LOOKBACK_HOURS))

    entries, problems, checked = [], [], []
    for repo in repos:
        repo = str(repo).strip().strip("/")
        key = f"github:{repo.lower()}"
        since = last_checked(key, lookback)
        query = urllib.parse.urlencode({"since": since, "per_page": MAX_COMMITS})
        payload, problem = _get_json(f"{GITHUB_API}/repos/{repo}/commits?{query}", headers)

        if problem:
            problems.append(f"GitHub {repo}: {problem}")
            continue        # deliberately not marked as checked
        if not isinstance(payload, list):
            problems.append(f"GitHub {repo}: unexpected response")
            continue

        for commit in payload:
            info = (commit or {}).get("commit") or {}
            author = (commit or {}).get("author") or {}
            login = str(author.get("login") or "").strip()
            name = str((info.get("author") or {}).get("name") or login or "someone")
            message = str(info.get("message") or "").strip().splitlines()
            if not message:
                continue
            entries.append({
                "source": "github",
                "repo": repo,
                "who": login or name,
                "mine": bool(me and login.lower() == me),
                "summary": message[0][:160],
                "when": str((info.get("author") or {}).get("date") or ""),
                "url": str(commit.get("html_url") or ""),
            })
        checked.append(key)

    return {"entries": entries, "problems": problems, "checked": checked}


# ---------------------------------------------------------------- notion

def _notion_title(page: dict) -> str:
    """A page's title, wherever Notion happens to have put it."""
    props = page.get("properties") or {}
    for value in props.values():
        if isinstance(value, dict) and value.get("type") == "title":
            parts = [p.get("plain_text", "") for p in (value.get("title") or [])]
            joined = "".join(parts).strip()
            if joined:
                return joined[:120]
    # Databases carry a top-level title instead of a title property.
    parts = [p.get("plain_text", "") for p in (page.get("title") or [])]
    return ("".join(parts).strip() or "Untitled")[:120]


def _notion_blocks(page_id: str, headers: dict) -> list[str]:
    """Plain text of the top-level blocks, as raw material for the summary."""
    payload, problem = _get_json(
        f"{NOTION_API}/blocks/{page_id}/children?page_size={MAX_BLOCKS}", headers)
    if problem or not isinstance(payload, dict):
        return []
    lines = []
    for block in (payload.get("results") or []):
        body = block.get(block.get("type") or "", {})
        if not isinstance(body, dict):
            continue
        text = "".join(t.get("plain_text", "") for t in (body.get("rich_text") or []))
        text = " ".join(text.split())
        if text:
            lines.append(text[:200])
    return lines


def notion_activity(cfg: dict, now: dt.datetime | None = None) -> dict:
    """Pages edited since the last read, among those shared with the integration.

    Note on the API: Notion's search endpoint cannot filter on
    last_edited_time - its `filter` only narrows by object type. So results
    are sorted by last_edited_time descending and cut off here, client-side,
    at the stored timestamp. Sorted order means we can stop at the first page
    older than the cutoff rather than walking the whole workspace.
    """
    opts = ((cfg.get("devlog") or {}).get("notion") or {})
    if not opts.get("enabled", True):
        return {"entries": [], "problems": [], "checked": []}

    token = _token(opts, "MORNING_BRIEF_NOTION_TOKEN", "NOTION_TOKEN")
    if not token:
        return {"entries": [], "checked": [],
                "problems": ["Notion: no token set - see the README"]}

    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
    }
    workspace = str(opts.get("workspace") or "workspace").strip()
    key = f"notion:{workspace.lower()}"
    lookback = int(opts.get("lookback_hours", DEFAULT_LOOKBACK_HOURS))
    since = last_checked(key, lookback)

    body = {
        "sort": {"direction": "descending", "timestamp": "last_edited_time"},
        "page_size": MAX_PAGES,
    }
    query = str(opts.get("query") or "").strip()
    if query:
        body["query"] = query

    payload, problem = _get_json(f"{NOTION_API}/search", headers, body=body)
    if problem:
        return {"entries": [], "checked": [], "problems": [f"Notion: {problem}"]}
    if not isinstance(payload, dict):
        return {"entries": [], "checked": [], "problems": ["Notion: unexpected response"]}

    results = payload.get("results") or []
    if not results:
        # An integration with nothing shared with it looks exactly like a
        # quiet workspace, so say which it probably is.
        return {"entries": [], "checked": [key],
                "problems": ["Notion: nothing shared with the integration yet"]
                            if not query else []}

    only_pages = [p for p in (opts.get("page_ids") or []) if str(p).strip()]
    entries, seen_any_newer = [], False
    for page in results:
        edited = str(page.get("last_edited_time") or "")
        if edited and edited <= since:
            break               # sorted descending: everything after is older
        seen_any_newer = True
        page_id = str(page.get("id") or "")
        if only_pages and page_id not in only_pages:
            continue
        editor = ((page.get("last_edited_by") or {}).get("id") or "")
        entries.append({
            "source": "notion",
            "who": str(opts.get("collaborator") or "").strip() or "Notion",
            "mine": False,
            "title": _notion_title(page),
            "summary": "; ".join(_notion_blocks(page_id, headers))[:400],
            "when": edited,
            "url": str(page.get("url") or ""),
            "editor_id": editor,
        })

    problems = []
    if not entries and not seen_any_newer:
        problems = []           # simply nothing changed; not worth reporting
    return {"entries": entries, "problems": problems, "checked": [key]}


# ------------------------------------------------------------- summarise

def _prompt(personality: str, mine: list[dict], theirs: dict[str, list[dict]]) -> str:
    def lines(rows):
        out = []
        for row in rows:
            if row["source"] == "github":
                out.append(f"- [{row['repo']}] {row['summary']}")
            else:
                out.append(f"- [Notion: {row['title']}] {row['summary'] or '(no text)'}")
        return "\n".join(out) or "(nothing)"

    blocks = [f"WHAT I DID:\n{lines(mine)}"]
    for who, rows in theirs.items():
        blocks.append(f"WHAT {who.upper()} DID:\n{lines(rows)}")

    return (
        f"{personality}\n\n"
        "Below is the raw activity from the last day: commit messages and "
        "edited Notion pages, grouped by who did them.\n\n"
        + "\n\n".join(blocks) +
        "\n\nWrite the development update for the spoken briefing. Hard rules:\n"
        "- Two or three sentences per person, no more.\n"
        "- Plain prose for a text-to-speech engine: no markdown, no bullets, "
        "no code identifiers read out character by character, no URLs.\n"
        "- Address the listener as 'you' for their own work, and name the "
        "other person for theirs.\n"
        "- Say what changed in plain language. 'You added the calendar import' "
        "beats 'you committed feat(cal): add ics parse'.\n"
        "- Only describe what is listed above. If something is unclear, leave "
        "it out rather than guessing what it meant.\n"
        "- No greeting and no sign-off; this sits in the middle of a longer "
        "briefing."
    )


def summarise(activity: dict, cfg: dict) -> str:
    """Two or three sentences per person. Empty string if the model can't help.

    The caller falls back to the mechanical rendering, so every failure here
    is quiet - the same contract spoken_via_llm() has.
    """
    entries = activity.get("entries") or []
    if not entries:
        return ""

    llm_cfg = (cfg.get("llm") or {})
    if not llm_cfg.get("enabled", False):
        return ""
    personality = (llm_cfg.get("personality") or "").strip()
    if not personality:
        return ""

    try:
        import llm as _llm
    except Exception:
        return ""

    base_url = llm_cfg.get("base_url") or _llm.DEFAULT_BASE_URL
    model = (llm_cfg.get("model") or "").strip()
    if not model:
        available = _llm.list_models(base_url, timeout=4)
        model = available[0] if available else ""
    if not model:
        return ""

    mine = [e for e in entries if e.get("mine")]
    theirs: dict[str, list[dict]] = {}
    for entry in entries:
        if entry.get("mine"):
            continue
        theirs.setdefault(str(entry.get("who") or "your collaborator"), []).append(entry)

    try:
        text = _llm.generate(
            _prompt(personality, mine, theirs),
            model=model,
            base_url=base_url,
            timeout=int(llm_cfg.get("timeout", 120)),
            temperature=float(llm_cfg.get("temperature", 0.4)),
            # Same reason as the main briefing: a reasoning model spends most
            # of its budget thinking before it writes a word.
            max_tokens=int(llm_cfg.get("max_tokens", 16000)),
        )
    except Exception:
        return ""
    return (text or "").strip()


# -------------------------------------------------------------- assembly

def collect(cfg: dict, now: dt.datetime | None = None, advance: bool = True) -> dict:
    """Everything new since last time, summarised, accumulated for today.

    `advance=False` reads without moving the timestamps, which is what a
    preview wants - otherwise looking at the briefing twice would make the
    second look empty.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    github = github_activity(cfg, now)
    notion = notion_activity(cfg, now)

    entries = list(github["entries"]) + list(notion["entries"])
    problems = list(github["problems"]) + list(notion["problems"])

    state = _load_state()
    today = _utc(now)[:10]
    stored = state.get("digest") or {}
    kept = list(stored.get("entries") or []) if stored.get("date") == today else []

    # Accumulate rather than replace: a refresh at noon should still show
    # what came in overnight, plus whatever has landed since.
    known = {(e.get("url") or "") + e.get("summary", "")[:60] for e in kept}
    for entry in entries:
        if (entry.get("url") or "") + entry.get("summary", "")[:60] not in known:
            kept.append(entry)

    summary = ""
    if kept:
        summary = summarise({"entries": kept}, cfg)

    if advance:
        for key in github["checked"] + notion["checked"]:
            state["last_checked"][key] = _utc(now)
        state["digest"] = {"date": today, "entries": kept, "summary": summary}
        _save_state(state)

    return {"entries": kept, "summary": summary, "problems": problems}


def plain_summary(activity: dict) -> str:
    """Mechanical fallback when the model is off or unreachable."""
    entries = activity.get("entries") or []
    if not entries:
        return ""
    mine = [e for e in entries if e.get("mine")]
    theirs = [e for e in entries if not e.get("mine")]

    parts = []
    if mine:
        repos = sorted({e["repo"] for e in mine if e.get("repo")})
        where = f" in {' and '.join(repos)}" if repos else ""
        count = len(mine)
        parts.append(f"You pushed {count} change{'s' if count != 1 else ''}{where}.")
    for who in sorted({str(e.get("who") or "") for e in theirs if e.get("who")}):
        rows = [e for e in theirs if e.get("who") == who]
        pages = [e["title"] for e in rows if e.get("source") == "notion"]
        commits = [e for e in rows if e.get("source") == "github"]
        bits = []
        if commits:
            bits.append(f"{len(commits)} commit{'s' if len(commits) != 1 else ''}")
        if pages:
            bits.append("updated " + ", ".join(pages[:2]))
        if bits:
            parts.append(f"{who}: {' and '.join(bits)}.")
    return " ".join(parts)
