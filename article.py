"""Pull the readable body text out of a news article page.

Standard library only, like everything else here - no readability-lxml, no
newspaper3k, no BeautifulSoup. That keeps the app plug-and-play, at the cost
of the extraction being heuristic rather than clever.

Three strategies, best first:

  1. schema.org `articleBody` from a JSON-LD block. Cleanest by far when the
     site publishes it, because it is the body the publisher declared, not a
     guess from the markup.
  2. Paragraphs inside the largest <article> element.
  3. Every <p> on the page, if enough of them look like prose.

Measured against the eight feeds shipped in config.example.json (July 2026):
News24, BusinessTech, BBC, Daily Maverick, Moneyweb and SABC all yield usable
text; eNCA renders its body without any <p> tags at all and returns nothing.
So `extract` returning "" is an ordinary outcome, not an error - the caller is
expected to fall through to the next story rather than treat it as a failure.
"""

from __future__ import annotations

import html as htmlmod
import json
import re
import urllib.request

# A plain scripted User-Agent gets 403s from some publishers; this doesn't.
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
TIMEOUT = 15

MIN_PARAGRAPH_WORDS = 12      # shorter than this is a caption, byline or link
MAX_PARAGRAPH_WORDS = 400     # longer is a markup accident, not a paragraph
MIN_BODY_WORDS = 120          # below this we haven't really found the article
MAX_BODY_WORDS = 1400         # roughly nine minutes read aloud; plenty

TAG_RE = re.compile(r"<[^>]+>")
# Whole elements whose text is never part of the article.
CHROME_RE = re.compile(
    r"<(script|style|noscript|svg|figure|figcaption|aside|nav|footer|header|form)\b.*?</\1>",
    re.S | re.I)
LD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.S | re.I)
PARA_RE = re.compile(r"<p\b[^>]*>(.*?)</p>", re.S | re.I)
ARTICLE_RE = re.compile(r"<article\b[^>]*>(.*?)</article>", re.S | re.I)
META_CHARSET_RE = re.compile(rb'charset=["\']?([\w-]+)', re.I)

# Furniture that sits inside the body markup on these sites and reads badly.
BOILERPLATE_RE = re.compile(
    r"^\s*(also read|read more|watch|listen|related|subscribe|sign up|"
    r"follow us|share this|advertisement|sponsored|photo|image|file picture|"
    r"picture|source|reporting by|additional reporting|copyright|"
    r"all rights reserved|comments? section|download the app)\b[:\s]",
    re.I)


def _decode(raw: bytes, header_charset: str | None) -> str:
    """Decode a page, trusting the document's own <meta> over a wrong header."""
    meta = META_CHARSET_RE.search(raw[:4096])
    for candidate in (meta.group(1).decode("ascii", "ignore") if meta else None,
                      header_charset, "utf-8"):
        if not candidate:
            continue
        try:
            return raw.decode(candidate, "replace")
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", "replace")


def _cut_comments(page: str) -> str:
    """Drop everything from the start of the reader-comments section.

    Moneyweb (WordPress) wraps every reader comment in its own <article>
    element, and on a story with a busy thread those outweigh the real body:
    picking the largest <article> returned 259 words of readers arguing about
    currency speculation rather than the article. Comments always follow the
    body, so truncating at the first marker is simple and safe - and it fixes
    the <p>-density path too, which would otherwise blend comments into the
    text.
    """
    lowered = page.lower()
    cuts = [i for i in (
        lowered.find('id="comments"'),
        lowered.find('class="comments'),
        lowered.find('class="comment-list'),
        lowered.find('id="respond"'),
        lowered.find('disqus_thread'),
        lowered.find('<article class="comment'),
    ) if i > 0]
    # Only trust a marker that appears after some real content, so a stray
    # "comments" link in the page furniture cannot truncate the whole article.
    cuts = [i for i in cuts if i > 1000]
    return page[:min(cuts)] if cuts else page


def _text(fragment: str) -> str:
    fragment = CHROME_RE.sub(" ", fragment)
    return " ".join(htmlmod.unescape(TAG_RE.sub(" ", fragment)).split())


def _usable(paragraphs: list[str]) -> list[str]:
    """Keep prose, drop furniture, and never repeat a paragraph."""
    out: list[str] = []
    seen: set[str] = set()
    for para in paragraphs:
        words = len(para.split())
        if not MIN_PARAGRAPH_WORDS <= words <= MAX_PARAGRAPH_WORDS:
            continue
        if BOILERPLATE_RE.match(para):
            continue
        key = para[:80].lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(para)
    return out


def _walk_ld(node, found: list[str]) -> None:
    """Find articleBody anywhere in a JSON-LD blob (@graph, lists, nesting)."""
    if isinstance(node, dict):
        body = node.get("articleBody")
        if isinstance(body, str) and len(body.split()) >= MIN_BODY_WORDS:
            found.append(body)
        for value in node.values():
            _walk_ld(value, found)
    elif isinstance(node, list):
        for value in node:
            _walk_ld(value, found)


def _trim(paragraphs: list[str]) -> str:
    """Join paragraphs, stopping once we have plenty."""
    kept: list[str] = []
    total = 0
    for para in paragraphs:
        kept.append(para)
        total += len(para.split())
        if total >= MAX_BODY_WORDS:
            break
    return "\n\n".join(kept)


def extract(page: str) -> tuple[str, str]:
    """(method, body_text) for a page's HTML. Empty text means 'not found'."""
    page = _cut_comments(page)

    # 1. What the publisher declared.
    found: list[str] = []
    for blob in LD_RE.findall(page):
        try:
            _walk_ld(json.loads(blob.strip()), found)
        except (ValueError, TypeError):
            continue
    if found:
        body = " ".join(htmlmod.unescape(max(found, key=len)).split())
        # articleBody arrives as one run of text; split it back into paragraphs
        # so the trimming and boilerplate rules still apply.
        paragraphs = _usable([p.strip() for p in re.split(r"\n+", body)] or [body])
        if paragraphs:
            return "json-ld", _trim(paragraphs)
        if len(body.split()) >= MIN_BODY_WORDS:
            return "json-ld", " ".join(body.split()[:MAX_BODY_WORDS])

    # Strip page furniture before looking for paragraphs. Doing it per
    # paragraph isn't enough: a <p> nested inside <nav> or <footer> survives
    # that, and site navigation then gets read aloud as though it were news.
    # This happens after the JSON-LD pass on purpose - that data lives in a
    # <script>, which is one of the things being removed here.
    page = CHROME_RE.sub(" ", page)

    # 2. The <article> element with the most prose in it. Scored on usable
    #    paragraph words rather than HTML size: the biggest block of markup is
    #    not reliably the article, and picking by size is what let a comment
    #    thread win on Moneyweb.
    best: list[str] = []
    best_words = 0
    for block in ARTICLE_RE.findall(page):
        paragraphs = _usable([_text(p) for p in PARA_RE.findall(block)])
        words = sum(len(p.split()) for p in paragraphs)
        if words > best_words:
            best, best_words = paragraphs, words
    if best_words >= MIN_BODY_WORDS:
        return "article-tag", _trim(best)

    # 3. Everything that looks like a paragraph.
    paragraphs = _usable([_text(p) for p in PARA_RE.findall(page)])
    if sum(len(p.split()) for p in paragraphs) >= MIN_BODY_WORDS:
        return "p-density", _trim(paragraphs)

    return "none", ""


def fetch_article(url: str, timeout: int = TIMEOUT) -> dict:
    """Fetch a URL and extract its body.

    Never raises for an ordinary web failure - the caller gets `ok: False` and
    a reason, because "this story wouldn't open" is a normal thing to tell the
    listener, not a crash.
    """
    if not (url or "").startswith(("http://", "https://")):
        return {"ok": False, "error": "that isn't a link I can open"}

    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-ZA,en;q=0.9",
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(3_000_000)      # a news page is never this big
            page = _decode(raw, resp.headers.get_content_charset())
            final_url = resp.url
    except Exception as exc:
        return {"ok": False, "error": f"couldn't open the article ({type(exc).__name__})"}

    method, body = extract(page)
    if not body:
        return {"ok": False, "error": "couldn't find the article text on that page",
                "method": method, "url": final_url}
    return {"ok": True, "text": body, "method": method, "url": final_url,
            "words": len(body.split())}


# ------------------------------------------------------------------ script

def build_prompt(personality: str, title: str, source: str, body: str) -> str:
    """Ask the model to talk the listener through one story.

    Deliberately not "summarise this". The briefing already gives headlines;
    the point of asking for a story is to hear what's actually going on, so
    the instruction is to explain rather than compress.
    """
    return (
        f"{personality}\n\n"
        f"You're picking up one story in more depth because the listener asked "
        f"to hear about it. This is not a headline read - it's the segment "
        f"where you actually explain what's going on.\n\n"
        f"SOURCE: {source}\n"
        f"HEADLINE: {title}\n\n"
        f"ARTICLE TEXT:\n{body}\n\n"
        f"Write the spoken segment now. Hard rules:\n"
        f"- Plain prose for a text-to-speech engine: no markdown, no bullets, "
        f"no headings, no emojis, no URLs, no 'according to the article'.\n"
        f"- Open by naming the story, then explain it: what happened, who it "
        f"affects, and why it matters. Give the background a headline can't.\n"
        f"- No greeting and no reference to the time of day. This is played on "
        f"demand and could be three in the afternoon; start on the story.\n"
        f"- Everything you say must come from the article text above. Do not "
        f"add facts, figures, names or context you were not given. If the "
        f"article leaves something open, say that it's unclear.\n"
        f"- Attribute opinions to whoever held them; don't adopt them.\n"
        f"- Say what's worth saying and then stop. No length limit, but don't "
        f"pad and don't repeat yourself.\n"
        f"- End cleanly. No sign-off, no 'back to the music' - something else "
        f"follows this."
    )


def plain_script(title: str, source: str, body: str) -> str:
    """The no-LLM fallback: introduce the story, then read it as written.

    Used when Ollama is switched off or unreachable. Reading the article
    verbatim is honest - it just isn't the radio version.
    """
    lead = f"From {source}: {title}." if source else f"{title}."
    return f"{lead}\n\n{body}"
