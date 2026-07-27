"""What you actually open: frequent sites and apps, plus your own pins.

Two very different data sources, handled differently because their quality
differs:

  Sites   Browser history is a good signal, but only after grouping. The raw
          table is topped by a PDF page anchor with 4,051 "visits" and deep
          CMS links nobody would click again, so visits are summed per domain
          and the domain is what gets offered.

  Apps    Windows' UserAssist keys record how often you launch things. It is
          the only per-app frequency signal available without extra software,
          and it is noisy - session counters, taskbar shortcut paths, and
          GUID-prefixed folder references all live in there.

Standard library only: sqlite3 reads the browser history, winreg reads
UserAssist. Nothing here leaves the machine, and shortcuts.json is git-ignored
along with the rest of your data.
"""

from __future__ import annotations

import codecs
import json
import os
import re
import shutil
import sqlite3
import tempfile
import urllib.parse
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
STORE = HERE / "shortcuts.json"

LOCAL = Path(os.environ.get("LOCALAPPDATA", ""))
BROWSERS = {
    "Edge": LOCAL / "Microsoft/Edge/User Data/Default/History",
    "Chrome": LOCAL / "Google/Chrome/User Data/Default/History",
    "Brave": LOCAL / "BraveSoftware/Brave-Browser/User Data/Default/History",
    "Vivaldi": LOCAL / "Vivaldi/User Data/Default/History",
}

# Domains that are noise in a "what do you actually use" list: you don't
# navigate to a search results page on purpose, and the CDN hosts are things
# other pages pull in.
BORING = {
    "google.com", "www.google.com", "bing.com", "duckduckgo.com",
    "googleadservices.com", "doubleclick.net", "gstatic.com",
    "googleusercontent.com", "localhost", "127.0.0.1",
}

# UserAssist rows that aren't apps.
NOT_AN_APP = re.compile(r"^UEME_|^Microsoft\.Windows\.(Shell|Cortana|Search)", re.I)

# UserAssist stores paths with the containing folder replaced by its
# KNOWNFOLDERID. Expanding them back to real paths is what makes an entry
# launchable - left as-is you get "TaskBar\File Explorer.lnk", which is
# relative to nothing and opens nothing.
def _known_folders() -> dict[str, Path]:
    appdata = Path(os.environ.get("APPDATA", ""))
    windir = Path(os.environ.get("WINDIR", r"C:\Windows"))
    return {
        "{9E3995AB-1F9C-4F13-B827-48B24B6C7174}":
            appdata / "Microsoft/Internet Explorer/Quick Launch/User Pinned",
        "{A77F5D77-2E2B-44C3-A6A2-ABA601054A51}":
            appdata / "Microsoft/Windows/Start Menu/Programs",
        "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}": windir / "System32",
        "{D65231B0-B2F1-4857-A4CE-A8E7C6EA7D27}": windir / "System32",
        "{F38BF404-1D43-42F2-9305-67DE0B28FC23}": windir,
        "{6D809377-6AF0-444B-8957-A3773F02200E}":
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")),
        "{7C5A40EF-A0FB-4BFC-874A-C0F2E0B9FA8E}":
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")),
    }

# Store apps whose identifier reads badly when mechanically prettified.
FRIENDLY = {
    "ScreenSketch": "Snipping Tool",
    "XboxGamingOverlay": "Xbox Game Bar",
    "OutlookForWindows": "Outlook",
    "WindowsTerminal": "Terminal",
    "MSEdge": "Microsoft Edge",
    "WindowsNotepad": "Notepad",
    "WindowsCalculator": "Calculator",
    "Windows.Explorer": "File Explorer",
}


# ------------------------------------------------------------------ store

def _load() -> dict:
    try:
        data = json.loads(STORE.read_text(encoding="utf-8-sig"))
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("pinned", [])
    data.setdefault("hidden", [])
    return data


def _save(data: dict) -> None:
    STORE.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                     encoding="utf-8")


def item_id(kind: str, target: str) -> str:
    """Stable id, so hiding something survives the counts changing."""
    return f"{kind}:{target.strip().lower()}"


# ------------------------------------------------------------------ sites

def _domain(url: str) -> str:
    try:
        netloc = urllib.parse.urlparse(url).netloc.lower()
    except ValueError:
        return ""
    return netloc[4:] if netloc.startswith("www.") else netloc


def browser_sites(limit: int = 8) -> list[dict]:
    """Most-visited domains, summed across every browser found.

    The history file is locked while the browser is running, so it is copied
    to a temp file and opened read-only.
    """
    totals: dict[str, dict] = {}
    for name, path in BROWSERS.items():
        if not path.exists():
            continue
        tmp_dir = tempfile.mkdtemp(prefix="mb-hist-")
        tmp = Path(tmp_dir) / "History"
        try:
            shutil.copy2(path, tmp)
            con = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
            try:
                rows = con.execute(
                    "SELECT url, title, visit_count FROM urls "
                    "WHERE visit_count > 0"
                ).fetchall()
            finally:
                con.close()
        except Exception:
            continue        # a browser mid-update, or a schema we don't know
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

        for url, title, visits in rows:
            if not url.startswith(("http://", "https://")):
                continue    # local PDFs dominate the raw table otherwise
            host = _domain(url)
            if not host or host in BORING:
                continue
            row = totals.setdefault(host, {"visits": 0, "title": "", "browser": name})
            row["visits"] += int(visits or 0)
            if not row["title"] and title:
                row["title"] = title.strip()[:60]

    out = [{
        "id": item_id("site", host),
        "kind": "site",
        "label": host,
        "detail": f"{row['visits']:,} visits · {row['browser']}",
        "target": f"https://{host}/",
        "visits": row["visits"],
        "source": "auto",
    } for host, row in totals.items()]
    out.sort(key=lambda r: -r["visits"])
    return out[:limit]


# ------------------------------------------------------------------- apps

def _pretty(name: str) -> str:
    """Turn a UserAssist row into something worth showing."""
    stem = name
    if "!" in stem:                       # Store app: Pub.Name_hash!Entry
        stem = stem.split("!", 1)[0].split("_", 1)[0]
        stem = stem.split(".")[-1] if "." in stem else stem
    else:
        stem = Path(stem).stem
    if stem in FRIENDLY:
        return FRIENDLY[stem]
    # CamelCase and dots into spaced words: "ScreenSketch" -> "Screen Sketch".
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", stem.replace(".", " "))
    return " ".join(spaced.split()).title() if spaced.islower() else " ".join(spaced.split())


def _launch_target(name: str) -> str:
    """How to actually start it.

    Store apps go through shell:AppsFolder, which is the only way to launch a
    packaged app - there is no .exe to point at. Everything else is expanded
    back to an absolute path.
    """
    if "!" in name and "\\" not in name:
        return f"shell:AppsFolder\\{name}"        # packaged app
    for guid, base in _known_folders().items():
        if guid in name:
            tail = name.split("}", 1)[-1].lstrip("\\")
            return str(base / tail)
    return name


def frequent_apps(limit: int = 8) -> list[dict]:
    """Apps by launch count, from UserAssist.

    Rows arrive ROT13-encoded with the run count as a little-endian int at
    byte 4 - an odd format, but it is what Explorer writes.
    """
    try:
        import winreg
    except ImportError:
        return []          # not Windows

    root = r"Software\Microsoft\Windows\CurrentVersion\Explorer\UserAssist"
    best: dict[str, int] = {}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, root) as key:
            for i in range(winreg.QueryInfoKey(key)[0]):
                guid = winreg.EnumKey(key, i)
                try:
                    with winreg.OpenKey(key, guid + r"\Count") as counts:
                        for j in range(winreg.QueryInfoKey(counts)[1]):
                            raw_name, data, _ = winreg.EnumValue(counts, j)
                            if not isinstance(data, bytes) or len(data) < 8:
                                continue
                            name = codecs.decode(raw_name, "rot13")
                            if NOT_AN_APP.search(name):
                                continue
                            if not (name.lower().endswith((".exe", ".lnk")) or "!" in name):
                                continue
                            runs = int.from_bytes(data[4:8], "little")
                            if runs < 2:
                                continue
                            # The same app shows up as both a .lnk and an .exe
                            # (and sometimes a Store id too); keep whichever
                            # recorded the most launches.
                            label = _pretty(name)
                            prev = best.get(label)
                            if prev is None or runs > prev[0]:
                                best[label] = (runs, name)
                except OSError:
                    continue
    except OSError:
        return []

    out = []
    for label, (runs, name) in best.items():
        target = _launch_target(name)
        out.append({
            "id": item_id("app", label),
            "kind": "app",
            "label": label,
            "detail": f"opened {runs} times",
            "target": target,
            "visits": runs,
            "source": "auto",
        })
    out.sort(key=lambda r: -r["visits"])
    return out[:limit]


# --------------------------------------------------------------- assembly

def listing(site_limit: int = 8, app_limit: int = 8) -> dict:
    """Everything the panel shows: detected items minus hidden, plus pins."""
    store = _load()
    hidden = set(store.get("hidden", []))

    sites = [s for s in browser_sites(site_limit * 2) if s["id"] not in hidden][:site_limit]
    apps = [a for a in frequent_apps(app_limit * 2) if a["id"] not in hidden][:app_limit]

    pinned_sites, pinned_apps = [], []
    for p in store.get("pinned", []):
        row = {**p, "source": "pinned", "detail": p.get("detail", "pinned")}
        (pinned_sites if p.get("kind") == "site" else pinned_apps).append(row)

    # Pins first - you asked for those explicitly.
    return {"sites": pinned_sites + sites, "apps": pinned_apps + apps}


def add(kind: str, label: str, target: str) -> dict:
    kind = (kind or "").strip().lower()
    if kind not in ("site", "app"):
        raise ValueError("kind must be 'site' or 'app'")
    label = (label or "").strip()
    target = (target or "").strip()
    if not target:
        raise ValueError("give it something to open")

    if kind == "site":
        if not target.startswith(("http://", "https://")):
            target = "https://" + target
        if not _domain(target):
            raise ValueError("that doesn't look like a web address")
        label = label or _domain(target)
    else:
        label = label or Path(target).stem

    store = _load()
    row = {"id": f"pin:{uuid.uuid4().hex[:12]}", "kind": kind,
           "label": label, "target": target}
    store["pinned"].append(row)
    # Adding something back that was hidden should un-hide it too.
    auto = item_id(kind, _domain(target) if kind == "site" else label)
    store["hidden"] = [h for h in store["hidden"] if h != auto]
    _save(store)
    return row


def remove(item: str) -> bool:
    """Delete a pin, or hide a detected item so it stops coming back."""
    store = _load()
    before = len(store["pinned"])
    store["pinned"] = [p for p in store["pinned"] if p.get("id") != item]
    if len(store["pinned"]) < before:
        _save(store)
        return True

    if item not in store["hidden"]:
        store["hidden"].append(item)
        _save(store)
        return True
    return False


def restore_hidden() -> int:
    store = _load()
    count = len(store.get("hidden", []))
    store["hidden"] = []
    _save(store)
    return count


def resolve(item: str) -> dict | None:
    """Find an item by id in the current listing.

    Opening is only ever done by id. The alternative - taking a path from the
    request body - would turn a POST on a local port into "run anything",
    which any page open in the browser could reach.
    """
    data = listing(site_limit=50, app_limit=50)
    for row in data["sites"] + data["apps"]:
        if row.get("id") == item:
            return row
    return None
