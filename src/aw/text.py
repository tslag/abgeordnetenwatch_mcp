"""Text helpers: HTML stripping, name parsing and umlaut variants."""
from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser

_BLOCK_TAGS = {"p", "br", "li", "ul", "ol", "div", "h1", "h2", "h3", "h4", "h5", "h6", "tr"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def strip_html(html: str | None, max_chars: int | None = None) -> str | None:
    """HTML -> plain text, whitespace collapsed, optionally cut at a word boundary."""
    if not html:
        return None
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    text = unescape("".join(parser.parts)).replace("\xa0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\s*\n\s*", "\n", text).strip()
    if max_chars is not None and len(text) > max_chars:
        cut = text[:max_chars].rsplit(" ", 1)[0].rstrip(",.;:-– ")
        text = cut + "…"
    return text or None


# --- names -----------------------------------------------------------------------------------

_TITLES = {"dr", "prof", "dipl", "ing", "h.c", "mdb", "mdl", "mdep", "herr", "frau"}
_PARTICLES = {"von", "van", "vom", "der", "den", "de", "del", "di", "da", "zu", "zur", "zum", "ter", "ten", "la", "le", "af", "al"}


def parse_name(query: str) -> tuple[str | None, str]:
    """Split a free-text name into (first_name, last_name).

    Handles 'Last, First', academic titles and last-name particles ('Ursula von der Leyen').
    A single token is treated as a last name.
    """
    query = " ".join(query.split())
    if "," in query:
        last, _, first = query.partition(",")
        return (_clean(first) or None, _clean(last))
    tokens = [t for t in query.split(" ") if t and t.rstrip(".").lower() not in _TITLES]
    if not tokens:
        return (None, query.strip())
    if len(tokens) == 1:
        return (None, tokens[0])
    idx = len(tokens) - 1
    while idx > 1 and tokens[idx - 1].lower() in _PARTICLES:
        idx -= 1
    return (" ".join(tokens[:idx]), " ".join(tokens[idx:]))


def _clean(s: str) -> str:
    return " ".join(t for t in s.split() if t.rstrip(".").lower() not in _TITLES)


_TO_ASCII = (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss"), ("Ä", "Ae"), ("Ö", "Oe"), ("Ü", "Ue"))
_TO_UMLAUT = (("ae", "ä"), ("oe", "ö"), ("ue", "ü"), ("ss", "ß"), ("Ae", "Ä"), ("Oe", "Ö"), ("Ue", "Ü"))


def name_variants(text: str) -> list[str]:
    """Spelling variants for a name token. The API does not transliterate ('Mueller' != 'Müller').

    Returns the original first, then the ASCII-folded and umlaut-restored forms (when different).
    """
    variants = [text]
    folded = text
    for src, dst in _TO_ASCII:
        folded = folded.replace(src, dst)
    restored = text
    for src, dst in _TO_UMLAUT:
        restored = restored.replace(src, dst)
    for v in (folded, restored):
        if v not in variants:
            variants.append(v)
    return variants
