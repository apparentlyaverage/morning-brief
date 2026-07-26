"""Music providers.

Everything that plays music sits behind one interface so the rest of the app
never talks to a specific service. Two providers today:

  CiderProvider   - Cider's local API. Full control: playlists, queue, volume.
  SystemProvider  - Windows' global media transport controls. Works with any
                    player that registers there (Spotify desktop, Apple Music,
                    YouTube Music in a browser) with no API key, no developer
                    agreement and no paid tier - but it can only drive what is
                    already playing. It cannot start a named playlist, read a
                    queue, or set volume.

Providers advertise what they can do via `capabilities`, and callers are
expected to check rather than assume; an unsupported call returns
{"ok": False, "error": ...} instead of raising.
"""

from __future__ import annotations

import json
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent

# 127.0.0.1, never "localhost" - localhost resolves to ::1 first and these
# servers bind IPv4 only, costing about two seconds per request.
CIDER_BASE = "http://127.0.0.1:10767"


def _http_json(url, method="GET", body=None, headers=None, timeout=12):
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


class Provider:
    """Base. Anything unsupported answers politely rather than blowing up."""

    name = "none"
    label = "None"
    capabilities = {"transport": False, "seek": False, "volume": False,
                    "queue": False, "playlists": False}

    def available(self) -> bool:
        return False

    def state(self, full: bool = True) -> dict:
        return self._no("state")

    def control(self, action: str, **kw) -> dict:
        return self._no(action)

    def queue(self, limit: int = 60) -> dict:
        return self._no("queue")

    def playlists(self, query: str = "") -> dict:
        return self._no("playlists")

    def start_playlist(self, name: str) -> dict:
        return self._no("start_playlist")

    @staticmethod
    def _no(what: str) -> dict:
        return {"ok": False, "error": f"this music source does not support {what}"}


# --------------------------------------------------------------------- Cider

class CiderProvider(Provider):
    name = "cider"
    label = "Cider (Apple Music)"
    capabilities = {"transport": True, "seek": True, "volume": True,
                    "queue": True, "playlists": True}

    def __init__(self, token: str = ""):
        self.token = token or ""
        self._playlist_cache = {"at": 0.0, "data": None}

    def _api(self, path, method="GET", body=None):
        return _http_json(f"{CIDER_BASE}{path}", method,
                          body if body is not None else ({} if method != "GET" else None),
                          {"apptoken": self.token})

    def available(self) -> bool:
        if not self.token:
            return False
        try:
            self._api("/api/v1/playback/is-playing")
            return True
        except Exception:
            return False

    def state(self, full: bool = True) -> dict:
        if not self.token:
            return {"ok": False, "error": "no Cider token"}
        try:
            playing = self._api("/api/v1/playback/is-playing").get("is_playing", False)
            shuffle = self._api("/api/v1/playback/shuffle-mode").get("value", 0)
            repeat = self._api("/api/v1/playback/repeat-mode").get("value", 0)
        except Exception as exc:
            return {"ok": False, "error": f"Cider unreachable ({type(exc).__name__})"}

        out = {"ok": True, "provider": self.name, "playing": bool(playing),
               "shuffle": int(shuffle or 0), "repeat": int(repeat or 0), "full": full}
        if not full:
            return out
        try:
            info = self._api("/api/v1/playback/now-playing").get("info", {}) or {}
            ms = info.get("durationInMillis")
            out.update(track=info.get("name", ""), artist=info.get("artistName", ""),
                       album=info.get("albumName", ""),
                       artwork=(info.get("artwork") or {}).get("url", ""),
                       elapsed=info.get("currentPlaybackTime"),
                       duration=(ms / 1000.0) if ms else None)
        except Exception:
            out["full"] = False
        return out

    def control(self, action: str, **kw) -> dict:
        simple = {"play": "/api/v1/playback/play", "pause": "/api/v1/playback/pause",
                  "next": "/api/v1/playback/next", "previous": "/api/v1/playback/previous",
                  "shuffle": "/api/v1/playback/toggle-shuffle",
                  "repeat": "/api/v1/playback/toggle-repeat"}
        try:
            if action == "seek":
                self._api("/api/v1/playback/seek", "POST",
                          {"position": max(0.0, float(kw.get("position", 0)))})
            elif action == "jump":
                self._api("/api/v1/playback/queue/change-to-index", "POST",
                          {"index": max(0, int(kw.get("index", 0)))})
            elif action == "volume":
                self._api("/api/v1/playback/volume", "POST",
                          {"volume": max(0.0, min(1.0, float(kw.get("value", 0.5))))})
            elif action == "set-shuffle":
                # Cider only exposes a toggle, so read first and flip if needed.
                want = 1 if kw.get("on") else 0
                now = int(self._api("/api/v1/playback/shuffle-mode").get("value", 0) or 0)
                if now != want:
                    self._api("/api/v1/playback/toggle-shuffle", "POST")
            elif action in simple:
                self._api(simple[action], "POST")
            else:
                return {"ok": False, "error": f"unknown action '{action}'"}
        except Exception as exc:
            return {"ok": False, "error": f"Cider: {type(exc).__name__}"}

        time.sleep(0.4)  # Cider's reported state lags a command by a beat
        return self.state(full=action in ("next", "previous", "jump", "seek"))

    def all_playlists(self, force: bool = False) -> list[dict]:
        """Every playlist, following pagination - a page is capped at 100."""
        cache = self._playlist_cache
        if not force and cache["data"] and time.time() - cache["at"] < 120:
            return cache["data"]

        out, offset = [], 0
        while offset < 5000:
            data = self._api("/api/v1/amapi/run-v3", "POST",
                             {"path": f"/v1/me/library/playlists?limit=100&offset={offset}"})
            page = data.get("data") or {}
            items = page.get("data") or []
            if not items:
                break
            for entry in items:
                name = (entry.get("attributes") or {}).get("name", "")
                if name:
                    out.append({"name": name, "id": entry.get("id", "")})
            if not page.get("next"):
                break
            offset += 100

        cache.update(at=time.time(), data=out)
        return out

    def playlists(self, query: str = "") -> dict:
        if not self.token:
            return {"ok": False, "error": "No Cider API token saved yet."}
        try:
            names = self.all_playlists()
        except Exception as exc:
            return {"ok": False, "error": f"Cider unreachable ({type(exc).__name__})"}
        if not query.strip():
            return {"ok": True, "exact": None, "matches": names[:40], "total": len(names)}
        needle = query.strip().lower()
        exact = next((n for n in names if n["name"].strip().lower() == needle), None)
        partial = [n for n in names if needle in n["name"].lower() and n is not exact]
        return {"ok": True, "exact": exact, "matches": partial[:40], "total": len(names)}

    def start_playlist(self, name: str) -> dict:
        found = self.playlists(name)
        if not found.get("ok"):
            return found
        match = found.get("exact") or (found.get("matches") or [None])[0]
        if not match:
            return {"ok": False, "error": f"no playlist matching '{name}'"}
        try:
            self._api("/api/v1/playback/play-item-href", "POST",
                      {"href": f"/v1/me/library/playlists/{match['id']}"})
        except Exception as exc:
            return {"ok": False, "error": f"Cider: {type(exc).__name__}"}
        return {"ok": True, "playlist": match["name"]}

    def queue(self, limit: int = 60) -> dict:
        if not self.token:
            return {"ok": False, "error": "no Cider token"}
        try:
            raw = self._api("/api/v1/playback/queue")
            now = self._api("/api/v1/playback/now-playing").get("info", {}) or {}
        except Exception as exc:
            return {"ok": False, "error": f"Cider unreachable ({type(exc).__name__})"}

        items = raw if isinstance(raw, list) else (raw.get("data") or [])
        playing_name = (now.get("name") or "").strip()
        playing_artist = (now.get("artistName") or "").strip()

        out, current = [], -1
        for index, entry in enumerate(items[:limit]):
            attrs = (entry.get("attributes") if isinstance(entry, dict) else {}) or {}
            name = (attrs.get("name") or "").strip()
            artist = (attrs.get("artistName") or "").strip()
            # No explicit "current index" exists, so match the playing track.
            # First match wins, or a repeat later in the queue steals it.
            if current < 0 and name == playing_name and artist == playing_artist:
                current = index
            ms = attrs.get("durationInMillis")
            out.append({"index": index, "name": name, "artist": artist,
                        "album": attrs.get("albumName", ""),
                        "seconds": (ms / 1000.0) if ms else None,
                        "artwork": (attrs.get("artwork") or {}).get("url", "")})
        return {"ok": True, "items": out, "current": current, "total": len(items)}


# ------------------------------------------------------------ system media

class SystemProvider(Provider):
    """Whatever Windows itself is playing - any app, any service."""

    name = "system"
    label = "Whatever's playing (Spotify, YouTube Music, anything)"
    capabilities = {"transport": True, "seek": True, "volume": False,
                    "queue": False, "playlists": False}

    def __init__(self, app: str = ""):
        self.app = app or ""          # "" means whichever session is active
        self._cache = {"at": 0.0, "data": None}

    def _run(self, *args, timeout=25) -> dict:
        script = HERE / "media.ps1"
        if not script.exists():
            return {"ok": False, "error": "media.ps1 is missing"}
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-File", str(script), *args],
                capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "Windows media controls timed out"}
        text = (proc.stdout or "").strip()
        if not text:
            return {"ok": False, "error": (proc.stderr or "no response").strip()[:120]}
        try:
            return json.loads(text.splitlines()[-1])
        except json.JSONDecodeError:
            return {"ok": False, "error": "unreadable response from media controls"}

    def sessions(self) -> list[dict]:
        result = self._run("-Action", "list")
        return result if isinstance(result, list) else []

    def available(self) -> bool:
        return bool(self.sessions())

    def state(self, full: bool = True) -> dict:
        # Each call spawns PowerShell (~0.7-1.5s), so a short cache keeps a
        # 5-second UI poll to one round trip rather than several.
        cache = self._cache
        if cache["data"] and time.time() - cache["at"] < 1.5:
            return cache["data"]

        args = ["-Action", "state"] + (["-App", self.app] if self.app else [])
        result = self._run(*args)
        if not result.get("ok"):
            return {"ok": False, "error": result.get("error", "nothing is playing")}

        out = {"ok": True, "provider": self.name, "full": True,
               "playing": bool(result.get("playing")),
               "track": result.get("track", ""), "artist": result.get("artist", ""),
               "album": result.get("album", ""), "artwork": "",
               "elapsed": result.get("elapsed"), "duration": result.get("duration") or None,
               "shuffle": 0, "repeat": 0,
               "source_app": result.get("app", ""),
               # These vary per app: a YouTube video has no next track.
               "can_next": bool(result.get("can_next")),
               "can_previous": bool(result.get("can_previous")),
               "can_seek": bool(result.get("can_seek"))}
        cache.update(at=time.time(), data=out)
        return out

    def control(self, action: str, **kw) -> dict:
        allowed = {"play", "pause", "playpause", "next", "previous", "seek"}
        if action not in allowed:
            return self._no(action)
        args = ["-Action", action]
        if action == "seek":
            args += ["-Position", str(float(kw.get("position", 0)))]
        if self.app:
            args += ["-App", self.app]
        result = self._run(*args)
        if not result.get("ok"):
            return {"ok": False, "error": result.get("error", "the player refused that")}
        self._cache["data"] = None      # force a fresh read
        time.sleep(0.3)
        return self.state()


# ------------------------------------------------------------------ factory

def get_provider(cfg: dict) -> Provider:
    """Pick a provider from config. 'auto' prefers Cider, falls back to Windows."""
    music = cfg.get("music") or {}
    choice = (music.get("provider") or "auto").lower()
    cider_token = (cfg.get("cider") or {}).get("token", "")

    if choice == "cider":
        return CiderProvider(cider_token)
    if choice == "system":
        return SystemProvider(music.get("app", ""))

    cider = CiderProvider(cider_token)
    return cider if cider.available() else SystemProvider(music.get("app", ""))


def describe_providers(cfg: dict) -> list[dict]:
    """For the settings page: what's on offer and what each can actually do."""
    cider = CiderProvider((cfg.get("cider") or {}).get("token", ""))
    system = SystemProvider()
    return [
        {"name": p.name, "label": p.label, "capabilities": p.capabilities,
         "available": p.available()}
        for p in (cider, system)
    ]
