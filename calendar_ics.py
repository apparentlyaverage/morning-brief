"""Minimal .ics (iCalendar) reader - standard library only.

Handles what a student calendar export actually contains: one-off events and
simple repeats. Deliberately not a full RFC 5545 implementation.

Supported:
  - line unfolding, escaped text (\\, \\n \\; \\,)
  - VEVENT with SUMMARY / LOCATION / DESCRIPTION
  - DTSTART / DTEND as date, floating datetime, or UTC (Z)
  - all-day events
  - RRULE FREQ=DAILY / WEEKLY / MONTHLY / YEARLY, with INTERVAL, COUNT,
    UNTIL and (for WEEKLY) BYDAY
  - EXDATE exclusions

Not supported - these are reported rather than silently mishandled:
  - VTIMEZONE conversion (TZID is read but treated as local time)
  - BYSETPOS / BYMONTHDAY / complex recurrence
  - recurrence overrides (RECURRENCE-ID)
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

WEEKDAY_CODES = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
MAX_OCCURRENCES = 400  # safety net for an unbounded RRULE


def _unfold(text: str) -> list[str]:
    """Continuation lines start with a space or tab and belong to the line above."""
    out: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and out:
            out[-1] += raw[1:]
        else:
            out.append(raw)
    return out


def _unescape(value: str) -> str:
    return (value.replace("\\n", " ").replace("\\N", " ")
                 .replace("\\,", ",").replace("\\;", ";")
                 .replace("\\\\", "\\").strip())


def _parse_dt(value: str) -> tuple[dt.datetime | None, bool]:
    """Return (datetime, is_all_day). UTC values are converted to local time."""
    value = value.strip()
    if re.fullmatch(r"\d{8}", value):
        return dt.datetime.strptime(value, "%Y%m%d"), True
    m = re.fullmatch(r"(\d{8}T\d{6})(Z?)", value)
    if m:
        stamp = dt.datetime.strptime(m.group(1), "%Y%m%dT%H%M%S")
        if m.group(2) == "Z":
            stamp = stamp.replace(tzinfo=dt.timezone.utc).astimezone().replace(tzinfo=None)
        return stamp, False
    return None, False


def _split_line(line: str) -> tuple[str, dict[str, str], str]:
    """'DTSTART;TZID=Africa/Johannesburg:20260727T074500' -> name, params, value."""
    if ":" not in line:
        return "", {}, ""
    head, value = line.split(":", 1)
    parts = head.split(";")
    name = parts[0].upper()
    params = {}
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            params[k.upper()] = v
    return name, params, value


def _expand(start: dt.datetime, rule: str, exdates: set[dt.date],
            window_start: dt.date, window_end: dt.date) -> list[dt.datetime]:
    """Expand an RRULE into occurrences landing inside the window."""
    parts = {}
    for chunk in rule.split(";"):
        if "=" in chunk:
            k, v = chunk.split("=", 1)
            parts[k.upper()] = v.upper()

    freq = parts.get("FREQ", "")
    if freq not in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY"):
        return []

    interval = int(parts.get("INTERVAL", "1") or 1)
    count = int(parts["COUNT"]) if parts.get("COUNT", "").isdigit() else None
    until = None
    if parts.get("UNTIL"):
        until_dt, _ = _parse_dt(parts["UNTIL"])
        until = until_dt.date() if until_dt else None

    bydays = [WEEKDAY_CODES[d[-2:]] for d in parts.get("BYDAY", "").split(",")
              if d and d[-2:] in WEEKDAY_CODES]

    hits: list[dt.datetime] = []
    emitted = 0
    cursor = start

    for _ in range(MAX_OCCURRENCES):
        if until and cursor.date() > until:
            break
        if count is not None and emitted >= count:
            break

        if freq == "WEEKLY" and bydays:
            # Emit each requested weekday within this week.
            monday = cursor - dt.timedelta(days=cursor.weekday())
            for wd in sorted(bydays):
                moment = monday + dt.timedelta(days=wd)
                moment = moment.replace(hour=start.hour, minute=start.minute)
                if moment < start:
                    continue
                if until and moment.date() > until:
                    continue
                emitted += 1
                if count is not None and emitted > count:
                    break
                if window_start <= moment.date() <= window_end and moment.date() not in exdates:
                    hits.append(moment)
        else:
            emitted += 1
            if window_start <= cursor.date() <= window_end and cursor.date() not in exdates:
                hits.append(cursor)

        if cursor.date() > window_end:
            break

        if freq == "DAILY":
            cursor += dt.timedelta(days=interval)
        elif freq == "WEEKLY":
            cursor += dt.timedelta(weeks=interval)
        elif freq == "MONTHLY":
            month = cursor.month - 1 + interval
            year = cursor.year + month // 12
            month = month % 12 + 1
            day = min(cursor.day, [31, 29 if year % 4 == 0 and (year % 100 or year % 400 == 0)
                                   else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
            cursor = cursor.replace(year=year, month=month, day=day)
        else:  # YEARLY
            try:
                cursor = cursor.replace(year=cursor.year + interval)
            except ValueError:      # 29 Feb in a non-leap year
                cursor = cursor.replace(year=cursor.year + interval, day=28)

    return hits


def parse(path: str | Path, days_ahead: int = 30,
          today: dt.date | None = None) -> dict:
    """Read an .ics file and return upcoming events plus any caveats."""
    today = today or dt.date.today()
    window_end = today + dt.timedelta(days=days_ahead)

    text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    events: list[dict] = []
    warnings: set[str] = set()

    current: dict | None = None
    for line in _unfold(text):
        upper = line.strip().upper()
        if upper == "BEGIN:VEVENT":
            current = {"exdates": set()}
            continue
        if upper == "END:VEVENT":
            if current:
                events.append(current)
            current = None
            continue
        if current is None:
            continue

        name, params, value = _split_line(line)
        if name == "SUMMARY":
            current["summary"] = _unescape(value)
        elif name == "LOCATION":
            current["location"] = _unescape(value)
        elif name == "DTSTART":
            stamp, all_day = _parse_dt(value)
            current["start"], current["all_day"] = stamp, all_day
            if "TZID" in params:
                warnings.add("Times are read as local time; TZID zones are not converted.")
        elif name == "DTEND":
            stamp, _ = _parse_dt(value)
            current["end"] = stamp
        elif name == "RRULE":
            current["rrule"] = value.strip()
        elif name == "EXDATE":
            for chunk in value.split(","):
                stamp, _ = _parse_dt(chunk)
                if stamp:
                    current["exdates"].add(stamp.date())
        elif name == "RECURRENCE-ID":
            warnings.add("Some repeating events have per-occurrence edits, which are ignored.")

    out: list[dict] = []
    for ev in events:
        start = ev.get("start")
        if not start:
            continue
        summary = ev.get("summary") or "(untitled)"

        if ev.get("rrule"):
            rule = ev["rrule"]
            if any(k in rule.upper() for k in ("BYSETPOS", "BYMONTHDAY", "BYYEARDAY")):
                warnings.add(f"'{summary}' uses a complex repeat rule that may be approximated.")
            moments = _expand(start, rule, ev["exdates"], today, window_end)
        else:
            moments = [start] if today <= start.date() <= window_end else []

        for moment in moments:
            out.append({
                "summary": summary,
                "location": ev.get("location", ""),
                "date": moment.date().isoformat(),
                "time": "" if ev.get("all_day") else moment.strftime("%H:%M"),
                "all_day": bool(ev.get("all_day")),
            })

    out.sort(key=lambda e: (e["date"], e["time"] or "00:00"))
    return {"events": out, "warnings": sorted(warnings), "count": len(out)}
