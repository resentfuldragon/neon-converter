"""End-to-end smoke test: builds sample files, runs every converter, checks the results.

    python tests/smoke_test.py [output-folder]
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

from PIL import Image, ImageDraw  # noqa: E402

from neonconvert import docs, images, media, tools  # noqa: E402

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="neon_smoke_"))
SRC = OUT / "src"
SRC.mkdir(parents=True, exist_ok=True)
results: list[tuple[str, bool, str, float]] = []


def make_samples() -> dict[str, Path]:
    from docx import Document
    from docx.shared import Inches

    s: dict[str, Path] = {}
    # picture used inside the documents
    pic = SRC / "chart.png"
    im = Image.new("RGBA", (400, 240), (20, 10, 40, 255))
    d = ImageDraw.Draw(im)
    for i, c in enumerate([(0, 229, 255), (176, 38, 255), (255, 42, 109)]):
        d.rectangle([40 + i * 120, 200 - (i + 1) * 50, 120 + i * 120, 200], fill=c)
    im.save(pic)

    doc = Document()
    doc.core_properties.title = "Neon Test Book"
    doc.add_heading("Neon Test Book", 0)
    for ch in range(1, 4):
        doc.add_heading(f"Chapter {ch}: The Grid {ch}", 1)
        for para in range(3):
            doc.add_paragraph(
                f"Paragraph {para + 1} of chapter {ch}. Rain hissed on the neon signs while the "
                "city hummed in blue and pink. " * 4)
        doc.add_heading(f"Section {ch}.1", 2)
        doc.add_paragraph("A short subsection with a list:")
        for item in ("alpha", "beta", "gamma"):
            doc.add_paragraph(item, style="List Bullet")
        if ch == 2:
            t = doc.add_table(rows=4, cols=3)
            t.style = "Table Grid"
            for r in range(4):
                for c in range(3):
                    t.cell(r, c).text = ["Name", "Color", "Hex"][c] if r == 0 else f"R{r}C{c}"
            doc.add_picture(str(pic), width=Inches(3))
        doc.add_page_break()
    s["docx"] = SRC / "book.docx"
    doc.save(s["docx"])

    s["txt"] = SRC / "notes.txt"
    s["txt"].write_text("First paragraph line one\nline two of the same paragraph.\n\n"
                        "Second paragraph — with unicode ✓.\n", encoding="utf-8")
    s["md"] = SRC / "readme.md"
    s["md"].write_text("# Title\n\n## Part A\n\nSome *markdown* text.\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n"
                       "## Part B\n\nMore text.\n", encoding="utf-8")
    s["html"] = SRC / "page.html"
    s["html"].write_text("<html><head><title>Page</title></head><body><h1>Heading</h1><p>Hello "
                         "<b>world</b>.</p><h2>Sub</h2><p>More.</p></body></html>", encoding="utf-8")

    # images
    s["png"] = pic
    s["jpg"] = SRC / "photo.jpg"
    Image.new("RGB", (1200, 800), (40, 80, 200)).save(s["jpg"], quality=92)
    frames = [Image.new("RGB", (120, 80), c) for c in [(0, 229, 255), (176, 38, 255), (255, 42, 109)]]
    s["gif"] = SRC / "anim.gif"
    frames[0].save(s["gif"], save_all=True, append_images=frames[1:], duration=200, loop=0)
    s["svg"] = SRC / "logo.svg"
    s["svg"].write_text('<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64">'
                        '<circle cx="32" cy="32" r="28" fill="#ff2a6d"/></svg>', encoding="utf-8")

    # video with audio
    s["mp4"] = SRC / "clip.mp4"
    subprocess.run([tools.ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc=duration=3:size=640x360:rate=25",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(s["mp4"])],
                   check=True, creationflags=tools.CREATE_NO_WINDOW)
    return s


def run(name: str, fn, check=None) -> Path | list[Path] | None:
    t0 = time.time()
    try:
        out = fn()
        note = ""
        if check:
            note = check(out) or ""
        first = out[0] if isinstance(out, list) else out
        size = tools.human_size(first.stat().st_size) if first and first.exists() else "?"
        results.append((name, True, f"{size} {note}".strip(), time.time() - t0))
        return out
    except BaseException as exc:  # noqa: BLE001
        results.append((name, False, f"{exc.__class__.__name__}: {exc}", time.time() - t0))
        traceback.print_exc()
        return None


def doc(src: Path, target: str, **kw):
    opts = docs.Options(target=target, **kw)
    return lambda: docs.convert(src, OUT, opts, tools.Job(), lambda s: None, lambda f: None)


def pdf_toc(p: Path) -> str:
    import pymupdf

    with pymupdf.open(str(p)) as d:
        toc = d.get_toc()
        links = sum(len(pg.get_links()) for pg in d)
        return f"pages={len(d)} bookmarks={len(toc)} links={links} first={[t[1] for t in toc[:3]]}"


def epub_info(p: Path) -> str:
    import zipfile

    with zipfile.ZipFile(p) as z:
        names = z.namelist()
        chapters = [n for n in names if n.endswith(".xhtml") and "ch" in n.lower()]
        imgs = [n for n in names if n.lower().endswith((".png", ".jpg", ".jpeg"))]
        body = " ".join(z.read(n).decode("utf-8", "ignore") for n in names if n.endswith(".xhtml"))
        return f"chapters={len(chapters)} images={len(imgs)} tables={body.count('<table')} h1={body.count('<h1')}"


def main() -> None:
    print("Output folder:", OUT)
    print("ffmpeg:", bool(tools.ffmpeg_path()), "pandoc:", bool(tools.pandoc_path()),
          "libreoffice:", bool(tools.libreoffice_path()), "calibre:", bool(tools.calibre_path()))
    s = make_samples()

    # ---- documents
    pdf = run("docx -> pdf (LibreOffice)", doc(s["docx"], "pdf"), pdf_toc)
    epub = run("docx -> epub (Pandoc)", doc(s["docx"], "epub"), epub_info)
    if epub:
        run("epub -> pdf (MuPDF, A5)", doc(epub, "pdf", page_size="A5"), pdf_toc)
        for t in ("docx", "txt", "md", "html", "fb2", "rtf", "odt"):
            run(f"epub -> {t}", doc(epub, t))
    if pdf:
        run("pdf -> epub (layout + Pandoc)", doc(pdf, "epub"), epub_info)
        run("pdf -> docx (pdf2docx)", doc(pdf, "docx"))
        for t in ("txt", "html", "md", "odt"):
            run(f"pdf -> {t}", doc(pdf, t))
    for t in ("doc", "odt", "rtf", "html", "md", "txt"):
        run(f"docx -> {t}", doc(s["docx"], t))
    run("txt -> epub", doc(s["txt"], "epub"), epub_info)
    run("txt -> pdf", doc(s["txt"], "pdf"), pdf_toc)
    run("md -> pdf", doc(s["md"], "pdf"), pdf_toc)
    run("md -> docx", doc(s["md"], "docx"))
    run("html -> pdf (LibreOffice)", doc(s["html"], "pdf"), pdf_toc)

    def story():
        out = OUT / "story_fallback.pdf"
        docs._html_to_pdf(s["html"], out, docs.Ctx(tools.Job(), "page", docs.Options()))
        return out
    run("html -> pdf (MuPDF fallback)", story, pdf_toc)
    run("mobi output (expects Calibre)", doc(s["docx"], "mobi"))

    # ---- images
    def img(src, target, **kw):
        o = images.Options(target=target, **kw)
        return lambda: images.convert(src, OUT, o, tools.Job(), lambda s: None, lambda f: None)

    def dims(outs):
        with Image.open(outs[0]) as im:
            return f"{im.format} {im.size} {im.mode} frames={getattr(im, 'n_frames', 1)}"

    for t in ("jpg", "webp", "bmp", "tiff", "ico", "avif", "heic", "gif", "pdf"):
        run(f"png -> {t}", img(s["png"], t), None if t == "pdf" else dims)
    run("jpg -> png 50%", img(s["jpg"], "png", resize="50%"), dims)
    run("gif -> webp (animated)", img(s["gif"], "webp"), dims)
    run("svg -> png", img(s["svg"], "png"), dims)
    if pdf:
        run("pdf -> png pages", img(pdf, "png"), lambda o: f"{len(o)} pages, first {dims(o)}")
    run("combine 3 images -> pdf", lambda: images.combine_to_pdf(
        [s["png"], s["jpg"], s["gif"]], OUT / "combined.pdf", images.Options(target="pdf"),
        tools.Job(), lambda i, p: None), pdf_toc)

    # ---- video / audio
    def med(src, mode, target, **kw):
        o = media.Options(mode=mode, target=target, **kw)
        return lambda: media.convert(src, OUT, o, tools.Job(), lambda s: None, lambda f: None)

    for t in ("mkv", "mov", "webm", "avi", "gif"):
        run(f"mp4 -> {t}", med(s["mp4"], "video", t))
    run("mp4 -> mp4 720p re-encode", med(s["mp4"], "video", "mp4", fast=False, resolution="720p"))
    for t in ("mp3", "m4a", "wav", "flac", "ogg", "opus"):
        run(f"mp4 -> audio {t}", med(s["mp4"], "audio", t))
    mp3 = OUT / "clip.mp3"
    if mp3.exists():
        def audio_as_video():
            try:
                med(mp3, "video", "mp4")()
            except tools.ConversionError:
                return [mp3]  # expected: a clear message instead of a crash
            raise AssertionError("an audio file converted as video should be refused")
        run("mp3 -> video refused nicely", audio_as_video, lambda o: "friendly error ✓")

    print("\n" + "=" * 100)
    for name, ok, note, secs in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name:<34} {secs:5.1f}s  {note}")
    print(f"\n{sum(r[1] for r in results)}/{len(results)} passed")


if __name__ == "__main__":
    main()
