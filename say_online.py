"""Render text to speech with one of Microsoft's online neural voices.

    python say_online.py --voice en-ZA-LeahNeural --file briefing-spoken.txt --out out.mp3

Used by speak.ps1 for the good voices. Falls back to the offline Windows
engine (Mark) if this fails for any reason - see speak.ps1.

Why the resolver patch below: aiohttp picks c-ares (via aiodns) as its DNS
resolver when aiodns is installed, and on this machine c-ares cannot read the
Windows DNS configuration - every lookup dies with "Could not contact DNS
servers" even though the network is fine. Forcing aiohttp's threaded resolver
makes it use the ordinary OS lookup instead. Patching both module namespaces
is deliberate: connector.py does `from .resolver import DefaultResolver`, so
patching only aiohttp.resolver would leave the connector's copy untouched.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

try:
    import aiohttp.connector
    import aiohttp.resolver

    aiohttp.resolver.DefaultResolver = aiohttp.resolver.ThreadedResolver
    aiohttp.connector.DefaultResolver = aiohttp.resolver.ThreadedResolver
except Exception:  # aiohttp missing or restructured - let edge_tts fail loudly
    pass

import edge_tts  # noqa: E402


async def synth(text: str, voice: str, out: Path, rate: str, volume: str) -> None:
    communicate = edge_tts.Communicate(text, voice, rate=rate, volume=volume)
    await communicate.save(str(out))


def main() -> int:
    ap = argparse.ArgumentParser(description="Online neural text-to-speech")
    ap.add_argument("--voice", default="en-ZA-LeahNeural")
    ap.add_argument("--text")
    ap.add_argument("--file")
    ap.add_argument("--out", required=True)
    # edge-tts wants these as signed percentages, e.g. "-10%" or "+0%".
    ap.add_argument("--rate", default="+0%")
    ap.add_argument("--volume", default="+0%")
    ap.add_argument("--list", action="store_true", help="list en-* voices and exit")
    args = ap.parse_args()

    if args.list:
        voices = asyncio.run(edge_tts.list_voices())
        for v in sorted(voices, key=lambda x: x["ShortName"]):
            if v["ShortName"].startswith("en-"):
                print(f"{v['ShortName']:<34} {v['Gender']:<7} {v['Locale']}")
        return 0

    text = args.text
    if args.file:
        text = Path(args.file).read_text(encoding="utf-8-sig")
    if not text or not text.strip():
        print("nothing to say", file=sys.stderr)
        return 2

    out = Path(args.out)
    asyncio.run(synth(text, args.voice, out, args.rate, args.volume))

    if not out.exists() or out.stat().st_size == 0:
        print("no audio produced", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
