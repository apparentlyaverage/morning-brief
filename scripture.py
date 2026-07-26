"""Verse of the day for Christianity, Islam and Hinduism.

All three sources are free and need no key:
  Christianity  labs.bible.org          (World English Bible)
  Islam         quranapi.pages.dev      (Qur'an with English translation)
  Hinduism      vedicscriptures.github.io (Bhagavad Gita)

The verse is chosen from the date, so it's the same all day and changes at
midnight, and it's cached to disk so a morning with no internet still has
something to read.
"""

from __future__ import annotations

import datetime as dt
import json
import random
import time
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
CACHE = HERE / "verse_cache.json"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) morning-brief/1.0"}
TIMEOUT = 20  # the Gita source has been measured at up to 9s when healthy

FAITHS = {
    "christianity": "Christianity",
    "islam": "Islam",
    "hinduism": "Hinduism",
}

# Well-known passages rather than a random reference: a random Bible verse can
# land somewhere that reads oddly with no context on a Tuesday morning.
BIBLE_PASSAGES = [
    "Psalm 23:1", "Psalm 46:1", "Psalm 118:24", "Psalm 121:1-2", "Psalm 27:1",
    "Proverbs 3:5-6", "Proverbs 16:3", "Proverbs 17:22", "Ecclesiastes 3:1",
    "Isaiah 40:31", "Isaiah 41:10", "Jeremiah 29:11", "Lamentations 3:22-23",
    "Micah 6:8", "Matthew 5:9", "Matthew 6:34", "Matthew 11:28",
    "John 3:16", "John 14:27", "Romans 8:28", "Romans 12:12",
    "1 Corinthians 13:4-5", "2 Corinthians 4:16", "Galatians 5:22-23",
    "Ephesians 4:32", "Philippians 4:6-7", "Philippians 4:13",
    "Colossians 3:23", "1 Thessalonians 5:16-18", "Hebrews 11:1",
    "James 1:5", "1 Peter 5:7", "1 John 4:19",
]

# (surah, ayah) pairs - verified references, so no invalid lookups.
QURAN_AYAHS = [
    (1, 5), (2, 152), (2, 153), (2, 186), (2, 255), (2, 286),
    (3, 139), (3, 159), (3, 190), (4, 86), (5, 8), (6, 73),
    (9, 51), (11, 88), (13, 11), (13, 28), (14, 7), (16, 90),
    (17, 23), (18, 46), (20, 25), (21, 87), (24, 35), (25, 63),
    (28, 77), (29, 69), (31, 18), (39, 53), (40, 60), (41, 33),
    (49, 13), (55, 13), (64, 11), (65, 3), (93, 5), (94, 5),
    (103, 2), (107, 7), (112, 1),
]

# Verses per chapter of the Bhagavad Gita, chapters 1-18.
GITA_CHAPTER_LENGTHS = [47, 72, 43, 42, 29, 47, 30, 28, 34,
                        42, 55, 20, 35, 27, 20, 24, 28, 78]


def _get(url: str, attempts: int = 3) -> str:
    """Fetch with retries.

    The Gita source in particular drops roughly one request in three with an
    SSL handshake timeout, and can take up to nine seconds when it does answer.
    Retrying costs nothing on a good morning and saves a blank verse on a bad
    one - and the result is cached for the rest of the day either way.
    """
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return resp.read().decode("utf-8", "replace")
        except Exception as exc:
            last = exc
            if attempt < attempts - 1:
                time.sleep(1.5 * (attempt + 1))
    raise last if last else RuntimeError("request failed")


def _clean(text: str) -> str:
    return " ".join((text or "").replace("\n", " ").split()).strip()


def _pick(seq, day: dt.date):
    """Same choice all day, different tomorrow."""
    rng = random.Random(day.toordinal())
    return seq[rng.randrange(len(seq))]


# ------------------------------------------------------------------ sources

def _christianity(day: dt.date) -> dict:
    passage = _pick(BIBLE_PASSAGES, day)
    raw = json.loads(_get("https://labs.bible.org/api/?passage="
                          + urllib.parse.quote(passage) + "&type=json"))
    verses = raw if isinstance(raw, list) else [raw]
    text = " ".join(_clean(v.get("text", "")) for v in verses)
    first = verses[0] if verses else {}
    ref = f"{first.get('bookname', '')} {first.get('chapter', '')}:{first.get('verse', '')}".strip()
    return {"reference": ref or passage, "text": text, "source": "World English Bible"}


def _islam(day: dt.date) -> dict:
    surah, ayah = _pick(QURAN_AYAHS, day)
    data = json.loads(_get(f"https://quranapi.pages.dev/api/{surah}/{ayah}.json"))

    english = data.get("english")
    if isinstance(english, list):
        english = " ".join(str(x) for x in english)
    if not english:
        # Field names vary between builds of this API - fall back to any
        # plausible English string rather than showing nothing.
        for key in ("translation", "text", "englishTranslation"):
            if isinstance(data.get(key), str) and data[key].strip():
                english = data[key]
                break

    name = data.get("surahName") or f"Surah {surah}"
    translated = data.get("surahNameTranslation") or ""
    ref = f"{name} {surah}:{ayah}"
    if translated:
        ref += f" ({translated})"
    return {"reference": ref, "text": _clean(english or ""),
            "arabic": _clean(data.get("arabic1") or ""), "source": "Qur'an"}


def _find_english(node) -> str:
    """Gita translations sit under per-translator objects keyed 'et'."""
    if isinstance(node, dict):
        if isinstance(node.get("et"), str) and node["et"].strip():
            return node["et"]
        for value in node.values():
            found = _find_english(value)
            if found:
                return found
    return ""


def _hinduism(day: dt.date) -> dict:
    rng = random.Random(day.toordinal())
    chapter = rng.randrange(1, 19)
    verse = rng.randrange(1, GITA_CHAPTER_LENGTHS[chapter - 1] + 1)
    data = json.loads(_get(f"https://vedicscriptures.github.io/slok/{chapter}/{verse}"))
    return {
        "reference": f"Bhagavad Gita {chapter}.{verse}",
        "text": _clean(_find_english(data)),
        "sanskrit": _clean(data.get("slok") or ""),
        "source": "Bhagavad Gita",
    }


SOURCES = {"christianity": _christianity, "islam": _islam, "hinduism": _hinduism}


# ------------------------------------------------------------------- public

def verse_of_the_day(faith: str, day: dt.date | None = None,
                     use_cache: bool = True) -> dict | None:
    """Today's verse, or None if the faith is unknown/disabled."""
    faith = (faith or "").strip().lower()
    if faith not in SOURCES:
        return None
    day = day or dt.date.today()
    stamp = day.isoformat()

    cache = {}
    if use_cache and CACHE.exists():
        try:
            cache = json.loads(CACHE.read_text(encoding="utf-8-sig"))
        except Exception:
            cache = {}
        hit = cache.get(faith)
        if hit and hit.get("date") == stamp and hit.get("text"):
            return hit

    try:
        verse = SOURCES[faith](day)
    except Exception as exc:
        stale = cache.get(faith)
        if stale and stale.get("text"):
            return {**stale, "stale": True}
        return {"reference": "", "text": "", "faith": faith,
                "error": f"{type(exc).__name__}: {exc}"}

    verse.update(faith=faith, faith_label=FAITHS[faith], date=stamp)
    if not verse.get("text"):
        verse["error"] = "the source returned no English text"

    cache[faith] = verse
    try:
        CACHE.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    return verse
