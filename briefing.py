"""Morning briefing: weather, South African news, and today's classes.

Standard library only. Run with:  python briefing.py
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import datetime as dt
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
TIMETABLE_PATH = HERE / "timetable.json"
LLM_STATUS_PATH = HERE / "llm_status.json"  # last-run note: "llm" | "fallback: <reason>"

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) morning-briefing/1.0"
HTTP_TIMEOUT = 12

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

# WMO weather interpretation codes -> (description, emoji)
WMO = {
    0: ("Clear sky", "☀️"),
    1: ("Mainly clear", "\U0001f324️"),
    2: ("Partly cloudy", "⛅"),
    3: ("Overcast", "☁️"),
    45: ("Fog", "\U0001f32b️"),
    48: ("Rime fog", "\U0001f32b️"),
    51: ("Light drizzle", "\U0001f327️"),
    53: ("Drizzle", "\U0001f327️"),
    55: ("Heavy drizzle", "\U0001f327️"),
    56: ("Freezing drizzle", "\U0001f328️"),
    57: ("Freezing drizzle", "\U0001f328️"),
    61: ("Light rain", "\U0001f326️"),
    63: ("Rain", "\U0001f327️"),
    65: ("Heavy rain", "\U0001f327️"),
    66: ("Freezing rain", "\U0001f328️"),
    67: ("Freezing rain", "\U0001f328️"),
    71: ("Light snow", "\U0001f328️"),
    73: ("Snow", "❄️"),
    75: ("Heavy snow", "❄️"),
    77: ("Snow grains", "❄️"),
    80: ("Light showers", "\U0001f326️"),
    81: ("Showers", "\U0001f327️"),
    82: ("Violent showers", "⛈️"),
    85: ("Snow showers", "\U0001f328️"),
    86: ("Snow showers", "\U0001f328️"),
    95: ("Thunderstorm", "⛈️"),
    96: ("Thunderstorm + hail", "⛈️"),
    99: ("Thunderstorm + hail", "⛈️"),
}


# --------------------------------------------------------------------------
# terminal helpers
# --------------------------------------------------------------------------

class C:
    """ANSI colours, blanked out when the output is not a terminal."""
    RESET = "\033[0m"
    DIM = "\033[2m"
    BOLD = "\033[1m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    WHITE = "\033[97m"

    @classmethod
    def disable(cls) -> None:
        for attr in dir(cls):
            if attr.isupper():
                setattr(cls, attr, "")


def enable_ansi() -> None:
    """Turn on virtual-terminal processing so ANSI codes work in conhost."""
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
    except Exception:
        pass


def rule(char: str = "─", width: int = 62) -> str:
    return C.DIM + char * width + C.RESET


def heading(text: str, colour: str) -> str:
    return f"\n{colour}{C.BOLD}{text}{C.RESET}\n{rule()}"


# --------------------------------------------------------------------------
# network
# --------------------------------------------------------------------------

def fetch(url: str, timeout: int = HTTP_TIMEOUT) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_json(url: str, timeout: int = HTTP_TIMEOUT):
    return json.loads(fetch(url, timeout).decode("utf-8", "replace"))


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

def load_config() -> dict:
    if not CONFIG_PATH.exists():
        sys.exit(f"Missing config file: {CONFIG_PATH}")
    # utf-8-sig, not utf-8: PowerShell 5.1's Set-Content -Encoding utf8 writes
    # a BOM, so the config setup.ps1 writes on a fresh install is unreadable as
    # plain utf-8 - the app died on its very first run. Notepad does the same.
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")


# The geocoding database still uses several pre-2018 South African place names.
CITY_ALIASES = {
    "makhanda": "Grahamstown",
    "gqeberha": "Port Elizabeth",
    "kariega": "Uitenhage",
    "ethekwini": "Durban",
    "tshwane": "Pretoria",
    "mangaung": "Bloemfontein",
    "nelson mandela bay": "Port Elizabeth",
}


def _geocode(name: str, country: str) -> tuple[float, float] | None:
    params = urllib.parse.urlencode({
        "name": name,
        "count": 10,
        "language": "en",
        "format": "json",
    })
    try:
        data = fetch_json(f"https://geocoding-api.open-meteo.com/v1/search?{params}")
    except Exception:
        return None

    results = data.get("results") or []
    if not results:
        return None

    # Prefer a hit in the configured country; the database has, for example,
    # a Makhanda in Kazakhstan and a Grahamstown in Australia.
    for hit in results:
        if (hit.get("country_code") or "").upper() == country.upper():
            return hit["latitude"], hit["longitude"]
    return None


def resolve_coords(cfg: dict) -> tuple[float, float] | None:
    """Geocode the configured city once, then cache the result in config.json."""
    if cfg.get("latitude") is not None and cfg.get("longitude") is not None:
        return float(cfg["latitude"]), float(cfg["longitude"])

    city = cfg.get("city")
    if not city:
        return None
    country = cfg.get("country", "ZA")

    candidates = [city]
    alias = CITY_ALIASES.get(city.strip().lower())
    if alias:
        candidates.append(alias)

    for candidate in candidates:
        coords = _geocode(candidate, country)
        if coords:
            cfg["latitude"], cfg["longitude"] = coords
            save_config(cfg)
            return coords
    return None


# --------------------------------------------------------------------------
# weather
# --------------------------------------------------------------------------

def get_weather(cfg: dict) -> dict | None:
    coords = resolve_coords(cfg)
    if not coords:
        return None
    lat, lon = coords

    params = urllib.parse.urlencode({
        "latitude": lat,
        "longitude": lon,
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m",
        "hourly": "temperature_2m,precipitation_probability,weather_code",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                 "precipitation_probability_max,precipitation_sum,sunrise,sunset,uv_index_max",
        "timezone": cfg.get("timezone", "Africa/Johannesburg"),
        "forecast_days": 1,
    })
    try:
        return fetch_json(f"https://api.open-meteo.com/v1/forecast?{params}")
    except Exception as exc:
        return {"_error": str(exc)}


def describe(code) -> tuple[str, str]:
    try:
        return WMO.get(int(code), ("Unknown", "\U0001f321️"))
    except (TypeError, ValueError):
        return ("Unknown", "\U0001f321️")


def render_weather(weather: dict | None, cfg: dict, now: dt.datetime) -> str:
    out = [heading(f"WEATHER — {cfg.get('city', 'unknown')}", C.CYAN)]

    if weather is None:
        out.append(f"  {C.RED}Could not work out coordinates for "
                   f"'{cfg.get('city')}'. Check the city name in config.json.{C.RESET}")
        return "\n".join(out)
    if "_error" in weather:
        out.append(f"  {C.RED}Weather unavailable ({weather['_error']}){C.RESET}")
        return "\n".join(out)

    cur = weather.get("current", {})
    daily = weather.get("daily", {})
    desc, icon = describe(cur.get("weather_code"))

    temp = cur.get("temperature_2m")
    feels = cur.get("apparent_temperature")
    hi = (daily.get("temperature_2m_max") or [None])[0]
    lo = (daily.get("temperature_2m_min") or [None])[0]
    rain = (daily.get("precipitation_probability_max") or [None])[0]
    mm = (daily.get("precipitation_sum") or [None])[0]
    uv = (daily.get("uv_index_max") or [None])[0]
    wind = cur.get("wind_speed_10m")
    sunrise = (daily.get("sunrise") or [None])[0]
    sunset = (daily.get("sunset") or [None])[0]

    out.append(f"  {icon}  {C.BOLD}{C.WHITE}{temp:.0f}°C{C.RESET}  {desc}"
               f"{C.DIM}  (feels like {feels:.0f}°){C.RESET}")

    if hi is not None and lo is not None:
        out.append(f"  {C.DIM}Today{C.RESET}      high {C.RED}{hi:.0f}°{C.RESET}"
                   f"   low {C.BLUE}{lo:.0f}°{C.RESET}"
                   f"   wind {wind:.0f} km/h"
                   + (f"   UV {uv:.0f}" if uv is not None else ""))

    if rain is not None:
        bar_len = int(round(rain / 10))
        bar = "█" * bar_len + C.DIM + "░" * (10 - bar_len) + C.RESET
        colour = C.BLUE if rain >= 40 else C.DIM
        extra = f"  ({mm:.1f} mm)" if mm else ""
        out.append(f"  {C.DIM}Rain{C.RESET}       {colour}{bar}{C.RESET} {rain:.0f}%{extra}")

    if sunrise and sunset:
        out.append(f"  {C.DIM}Sun{C.RESET}        up {sunrise[11:16]}   down {sunset[11:16]}")

    # Next few daylight hours, so you know what to wear / whether to take a jacket.
    hourly = weather.get("hourly", {})
    times = hourly.get("time") or []
    temps = hourly.get("temperature_2m") or []
    pops = hourly.get("precipitation_probability") or []
    codes = hourly.get("weather_code") or []

    upcoming = []
    for i, stamp in enumerate(times):
        try:
            when = dt.datetime.fromisoformat(stamp)
        except ValueError:
            continue
        if when < now.replace(tzinfo=None, minute=0, second=0, microsecond=0):
            continue
        if len(upcoming) >= 4:
            break
        if i < len(temps) and i < len(pops) and i < len(codes):
            _, ic = describe(codes[i])
            upcoming.append(f"{when.strftime('%H:%M')} {ic} {temps[i]:.0f}° "
                            f"{C.DIM}{pops[i]:.0f}%{C.RESET}")

    if upcoming:
        out.append(f"  {C.DIM}Ahead{C.RESET}      " + "   ".join(upcoming))

    if rain is not None and rain >= 50:
        out.append(f"  {C.YELLOW}→ Take a jacket.{C.RESET}")

    return "\n".join(out)


# --------------------------------------------------------------------------
# news
# --------------------------------------------------------------------------

TAG_RE = re.compile(r"<[^>]+>")
# Feeds bolt their own masthead onto headlines, e.g. "News24 | Real headline".
PREFIX_RE = re.compile(r"^[A-Za-z0-9&.' ]{2,20}\s*\|\s*")
# Daily Maverick style all-caps kicker, e.g. "DIPLOMACY DANCE: Ambassador ...".
KICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9 ,'&/-]{3,40}:\s*")


def clean_text(raw: str | None) -> str:
    if not raw:
        return ""
    text = TAG_RE.sub("", raw)
    text = html.unescape(text)
    return " ".join(text.split())


def tidy_title(title: str) -> str:
    """Drop the masthead prefix feeds put on their own headlines."""
    return PREFIX_RE.sub("", title).strip()


def speakable_title(title: str) -> str:
    """As above, minus the shouty kicker that speech engines spell out letter by letter."""
    return KICKER_RE.sub("", tidy_title(title)).strip()


def strip_ns(tag: str) -> str:
    return tag.split("}", 1)[-1]


def parse_feed(data: bytes, limit: int) -> list[dict]:
    """Parse RSS 2.0 or Atom into a common shape."""
    root = ET.fromstring(data)
    items: list[dict] = []

    nodes = root.findall(".//item")
    if not nodes:  # Atom
        nodes = [n for n in root.iter() if strip_ns(n.tag) == "entry"]

    for node in nodes[:limit]:
        title = summary = link = ""
        for child in node:
            name = strip_ns(child.tag)
            if name == "title" and not title:
                title = clean_text(child.text)
            elif name in ("description", "summary") and not summary:
                summary = clean_text(child.text)
            elif name == "link" and not link:
                # RSS puts the URL in the element text; Atom puts it in href
                # and may also carry rel="self", which is not the article.
                if child.get("rel") in (None, "alternate"):
                    link = (child.get("href") or clean_text(child.text) or "").strip()
        if title:
            items.append({"title": title, "summary": summary, "link": link})
    return items


def get_news(cfg: dict) -> list[dict]:
    # A feed is on unless the settings page explicitly switched it off.
    feeds = [f for f in cfg.get("feeds", []) if f.get("enabled", True)]
    limit = int(cfg.get("news_limit_per_feed", 4))
    results: list[dict] = []

    def one(feed: dict) -> dict:
        try:
            items = parse_feed(fetch(feed["url"], timeout=10), limit)
            return {"name": feed["name"], "items": items, "error": None}
        except Exception as exc:
            return {"name": feed["name"], "items": [], "error": str(exc)}

    if not feeds:
        return results

    with futures.ThreadPoolExecutor(max_workers=min(8, len(feeds))) as pool:
        for res in pool.map(one, feeds):
            results.append(res)
    return results


def render_news(sources: list[dict], cfg: dict) -> str:
    out = [heading("NEWS", C.YELLOW)]

    live = [s for s in sources if s["items"]]
    if not live:
        out.append(f"  {C.RED}No feeds reachable — are you online?{C.RESET}")
        return "\n".join(out)

    budget = int(cfg.get("total_headlines", 12))
    seen: set[str] = set()
    shown = 0

    # Round-robin across sources so one chatty feed doesn't drown the rest.
    depth = 0
    while shown < budget:
        added_this_pass = False
        for src in live:
            if shown >= budget:
                break
            if depth >= len(src["items"]):
                continue
            item = src["items"][depth]
            key = item["title"].lower()[:60]
            if key in seen:
                continue
            seen.add(key)
            added_this_pass = True
            shown += 1
            out.append(f"  {C.DIM}{src['name']:<16}{C.RESET}{tidy_title(item['title'])}")
        if not added_this_pass:
            break
        depth += 1

    dead = [s["name"] for s in sources if s["error"]]
    if dead:
        out.append(f"  {C.DIM}(unreachable: {', '.join(dead)}){C.RESET}")

    return "\n".join(out)


# --------------------------------------------------------------------------
# classes
# --------------------------------------------------------------------------

def load_timetable() -> dict:
    if not TIMETABLE_PATH.exists():
        return {}
    try:
        # utf-8-sig for the same reason as the config: a timetable saved from
        # Notepad or written by PowerShell carries a BOM.
        return json.loads(TIMETABLE_PATH.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        return {"_error": f"timetable.json is not valid JSON — {exc}"}


def _span(block: dict) -> tuple[dt.date, dt.date] | None:
    try:
        return dt.date.fromisoformat(block["start"]), dt.date.fromisoformat(block["end"])
    except (KeyError, TypeError, ValueError):
        return None


def academic_status(timetable: dict, today: dt.date) -> tuple[bool, str]:
    """(lectures_running, human explanation) for today's date."""
    cal = timetable.get("_calendar") or {}
    terms = cal.get("terms") or []
    if not terms:
        return True, ""

    # Swot weeks, exams and Easter sit inside a term but have no normal classes.
    for block in cal.get("no_lectures") or []:
        span = _span(block)
        if span and span[0] <= today <= span[1]:
            return False, block.get("name", "no lectures")

    for term in terms:
        span = _span(term)
        if span and span[0] <= today <= span[1]:
            return True, term.get("name", "")

    # Between terms — say when the next one starts.
    upcoming = sorted(
        (s for s in (_span(t) for t in terms) if s and s[0] > today),
        key=lambda s: s[0],
    )
    if upcoming:
        start = upcoming[0][0]
        days = (start - today).days
        when = "tomorrow" if days == 1 else f"in {days} days"
        return False, f"vacation — next term starts {when} ({start:%d %b})"

    # Past the last term on record. Saying "vacation" here would be quietly
    # wrong for months, so say the calendar has run out instead.
    ends = [s[1] for s in (_span(t) for t in terms) if s]
    if ends and today > max(ends):
        return False, (f"the term dates only go to {max(ends):%d %b %Y} — "
                       f"add next year's to timetable.json")
    return False, "vacation"


def render_classes(timetable: dict, now: dt.datetime) -> str:
    out = [heading("TODAY'S CLASSES", C.GREEN)]

    if "_error" in timetable:
        out.append(f"  {C.RED}{timetable['_error']}{C.RESET}")
        return "\n".join(out)

    today = now.date()
    running, note = academic_status(timetable, today)
    if not running:
        out.append(f"  {C.DIM}No classes — {note}.{C.RESET}")
        return "\n".join(out)

    day_key = WEEKDAYS[today.weekday()]
    classes = timetable.get(day_key) or []
    if not classes:
        out.append(f"  {C.DIM}Nothing scheduled. Free day.{C.RESET}")
        return "\n".join(out)

    def sort_key(entry: dict) -> str:
        return entry.get("start", "99:99")

    classes = sorted(classes, key=sort_key)
    current_hm = now.strftime("%H:%M")
    next_up = None

    for entry in classes:
        start = entry.get("start", "??:??")
        end = entry.get("end", "")
        name = entry.get("name", "Untitled")
        venue = entry.get("venue", "")
        note = entry.get("note", "")

        done = bool(end) and end < current_hm
        active = bool(end) and start <= current_hm <= end

        if active:
            marker, colour = "▶", C.GREEN + C.BOLD
        elif done:
            marker, colour = "✓", C.DIM
        else:
            marker, colour = "○", C.WHITE
            if next_up is None:
                next_up = entry

        window = f"{start}–{end}" if end else start
        line = f"  {colour}{marker} {window:<12}{name}{C.RESET}"
        if venue:
            line += f"  {C.DIM}@ {venue}{C.RESET}"
        out.append(line)
        if note:
            out.append(f"      {C.DIM}{note}{C.RESET}")

    if next_up:
        try:
            start_t = dt.datetime.strptime(next_up["start"], "%H:%M").time()
            start_dt = dt.datetime.combine(today, start_t)
            delta = start_dt - now.replace(tzinfo=None)
            mins = int(delta.total_seconds() // 60)
            if 0 <= mins <= 600:
                hrs, rem = divmod(mins, 60)
                pretty = f"{hrs}h {rem}m" if hrs else f"{rem}m"
                out.append(f"\n  {C.GREEN}→ Next: {next_up['name']} "
                           f"in {pretty} ({next_up['start']}){C.RESET}")
        except (ValueError, KeyError):
            pass

    return "\n".join(out)


# --------------------------------------------------------------------------
# calendar (imported .ics, written by the settings page)
# --------------------------------------------------------------------------

CALENDAR_PATH = HERE / "calendar.json"


def load_calendar() -> dict:
    """Kept for callers that want the raw import; events now come from the store."""
    if not CALENDAR_PATH.exists():
        return {"events": []}
    try:
        return json.loads(CALENDAR_PATH.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError:
        return {"events": []}


def upcoming_events(cal: dict, cfg: dict, today: dt.date) -> list[dict]:
    """Imported events plus hand-added ones, minus anything deleted."""
    opts = cfg.get("calendar_opts") or {}
    days = int(opts.get("days_ahead", 7))
    try:
        import events_store
        merged = events_store.upcoming(days_ahead=days, today=today)
    except Exception:
        # Fall back to the raw import so a broken store can't cost you the section.
        horizon = today + dt.timedelta(days=days)
        merged = [ev for ev in cal.get("events", [])
                  if ev.get("date") and today <= dt.date.fromisoformat(ev["date"]) <= horizon]

    out = []
    for ev in merged:
        try:
            out.append({**ev, "_date": dt.date.fromisoformat(ev["date"])})
        except (KeyError, ValueError):
            continue
    return out


def friendly_day(day: dt.date, today: dt.date) -> str:
    delta = (day - today).days
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    if delta < 7:
        return day.strftime("%A")
    return day.strftime("%a %d %b")


def render_upcoming(cal: dict, cfg: dict, now: dt.datetime) -> str:
    today = now.date()
    events = upcoming_events(cal, cfg, today)
    if not events:
        return ""

    out = [heading("COMING UP", C.BLUE)]
    for ev in events[:8]:
        when = friendly_day(ev["_date"], today)
        at = f" {ev['time']}" if ev.get("time") else ""
        # 18 wide: "Sun 02 Aug 09:00" is 16 characters and would touch the title.
        line = f"  {C.WHITE}{when + at:<18}{C.RESET}{ev['summary']}"
        if ev.get("location"):
            line += f"  {C.DIM}@ {ev['location']}{C.RESET}"
        out.append(line)
    extra = len(events) - 8
    if extra > 0:
        out.append(f"  {C.DIM}...and {extra} more in the next few days{C.RESET}")
    return "\n".join(out)


def spoken_upcoming(cal: dict, cfg: dict, now: dt.datetime) -> str:
    today = now.date()
    events = upcoming_events(cal, cfg, today)
    if not events:
        return ""

    limit = int((cfg.get("calendar_opts") or {}).get("max_events", 3))
    picked = events[:limit]

    lead = "One thing coming up." if len(picked) == 1 else "Coming up."
    parts = []
    for ev in picked:
        when = friendly_day(ev["_date"], today)
        # say_time so "09:00" is read as "9 AM", not "zero nine hundred".
        at = f" at {say_time(ev['time'])}" if ev.get("time") else ""
        parts.append(f"{ev['summary']}, {when}{at}.")
    return " ".join([lead] + parts)


# --------------------------------------------------------------------------
# verse of the day and markets
# --------------------------------------------------------------------------

def get_verse(cfg: dict) -> dict | None:
    opts = cfg.get("verse") or {}
    if not opts.get("enabled"):
        return None
    try:
        import scripture
        return scripture.verse_of_the_day(opts.get("faith", ""))
    except Exception:
        return None


def get_stocks(cfg: dict) -> list[dict]:
    opts = cfg.get("stocks") or {}
    symbols = opts.get("symbols") or []
    if not opts.get("enabled") or not symbols:
        return []
    try:
        import stocks as stocks_mod
        return stocks_mod.quotes(symbols)
    except Exception:
        return []


def wrap(text: str, width: int = 60, indent: str = "  ") -> list[str]:
    words, lines, line = text.split(), [], ""
    for word in words:
        if len(line) + len(word) + 1 > width:
            lines.append(indent + line)
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        lines.append(indent + line)
    return lines


def render_verse(verse: dict | None) -> str:
    if not verse or not verse.get("text"):
        return ""
    out = [heading(f"VERSE OF THE DAY — {verse.get('faith_label', '')}".rstrip(" —"), C.MAGENTA)]
    out.extend(wrap(verse["text"]))
    ref = verse.get("reference", "")
    if ref:
        out.append(f"  {C.DIM}— {ref}{C.RESET}")
    if verse.get("stale"):
        out.append(f"  {C.DIM}(yesterday's verse — the source was unreachable){C.RESET}")
    return "\n".join(out)


def spoken_verse(verse: dict | None) -> str:
    if not verse or not verse.get("text"):
        return ""
    ref = verse.get("reference", "")
    lead = f"Today's verse, from {ref}." if ref else "Today's verse."
    return f"{lead} {verse['text']}"


def render_stocks(quotes: list[dict]) -> str:
    if not quotes:
        return ""
    out = [heading("MARKETS", C.YELLOW)]
    for q in quotes:
        if q.get("error"):
            out.append(f"  {C.DIM}{q['symbol']:<12}{q['error']}{C.RESET}")
            continue
        pct = q.get("percent")
        colour = C.GREEN if (pct or 0) >= 0 else C.RED
        arrow = "▲" if (pct or 0) >= 0 else "▼"
        price = f"{q['currency']} {q['price']:,.2f}" if q.get("price") is not None else "—"
        move = f"{arrow} {abs(pct):.2f}%" if pct is not None else "—"
        out.append(f"  {C.WHITE}{q['symbol']:<12}{C.RESET}{price:<16}{colour}{move:<12}{C.RESET}"
                   f"{C.DIM}{q['name'][:28]}{C.RESET}")
    return "\n".join(out)


def spoken_stocks(quotes: list[dict], cfg: dict) -> str:
    if not quotes:
        return ""
    try:
        import stocks as stocks_mod
        limit = int((cfg.get("stocks") or {}).get("spoken", 3))
        return stocks_mod.spoken(quotes, limit=limit)
    except Exception:
        return ""


# --------------------------------------------------------------------------
# spoken version
#
# The on-screen briefing is full of box drawing, emoji and bar charts, none of
# which survive a speech engine. So the same data gets rendered a second time
# as plain prose.
# --------------------------------------------------------------------------

def say_time(hhmm: str) -> str:
    """'08:00' -> '8 AM';  '14:45' -> '2:45 PM'."""
    try:
        hour, minute = (int(part) for part in hhmm.split(":"))
    except (ValueError, AttributeError):
        return hhmm
    suffix = "AM" if hour < 12 else "PM"
    hour12 = hour % 12 or 12
    return f"{hour12} {suffix}" if minute == 0 else f"{hour12}:{minute:02d} {suffix}"


def spoken_weather(weather: dict | None, cfg: dict) -> str:
    if not weather or "_error" in weather:
        return "I couldn't fetch the weather this morning."

    cur = weather.get("current", {})
    daily = weather.get("daily", {})
    desc, _ = describe(cur.get("weather_code"))
    city = cfg.get("city", "your area")

    temp = cur.get("temperature_2m")
    hi = (daily.get("temperature_2m_max") or [None])[0]
    lo = (daily.get("temperature_2m_min") or [None])[0]
    rain = (daily.get("precipitation_probability_max") or [None])[0]

    parts = [f"In {city} it's {temp:.0f} degrees, {desc.lower()}."]
    if hi is not None and lo is not None:
        parts.append(f"Today's high is {hi:.0f}, low of {lo:.0f}.")
    if rain is not None:
        if rain >= 60:
            parts.append(f"There's a {rain:.0f} percent chance of rain, so take a jacket.")
        elif rain >= 25:
            parts.append(f"About a {rain:.0f} percent chance of rain.")
        else:
            parts.append("Rain is unlikely.")
    return " ".join(parts)


def spoken_classes(timetable: dict, now: dt.datetime) -> str:
    if "_error" in timetable:
        return "I couldn't read your timetable file."

    today = now.date()
    running, note = academic_status(timetable, today)
    if not running:
        return f"No classes today — {note}."

    classes = sorted(
        timetable.get(WEEKDAYS[today.weekday()]) or [],
        key=lambda entry: entry.get("start", "99:99"),
    )
    if not classes:
        return "You have no classes today."

    count = len(classes)
    lead = "You have one class today." if count == 1 else f"You have {count} classes today."

    lines = []
    for i, entry in enumerate(classes):
        name = entry.get("name", "a class")
        when = say_time(entry.get("start", ""))
        venue = entry.get("venue", "")
        if i == 0:
            line = f"First up, {name} at {when}"
        elif i == count - 1:
            line = f"And {name} at {when}"
        else:
            line = f"Then {name} at {when}"
        if venue:
            line += f", in {venue}"
        lines.append(line + ".")

    spoken = " ".join([lead] + lines)

    notes = [f"For {e.get('name', 'that one')}: {e['note']}"
             for e in classes if e.get("note")]
    if notes:
        spoken += " Two things to remember. " if len(notes) > 1 else " One thing to remember. "
        spoken += " ".join(n.rstrip(".") + "." for n in notes)
    return spoken


def spoken_news(sources: list[dict], cfg: dict) -> str:
    limit = int(cfg.get("speech", {}).get("headlines", 5))
    live = [s for s in sources if s["items"]]
    if not live:
        return "I couldn't reach the news feeds."

    seen: set[str] = set()
    headlines: list[str] = []
    depth = 0
    while len(headlines) < limit:
        added = False
        for src in live:
            if len(headlines) >= limit:
                break
            if depth >= len(src["items"]):
                continue
            title = speakable_title(src["items"][depth]["title"])
            key = title.lower()[:60]
            if not title or key in seen:
                continue
            seen.add(key)
            headlines.append(title)
            added = True
        if not added:
            break
        depth += 1

    if not headlines:
        return "No headlines this morning."

    lead = "Here are the headlines." if len(headlines) > 1 else "One headline this morning."
    return " ".join([lead] + [h.rstrip(".") + "." for h in headlines])


def ordinal(day: int) -> str:
    """25 -> '25th'. Speech engines read the suffix as 'twenty fifth'."""
    if 11 <= day % 100 <= 13:
        return f"{day}th"
    suffix = {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{day}{suffix}"


def build_spoken(cfg, now, weather, timetable, news, cal, verse=None, quotes=None) -> str:
    fallback = _join_fallback_spoken(cfg, now, weather, timetable, news, cal, verse, quotes)
    rewritten = spoken_via_llm(cfg, now, weather, timetable, news, cal)
    if not rewritten:
        return fallback

    # The model rewrites the briefing, but it is never shown the scripture:
    # a verse must be read exactly as written, not paraphrased. Markets are
    # appended for the same reason - the numbers should not be reworded.
    tail = [block for block in (spoken_stocks(quotes or [], cfg), spoken_verse(verse)) if block]
    return "\n".join([rewritten] + tail) if tail else rewritten


def _join_fallback_spoken(cfg, now, weather, timetable, news, cal,
                          verse=None, quotes=None) -> str:
    parts = [
        f"{greeting(now, cfg.get('name', ''))}. "
        f"It's {now:%A} the {ordinal(now.day)} of {now:%B}.",
        spoken_weather(weather, cfg),
        spoken_classes(timetable, now),
    ]
    for block in (spoken_upcoming(cal, cfg, now),
                  spoken_stocks(quotes or [], cfg),
                  spoken_news(news, cfg),
                  spoken_verse(verse)):
        if block:
            parts.append(block)
    parts.append("Have a good day.")
    return "\n".join(parts)


def _record_llm_status(note: str) -> None:
    """Tiny breadcrumb the UI's status pane can read on the next refresh."""
    try:
        LLM_STATUS_PATH.write_text(
            json.dumps({"note": note, "at": dt.datetime.now().isoformat(timespec="seconds")}) + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass


def _llm_payload(cfg, now, weather, timetable, news, cal) -> dict:
    """Compact, speech-friendly view of the briefing for the model."""
    diary = upcoming_events(cal, cfg, now.date())[:3]
    diary_lines = [
        f"- {ev['summary']} on {friendly_day(ev['_date'], now.date())}"
        + (f" at {say_time(ev['time'])}" if ev.get('time') else "")
        for ev in diary
    ] or ["(none)"]

    head_limit = int(cfg.get("speech", {}).get("headlines", 5))
    seen: set[str] = set()
    headline_lines: list[str] = []
    depth = 0
    while len(headline_lines) < head_limit:
        added = False
        for src in [s for s in news if s["items"]]:
            if len(headline_lines) >= head_limit or depth >= len(src["items"]):
                continue
            title = speakable_title(src["items"][depth]["title"])
            key = title.lower()[:60]
            if not title or key in seen:
                continue
            seen.add(key)
            headline_lines.append(f"- [{src['name']}] {title}")
            added = True
        if not added:
            break
        depth += 1

    # Weather is summarised as plain text so the model doesn't have to parse
    # the API shape itself.
    return {
        "date": now.strftime("%A %d %B %Y"),
        "name": cfg.get("name", ""),
        "weather": spoken_weather(weather, cfg),
        "classes": spoken_classes(timetable, now),
        "upcoming": "\n".join(diary_lines),
        "headlines": "\n".join(headline_lines) or "(none)",
    }


def _build_llm_prompt(personality: str, payload: dict, max_words: int) -> str:
    # max_words of 0 (or less) means no hard cap. A number makes the model
    # count instead of write, and you can hear it - it starts clipping the last
    # story to fit. Without one, ask for judgement rather than arithmetic.
    length_rule = (
        f"- Hard cap: {max_words} words for the entire script. If a section would push you over, "
        f"trim it; never exceed the cap.\n"
        if max_words and max_words > 0 else
        "- Say what's worth saying and then stop. Don't pad, don't repeat yourself, and don't "
        "rush the end to save room - there is no length limit.\n"
    )
    return (
        f"{personality}\n\n"
        f"Today's date: {payload['date']}\n"
        f"Listener's name: {payload['name'] or 'unknown'}\n\n"
        f"WEATHER (already spoken-ready):\n{payload['weather']}\n\n"
        f"CLASSES (already spoken-ready):\n{payload['classes']}\n\n"
        f"UPCOMING EVENTS:\n{payload['upcoming']}\n\n"
        f"HEADLINES (raw, you may compress — do not invent new ones):\n{payload['headlines']}\n\n"
        f"Write the final spoken script now. Hard rules:\n"
        f"- Plain prose for a text-to-speech engine: no markdown, no bullets, no emojis, no URLs.\n"
        f"- South African news first, world news last.\n"
        f"- One short greeting line, then weather, then classes/todos, then headlines, then a sign-off.\n"
        f"{length_rule}"
        f"- Do not invent facts. If a section is missing, say so briefly in plain words."
    )


def spoken_via_llm(cfg, now, weather, timetable, news, cal) -> str | None:
    """Ask the local LLM to rewrite the spoken script. Returns None on any failure.

    The caller falls back to the mechanical build_spoken() output, so every
    failure mode here is silent: no Ollama, no model, timeout, HTTP error,
    empty response, or response that's clearly malformed.
    """
    llm_cfg = (cfg.get("llm") or {})
    if not llm_cfg.get("enabled", False):
        _record_llm_status("fallback: disabled in config")
        return None

    personality = (llm_cfg.get("personality") or "").strip()
    if not personality:
        _record_llm_status("fallback: no personality string")
        return None

    try:
        import llm as _llm
    except Exception as exc:
        _record_llm_status(f"fallback: import error ({type(exc).__name__})")
        return None

    base_url = llm_cfg.get("base_url") or _llm.DEFAULT_BASE_URL
    primary = (llm_cfg.get("model") or "").strip()
    fallback = (llm_cfg.get("fallback_model") or "").strip()

    # If `model` is unset, default to the first model Ollama has loaded, so
    # a fresh install with a single pulled model works out of the box.
    if not primary:
        available = _llm.list_models(base_url, timeout=4)
        if not available:
            _record_llm_status("fallback: Ollama has no models")
            return None
        primary = available[0]

    payload = _llm_payload(cfg, now, weather, timetable, news, cal)
    prompt = _build_llm_prompt(
        personality,
        payload,
        max_words=int(llm_cfg.get("max_words", 110)),
    )

    temperature = float(llm_cfg.get("temperature", 0.4))
    timeout = int(llm_cfg.get("timeout", 120))
    # Generous by default: a reasoning model needs room to finish thinking
    # before it writes the answer, and this runs once a morning behind three
    # minutes of music, so waiting costs nothing.
    max_tokens = int(llm_cfg.get("max_tokens", 16000))

    text = _llm.generate(
        prompt,
        model=primary,
        base_url=base_url,
        timeout=timeout,
        temperature=temperature,
        max_tokens=max_tokens,
    )

    # If the primary came back empty (e.g. it's a thinking model with a
    # short num_predict budget, or simply not pulled yet), try the fallback
    # model with a longer budget so a chain-of-thought model can fit.
    if not text and fallback and fallback != primary:
        try:
            text = _llm.generate(
                prompt,
                model=fallback,
                base_url=base_url,
                timeout=timeout,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except Exception:
            text = None
        if text:
            primary = fallback  # for the status note

    if not text:
        _record_llm_status(f"fallback: empty response from {primary}")
        return None

    # A defensive cleanup: strip code fences the model occasionally wraps the
    # answer in, and drop a stray leading "Here is the script:" preamble.
    cleaned = text.strip()
    cleaned = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", cleaned).strip()
    cleaned = re.sub(
        r"^(here(?:'s| is) (?:the |your )?(?:final )?(?:spoken )?script[:.\s]+)",
        "", cleaned, flags=re.IGNORECASE,
    ).strip()
    if not cleaned:
        _record_llm_status(f"fallback: cleaned response was empty ({primary})")
        return None

    _record_llm_status(f"llm: {primary}")
    return cleaned


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def greeting(now: dt.datetime, name: str) -> str:
    hour = now.hour
    if hour < 5:
        word = "You're up early"
    elif hour < 12:
        word = "Good morning"
    elif hour < 17:
        word = "Good afternoon"
    else:
        word = "Good evening"
    return f"{word}, {name}" if name else word


def build_data(cfg, now, weather, timetable, news, cal, verse=None, quotes=None) -> dict:
    """Structured version of the same briefing, for the dashboard to render."""
    today = now.date()
    running, note = academic_status(timetable, today)
    classes = sorted(timetable.get(WEEKDAYS[today.weekday()]) or [],
                     key=lambda e: e.get("start", "99:99")) if running else []

    cur = (weather or {}).get("current", {}) if isinstance(weather, dict) else {}
    daily = (weather or {}).get("daily", {}) if isinstance(weather, dict) else {}
    desc, icon = describe(cur.get("weather_code"))

    seen: set[str] = set()
    headlines = []
    for src in news:
        for item in src.get("items", []):
            title = tidy_title(item["title"])
            key = title.lower()[:60]
            if not title or key in seen:
                continue
            seen.add(key)
            headlines.append({
                "source": src["name"],
                "title": title,
                "summary": item.get("summary", "")[:280],
                "link": item.get("link", ""),
            })

    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "name": cfg.get("name", ""),
        "greeting": greeting(now, cfg.get("name", "")),
        "city": cfg.get("city", ""),
        "weather": None if not cur else {
            "temp": cur.get("temperature_2m"),
            "feels": cur.get("apparent_temperature"),
            "desc": desc,
            "icon": icon,
            "high": (daily.get("temperature_2m_max") or [None])[0],
            "low": (daily.get("temperature_2m_min") or [None])[0],
            "rain": (daily.get("precipitation_probability_max") or [None])[0],
            "sunrise": ((daily.get("sunrise") or [""])[0] or "")[11:16],
            "sunset": ((daily.get("sunset") or [""])[0] or "")[11:16],
        },
        "term": {"in_session": running, "note": note},
        "classes": classes,
        "events": [{k: v for k, v in e.items() if not k.startswith("_")}
                   for e in upcoming_events(cal, cfg, today)],
        "headlines": headlines,
        "verse": verse or None,
        "stocks": quotes or [],
    }


def build(cfg: dict, now: dt.datetime) -> tuple[str, str, dict]:
    """Fetch once, render three ways: on-screen, spoken, and structured."""
    # All four network calls at once - the scripture sources in particular are
    # slow, and there's no reason to wait on them in series.
    with futures.ThreadPoolExecutor(max_workers=4) as pool:
        weather_f = pool.submit(get_weather, cfg)
        news_f = pool.submit(get_news, cfg)
        verse_f = pool.submit(get_verse, cfg)
        stocks_f = pool.submit(get_stocks, cfg)
        weather = weather_f.result()
        news = news_f.result()
        verse = verse_f.result()
        quotes = stocks_f.result()

    timetable = load_timetable()
    cal = load_calendar()

    header = (f"{C.BOLD}{C.MAGENTA}{greeting(now, cfg.get('name', ''))}{C.RESET}\n"
              f"{C.DIM}{now.strftime('%A, %d %B %Y · %H:%M')}{C.RESET}")

    sections = [
        header,
        render_weather(weather, cfg, now),
        render_classes(timetable, now),
    ]
    diary = render_upcoming(cal, cfg, now)
    if diary:
        sections.append(diary)
    for block in (render_stocks(quotes), render_verse(verse)):
        if block:
            sections.append(block)
    sections.append(render_news(news, cfg))
    sections.append("")

    display = "\n".join(sections)
    return (display,
            build_spoken(cfg, now, weather, timetable, news, cal, verse, quotes),
            build_data(cfg, now, weather, timetable, news, cal, verse, quotes))


def main() -> int:
    ap = argparse.ArgumentParser(description="Morning briefing")
    ap.add_argument("--no-color", action="store_true", help="plain text, no ANSI codes")
    ap.add_argument("--out", metavar="FILE", help="also write the briefing to this file")
    ap.add_argument("--speak-out", metavar="FILE",
                    help="write the spoken script to this file (for the TTS wrapper)")
    ap.add_argument("--speak-only", action="store_true",
                    help="print the spoken script instead of the on-screen briefing")
    ap.add_argument("--data-out", metavar="FILE",
                    help="write the structured briefing (for the dashboard) to this file")
    args = ap.parse_args()

    if args.no_color or not sys.stdout.isatty():
        C.disable()
    else:
        enable_ansi()

    cfg = load_config()
    tz = ZoneInfo(cfg.get("timezone", "Africa/Johannesburg"))
    now = dt.datetime.now(tz)

    text, spoken, data = build(cfg, now)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print(spoken if args.speak_only else text)

    if args.out:
        plain = re.sub(r"\033\[[0-9;]*m", "", text)
        # utf-8-sig: the BOM makes Notepad and PowerShell 5.1 read it correctly.
        Path(args.out).write_text(plain, encoding="utf-8-sig")

    if args.speak_out:
        Path(args.speak_out).write_text(spoken, encoding="utf-8-sig")

    if args.data_out:
        Path(args.data_out).write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
