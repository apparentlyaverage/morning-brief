"""Stock and index quotes.

Uses Yahoo Finance's chart endpoint directly over the standard library rather
than yfinance. yfinance is a wrapper around this same endpoint, and pulling in
pandas/numpy for a handful of numbers would work against keeping this app
plug-and-play.

Ticker suffixes worth knowing: JSE is .JO (NPN.JO), London .L, Frankfurt .DE.
An index starts with a caret (^GSPC, ^J203). Bare symbols are US listings.
"""

from __future__ import annotations

import concurrent.futures as futures
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=5d&interval=1d"
# A browser-ish agent: the endpoint returns 429/403 to obviously scripted clients.
UA = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")}
TIMEOUT = 12

# Yahoo quotes some listings in minor units - JSE prices come back in cents.
MINOR_UNIT_CURRENCIES = {"ZAc": ("ZAR", 100.0), "GBp": ("GBP", 100.0)}


def _get(url: str, attempts: int = 3) -> str:
    """Fetch with retries - this endpoint intermittently drops the TLS handshake."""
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 400):     # a bad ticker will not fix itself
                raise
            last = exc
        except Exception as exc:
            last = exc
        if attempt < attempts - 1:
            time.sleep(1.0 * (attempt + 1))
    raise last if last else RuntimeError("request failed")


def _fetch(symbol: str) -> dict:
    symbol = symbol.strip()
    if not symbol:
        return {}
    url = CHART.format(symbol=urllib.parse.quote(symbol))
    payload = json.loads(_get(url))

    result = ((payload.get("chart") or {}).get("result") or [None])[0]
    if not result:
        raise ValueError("no data for that symbol")
    meta = result.get("meta") or {}

    price = meta.get("regularMarketPrice")
    previous = meta.get("chartPreviousClose") or meta.get("previousClose")

    # Fall back to the daily closes when the meta block omits a previous close.
    if previous is None:
        closes = [c for c in (((result.get("indicators") or {}).get("quote") or [{}])[0]
                              .get("close") or []) if c is not None]
        if len(closes) >= 2:
            price, previous = closes[-1], closes[-2]

    currency = meta.get("currency") or ""
    if currency in MINOR_UNIT_CURRENCIES and price is not None:
        currency, divisor = MINOR_UNIT_CURRENCIES[currency]
        price = price / divisor
        if previous is not None:
            previous = previous / divisor

    change = pct = None
    if price is not None and previous:
        change = price - previous
        pct = change / previous * 100

    return {
        "symbol": meta.get("symbol", symbol),
        "name": meta.get("shortName") or meta.get("longName") or meta.get("symbol", symbol),
        "exchange": meta.get("fullExchangeName", ""),
        "currency": currency,
        "price": price,
        "change": change,
        "percent": pct,
    }


CACHE = Path(__file__).resolve().parent / "stock_cache.json"


def _cache_read() -> dict:
    try:
        return json.loads(CACHE.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def quotes(symbols: list[str]) -> list[dict]:
    """Fetch several symbols at once; a bad ticker reports itself, not an exception.

    A symbol that fails falls back to its last known price rather than a blank
    row - this endpoint drops roughly one call in ten, and a slightly stale
    number is more use at 06:30 than "no response".
    """
    symbols = [s.strip() for s in (symbols or []) if s and s.strip()]
    if not symbols:
        return []
    cached = _cache_read()

    def one(symbol: str) -> dict:
        try:
            fresh = _fetch(symbol)
            cached[symbol] = fresh
            return fresh
        except Exception as exc:
            stale = cached.get(symbol)
            if stale and stale.get("price") is not None:
                return {**stale, "stale": True}
            text = str(exc)
            if "404" in text or "400" in text:
                reason = "unknown symbol"
            elif "timed out" in text.lower() or "handshake" in text.lower():
                reason = "no response"
            else:
                reason = type(exc).__name__
            return {"symbol": symbol, "name": symbol, "error": reason}

    with futures.ThreadPoolExecutor(max_workers=min(8, len(symbols))) as pool:
        results = list(pool.map(one, symbols))

    try:
        CACHE.write_text(json.dumps(cached, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return results


def spoken(quote_list: list[dict], limit: int = 3) -> str:
    """One sentence for the spoken briefing."""
    usable = [q for q in quote_list if not q.get("error") and q.get("percent") is not None]
    if not usable:
        return ""
    parts = []
    for q in usable[:limit]:
        direction = "up" if q["percent"] >= 0 else "down"
        parts.append(f"{q['name']} {direction} {abs(q['percent']):.1f} percent")
    return "Markets: " + ", ".join(parts) + "."
