"""Calendar events the user manages by hand, merged with imported .ics events.

Two stores, deliberately kept apart:

  calendar.json  - whatever the last .ics import produced. Replaced wholesale
                   on every import, so nothing hand-written may live here.
  events.json    - {"manual": [...], "hidden": [...]}. Manual events the user
                   added, plus keys of imported events they deleted.

Deleting an imported event records its key rather than editing calendar.json,
so it stays deleted when the same .ics is imported again.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
CALENDAR = HERE / "calendar.json"
EVENTS = HERE / "events.json"


def _load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return default


def _save(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_store() -> dict:
    store = _load(EVENTS, {})
    if not isinstance(store, dict):
        store = {}
    store.setdefault("manual", [])
    store.setdefault("hidden", [])
    return store


def event_key(ev: dict) -> str:
    """Stable id for an imported event, so 'deleted' survives a re-import."""
    raw = f"{ev.get('date','')}|{ev.get('time','')}|{ev.get('summary','')}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def all_events() -> list[dict]:
    """Imported events (minus deleted ones) plus manual events, date-sorted."""
    store = load_store()
    hidden = set(store.get("hidden", []))

    out: list[dict] = []
    for ev in (_load(CALENDAR, {}) or {}).get("events", []):
        key = event_key(ev)
        if key in hidden:
            continue
        out.append({**ev, "id": key, "source": "ics"})

    for ev in store.get("manual", []):
        out.append({**ev, "source": "manual"})

    out.sort(key=lambda e: (e.get("date", ""), e.get("time") or "00:00"))
    return out


def upcoming(days_ahead: int = 7, today: dt.date | None = None) -> list[dict]:
    today = today or dt.date.today()
    horizon = today + dt.timedelta(days=days_ahead)
    out = []
    for ev in all_events():
        try:
            day = dt.date.fromisoformat(ev["date"])
        except (KeyError, ValueError):
            continue
        if today <= day <= horizon:
            out.append(ev)
    return out


def add_event(date: str, summary: str, time: str = "", location: str = "",
              note: str = "") -> dict:
    summary = (summary or "").strip()
    if not summary:
        raise ValueError("give the event a name")
    try:
        dt.date.fromisoformat(date)
    except (TypeError, ValueError):
        raise ValueError("date must look like 2026-07-27")
    time = (time or "").strip()
    if time:
        try:
            dt.datetime.strptime(time, "%H:%M")
        except ValueError:
            raise ValueError("time must look like 14:15")

    event = {
        "id": "m-" + uuid.uuid4().hex[:12],
        "date": date,
        "time": time,
        "summary": summary,
        "location": (location or "").strip(),
        "note": (note or "").strip(),
        "all_day": not time,
    }
    store = load_store()
    store["manual"].append(event)
    _save(EVENTS, store)
    return event


def delete_event(event_id: str) -> bool:
    """Remove a manual event, or mark an imported one as deleted."""
    store = load_store()
    before = len(store["manual"])
    store["manual"] = [e for e in store["manual"] if e.get("id") != event_id]
    if len(store["manual"]) < before:
        _save(EVENTS, store)
        return True

    # Not one of ours - it must be an imported event, so remember to hide it.
    if event_id not in store["hidden"]:
        store["hidden"].append(event_id)
        _save(EVENTS, store)
        return True
    return False


def restore_hidden() -> int:
    """Un-delete every imported event that was hidden."""
    store = load_store()
    count = len(store.get("hidden", []))
    store["hidden"] = []
    _save(EVENTS, store)
    return count
