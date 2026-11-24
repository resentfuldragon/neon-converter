"""Page size for PDF output. "Original" keeps the source's own size.

* Word (DOCX), OpenDocument (ODT) and RTF files store their page size and margins — use them.
* E-books have no fixed pages, so they get a book-shaped page (6 in wide) whose proportions
  match the cover, so the cover fills page one exactly.
* Text, Markdown and HTML have no page size at all: use the paper of your Windows region
  (Letter in the US, A4 almost everywhere else).
"""
from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

CHOICES = ["Original", "A4", "Letter", "A5"]
NAMED = {"A4": (595.28, 841.89), "Letter": (612.0, 792.0), "A5": (419.53, 595.28)}
DEFAULT_MARGIN = 54.0  # 0.75 in
BOOK_MARGIN = 46.0
BOOK_WIDTH = 432.0  # 6 in, a common trade-paperback width
EBOOKS = {"epub", "mobi", "azw3", "fb2"}


@dataclass(frozen=True)
class Page:
    width: float  # points
    height: float
    margins: tuple[float, float, float, float]  # left, top, right, bottom (points)
    source: str  # "document" | "book" | "regional" | "chosen"

    def label(self) -> str:
        return f"{self.width / 72:.2f} × {self.height / 72:.2f} in"


def regional_paper() -> str:
    """'Letter' when Windows is set to US measurements, otherwise 'A4'."""
    try:
        import ctypes

        buf = ctypes.create_unicode_buffer(4)
        if ctypes.windll.kernel32.GetLocaleInfoEx(None, 0x0D, buf, 4):  # LOCALE_IMEASURE
            return "Letter" if buf.value == "1" else "A4"
    except Exception:
        pass
    return "A4"


def for_source(src: Path, fmt: str, choice: str, cover_size: tuple[int, int] | None = None) -> Page:
    if choice in NAMED:
        w, h = NAMED[choice]
        return Page(w, h, (DEFAULT_MARGIN,) * 4, "chosen")
    page = read_document_page(src, fmt)
    if page:
        return page
    if fmt in EBOOKS:
        ratio = cover_size[1] / cover_size[0] if cover_size and cover_size[0] else 1.5
        ratio = min(1.75, max(1.25, ratio))
        return Page(BOOK_WIDTH, round(BOOK_WIDTH * ratio, 1), (BOOK_MARGIN,) * 4, "book")
    w, h = NAMED[regional_paper()]
    return Page(w, h, (DEFAULT_MARGIN,) * 4, "regional")


# --------------------------------------------------------------------------- readers

def _attr(tag: str, name: str) -> str | None:
    m = re.search(rf'\b{re.escape(name)}\s*=\s*"([^"]*)"', tag)
    return m.group(1) if m else None


def _length_pt(value: str | None) -> float | None:
    """'21.001cm', '8.5in', '210mm', '612pt' -> points."""
    if not value:
        return None
    m = re.match(r"\s*([\d.]+)\s*(cm|mm|in|pt|pc|px)?", value)
    if not m:
        return None
    n = float(m.group(1))
    return n * {"cm": 72 / 2.54, "mm": 72 / 25.4, "in": 72, "pt": 1, "pc": 12, "px": 0.75, None: 1}[m.group(2)]


def _sane(w, h, margins) -> Page | None:
    if not w or not h or not (144 <= w <= 3000 and 144 <= h <= 3000):
        return None
    ms = tuple(min(max(0.0, m or 0.0), min(w, h) / 3) for m in margins)
    if not any(ms):
        ms = (DEFAULT_MARGIN,) * 4
    return Page(round(w, 2), round(h, 2), ms, "document")


def read_document_page(src: Path, fmt: str) -> Page | None:
    try:
        if fmt == "docx":
            with zipfile.ZipFile(src) as z:
                xml = z.read("word/document.xml").decode("utf-8", "ignore")
            sizes = re.findall(r"<w:pgSz\b[^>]*>", xml)
            marg = re.findall(r"<w:pgMar\b[^>]*>", xml)
            if not sizes:
                return None
            sz, mg = sizes[-1], (marg[-1] if marg else "")
            tw = lambda tag, k: (float(_attr(tag, k)) / 20) if _attr(tag, k) else None  # noqa: E731  twips
            return _sane(tw(sz, "w:w"), tw(sz, "w:h"),
                         (tw(mg, "w:left"), tw(mg, "w:top"), tw(mg, "w:right"), tw(mg, "w:bottom")))
        if fmt == "odt":
            with zipfile.ZipFile(src) as z:
                xml = z.read("styles.xml").decode("utf-8", "ignore")
            props = re.search(r"<style:page-layout-properties\b[^>]*>", xml)
            if not props:
                return None
            p = props.group(0)
            return _sane(_length_pt(_attr(p, "fo:page-width")), _length_pt(_attr(p, "fo:page-height")),
                         tuple(_length_pt(_attr(p, f"fo:margin-{s}")) for s in ("left", "top", "right", "bottom")))
        if fmt == "rtf":
            head = src.read_bytes()[:65536].decode("latin-1")

            def tw(word):
                m = re.search(rf"\\{word}(\d+)", head)
                return int(m.group(1)) / 20 if m else None
            return _sane(tw("paperw"), tw("paperh"), (tw("margl"), tw("margt"), tw("margr"), tw("margb")))
    except Exception:
        return None
    return None
