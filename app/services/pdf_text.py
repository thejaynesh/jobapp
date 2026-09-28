"""
The text of a PDF as a machine reads it.

An ATS never sees the page; it sees whatever text comes back out of the file.
That can differ from what the template meant to print — a ligature that
extracts as one glyph, a line-break hyphen, a section the one-page trim cut —
so the keyword check runs on this rather than on the template's input.
"""

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# Typographic ligatures (U+FB00–FB06). One of these in extracted text means a
# word like "efficient" reached the parser as "e\ufb03cient".
_LIGATURES = re.compile("[\ufb00-\ufb06]")


def extract(path) -> str:
    """All pages' text, or "" when the file cannot be read."""
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(Path(path)))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as exc:
        logger.warning("pdf_text: could not read %s: %s", path, exc)
        return ""


def normalize(text: str) -> str:
    """Lowercase, words re-joined across line-break hyphens, whitespace collapsed."""
    text = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", text or "")
    return " ".join(text.lower().split())


def ligatures(text: str) -> int:
    """How many ligature glyphs came out — each one a word a search can miss."""
    return len(_LIGATURES.findall(text or ""))


# Control characters other than whitespace. A bitmap (Type 3) font has no
# character map, and its glyphs come out as these: "e\x1ecien t" for
# "efficient". Any at all means a parser is reading garbled text.
_CONTROL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def garbled(text: str) -> int:
    """How many characters came out as control codes rather than letters."""
    return len(_CONTROL.findall(text or ""))


def coverage(text: str, keywords: list[str]) -> tuple[list[str], list[str]]:
    """Which keywords appear in the extracted text, and which do not."""
    haystack = normalize(text)
    present = [k for k in keywords if " ".join(k.lower().split()) in haystack]
    missing = [k for k in keywords if k not in present]
    return present, missing
