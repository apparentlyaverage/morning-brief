"""Settings UI for the morning briefing.

    python ui_server.py            then open http://127.0.0.1:8765

Standard library only, apart from edge_tts (already installed for the voices).
Binds to localhost only - this is a control panel for the machine it runs on,
not something to expose on a network.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import briefing
import calendar_ics
import documents
import events_store
import music
import scripture
import stocks

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "config.json"
TIMETABLE = HERE / "timetable.json"
CALENDAR = HERE / "calendar.json"
TODOS = HERE / "todos.json"
BRIEF_DATA = HERE / "briefing_data.json"
INDEX = HERE / "ui" / "index.html"
DASHBOARD = HERE / "ui" / "dashboard.html"

PORT = 8765
# 127.0.0.1, never "localhost": that resolves to ::1 first, and these servers
# bind IPv4 only, so every request wastes ~2s failing over to IPv4.
CIDER = "http://127.0.0.1:10767"
OLLAMA = "http://127.0.0.1:11434"
VOICE_LIST_URL = ("https://speech.platform.bing.com/consumer/speech/synthesize/"
                  "readaloud/voices/list?trustedclienttoken=6A5AA1D4EAFF4E9FB37E23D68491D6F4")

_voice_cache: dict = {"at": 0.0, "data": None}


# ---------------------------------------------------------------- helpers

def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return default


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def http_json(url: str, method: str = "GET", body=None, headers=None, timeout=12):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
    return json.loads(raw) if raw.strip() else {}


def cider_token() -> str:
    return str((load_json(CONFIG, {}).get("cider") or {}).get("token") or "")


def provider() -> music.Provider:
    """The configured music source, resolved fresh so a settings change applies
    without restarting the server."""
    return music.get_provider(load_json(CONFIG, {}))


def cider_api(path: str, method: str = "GET", body=None):
    return http_json(f"{CIDER}{path}", method, body if body is not None else ({} if method != "GET" else None),
                     {"apptoken": cider_token()})


_playlist_cache: dict = {"at": 0.0, "data": None}


def all_playlists(force: bool = False) -> list[dict]:
    """Every library playlist, following pagination.

    The API caps a page at 100. A library larger than that would otherwise
    silently hide playlists, and a name search would wrongly report 'missing'.
    Cached briefly - walking a large library takes a few seconds per page.
    """
    if not force and _playlist_cache["data"] and time.time() - _playlist_cache["at"] < 120:
        return _playlist_cache["data"]

    out: list[dict] = []
    offset = 0
    while offset < 5000:  # backstop against a pagination cursor that never ends
        data = cider_api("/api/v1/amapi/run-v3", "POST",
                         {"path": f"/v1/me/library/playlists?limit=100&offset={offset}"})
        page = (data.get("data") or {})
        items = page.get("data") or []
        if not items:
            break
        for i in items:
            name = (i.get("attributes") or {}).get("name", "")
            if name:
                out.append({"name": name, "id": i.get("id", "")})
        if not page.get("next"):
            break
        offset += 100

    _playlist_cache.update(at=time.time(), data=out)
    return out


def get_voices() -> list[dict]:
    """Microsoft's neural voice catalogue, cached for an hour."""
    if _voice_cache["data"] and time.time() - _voice_cache["at"] < 3600:
        return _voice_cache["data"]
    raw = http_json(VOICE_LIST_URL, timeout=25)
    voices = [{
        "id": v["ShortName"],
        "name": v.get("FriendlyName", v["ShortName"]).replace("Microsoft ", "").replace(" Online (Natural)", ""),
        "locale": v.get("Locale", ""),
        "gender": v.get("Gender", ""),
        "personalities": (v.get("VoiceTag") or {}).get("VoicePersonalities", []),
    } for v in raw]
    voices.sort(key=lambda v: (v["locale"], v["id"]))
    _voice_cache.update(at=time.time(), data=voices)
    return voices


# ---------------------------------------------------------------- handler

class Handler(BaseHTTPRequestHandler):
    server_version = "MorningBriefingUI"

    def log_message(self, fmt, *args):  # quieter console
        pass

    # -- plumbing ---------------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, code: int = 200):
        self._send(code, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def _fail(self, message: str, code: int = 400):
        self._json({"ok": False, "error": message}, code)

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    # -- routing ----------------------------------------------------------
    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        route, query = url.path, urllib.parse.parse_qs(url.query)
        try:
            if route in ("/", "/index.html"):
                if not INDEX.exists():
                    return self._fail("ui/index.html is missing", 500)
                return self._send(200, INDEX.read_bytes(), "text/html; charset=utf-8")
            if route in ("/dashboard", "/dashboard.html"):
                if not DASHBOARD.exists():
                    return self._fail("ui/dashboard.html is missing", 500)
                return self._send(200, DASHBOARD.read_bytes(), "text/html; charset=utf-8")
            # The service worker must be served from the root, or its scope is
            # limited to /ui/ and it can't control /dashboard.
            if route == "/sw.js":
                return self.static_file(HERE / "ui" / "sw.js")
            if route.startswith("/ui/"):
                return self.static_file(HERE / "ui" / Path(route[4:]).name)
            if route == "/api/briefing-data":
                return self._json(self.briefing_data())
            if route == "/api/events":
                return self._json({"ok": True, "events": events_store.all_events()})
            if route == "/api/verse":
                return self._json(self.verse(query.get("faith", [""])[0]))
            if route == "/api/stocks":
                return self._json(self.stock_quotes())
            if route == "/api/library":
                return self._json({"ok": True, "books": documents.list_books()})
            if route == "/api/passage":
                return self._json(self.passage(query))
            if route == "/api/todos":
                return self._json({"ok": True, "todos": load_json(TODOS, [])})
            if route == "/api/playback":
                return self._json(self.playback(query.get("full", ["1"])[0] != "0"))
            if route == "/api/month":
                return self._json(self.month(query.get("ym", [""])[0]))
            if route == "/api/queue":
                return self._json(self.queue())
            if route == "/api/providers":
                return self._json({"ok": True,
                                   "providers": music.describe_providers(load_json(CONFIG, {})),
                                   "active": provider().name})
            if route == "/api/config":
                return self._json({
                    "config": load_json(CONFIG, {}),
                    "timetable": load_json(TIMETABLE, {}),
                    "calendar": load_json(CALENDAR, {"events": [], "warnings": []}),
                })
            if route == "/api/status":
                return self._json(self.status())
            if route == "/api/voices":
                return self._json({"ok": True, "voices": get_voices()})
            if route == "/api/geocode":
                return self._json(self.geocode(query.get("q", [""])[0]))
            if route == "/api/playlists":
                return self._json(self.playlists(query.get("q", [""])[0]))
            if route == "/api/preview":
                return self._json(self.preview())
        except urllib.error.HTTPError as exc:
            return self._fail(f"{exc.code} from upstream: {exc.reason}", 502)
        except Exception as exc:
            return self._fail(f"{type(exc).__name__}: {exc}", 500)
        self._fail("not found", 404)

    def do_POST(self):
        route = urllib.parse.urlparse(self.path).path
        try:
            if route == "/api/config":
                payload = json.loads(self._body() or b"{}")
                if "config" in payload:
                    save_json(CONFIG, payload["config"])
                if "timetable" in payload:
                    save_json(TIMETABLE, payload["timetable"])
                return self._json({"ok": True})
            if route == "/api/calendar":
                return self._json(self.import_calendar(self._body()))
            if route == "/api/voice-preview":
                return self.voice_preview(json.loads(self._body() or b"{}"))
            if route == "/api/test":
                return self._json(self.test_run(json.loads(self._body() or b"{}")))
            if route == "/api/schedule":
                return self._json(self.set_schedule(json.loads(self._body() or b"{}")))
            if route == "/api/todos":
                todos = json.loads(self._body() or b"[]")
                if not isinstance(todos, list):
                    return self._fail("expected a list of todos")
                save_json(TODOS, todos)
                return self._json({"ok": True, "todos": todos})
            if route == "/api/refresh":
                return self._json(self.refresh_briefing())
            if route == "/api/events/add":
                payload = json.loads(self._body() or b"{}")
                try:
                    event = events_store.add_event(
                        payload.get("date", ""), payload.get("summary", ""),
                        payload.get("time", ""), payload.get("location", ""))
                except ValueError as exc:
                    return self._fail(str(exc))
                return self._json({"ok": True, "event": event})
            if route == "/api/events/delete":
                payload = json.loads(self._body() or b"{}")
                removed = events_store.delete_event(payload.get("id", ""))
                return self._json({"ok": True, "removed": removed})
            if route == "/api/library/add":
                return self._json(self.library_add())
            if route == "/api/library/delete":
                payload = json.loads(self._body() or b"{}")
                return self._json({"ok": documents.delete_book(payload.get("id", ""))})
            if route == "/api/library/position":
                payload = json.loads(self._body() or b"{}")
                try:
                    book = documents.set_position(payload.get("id", ""), payload.get("index", 0))
                except ValueError as exc:
                    return self._fail(str(exc))
                return self._json({"ok": True, "book": book})
            if route == "/api/read":
                return self.read_passage(json.loads(self._body() or b"{}"))
            if route == "/api/playback":
                return self._json(self.playback_action(json.loads(self._body() or b"{}")))
        except urllib.error.HTTPError as exc:
            return self._fail(f"{exc.code} from upstream: {exc.reason}", 502)
        except Exception as exc:
            return self._fail(f"{type(exc).__name__}: {exc}", 500)
        self._fail("not found", 404)

    # -- endpoints --------------------------------------------------------
    def status(self) -> dict:
        out = {}

        prov = provider()
        state = prov.state(full=False)
        if state.get("ok"):
            out["music"] = {"ok": True,
                            "detail": f"{prov.label} — "
                                      + ("playing" if state.get("playing") else "paused")}
        else:
            out["music"] = {"ok": False,
                            "detail": f"{prov.label} — {state.get('error', 'unavailable')}"}

        try:
            tags = http_json(f"{OLLAMA}/api/tags", timeout=4)
            models = [m["name"] for m in tags.get("models", [])]
            out["ollama"] = {"ok": bool(models), "detail": ", ".join(models) if models else "running, no models pulled",
                             "models": models}
        except Exception:
            out["ollama"] = {"ok": False, "detail": "not running", "models": []}

        # Whether the last briefing run used the LLM or fell back to the
        # mechanical script. Cheap to read; shows up on the status pane so a
        # silent regression (e.g. prompt mis-edited) is obvious.
        llm_status_path = HERE / "llm_status.json"
        if llm_status_path.exists():
            try:
                note = json.loads(llm_status_path.read_text(encoding="utf-8-sig")).get("note", "")
            except Exception:
                note = ""
            out["briefing_llm"] = {
                "ok": note.startswith("llm:"),
                "detail": note or "no run recorded yet",
            }
        else:
            out["briefing_llm"] = {"ok": False, "detail": "no run recorded yet"}

        try:
            get_voices()
            out["voices"] = {"ok": True, "detail": "catalogue reachable"}
        except Exception:
            out["voices"] = {"ok": False, "detail": "offline - will fall back to the local voice"}

        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-ScheduledTaskInfo -TaskName MorningBriefing).NextRunTime.ToString('yyyy-MM-dd HH:mm')"],
                capture_output=True, text=True, timeout=25)
            when = proc.stdout.strip()
            out["schedule"] = {"ok": bool(when), "detail": f"next run {when}" if when else "task not installed"}
        except Exception:
            out["schedule"] = {"ok": False, "detail": "could not read the scheduled task"}

        return out

    def geocode(self, q: str) -> dict:
        if not q.strip():
            return {"ok": False, "error": "type a town name"}
        params = urllib.parse.urlencode({"name": q, "count": 8, "language": "en", "format": "json"})
        data = http_json(f"https://geocoding-api.open-meteo.com/v1/search?{params}", timeout=15)
        results = [{
            "name": r["name"],
            "admin": r.get("admin1", ""),
            "country": r.get("country", ""),
            "country_code": r.get("country_code", ""),
            "latitude": r["latitude"],
            "longitude": r["longitude"],
            "timezone": r.get("timezone", ""),
        } for r in (data.get("results") or [])]

        # The geocoder still lists several South African towns under their
        # pre-2018 names, so offer the old name as a fallback search.
        alias = {"makhanda": "Grahamstown", "gqeberha": "Port Elizabeth", "kariega": "Uitenhage",
                 "ethekwini": "Durban", "tshwane": "Pretoria", "mangaung": "Bloemfontein"}.get(q.strip().lower())
        note = ""
        if not results and alias:
            return {**self.geocode(alias), "note": f"No match for '{q}' - showing results for '{alias}' instead."}
        return {"ok": True, "results": results, "note": note}

    def playlists(self, q: str) -> dict:
        prov = provider()
        if not prov.capabilities.get("playlists"):
            return {"ok": False,
                    "error": f"{prov.label} can't browse playlists - "
                             "start one in the app yourself and Morning Brief will pick it up."}
        names = prov.all_playlists()

        if not q.strip():
            return {"ok": True, "exact": None, "matches": names[:40], "total": len(names)}

        needle = q.strip().lower()
        exact = next((n for n in names if n["name"].strip().lower() == needle), None)
        partial = [n for n in names if needle in n["name"].lower() and n is not exact]
        return {"ok": True, "exact": exact, "matches": partial[:40], "total": len(names)}

    def import_calendar(self, raw: bytes) -> dict:
        if not raw.strip():
            return {"ok": False, "error": "empty file"}
        tmp = HERE / "_upload.ics"
        try:
            tmp.write_bytes(raw)
            parsed = calendar_ics.parse(tmp, days_ahead=60)
            if not parsed["events"] and b"BEGIN:VEVENT" not in raw.upper():
                return {"ok": False, "error": "That doesn't look like an .ics calendar file."}
            parsed["imported_at"] = dt.datetime.now().isoformat(timespec="seconds")
            save_json(CALENDAR, parsed)
            return {"ok": True, **parsed}
        finally:
            tmp.unlink(missing_ok=True)

    def voice_preview(self, payload: dict):
        voice = payload.get("voice") or "en-ZA-LeahNeural"
        text = (payload.get("text") or
                "Good morning. Here's your briefing: eight degrees and clear, "
                "with four classes today.")
        out = HERE / "_preview.mp3"
        proc = subprocess.run(
            [sys.executable, str(HERE / "say_online.py"), "--voice", voice,
             "--out", str(out), "--text", text],
            capture_output=True, text=True, timeout=90)
        if proc.returncode != 0 or not out.exists() or out.stat().st_size == 0:
            detail = (proc.stderr or "").strip().splitlines()
            return self._fail(detail[-1] if detail else "voice preview failed", 502)
        try:
            self._send(200, out.read_bytes(), "audio/mpeg")
        finally:
            out.unlink(missing_ok=True)

    def preview(self) -> dict:
        proc = subprocess.run([sys.executable, str(HERE / "briefing.py"), "--speak-only", "--no-color"],
                              capture_output=True, text=True, timeout=120, encoding="utf-8", errors="replace")
        if proc.returncode != 0:
            return {"ok": False, "error": (proc.stderr or "briefing.py failed").strip()[-400:]}
        return {"ok": True, "text": proc.stdout.strip()}

    def test_run(self, payload: dict) -> dict:
        what = payload.get("what", "briefing")
        script = "morning-routine.ps1" if what == "routine" else "run-briefing.ps1"
        args = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", str(HERE / script)]
        if what == "routine":
            args += ["-IntroSeconds", str(int(payload.get("intro", 5))), "-HoldSeconds", "1"]
        else:
            args += ["-HoldSeconds", "1"]
        # Detached: the UI shouldn't block for the length of a spoken briefing.
        subprocess.Popen(args, creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
        return {"ok": True, "started": script}

    # -- dashboard --------------------------------------------------------
    def briefing_data(self) -> dict:
        data = load_json(BRIEF_DATA, None)
        if not data:
            return {"ok": True, "data": None, "age_minutes": None}
        age = None
        try:
            made = dt.datetime.fromisoformat(data.get("generated_at", ""))
            # briefing.py stamps this with a ZoneInfo, so it's offset-aware -
            # comparing it to a naive now() is a TypeError, not a ValueError.
            now = dt.datetime.now(made.tzinfo) if made.tzinfo else dt.datetime.now()
            age = round((now - made).total_seconds() / 60)
        except (ValueError, TypeError):
            pass
        return {"ok": True, "data": data, "age_minutes": age}

    def refresh_briefing(self) -> dict:
        proc = subprocess.run(
            [sys.executable, str(HERE / "briefing.py"), "--no-color",
             "--data-out", str(BRIEF_DATA)],
            capture_output=True, text=True, timeout=180, encoding="utf-8", errors="replace")
        if proc.returncode != 0:
            return {"ok": False, "error": (proc.stderr or "briefing.py failed").strip()[-300:]}
        return self.briefing_data()

    def playback(self, full: bool = True) -> dict:
        """Transport state from whichever provider is configured.

        The response also carries the provider's capabilities so the dashboard
        can grey out what this source genuinely cannot do - a YouTube video has
        no next track, and nothing outside Cider exposes a queue.
        """
        prov = provider()
        state = prov.state(full=full)
        state.setdefault("provider", prov.name)
        state["capabilities"] = prov.capabilities
        return state

    def queue(self, limit: int = 60) -> dict:
        return provider().queue(limit)

    def playback_action(self, payload: dict) -> dict:
        prov = provider()
        action = payload.get("action", "")
        kwargs = {k: v for k, v in payload.items() if k != "action"}
        result = prov.control(action, **kwargs)
        result.setdefault("provider", prov.name)
        result["capabilities"] = prov.capabilities
        return result

    def month(self, ym: str) -> dict:
        """Per-day classes and events for a calendar grid."""
        try:
            year, month = (int(x) for x in ym.split("-"))
            first = dt.date(year, month, 1)
        except (ValueError, AttributeError):
            today = dt.date.today()
            first, year, month = today.replace(day=1), today.year, today.month

        nxt = dt.date(year + (month == 12), (month % 12) + 1, 1)
        timetable = load_json(TIMETABLE, {})

        # Merged store, not the raw import: manual events must show up and
        # deleted ones must stay gone.
        by_date: dict[str, list] = {}
        for ev in events_store.all_events():
            by_date.setdefault(ev.get("date", ""), []).append(ev)

        days = {}
        day = first
        while day < nxt:
            running, note = briefing.academic_status(timetable, day)
            classes = timetable.get(briefing.WEEKDAYS[day.weekday()]) or [] if running else []
            key = day.isoformat()
            days[key] = {
                "in_term": running,
                "note": note,
                "classes": sorted(classes, key=lambda c: c.get("start", "99:99")),
                "events": by_date.get(key, []),
            }
            day += dt.timedelta(days=1)

        return {"ok": True, "year": year, "month": month,
                "first_weekday": first.weekday(), "days": days}

    # -- static ------------------------------------------------------------
    TYPES = {".png": "image/png", ".js": "application/javascript",
             ".webmanifest": "application/manifest+json", ".json": "application/json",
             ".css": "text/css", ".svg": "image/svg+xml", ".ico": "image/x-icon"}

    def static_file(self, path: Path):
        # Path(...).name above strips any directory part, so a crafted URL
        # cannot climb out of ui/.
        if not path.exists() or not path.is_file():
            return self._fail("not found", 404)
        self._send(200, path.read_bytes(),
                   self.TYPES.get(path.suffix.lower(), "application/octet-stream"))

    # -- verse / markets / library ----------------------------------------
    def verse(self, faith: str) -> dict:
        cfg = load_json(CONFIG, {})
        faith = faith or (cfg.get("verse") or {}).get("faith", "christianity")
        result = scripture.verse_of_the_day(faith)
        if not result:
            return {"ok": False, "error": f"unknown faith '{faith}'"}
        return {"ok": True, "verse": result}

    def stock_quotes(self) -> dict:
        opts = load_json(CONFIG, {}).get("stocks") or {}
        return {"ok": True, "quotes": stocks.quotes(opts.get("symbols") or [])}

    def passage(self, query: dict) -> dict:
        book_id = query.get("id", [""])[0]
        try:
            index = int(query.get("index", ["0"])[0])
        except ValueError:
            index = 0
        try:
            return {"ok": True, **documents.get_passage(book_id, index)}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

    def library_add(self) -> dict:
        """Upload a document. The filename rides in a header so the body stays raw."""
        filename = self.headers.get("X-Filename") or "document.txt"
        filename = urllib.parse.unquote(filename)
        raw = self._body()
        try:
            return {"ok": True, "book": documents.add_document(filename, raw)}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

    def read_passage(self, payload: dict):
        """Synthesise one passage and return it as audio."""
        text = (payload.get("text") or "").strip()
        if not text:
            book_id, index = payload.get("id", ""), int(payload.get("index", 0))
            try:
                text = documents.get_passage(book_id, index)["text"]
            except ValueError as exc:
                return self._fail(str(exc))
        if not text:
            return self._fail("nothing to read")

        speech = load_json(CONFIG, {}).get("speech") or {}
        voice = payload.get("voice") or speech.get("online_voice") or "en-ZA-LeahNeural"
        rate = int(speech.get("rate", 0) or 0)
        pct = f"{'+' if rate >= 0 else '-'}{abs(rate * 5)}%"

        out = HERE / f"_read_{abs(hash(text)) % 10**8}.mp3"
        try:
            proc = subprocess.run(
                [sys.executable, str(HERE / "say_online.py"), "--voice", voice,
                 "--out", str(out), "--rate", pct, "--text", text],
                capture_output=True, text=True, timeout=180)
            if proc.returncode != 0 or not out.exists() or out.stat().st_size == 0:
                detail = (proc.stderr or "").strip().splitlines()
                return self._fail(detail[-1] if detail else "speech failed", 502)
            self._send(200, out.read_bytes(), "audio/mpeg")
        finally:
            out.unlink(missing_ok=True)

    def set_schedule(self, payload: dict) -> dict:
        when = str(payload.get("time", "")).strip()
        if not dt.datetime.strptime(when, "%H:%M"):
            return {"ok": False, "error": "time must look like 06:30"}
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(HERE / "install-task.ps1"), "-Time", when],
            capture_output=True, text=True, timeout=90)
        ok = proc.returncode == 0
        return {"ok": ok, "detail": (proc.stdout or proc.stderr).strip()[-300:]}


def main() -> int:
    if not INDEX.exists():
        print(f"missing {INDEX}", file=sys.stderr)
        return 1
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}"
    print(f"Morning Briefing settings: {url}")
    print("Press Ctrl+C to stop.")
    if "--no-open" not in sys.argv:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
