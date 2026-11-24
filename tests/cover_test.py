"""Covers and pictures must survive document conversions (PDF <-> EPUB, EPUB -> Word/HTML/...).

    python tests/cover_test.py [output-folder]
Set NEONCONVERTER_CALIBRE=<calibre folder> to also exercise the Calibre routes.
"""
from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import time
import traceback
import warnings
import zipfile
from pathlib import Path

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pymupdf  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

from neonconvert import docs, tools  # noqa: E402

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="neon_cover_"))
OUT.mkdir(parents=True, exist_ok=True)
SRC = OUT / "src"
SRC.mkdir(exist_ok=True)
results = []
noop = lambda *a: None  # noqa: E731


# ---------------------------------------------------------------- fixtures
def make_fixtures():
    cover = SRC / "cover.png"
    im = Image.new("RGB", (600, 900), (20, 10, 60))
    d = ImageDraw.Draw(im)
    for i in range(0, 900, 6):
        d.line([(0, i), (600, i)], fill=(20 + i // 8, 10, 60 + i // 6))
    d.rectangle([60, 300, 540, 520], fill=(255, 42, 109))
    im.save(cover)
    pic = SRC / "figure.png"
    fig = Image.new("RGB", (400, 260), "white")
    ImageDraw.Draw(fig).ellipse([60, 30, 340, 230], fill=(0, 229, 255))
    fig.save(pic)

    md = SRC / "story.md"
    md.write_text(
        "---\ntitle: Neon Nights\nauthor: Test Author\n---\n\n"
        "# Chapter One\n\nIt was raining in the city. " * 1 + "Words " * 80 +
        "\n\n![A cyan figure](figure.png)\n\n## The Alley\n\nMore text here. " + "More " * 60 +
        "\n\n# Chapter Two\n\nThe end is near. " + "Tale " * 80 + "\n", encoding="utf-8")
    epub = SRC / "book_cover.epub"
    subprocess.run([tools.pandoc_path(), str(md), "-o", str(epub), f"--epub-cover-image={cover}",
                    f"--resource-path={SRC}"], check=True, creationflags=tools.CREATE_NO_WINDOW)

    # EPUB whose cover exists only in the metadata (no cover page in the reading order)
    meta = SRC / "meta_cover.epub"
    with zipfile.ZipFile(meta, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                   '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
                   '</rootfiles></container>')
        z.writestr("OEBPS/content.opf",
                   '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="id">'
                   '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Meta Cover</dc:title>'
                   '<dc:identifier id="id">x1</dc:identifier><dc:language>en</dc:language>'
                   '<meta name="cover" content="cov"/></metadata><manifest>'
                   '<item id="cov" href="images/front.png" media-type="image/png"/>'
                   '<item id="c1" href="ch1.xhtml" media-type="application/xhtml+xml"/>'
                   '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/></manifest>'
                   '<spine toc="ncx"><itemref idref="c1"/></spine></package>')
        z.writestr("OEBPS/toc.ncx",
                   '<?xml version="1.0"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
                   '<head/><docTitle><text>Meta Cover</text></docTitle><navMap><navPoint id="n1" playOrder="1">'
                   '<navLabel><text>Only Chapter</text></navLabel><content src="ch1.xhtml"/></navPoint></navMap></ncx>')
        z.writestr("OEBPS/ch1.xhtml",
                   '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"><head><title>c</title></head>'
                   '<body><h1>Only Chapter</h1><p>' + "Text " * 120 + '</p></body></html>')
        z.write(cover, "OEBPS/images/front.png")

    # PDF book: full-page cover picture, then chapters with a figure and bookmarks
    pdf = SRC / "cover_book.pdf"
    doc = pymupdf.open()
    p = doc.new_page(width=420, height=630)
    p.insert_image(p.rect, filename=str(cover))
    for ch in (1, 2):
        p = doc.new_page(width=420, height=630)
        p.insert_text((40, 70), f"Chapter {ch}", fontsize=22)
        body = ("Neon rain fell on the quiet streets while the city hummed. " * 7).strip()
        p.insert_textbox(pymupdf.Rect(40, 90, 380, 300), body, fontsize=10)
        if ch == 1:
            p.insert_image(pymupdf.Rect(60, 320, 360, 515), filename=str(pic))
    doc.set_toc([[1, "Chapter 1", 2], [1, "Chapter 2", 3]])
    doc.save(pdf)
    return {"epub": epub, "meta": meta, "pdf": pdf}


# ---------------------------------------------------------------- inspectors
def pdf_info(p: Path):
    with pymupdf.open(p) as d:
        first_big = docs._page_has_big_image(d[0])
        n_img = sum(len(pg.get_images()) for pg in d)
        return first_big, n_img, len(d.get_toc())


def epub_info(p: Path):
    cover = docs._epub_cover(p)
    with zipfile.ZipFile(p) as z:
        imgs = [n for n in z.namelist() if n.lower().endswith((".png", ".jpg", ".jpeg", ".gif"))]
        body = " ".join(z.read(n).decode("utf-8", "ignore") for n in z.namelist()
                        if n.endswith((".xhtml", ".html")) and "cover" not in n.lower() and "nav" not in n.lower())
    return cover is not None, len(imgs), body.count("<img")


def zip_media(p: Path, prefix: str):
    with zipfile.ZipFile(p) as z:
        return len([n for n in z.namelist() if n.startswith(prefix) and not n.endswith("/")])


def conv(src, target, **kw):
    return docs.convert(src, OUT, docs.Options(target=target, **kw), tools.Job(), noop, noop)


def check(name, fn):
    t0 = time.time()
    try:
        note = fn()
        results.append((True, name, note or "", time.time() - t0))
    except BaseException as exc:  # noqa: BLE001
        traceback.print_exc()
        results.append((False, name, f"{exc.__class__.__name__}: {exc}", time.time() - t0))


def expect(cond, msg):
    if not cond:
        raise AssertionError(msg)


def main():
    print("calibre:", tools.calibre_path())
    f = make_fixtures()

    def epub_to_pdf(src):
        def run():
            out = conv(src, "pdf")
            big, n, toc = pdf_info(out)
            expect(big, "first page has no cover image")
            return f"{out.name}: cover page ✓ images={n} bookmarks={toc}  via {docs.describe_route('epub', 'pdf')}"
        return run

    check("EPUB (cover page) -> PDF", epub_to_pdf(f["epub"]))
    check("EPUB (metadata cover) -> PDF", epub_to_pdf(f["meta"]))

    def pdf_to_epub():
        out = conv(f["pdf"], "epub")
        has_cover, n_img, inline = epub_info(out)
        expect(has_cover, "no cover in EPUB metadata")
        expect(n_img >= 2, f"expected cover + figure, found {n_img} images")
        expect(inline == 1, f"cover page duplicated in text? {inline} inline images")
        return f"{out.name}: cover ✓ images={n_img} inline={inline}"
    check("PDF (picture cover) -> EPUB", pdf_to_epub)

    def kindle_round_trip(src_key, mid):
        def run():
            k = conv(f[src_key], mid)
            back = conv(k, "epub")
            has_cover, n_img, _ = epub_info(back)
            expect(has_cover, "cover lost")
            expect(n_img >= 2, f"images lost ({n_img})")
            return f"{k.name} -> {back.name}: cover ✓ images={n_img}"
        return run

    if tools.calibre_path():
        check("PDF -> MOBI -> EPUB", kindle_round_trip("pdf", "mobi"))
        check("EPUB -> AZW3 -> EPUB", kindle_round_trip("epub", "azw3"))

    def epub_to(target, inspect):
        def run():
            out = conv(f["epub"], target)
            n = inspect(out)
            expect(n >= 2, f"expected cover + figure, found {n} images")
            return f"{out.name}: images={n}  via {docs.describe_route('epub', target)}"
        return run

    check("EPUB -> DOCX", epub_to("docx", lambda p: zip_media(p, "word/media/")))
    check("EPUB -> ODT", epub_to("odt", lambda p: zip_media(p, "Pictures/")))
    check("EPUB -> HTML", epub_to("html", lambda p: len(re.findall(r"<img\b", p.read_text(encoding="utf-8")))))
    check("EPUB -> MD", epub_to("md", lambda p: len(list((p.parent / f"{p.stem}_files").rglob("*.*")))))
    check("EPUB -> RTF", epub_to("rtf", lambda p: p.read_text(encoding="latin-1").count("\\pict")))

    def pdf_to(target, inspect):
        def run():
            out = conv(f["pdf"], target)
            n = inspect(out)
            expect(n >= 2, f"expected cover + figure, found {n}")
            return f"{out.name}: images={n}  via {docs.describe_route('pdf', target)}"
        return run

    check("PDF -> DOCX", pdf_to("docx", lambda p: zip_media(p, "word/media/")))
    check("PDF -> HTML", pdf_to("html", lambda p: len(re.findall(r"<img\b", p.read_text(encoding="utf-8")))))

    print("\n" + "=" * 100)
    for ok, name, note, secs in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name:<30} {secs:5.1f}s  {note}")
    print(f"\n{sum(r[0] for r in results)}/{len(results)} passed")


if __name__ == "__main__":
    main()
