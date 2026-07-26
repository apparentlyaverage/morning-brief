"""Document library for the read-aloud / audiobook feature.

An uploaded document is extracted to plain text once, split into passages of a
few hundred words, and stored under library/. Each book remembers which
passage you stopped on, so you can pick it up where you left off.

Passages exist because text-to-speech needs bounded chunks - synthesising a
300-page PDF in one request would take minutes and be impossible to resume.

Formats: PDF (via pypdf), plus .txt / .md / .markdown.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
LIBRARY_DIR = HERE / "library"
INDEX = LIBRARY_DIR / "index.json"

WORDS_PER_PASSAGE = 220        # roughly 90 seconds of speech
SUPPORTED = {".pdf", ".txt", ".md", ".markdown", ".text"}


def _load_index() -> list[dict]:
    try:
        data = json.loads(INDEX.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_index(books: list[dict]) -> None:
    LIBRARY_DIR.mkdir(exist_ok=True)
    INDEX.write_text(json.dumps(books, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- extraction

def _extract_pdf(raw: bytes, tmp: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        raise ValueError("PDF support needs the pypdf package (pip install pypdf)")
    tmp.write_bytes(raw)
    try:
        reader = PdfReader(str(tmp))
        pages = []
        for page in reader.pages:
            try:
                pages.append(page.extract_text() or "")
            except Exception:
                pages.append("")
        text = "\n\n".join(pages)
    finally:
        tmp.unlink(missing_ok=True)

    if not text.strip():
        raise ValueError("No text found. This looks like a scanned PDF - "
                         "there's no text layer to read, only images.")
    return text


def _extract_text(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def clean_for_speech(text: str) -> str:
    """Tidy extracted text so it reads aloud sensibly."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)     # de-hyphenate line breaks
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Markdown furniture reads badly out loud.
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.M)
    text = re.sub(r"(\*\*|__|\*|`)", "", text)
    return text.strip()


def split_passages(text: str, words: int = WORDS_PER_PASSAGE) -> list[str]:
    """Split on paragraphs, packing them up to roughly `words` each."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    passages: list[str] = []
    buffer: list[str] = []
    count = 0

    for para in paragraphs:
        para_words = len(para.split())
        if para_words > words * 2:
            # A very long paragraph gets broken on sentence boundaries.
            if buffer:
                passages.append(" ".join(buffer)); buffer, count = [], 0
            sentences = re.split(r"(?<=[.!?])\s+", para)
            chunk: list[str] = []
            chunk_words = 0
            for sentence in sentences:
                chunk.append(sentence)
                chunk_words += len(sentence.split())
                if chunk_words >= words:
                    passages.append(" ".join(chunk)); chunk, chunk_words = [], 0
            if chunk:
                passages.append(" ".join(chunk))
            continue

        buffer.append(para)
        count += para_words
        if count >= words:
            passages.append("\n\n".join(buffer)); buffer, count = [], 0

    if buffer:
        passages.append("\n\n".join(buffer))
    return [p for p in passages if p.strip()]


# ------------------------------------------------------------------- library

def add_document(filename: str, raw: bytes) -> dict:
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED:
        raise ValueError(f"Can't read {suffix or 'that file'}. Supported: "
                         + ", ".join(sorted(SUPPORTED)))
    if not raw:
        raise ValueError("that file is empty")

    LIBRARY_DIR.mkdir(exist_ok=True)
    book_id = uuid.uuid4().hex[:12]

    if suffix == ".pdf":
        text = _extract_pdf(raw, LIBRARY_DIR / f"_tmp_{book_id}.pdf")
    else:
        text = _extract_text(raw)

    passages = split_passages(clean_for_speech(text))
    if not passages:
        raise ValueError("nothing readable in that file")

    (LIBRARY_DIR / f"{book_id}.json").write_text(
        json.dumps({"passages": passages}, ensure_ascii=False), encoding="utf-8")

    book = {
        "id": book_id,
        "title": Path(filename).stem[:120] or "Untitled",
        "filename": filename,
        "passages": len(passages),
        "words": sum(len(p.split()) for p in passages),
        "position": 0,
        "added": dt.datetime.now().isoformat(timespec="seconds"),
        "last_opened": None,
    }
    books = _load_index()
    books.insert(0, book)
    _save_index(books)
    return book


def list_books() -> list[dict]:
    return _load_index()


def get_book(book_id: str) -> dict | None:
    return next((b for b in _load_index() if b["id"] == book_id), None)


def get_passage(book_id: str, index: int) -> dict:
    book = get_book(book_id)
    if not book:
        raise ValueError("no such document")
    path = LIBRARY_DIR / f"{book_id}.json"
    if not path.exists():
        raise ValueError("the text for that document is missing")

    passages = json.loads(path.read_text(encoding="utf-8-sig"))["passages"]
    total = len(passages)
    index = max(0, min(int(index), total - 1))
    return {
        "id": book_id, "title": book["title"], "index": index, "total": total,
        "text": passages[index],
        "progress": round((index + 1) / total * 100),
    }


def set_position(book_id: str, index: int) -> dict:
    books = _load_index()
    for book in books:
        if book["id"] == book_id:
            book["position"] = max(0, min(int(index), book["passages"] - 1))
            book["last_opened"] = dt.datetime.now().isoformat(timespec="seconds")
            _save_index(books)
            return book
    raise ValueError("no such document")


def delete_book(book_id: str) -> bool:
    books = _load_index()
    remaining = [b for b in books if b["id"] != book_id]
    if len(remaining) == len(books):
        return False
    _save_index(remaining)
    (LIBRARY_DIR / f"{book_id}.json").unlink(missing_ok=True)
    return True
