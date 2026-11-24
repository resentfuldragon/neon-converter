"""Documents and e-books.

Each converter we have (Pandoc, LibreOffice, MuPDF, pdf2docx, PyMuPDF4LLM, KindleUnpack,
and Calibre when installed) is an *edge* between two formats with a cost. A conversion is
the cheapest path through that graph, so e.g. PDF -> EPUB runs as PDF -> Markdown (layout
analysis finds headings, tables and images) -> EPUB (Pandoc splits chapters and builds a
clickable table of contents). Costs encode which engine keeps structure and styling best.
"""
from __future__ import annotations

import heapq
import html
import itertools
import json
import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import pages, privacy, tools
from .tools import ConversionError

OUTPUTS: list[tuple[str, str]] = [
    ("pdf", "PDF"),
    ("epub", "EPUB"),
    ("mobi", "MOBI  (Kindle)"),
    ("azw3", "AZW3  (Kindle)"),
    ("docx", "DOCX  (Word)"),
    ("doc", "DOC  (Word 97)"),
    ("odt", "ODT  (OpenDocument)"),
    ("rtf", "RTF  (Rich Text)"),
    ("txt", "TXT  (Plain text)"),
    ("html", "HTML  (Web page)"),
    ("md", "MD  (Markdown)"),
    ("fb2", "FB2  (FictionBook)"),
]

INPUT_EXTS = {
    "pdf", "epub", "mobi", "azw3", "azw", "prc", "fb2", "docx", "doc", "odt", "rtf",
    "txt", "text", "html", "htm", "xhtml", "md", "markdown", "xps", "oxps", "cbz",
}
ALIASES = {"azw": "mobi", "prc": "mobi", "text": "txt", "htm": "html", "xhtml": "html",
           "markdown": "md", "oxps": "xps"}

PANDOC_IN = {"docx": "docx", "odt": "odt", "epub": "epub", "fb2": "fb2", "html": "html",
             "md": "markdown", "rtf": "rtf"}
PANDOC_OUT = {"docx": "docx", "odt": "odt", "epub": "epub3", "fb2": "fb2", "html": "html5",
              "md": "gfm", "rtf": "rtf", "txt": "plain"}
LO_IN = {"docx", "doc", "odt", "rtf", "txt", "html"}
LO_OFFICE = {"docx", "doc", "odt", "rtf"}
CAL_IN = {"epub", "mobi", "azw3", "fb2", "docx", "odt", "rtf", "txt", "html", "pdf"}
CAL_OUT = {"epub", "mobi", "azw3", "fb2", "docx", "pdf", "rtf", "txt"}
MUPDF_TO_PDF = {"epub", "mobi", "fb2", "xps", "cbz"}
EBOOK = {"epub", "mobi", "azw3", "fb2"}

PAGE_SIZES = pages.CHOICES


def fmt_of(path: str | Path) -> str:
    ext = Path(path).suffix.lower().lstrip(".")
    return ALIASES.get(ext, ext)


@dataclass
class Options:
    target: str = "pdf"
    page_size: str = "Original"  # or A4 / Letter / A5
    toc: bool = True  # add a clickable contents page to EPUB / HTML output
    private: bool = False  # remove author names and other identifying metadata from the output


@dataclass
class Ctx:
    job: tools.Job
    title: str
    opts: Options
    final: bool = False
    progress: Callable[[float], None] = field(default=lambda f: None)
    cover: Path | None = None  # cover image found/made by an earlier step, used for e-book output
    page: pages.Page | None = None  # page size for PDF/Word output, worked out from the source file


@dataclass(frozen=True)
class Edge:
    src: str
    dst: str
    cost: float
    engine: str
    fn: Callable
    then_only: frozenset = frozenset()  # if set, only these engines may follow this step


# =========================================================================== routing

def _calibre_cost(s: str, d: str) -> float:
    if s == "pdf":
        return 3.0  # Calibre's PDF reader loses tables; the layout-analysis route wins
    if d == "rtf":
        return 1.6  # Calibre's RTF writer drops images; Pandoc keeps them
    if {s, d} <= {"epub", "mobi", "azw3"}:
        return 0.9  # Kindle <-> EPUB is Calibre's native, highest-fidelity path
    if s in ("mobi", "azw3") or d in ("mobi", "azw3"):
        return 0.95
    if s in EBOOK and d in {"pdf", "docx", "rtf", "txt", "epub", "fb2"}:
        return 0.95  # Calibre keeps the book's CSS styling best
    return 1.5


def build_edges() -> list[Edge]:
    edges: list[Edge] = []

    def add(s, d, cost, engine, fn, then_only=frozenset()):
        if s != d:
            edges.append(Edge(s, d, cost, engine, fn, frozenset(then_only)))

    if tools.pandoc_path():
        for s in PANDOC_IN:
            for d in PANDOC_OUT:
                add(s, d, 1.0, "Pandoc", _pandoc)
    add("pdf", "md", 0.95, "Layout analysis", _pdf_to_md)
    add("pdf", "txt", 1.0, "MuPDF", _pdf_to_txt)
    # pdf2docx output mirrors the page layout (no heading styles): fine for other office
    # formats, but e-books must come from the structure-recovery route so chapters survive
    add("pdf", "docx", 1.0, "pdf2docx", _pdf_to_docx, then_only={"LibreOffice"})
    for s in MUPDF_TO_PDF:
        # MuPDF reads old-style MOBI but sees no chapters; unpacking to EPUB first keeps bookmarks
        add(s, "pdf", 2.5 if s == "mobi" else 1.1, "MuPDF", _mupdf_to_pdf)
    add("mobi", "epub", 1.2, "KindleUnpack", _kindle_unpack)
    add("azw3", "epub", 1.2, "KindleUnpack", _kindle_unpack)
    add("txt", "html", 1.2, "Text", _txt_to_html)
    add("html", "pdf", 3.0, "MuPDF", _html_to_pdf)
    if tools.libreoffice_path():
        for s in LO_IN:
            # HTML goes via DOCX instead so headings become PDF bookmarks
            add(s, "pdf", 2.5 if s == "html" else 1.0, "LibreOffice", _libreoffice)
            if s in LO_OFFICE:
                for d in LO_OFFICE:  # DOCX is the best-supported hand-off format for the next step
                    add(s, d, 0.85 if d == "docx" else 0.9, "LibreOffice", _libreoffice)
    if tools.calibre_path():
        for s in CAL_IN:
            for d in CAL_OUT:
                add(s, d, _calibre_cost(s, d), "Calibre", _calibre)
    return edges


# Never pass *through* these: plain text throws away headings/tables/images, and a PDF made
# mid-route would have to be reflowed again. (XPS/CBZ can only be read via PDF, so they may.)
LOSSY = {"txt", "pdf"}
PDF_ONLY_SOURCES = {"xps", "cbz"}


def find_route(src: str, dst: str, edges: list[Edge]) -> list[Edge] | None:
    if src == dst:
        return None
    adj: dict[str, list[Edge]] = {}
    for e in edges:
        adj.setdefault(e.src, []).append(e)
    tie = itertools.count()
    best = {src: 0.0}
    heap: list = [(0.0, next(tie), src, [])]
    while heap:
        cost, _, node, path = heapq.heappop(heap)
        if node == dst:
            return path
        if cost > best.get(node, float("inf")):
            continue
        for e in adj.get(node, []):
            if e.dst in LOSSY and e.dst != dst and not (e.dst == "pdf" and src in PDF_ONLY_SOURCES):
                continue
            if any(p.then_only and e.engine not in p.then_only for p in path):
                continue  # a restriction from an earlier step still applies
            nc = cost + e.cost
            if nc < best.get(e.dst, float("inf")) - 1e-9:
                best[e.dst] = nc
                heapq.heappush(heap, (nc, next(tie), e.dst, path + [e]))
    return None


def missing_engine(target: str) -> str | None:
    """Why an output format can't be produced at all right now (None if it can)."""
    edges = build_edges()
    if any(e.dst == target for e in edges):
        return None
    if target in ("mobi", "azw3"):
        return "needs Calibre"
    if target == "doc":
        return "needs LibreOffice"
    return "unavailable"


def describe_route(src_fmt: str, target: str) -> str:
    route = find_route(src_fmt, target, build_edges())
    if not route:
        return ""
    return " → ".join([route[0].src.upper()] + [f"{e.dst.upper()}" for e in route])


# =========================================================================== conversion

def page_for(src: Path, sf: str, choice: str) -> pages.Page:
    """The PDF page for this source ('Original' reads it from the file; e-books use the cover's shape)."""
    cover_size = None
    if choice == "Original" and sf == "epub":
        cover = _epub_cover(src)
        if cover:
            try:
                import io

                from PIL import Image

                with Image.open(io.BytesIO(cover[1])) as im:
                    cover_size = im.size
            except Exception:
                pass
    return pages.for_source(src, sf, choice, cover_size)


def convert(src: Path, out_dir: Path, opts: Options, job: tools.Job,
            status: Callable[[str], None], progress: Callable[[float], None]) -> Path:
    sf, target = fmt_of(src), opts.target
    if sf not in {ALIASES.get(x, x) for x in INPUT_EXTS}:
        raise ConversionError(f"{src.suffix or 'This file type'} isn't a supported document")
    if sf == target:
        raise tools.AlreadyTarget(f"Already a {target.upper()} file — skipped")
    route = find_route(sf, target, build_edges())
    if not route:
        why = missing_engine(target)
        extra = f" ({why})" if why else ""
        raise ConversionError(f"Can't convert {sf.upper()} to {target.upper()}{extra}")

    out = tools.unique_path(out_dir, src.stem, target)
    tmp = Path(tempfile.mkdtemp(prefix="neon_doc_"))
    ctx = Ctx(job=job, title=src.stem, opts=opts)
    ctx.page = page_for(src, sf, opts.page_size)
    try:
        cur = src
        n = len(route)
        for i, e in enumerate(route):
            ctx.final = i == n - 1
            if ctx.final:
                nxt = out
            else:
                step_dir = tmp / f"step{i}"
                step_dir.mkdir()
                nxt = step_dir / f"{src.stem}.{e.dst}"
            label = f"{e.engine}: {e.src.upper()} → {e.dst.upper()}"
            status(label if n == 1 else f"[{i + 1}/{n}] {label}")
            ctx.progress = lambda f, i=i: progress((i + max(0.0, min(1.0, f))) / n)
            progress(i / n)
            e.fn(cur, nxt, ctx)
            job.check()
            if not nxt.exists() or nxt.stat().st_size == 0:
                raise ConversionError(f"{e.engine} produced no output")
            cur = nxt
        if opts.private:
            status("Removing identifying info…")
            privacy.scrub(out)
        progress(1.0)
        return out
    except BaseException:
        if out.exists():
            out.unlink(missing_ok=True)
        media = out.parent / f"{out.stem}_files"
        if target == "md" and media.is_dir():
            shutil.rmtree(media, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def merge_pdfs(parts: list[tuple[str, Path]], out: Path, job: tools.Job, private: bool = False) -> Path:
    """Join PDFs in order. Each file becomes a top-level bookmark with its own chapters nested inside."""
    _merge(parts, out, job)
    if private:
        privacy.scrub(out)
    return out


def _merge(parts: list[tuple[str, Path]], out: Path, job: tools.Job) -> Path:
    import pymupdf

    merged = pymupdf.open()
    toc: list[list] = []
    try:
        for title, path in parts:
            job.check()
            with pymupdf.open(str(path)) as d:
                if d.needs_pass:
                    raise ConversionError(f"{path.name} is password-protected")
                start = len(merged)
                merged.insert_pdf(d)
                toc.append([1, title, start + 1])
                toc += [[lvl + 1, t, start + pg] for lvl, t, pg, *_ in d.get_toc(simple=True) if pg >= 1]
        if not len(merged):
            raise ConversionError("Nothing to merge")
        try:
            merged.set_toc(_clean_toc(toc))
        except Exception:
            merged.set_toc([e for e in toc if e[0] == 1])
        merged.save(str(out), garbage=3, deflate=True)
    except BaseException:
        out.unlink(missing_ok=True)
        raise
    finally:
        merged.close()
    return out


# =========================================================================== engines

EBOOK_TARGETS = {"epub", "mobi", "azw3", "fb2"}

# Pandoc reads an EPUB's chapters but not its cover; this filter puts the cover image first
# (unless the book's first pages already show it).
_COVER_FILTER = """
local cover, base = %s, %s
function Pandoc(doc)
  local found = false
  for i = 1, math.min(#doc.blocks, 6) do
    doc.blocks[i]:walk({ Image = function(img)
      if img.src:find(base, 1, true) then found = true end
    end })
  end
  if not found then
    table.insert(doc.blocks, 1, pandoc.Para({ pandoc.Image({ pandoc.Str("Cover") }, cover) }))
  end
  return doc
end
"""


def _lua_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _pandoc(src: Path, dst: Path, ctx: Ctx) -> None:
    sf, df = fmt_of(src), fmt_of(dst)
    cmd = [tools.pandoc_path(), str(src), "-f", PANDOC_IN[sf], "-t", PANDOC_OUT[df],
           "-o", str(dst), f"--resource-path={src.parent}"]
    if df == "html":
        cmd += ["-s", "--embed-resources", "-M", f"pagetitle={ctx.title}"]
        if ctx.opts.toc:
            cmd += ["--toc", "--toc-depth=3"]
    elif df == "epub":
        cmd += ["--split-level=1", "--toc-depth=3"]
        if ctx.opts.toc:
            cmd += ["--toc"]
        if sf not in ("epub", "fb2") and not _has_title(src, sf):
            cmd += ["-M", f"title={ctx.title}"]
        if ctx.cover and ctx.cover.exists():
            cmd += [f"--epub-cover-image={ctx.cover}"]
    elif df == "rtf":
        cmd += ["-s"]
    elif df in ("txt", "md"):
        cmd += ["--wrap=none"]
        if df == "md":
            cmd += [f"--extract-media={dst.stem}_files"]

    scratch = None
    if ctx.opts.private:  # drop author/date fields before they reach title pages and metadata
        scratch = Path(tempfile.mkdtemp(prefix="neon_pandoc_"))
        lua = scratch / "private.lua"
        lua.write_text(privacy.PANDOC_LUA, encoding="utf-8")
        cmd += ["--lua-filter", str(lua)]
    if sf == "epub" and df in ("html", "md", "odt", "docx", "rtf"):
        cover = _epub_cover(src)
        if cover:
            scratch = scratch or Path(tempfile.mkdtemp(prefix="neon_cover_"))
            img = scratch / cover[0]
            img.write_bytes(cover[1])
            lua = scratch / "cover.lua"
            lua.write_text(_COVER_FILTER % (_lua_str(img.as_posix()), _lua_str(cover[0])), encoding="utf-8")
            cmd += ["--lua-filter", str(lua)]
    try:
        rc, out, err = ctx.job.run(cmd, cwd=dst.parent)
    finally:
        if scratch:
            shutil.rmtree(scratch, ignore_errors=True)
    if rc != 0:
        raise ConversionError("Pandoc failed: " + tools.tail(err or out))


def _epub_cover(src: Path) -> tuple[str, bytes] | None:
    """(file name, image bytes) of an EPUB's cover, from its package metadata."""
    import posixpath
    import urllib.parse
    import zipfile

    def attr(tag: str, name: str) -> str:
        m = re.search(rf'\b{name}\s*=\s*["\']([^"\']*)["\']', tag)
        return m.group(1) if m else ""

    try:
        with zipfile.ZipFile(src) as z:
            container = z.read("META-INF/container.xml").decode("utf-8", "ignore")
            opf_path = re.search(r'full-path\s*=\s*["\']([^"\']+)["\']', container).group(1)
            opf = z.read(opf_path).decode("utf-8", "ignore")
            items = re.findall(r"<(?:\w+:)?item\b[^>]*>", opf)
            href = next((attr(i, "href") for i in items if "cover-image" in attr(i, "properties").split()), "")
            if not href:
                meta = next((m for m in re.findall(r"<(?:\w+:)?meta\b[^>]*>", opf)
                             if attr(m, "name") == "cover"), None)
                cover_id = attr(meta, "content") if meta else ""
                href = next((attr(i, "href") for i in items if cover_id and attr(i, "id") == cover_id), "")
            if not href:
                href = next((attr(i, "href") for i in items if attr(i, "media-type").startswith("image/")
                             and "cover" in (attr(i, "id") + attr(i, "href")).lower()), "")
            if not href or not href.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp")):
                return None
            name = posixpath.normpath(posixpath.join(posixpath.dirname(opf_path), urllib.parse.unquote(href)))
            return posixpath.basename(name), z.read(name)
    except Exception:
        return None


def _has_title(src: Path, sf: str) -> bool:
    """Does the document already carry a title Pandoc will use?"""
    import zipfile

    try:
        if sf in ("docx", "odt"):
            with zipfile.ZipFile(src) as z:
                name = "docProps/core.xml" if sf == "docx" else "meta.xml"
                xml = z.read(name).decode("utf-8", "ignore")
            return bool(re.search(r"<dc:title>\s*\S", xml))
        if sf == "html":
            head = src.read_text(encoding="utf-8", errors="ignore")[:20000]
            return bool(re.search(r"<title>\s*\S", head, re.I))
        if sf == "md":
            head = src.read_text(encoding="utf-8", errors="ignore")[:4000]
            return head.startswith("---") and bool(re.search(r"^title:\s*\S", head, re.M))
    except Exception:
        pass
    return False


def _lo_pdf_filter(sf: str) -> str:
    opts = {
        "ExportBookmarks": {"type": "boolean", "value": "true"},  # clickable chapter outline
        "ExportNotes": {"type": "boolean", "value": "false"},
        "UseTaggedPDF": {"type": "boolean", "value": "true"},
    }
    name = "writer_web_pdf_Export" if sf == "html" else "writer_pdf_Export"
    return f"pdf:{name}:{json.dumps(opts, separators=(',', ':'))}"


LO_FILTERS = {"docx": "docx:MS Word 2007 XML", "odt": "odt:writer8",
              "rtf": "rtf:Rich Text Format", "doc": "doc:MS Word 97"}


def _libreoffice(src: Path, dst: Path, ctx: Ctx) -> None:
    sf, df = fmt_of(src), fmt_of(dst)
    outdir = Path(tempfile.mkdtemp(prefix="neon_lo_"))
    try:
        if ctx.opts.private and sf in ("docx", "odt", "rtf"):
            # LibreOffice copies authors, comments and tracked changes across: give it a clean copy
            (outdir / "in").mkdir()
            src = privacy.scrubbed_copy(src, outdir / "in")
        profile = (tools.data_dir() / "lo_profile").as_uri()
        cmd = [tools.libreoffice_path(), f"-env:UserInstallation={profile}", "--headless",
               "--invisible", "--nologo", "--norestore", "--nolockcheck", "--nodefault"]
        if sf == "txt":
            cmd.append("--infilter=Text (encoded):UTF8")
        filt = _lo_pdf_filter(sf) if df == "pdf" else LO_FILTERS[df]
        cmd += ["--convert-to", filt, "--outdir", str(outdir), str(src)]
        rc, out, err = ctx.job.run(cmd)
        produced = sorted(outdir.glob(f"*.{df}"))
        if not produced:
            raise ConversionError("LibreOffice couldn't convert this file. " + tools.tail(err or out))
        shutil.move(str(produced[0]), str(dst))
    finally:
        shutil.rmtree(outdir, ignore_errors=True)


def _calibre(src: Path, dst: Path, ctx: Ctx) -> None:
    scratch = None
    try:
        if ctx.opts.private and fmt_of(src) in ("docx", "odt", "rtf", "epub", "html", "fb2"):
            # Calibre copies metadata and photo EXIF straight into Kindle files, which can't be
            # cleaned afterwards — so it only ever sees a scrubbed copy.
            scratch = Path(tempfile.mkdtemp(prefix="neon_cal_"))
            src = privacy.scrubbed_copy(src, scratch)
        _calibre_run(src, dst, ctx)
    finally:
        if scratch:
            shutil.rmtree(scratch, ignore_errors=True)


def _calibre_run(src: Path, dst: Path, ctx: Ctx) -> None:
    df = fmt_of(dst)
    cmd = [tools.calibre_path(), str(src), str(dst)]
    page = ctx.page or pages.for_source(src, fmt_of(src), ctx.opts.page_size)
    if df == "pdf":
        ml, mt, mr, mb = (f"{m:.1f}" for m in page.margins)
        cmd += ["--custom-size", f"{page.width:.1f}x{page.height:.1f}", "--unit", "point",
                "--pdf-page-numbers", "--pdf-page-margin-left", ml, "--pdf-page-margin-right", mr,
                "--pdf-page-margin-top", mt, "--pdf-page-margin-bottom", mb, "--preserve-cover-aspect-ratio"]
    elif df == "mobi":
        cmd += ["--mobi-file-type", "both"]
    elif df == "docx":
        # a Word file gets a real paper size: the source's own, the one chosen, or the regional default
        if page.source == "book":
            w, h = pages.NAMED[pages.regional_paper()]
            margins = (pages.DEFAULT_MARGIN,) * 4
        else:
            w, h, margins = page.width, page.height, page.margins
        cmd += ["--docx-custom-page-size", f"{w:.1f}x{h:.1f}", "--preserve-cover-aspect-ratio"]
        for side, m in zip(("left", "top", "right", "bottom"), margins):
            cmd += [f"--docx-page-margin-{side}", f"{m:.1f}"]
    if ctx.cover and ctx.cover.exists() and df in EBOOK_TARGETS:
        cmd += ["--cover", str(ctx.cover)]
    if ctx.opts.private:
        cmd += privacy.CALIBRE + ["--title", ctx.title]
    # chapters/sections for the navigation menu & PDF bookmarks (used when the source has no TOC)
    cmd += ["--level1-toc", "//h:h1", "--level2-toc", "//h:h2", "--level3-toc", "//h:h3"]
    if df == "epub" and ctx.opts.toc:
        cmd += ["--epub-inline-toc"]
    elif df in ("mobi", "azw3"):
        cmd += ["--mobi-toc-at-start"] if ctx.opts.toc else ["--no-inline-toc"]

    def on_line(line: str) -> None:
        m = re.match(r"\s*(\d+)%", line)
        if m:
            ctx.progress(int(m.group(1)) / 100)

    # keep the built-in Calibre's settings separate from any Calibre the user has installed
    env = dict(os.environ, CALIBRE_CONFIG_DIRECTORY=str(tools.data_dir() / "calibre-config"))
    rc, out, err = ctx.job.run(cmd, on_line=on_line, env=env)
    if rc != 0 or not dst.exists():
        raise ConversionError("Calibre failed: " + tools.tail(err or out))


# ----------------------------------------------------------------------- MuPDF family

def _open_pdf(src: Path):
    import pymupdf

    try:
        doc = pymupdf.open(str(src))
    except Exception as exc:
        raise ConversionError(f"Couldn't open the file: {exc}") from None
    if doc.needs_pass:
        doc.close()
        raise ConversionError("This PDF is password-protected")
    return doc


def _clean_toc(toc: list) -> list:
    """Make a TOC acceptable to set_toc: start at level 1, never jump more than one level."""
    fixed, prev = [], 0
    for item in toc:
        lvl, title, page = item[0], item[1], item[2]
        lvl = max(1, min(lvl, prev + 1))
        fixed.append([lvl, title, page])
        prev = lvl
    return fixed


def _mupdf_to_pdf(src: Path, dst: Path, ctx: Ctx) -> None:
    """EPUB / MOBI / FB2 / XPS / CBZ -> PDF with bookmarks and working links."""
    import pymupdf

    doc = _open_pdf(src)
    try:
        pg = ctx.page or pages.for_source(src, fmt_of(src), ctx.opts.page_size)
        W, H = pg.width, pg.height
        ml, mt, mr, mb = pg.margins if doc.is_reflowable else (0, 0, 0, 0)
        if doc.is_reflowable:
            # lay the book out in the text area, then place each page inside real margins
            doc.layout(width=W - ml - mr, height=H - mt - mb, fontsize=11)
        ctx.progress(0.2)
        body = pymupdf.open("pdf", doc.convert_to_pdf())
        ctx.progress(0.5)
        if doc.is_reflowable:
            pdf = pymupdf.open()
            inner = pymupdf.Rect(ml, mt, W - mr, H - mb)
            total = len(body)
            for pno in range(total):
                if pno % 10 == 0:
                    ctx.job.check()
                    ctx.progress(0.5 + 0.4 * pno / max(1, total))
                page = pdf.new_page(width=W, height=H)
                page.show_pdf_page(inner, body, pno)
                if total > 1:  # page number in the bottom margin
                    page.insert_textbox(pymupdf.Rect(0, H - mb * 0.7, W, H - mb * 0.2),
                                        str(pno + 1), fontsize=8, align=1, color=(0.45, 0.45, 0.45))
            body.close()
        else:
            pdf = body
        try:
            # EPUBs whose cover lives only in the metadata (not on a page) get a cover page
            shift = 0
            cover = _epub_cover(src) if fmt_of(src) == "epub" else None
            if cover and len(pdf) and not _page_has_big_image(pdf[0]):
                first = pdf[0].rect
                page = pdf.new_page(0, width=first.width, height=first.height)
                try:
                    page.insert_image(page.rect, stream=cover[1], keep_proportion=True)
                    shift = 1
                except Exception:
                    pdf.delete_page(0)
            toc = doc.get_toc(simple=True)
            if toc:
                try:
                    pdf.set_toc(_clean_toc([[lvl, title, page + shift] for lvl, title, page, *_ in toc]))
                except Exception:
                    pass
            _copy_links(doc, pdf, offset=(ml, mt), shift=shift)
            meta = {k: v for k, v in (doc.metadata or {}).items()
                    if k in ("title", "author", "subject", "keywords") and v}
            if meta:
                pdf.set_metadata(meta)
            ctx.job.check()
            pdf.save(str(dst), garbage=3, deflate=True)
        finally:
            pdf.close()
    finally:
        doc.close()


def _copy_links(src_doc, pdf, offset: tuple[float, float] = (0, 0), shift: int = 0) -> None:
    """convert_to_pdf drops links; recreate internal (TOC) and web links.

    `offset` (left, top) moves link boxes into the page margins; `shift` is the number of pages
    inserted in front (e.g. a cover page).
    """
    import pymupdf

    dx, dy = offset
    n = len(pdf)
    for pno in range(min(len(src_doc), n - shift)):
        try:
            links = src_doc[pno].get_links()
        except Exception:
            continue
        for ln in links:
            kind = ln.get("kind")
            new = None
            r = ln["from"]
            ln["from"] = pymupdf.Rect(r.x0 + dx, r.y0 + dy, r.x1 + dx, r.y1 + dy)
            if ln.get("to") is not None:
                ln["to"] = pymupdf.Point(ln["to"].x + dx, ln["to"].y + dy)
            if kind == pymupdf.LINK_URI and ln.get("uri"):
                new = {"kind": pymupdf.LINK_URI, "from": ln["from"], "uri": ln["uri"]}
            elif kind in (pymupdf.LINK_GOTO, pymupdf.LINK_NAMED):
                target = ln.get("page", -1)
                if (target is None or target < 0) and ln.get("uri"):
                    try:
                        loc = src_doc.resolve_link(ln["uri"])
                        target = src_doc.page_number_from_location(loc[0]) if loc and loc[0] else -1
                    except Exception:
                        target = -1
                if target is not None and 0 <= target + shift < n:
                    new = {"kind": pymupdf.LINK_GOTO, "from": ln["from"], "page": target + shift,
                           "to": ln.get("to") or pymupdf.Point(0, 0)}
            if new:
                try:
                    pdf[pno + shift].insert_link(new)
                except Exception:
                    pass


def _pdf_to_txt(src: Path, dst: Path, ctx: Ctx) -> None:
    doc = _open_pdf(src)
    try:
        paras: list[str] = []
        total = max(1, len(doc))
        for i, page in enumerate(doc):
            ctx.job.check()
            for b in page.get_text("blocks", sort=True):
                if b[6] != 0:  # image block
                    continue
                lines = [ln.strip() for ln in b[4].splitlines() if ln.strip()]
                if not lines:
                    continue
                text = lines[0]
                for ln in lines[1:]:
                    if text.endswith("-") and ln[:1].islower():
                        text = text[:-1] + ln
                    else:
                        text += " " + ln
                paras.append(text)
            ctx.progress((i + 1) / total)
        dst.write_text("\n\n".join(paras) + "\n", encoding="utf-8")
    finally:
        doc.close()


def _pdf_to_md(src: Path, dst: Path, ctx: Ctx) -> None:
    """Layout analysis: finds headings, paragraphs, tables and images; drops page headers/footers."""
    import pymupdf

    doc = _open_pdf(src)
    img_dir = dst.parent / f"{dst.stem}_files"
    try:
        total = len(doc)
        # The first page becomes the book's cover image. A picture-only cover page is then
        # left out of the text so it doesn't appear twice.
        first = doc[0]
        cover_page = total > 1 and _is_cover_page(first)
        ebook_out = ctx.opts.target in EBOOK_TARGETS
        prefix = ""
        if cover_page or ebook_out:
            img_dir.mkdir(parents=True, exist_ok=True)
            cover = img_dir / "cover.jpg"
            zoom = min(2.5, 1600 / max(1.0, first.rect.height))
            first.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False).save(str(cover), jpg_quality=88)
            if ebook_out:
                ctx.cover = cover
            elif cover_page:
                prefix = f"![Cover]({img_dir.name}/cover.jpg)\n\n"
        chunks: list[str] = []
        step = 4
        first_text_page = 1 if cover_page else 0
        for start in range(first_text_page, total, step):
            ctx.job.check()
            pages = list(range(start, min(start + step, total)))
            md = _markdown_pages(doc, pages, img_dir)
            chunks.append(md if isinstance(md, str) else str(md))
            ctx.progress(min(start + step, total) / max(1, total))
        text = prefix + "\n\n".join(chunks)
        if not text.strip():
            raise ConversionError("No text found — this PDF may be scanned images only")
        # make image links relative so the .md and its _files folder travel together
        if img_dir.exists():
            for variant in {str(img_dir), str(img_dir).replace("\\", "/")}:
                text = text.replace(variant + "/", f"{img_dir.name}/").replace(variant + "\\", f"{img_dir.name}/")
        dst.write_text(text, encoding="utf-8")
    finally:
        doc.close()


def _markdown_pages(doc, pages: list[int], img_dir: Path) -> str:
    """Headings come from the PDF's own bookmarks when it has them, else from font sizes.

    PyMuPDF4LLM's optional ML layout add-on (pymupdf-layout) is deliberately not used:
    it isn't published under an open-source license, so it can't ship in this app.
    """
    import pymupdf4llm

    if getattr(pymupdf4llm, "_use_layout", False):
        pymupdf4llm.use_layout(False)
    hdr = pymupdf4llm.TocHeaders(doc) if doc.get_toc() else None
    return pymupdf4llm.to_markdown(doc, pages=pages, hdr_info=hdr, write_images=True,
                                   image_path=str(img_dir), dpi=150, show_progress=False,
                                   image_size_limit=0.03)  # keep small illustrations too


def _is_cover_page(page) -> bool:
    """A page that is mostly picture with little text (a typical book cover)."""
    import pymupdf

    area = max(1.0, page.rect.width * page.rect.height)
    pic = 0.0
    for info in page.get_image_info():
        r = pymupdf.Rect(info["bbox"]) & page.rect
        if not r.is_empty:
            pic += r.width * r.height
    words = len(page.get_text("words"))
    return (pic / area > 0.45 and words < 120) or (words < 12 and pic / area > 0.1)


def _page_has_big_image(page, share: float = 0.3) -> bool:
    import pymupdf

    area = max(1.0, page.rect.width * page.rect.height)
    for info in page.get_image_info():
        r = pymupdf.Rect(info["bbox"]) & page.rect
        if not r.is_empty and r.width * r.height / area > share:
            return True
    return False


class _Pdf2DocxProgress(logging.Handler):
    """pdf2docx logs '(i/n) Page i' twice (parse, then build); turn that into progress + cancel."""

    def __init__(self, ctx: Ctx):
        super().__init__(logging.INFO)
        self.ctx, self.phase = ctx, 0

    def emit(self, record):  # no try/except on purpose: Cancelled must propagate
        self.ctx.job.check()
        msg = record.getMessage()
        if "Creating pages" in msg or "Making docx" in msg:
            self.phase = 1
        m = re.search(r"\((\d+)/(\d+)\)", msg)
        if m:
            i, n = int(m.group(1)), max(1, int(m.group(2)))
            self.ctx.progress((self.phase + i / n) / 2)


def _pdf_to_docx(src: Path, dst: Path, ctx: Ctx) -> None:
    """Layout-faithful PDF -> Word: keeps fonts, margins, tables and images in place."""
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from pdf2docx import Converter

    handler = _Pdf2DocxProgress(ctx)
    root = logging.getLogger()
    root.addHandler(handler)
    old_level = root.level
    root.setLevel(logging.INFO)
    try:
        try:
            cv = Converter(str(src))
        except Exception as exc:
            raise ConversionError(f"Couldn't open the PDF: {exc}") from None
        try:
            cv.convert(str(dst))
        finally:
            cv.close()
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)


def _html_to_pdf(src: Path, dst: Path, ctx: Ctx) -> None:
    """Fallback HTML -> PDF renderer, used only when LibreOffice isn't installed."""
    import pymupdf

    text = src.read_text(encoding="utf-8", errors="replace")
    story = pymupdf.Story(html=text, archive=pymupdf.Archive(str(src.parent)),
                          user_css="body { font-family: serif; line-height: 1.35; }")
    writer = pymupdf.DocumentWriter(str(dst))
    pg = ctx.page or pages.for_source(src, "html", ctx.opts.page_size)
    box = pymupdf.Rect(0, 0, pg.width, pg.height)
    ml, mt, mr, mb = pg.margins
    where = box + (ml, mt, -mr, -mb)
    more = True
    while more:
        ctx.job.check()
        dev = writer.begin_page(box)
        more, _ = story.place(where)
        story.draw(dev)
        writer.end_page()
    writer.close()


# ----------------------------------------------------------------------- small engines

def _kindle_unpack(src: Path, dst: Path, ctx: Ctx) -> None:
    import mobi

    try:
        tempdir, path = mobi.extract(str(src))
    except Exception as exc:
        raise ConversionError(
            f"Couldn't read this Kindle file — it may be DRM-protected ({exc})") from None
    try:
        p = Path(path)
        ext = p.suffix.lower()
        if ext == ".epub":
            shutil.copyfile(p, dst)
        elif ext in (".html", ".htm"):
            _pandoc(p, dst, ctx)
        else:
            raise ConversionError("This Kindle file holds a PDF, not a book (try Calibre)")
    finally:
        shutil.rmtree(tempdir, ignore_errors=True)


def _read_text_file(p: Path) -> str:
    raw = p.read_bytes()
    for enc in ("utf-8-sig", "utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else ("utf-8-sig",):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    return raw.decode("cp1252", errors="replace")


def _txt_to_html(src: Path, dst: Path, ctx: Ctx) -> None:
    text = _read_text_file(src).replace("\r\n", "\n").replace("\r", "\n")
    if re.search(r"\n\s*\n", text):
        # blank-line separated paragraphs; single newlines are hard wraps
        paras = [" ".join(ln.strip() for ln in block.splitlines())
                 for block in re.split(r"\n\s*\n", text)]
    else:
        paras = text.split("\n")
    body = "\n".join(f"<p>{html.escape(p.strip())}</p>" for p in paras if p.strip())
    dst.write_text(
        f'<!DOCTYPE html>\n<html><head><meta charset="utf-8"><title>{html.escape(ctx.title)}'
        f"</title></head>\n<body>\n{body}\n</body></html>\n",
        encoding="utf-8",
    )
