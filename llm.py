"""Thin client for the local Ollama server.

Standard library only, mirroring the http_json helper in ui_server.py.
Failures return None and a reason string; the caller decides what to do.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

DEFAULT_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_TIMEOUT = 25  # seconds; a small rewrite should land well under this


def _post_json(url: str, body: dict, timeout: int) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Accept", "application/json")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
    return json.loads(raw) if raw.strip() else {}


def _get_json(url: str, timeout: int) -> dict:
    req = urllib.request.Request(url, method="GET")
    req.add_header("Accept", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
    return json.loads(raw) if raw.strip() else {}


def is_available(base_url: str = DEFAULT_BASE_URL, timeout: int = 3) -> bool:
    """Quick liveness check: is Ollama running and does it have any models?"""
    try:
        data = _get_json(f"{base_url}/api/tags", timeout=timeout)
    except Exception:
        return False
    return bool(data.get("models"))


def list_models(base_url: str = DEFAULT_BASE_URL, timeout: int = 5) -> list[str]:
    try:
        data = _get_json(f"{base_url}/api/tags", timeout=timeout)
    except Exception:
        return []
    return [m.get("name", "") for m in data.get("models", []) if m.get("name")]


def generate(
    prompt: str,
    *,
    model: str,
    base_url: str = DEFAULT_BASE_URL,
    timeout: int = DEFAULT_TIMEOUT,
    temperature: float = 0.4,
    max_tokens: int = 16000,
) -> str | None:
    """Call Ollama's /api/generate. Returns the response text or None on any error.

    Uses non-streaming mode (stream=false) so we get one JSON blob back.

    On the token budget: a reasoning model spends most of its output on its own
    chain-of-thought before writing a word of the answer. If it runs out mid-
    thought the reply comes back empty with done_reason="length" - measured at
    0/4 usable with a 600 cap and 1/4 at 2000. With headroom it is 5/5, taking
    4-22 seconds. Since this runs once a morning inside a three-minute music
    intro, there is no reason to be stingy: give it room and wait.

    ("num_predict": -1 for a true no-limit is rejected with a 400 by the
    cloud-routed models, hence a large concrete ceiling instead.)
    """
    body = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        # Reduces - but does not eliminate - the thinking pass. `think: True`
        # is a 400 on minimax-m3:cloud, so this stays False and the ceiling
        # above is what actually keeps the answer intact.
        "think": False,
        "options": {
            "temperature": temperature,
            "num_predict": int(max_tokens),
        },
    }
    try:
        data = _post_json(f"{base_url}/api/generate", body, timeout=timeout)
    except urllib.error.HTTPError:
        return None
    except Exception:
        return None
    return (data.get("response") or "").strip() or None