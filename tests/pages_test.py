"""PDF page size: "Original" keeps each source's own size; named sizes are applied.

    python tests/pages_test.py [output-folder]
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import time
import traceback
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pymupdf  # noqa: E402
from docx import Document  # noqa: E402
from docx.enum.section import WD_ORIENT  # noqa: E402
from docx.shared import Inches, Mm  # noqa: E402
from PIL import Image  # noqa: E402

from neonconvert import docs, pages, tools  # noqa: E402

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="neon_pages_"))
OUT.mkdir(parents=True, exist_ok=True)
results = []
noop = lambda *a: None  # noqa: E731


def check(name, fn):
    t0 = time.time()
    try:
        results.append((True, name, fn() or "", time.time() - t0))
    except BaseException as exc:  # noqa: BLE001
        traceback.print_exc()
        results.append((False, name, f"{exc.__class__.__name__}: {exc}", time.time() - t0))


def size_of(pdf: Path, page=1):
    with pymupdf.open(pdf) as d:
        r = d[min(page, len(d) - 1)].rect
        return round(r.width), round(r.height)


def near(a, b, tol=3):
    return abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol


# fixtures ------------------------------------------------------------------
docx = OUT / "a5_landscape.docx"
d = Document()
sec = d.sections[0]
sec.orientation = WD_ORIENT.LANDSCAPE
sec.page_width, sec.page_height = Mm(210), Mm(148)
for side in ("left_margin", "right_margin", "top_margin", "bottom_margin"):
    setattr(sec, side, Inches(1))
d.add_heading("Wide page", 1)
d.add_paragraph("Hello " * 200)
d.save(docx)
A5_LAND = (595, 420)

md = OUT / "notes.md"
md.write_text("# Notes\n\n" + "Text " * 300, encoding="utf-8")
cover = OUT / "cover.png"
Image.new("RGB", (600, 900), "purple").save(cover)
epub = OUT / "book.epub"
subprocess.run([tools.pandoc_path(), str(md), "-o", str(epub), f"--epub-cover-image={cover}", "-M", "title=Book"],
               check=True, creationflags=tools.CREATE_NO_WINDOW)
odt = OUT / "letter.odt"
subprocess.run([tools.pandoc_path(), str(md), "-o", str(odt)], check=True, creationflags=tools.CREATE_NO_WINDOW)
rtf = OUT / "legal.rtf"
rtf.write_text(r"{\rtf1\ansi\paperw12240\paperh20160\margl720\margr720\margt720\margb720 "
               r"{\b Heading}\par " + "Body text. " * 200 + "}", encoding="latin-1")
regional = pages.NAMED[pages.regional_paper()]


def conv(src, target="pdf", size="Original", via=None):
    opts = docs.Options(target=target, page_size=size)
    if via:  # force one engine for this pair
        out = OUT / f"{src.stem}-{via}.{target}"
        ctx = docs.Ctx(tools.Job(), src.stem, opts)
        ctx.page = docs.page_for(src, docs.fmt_of(src), size)
        {"calibre": docs._calibre, "mupdf": docs._mupdf_to_pdf}[via](src, out, ctx)
        return out
    return docs.convert(src, OUT, opts, tools.Job(), noop, noop)


def t_detect():
    p1 = pages.read_document_page(docx, "docx")
    p2 = pages.read_document_page(rtf, "rtf")
    assert p1 and near((p1.width, p1.height), A5_LAND), p1
    assert p1.margins == (72.0, 72.0, 72.0, 72.0), p1.margins
    assert p2 and near((p2.width, p2.height), (612, 1008)) and p2.margins[0] == 36, p2
    return f"docx {p1.label()} margins {p1.margins[0]:.0f}pt · rtf {p2.label()} · regional {pages.regional_paper()}"


def t_docx_lo():
    out = conv(docx)
    s = size_of(out, 0)
    assert near(s, A5_LAND), s
    return f"{s}  via {docs.describe_route('docx', 'pdf')}"


def t_docx_calibre():
    out = conv(docx, via="calibre")
    s = size_of(out, 0)
    assert near(s, A5_LAND), s
    return f"{s} (A5 landscape kept)"


def t_epub(via):
    def run():
        out = conv(epub, via=via)
        s = size_of(out, 2)
        assert near(s, (432, 648)), s
        return f"{s} = 6x9 in, matches the 2:3 cover"
    return run


def t_md():
    out = conv(md)
    s = size_of(out, 0)
    assert near(s, regional), (s, regional)
    return f"{s} = {pages.regional_paper()}  via {docs.describe_route('md', 'pdf')}"


def t_named():
    out = conv(epub, size="A5")
    s = size_of(out, 1)
    assert near(s, (420, 595)), s
    out2 = conv(md, size="Letter")
    s2 = size_of(out2, 0)
    assert near(s2, (612, 792)), s2
    return f"EPUB→A5 {s} · MD→Letter {s2}"


def t_odt_rtf():
    o = conv(odt)
    r = conv(rtf)
    so, sr = size_of(o, 0), size_of(r, 0)
    po = pages.read_document_page(odt, "odt")
    assert po and near(so, (po.width, po.height)), (so, po)
    assert near(sr, (612, 1008)), sr
    return f"ODT {so} · RTF legal {sr}"


check("detect sizes & margins", t_detect)
check("DOCX A5-landscape → PDF (LibreOffice)", t_docx_lo)
if tools.calibre_path():
    check("DOCX A5-landscape → PDF (Calibre)", t_docx_calibre)
    check("EPUB → PDF book page (Calibre)", t_epub("calibre"))
check("EPUB → PDF book page (MuPDF)", t_epub("mupdf"))
check("Markdown → PDF regional paper", t_md)
check("ODT / RTF keep their size", t_odt_rtf)
check("named sizes A5 / Letter", t_named)

print("\n" + "=" * 100)
for ok, name, note, secs in results:
    print(f"{'PASS' if ok else 'FAIL'}  {name:<40} {secs:5.1f}s  {note}")
print(f"\n{sum(r[0] for r in results)}/{len(results)} passed")
