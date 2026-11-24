"""'Remove identifying info' must leave no trace of planted metadata in any output.

Every fixture hides unique marker strings (and GPS coordinates) in metadata — EXIF, XMP, IPTC,
comments, ICC profiles, PNG text, TIFF tags, video tags, document authors, template paths,
comment authors, photos embedded in documents — and each output is searched for them,
including inside zip entries, PDF streams and base64-embedded pictures.

    python tests/opsec_test.py [output-folder]
"""
from __future__ import annotations

import base64
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
from docx import Document  # noqa: E402
from docx.shared import Inches  # noqa: E402
from PIL import Image, ImageCms, TiffImagePlugin  # noqa: E402
from PIL.PngImagePlugin import PngInfo  # noqa: E402

from neonconvert import docs, editing, images, media, tools  # noqa: E402

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="neon_opsec_"))
SRC = OUT / "src"
SRC.mkdir(parents=True, exist_ok=True)
noop = lambda *a: None  # noqa: E731
MARKERS = [b"ZQXMAKE", b"ZQXMODEL", b"ZQXARTIST", b"ZQXSERIAL", b"ZQXXMP", b"ZQXCOMMENT", b"ZQXPNG",
           b"ZQXTIFF", b"ZQXTITLE", b"ZQXAUTHOR", b"ZQXCOMPANY", b"ZQXUSER", b"ZQXDOCCOMMENT", b"ZQXEXIF",
           b"ZQXPDFAUTHOR", b"ZQXBOOKAUTHOR", b"ZQXSOFTWARE", b"ZQXLOCATION", b"40.7128", b"+40.71"]
results = []


# =========================================================================== search helpers

def blobs(path: Path):
    """Every byte sequence a reader could see in a file: raw, zip entries, PDF objects/streams,
    base64 data-URIs, plus pixels-free metadata of images."""
    data = path.read_bytes()
    yield "raw", data
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            for n in z.namelist():
                yield n, z.read(n)
    if path.suffix.lower() == ".pdf":
        with pymupdf.open(path) as d:
            yield "pdf-metadata", repr(d.metadata).encode()
            yield "pdf-xmp", (d.get_xml_metadata() or "").encode()
            for x in range(1, d.xref_length()):
                try:
                    yield f"obj{x}", d.xref_object(x).encode("latin-1", "ignore")
                    if d.xref_is_stream(x):
                        yield f"stream{x}", d.xref_stream(x) or b""
                except Exception:
                    pass
    text = data.decode("latin-1")
    for m in re.finditer(r"base64,([A-Za-z0-9+/=]{100,})", text):
        try:
            yield "data-uri", base64.b64decode(m.group(1))
        except Exception:
            pass
    for m in re.finditer(r"<binary\b[^>]*>([^<]+)</binary>", text):
        try:
            yield "fb2-binary", base64.b64decode(m.group(1))
        except Exception:
            pass


def leaks(path: Path) -> list[str]:
    found = set()
    for where, blob in blobs(path):
        for mk in MARKERS:
            if mk in blob:
                found.add(f"{mk.decode()}@{where}")
    return sorted(found)


def check(name, fn):
    t0 = time.time()
    try:
        note = fn() or ""
        results.append((True, name, note, time.time() - t0))
    except BaseException as exc:  # noqa: BLE001
        traceback.print_exc()
        results.append((False, name, f"{exc.__class__.__name__}: {exc}", time.time() - t0))


def opens(p: Path) -> None:
    """Scrubbing rewrites files byte by byte — make sure each one still opens."""
    ext = p.suffix.lower()
    if ext == ".pdf":
        with pymupdf.open(p) as d:
            assert len(d) and d[0].get_text() is not None
    elif ext == ".docx":
        assert Document(str(p)).paragraphs is not None
    elif ext in (".odt", ".epub", ".rtf", ".html", ".fb2", ".md"):
        fmt = {".md": "markdown"}.get(ext, ext[1:])
        r = subprocess.run([tools.pandoc_path(), str(p), "-f", fmt, "-t", "plain"], capture_output=True,
                           creationflags=tools.CREATE_NO_WINDOW)
        assert r.returncode == 0, f"{p.name} no longer parses: {r.stderr[:200]}"
    elif ext in (".jpg", ".png", ".webp", ".heic", ".avif", ".tiff", ".gif", ".bmp", ".ico"):
        with Image.open(p) as im:
            im.load()
    elif ext in (".mp4", ".mkv", ".mov", ".webm", ".mp3", ".m4a", ".flac", ".ogg"):
        info = media.probe(p, tools.Job())
        assert info.duration and info.duration > 0.5, f"{p.name} looks broken"


def expect_clean(paths):
    bad = {p.name: leaks(p) for p in paths}
    bad = {k: v for k, v in bad.items() if v}
    assert not bad, f"metadata survived: {bad}"
    for p in paths:
        opens(p)
    return f"{len(paths)} outputs clean and still open"


# =========================================================================== fixtures

def exif_bytes():
    ex = Image.Exif()
    ex[0x010F] = "ZQXMAKE"      # Make
    ex[0x0110] = "ZQXMODEL"     # Model
    ex[0x013B] = "ZQXARTIST"    # Artist
    ex[0x0131] = "ZQXSOFTWARE"  # Software
    ex[0x010E] = "ZQXTITLE"     # ImageDescription
    ex.get_ifd(0x8769)[0xA431] = "ZQXSERIAL"  # BodySerialNumber
    gps = ex.get_ifd(0x8825)
    gps[1], gps[2], gps[3], gps[4] = "N", (40.0, 42.0, 46.08), "W", (74.0, 0.0, 21.5)
    gps[0x1C] = b"ASCII\x00\x00\x00ZQXLOCATION"  # GPSAreaInformation
    return ex.tobytes()


XMP = (b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
       b'<rdf:Description xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:creator>ZQXXMP</dc:creator>'
       b'</rdf:Description></rdf:RDF></x:xmpmeta>')
SRGB = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


def make_photo(path: Path, size=(320, 240)):
    im = Image.new("RGB", size, (30, 120, 200))
    im.save(path, "JPEG", quality=90, exif=exif_bytes(), xmp=XMP, comment=b"ZQXCOMMENT", icc_profile=SRGB)
    return path


photo = make_photo(SRC / "phone photo.jpg")
png = SRC / "screenshot.png"
info = PngInfo()
info.add_text("Author", "ZQXPNG")
info.add_itxt("XML:com.adobe.xmp", XMP.decode())
Image.new("RGBA", (200, 120), (200, 40, 90, 255)).save(png, pnginfo=info, exif=exif_bytes())
tif = SRC / "scan.tif"
ifd = TiffImagePlugin.ImageFileDirectory_v2()
ifd[315] = "ZQXTIFF"  # Artist
ifd[305] = "ZQXSOFTWARE"
ifd[700] = XMP
Image.new("RGB", (160, 120), "orange").save(tif, tiffinfo=ifd)


def ff(*args):
    subprocess.run([tools.ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y", *args], check=True,
                   creationflags=tools.CREATE_NO_WINDOW)


video = SRC / "clip.mp4"
ff("-f", "lavfi", "-i", "testsrc=duration=3:size=320x240:rate=25", "-f", "lavfi", "-i", "sine=duration=3",
   "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
   "-metadata", "title=ZQXTITLE", "-metadata", "artist=ZQXARTIST", "-metadata", "comment=ZQXCOMMENT",
   "-metadata", "location=+40.7128-074.0060/", "-metadata", "make=ZQXMAKE", "-metadata", "model=ZQXMODEL",
   "-metadata:s:v", "handler_name=ZQXSOFTWARE", str(video))
song = SRC / "song.mp3"
ff("-f", "lavfi", "-i", "sine=duration=3", "-c:a", "libmp3lame", "-metadata", "artist=ZQXARTIST",
   "-metadata", "comment=ZQXCOMMENT", str(song))


def make_docx(path: Path):
    d = Document()
    cp = d.core_properties
    cp.author, cp.last_modified_by, cp.comments, cp.title = "ZQXAUTHOR", "ZQXUSER", "ZQXDOCCOMMENT", "ZQXTITLE"
    d.add_heading("Report", 1)
    para = d.add_paragraph("A paragraph with a reviewer comment. " * 20)
    try:  # the comment's text is content; its author is metadata
        d.add_comment(para.runs, text="Please check this", author="ZQXAUTHOR", initials="ZQ")
    except Exception:
        pass
    pic = make_photo(SRC / "embedded.jpg", (400, 300))
    d.add_picture(str(pic), width=Inches(3))
    d.save(path)
    # add what Word itself writes: company, template path with the Windows user name, revision IDs
    tmp = path.with_suffix(".tmp")
    with zipfile.ZipFile(path) as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for n in zin.namelist():
            data = zin.read(n)
            if n == "docProps/app.xml":
                data = data.replace(b"</Properties>", b"<Company>ZQXCOMPANY</Company><Template>"
                                    b"C:\\Users\\ZQXUSER\\Normal.dotm</Template></Properties>")
            if n == "word/document.xml":
                data = data.replace(b"<w:p>", b'<w:p w:rsidR="00ZQ1234">', 1)
            zout.writestr(n, data)
        zout.writestr("word/_rels/settings.xml.rels",
                      '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/'
                      'package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
                      'officeDocument/2006/relationships/attachedTemplate" '
                      'Target="file:///C:/Users/ZQXUSER/AppData/Roaming/Microsoft/Templates/Normal.dotm" '
                      'TargetMode="External"/></Relationships>')
    tmp.replace(path)
    return path


report = make_docx(SRC / "report.docx")
pdf_src = SRC / "paper.pdf"
d = pymupdf.open()
pg = d.new_page()
pg.insert_text((72, 90), "Chapter One", fontsize=20)
pg.insert_textbox(pymupdf.Rect(72, 110, 520, 300), "Body text. " * 80, fontsize=10)
pg.insert_image(pymupdf.Rect(72, 320, 372, 545), filename=str(photo))
d.set_metadata({"author": "ZQXPDFAUTHOR", "creator": "ZQXSOFTWARE", "title": "ZQXTITLE"})
d.save(pdf_src)
md = SRC / "story.md"
md.write_text("# One\n\n" + "Words " * 200 + "\n\n![pic](embedded.jpg)\n", encoding="utf-8")
book = SRC / "book.epub"
subprocess.run([tools.pandoc_path(), str(md), "-o", str(book), "-M", "title=Book", "-M", "author=ZQXBOOKAUTHOR",
                f"--epub-cover-image={photo}", f"--resource-path={SRC}"], check=True,
               creationflags=tools.CREATE_NO_WINDOW)


# =========================================================================== checks

def sanity():
    # the fixtures really do carry the markers (otherwise "clean" would prove nothing)
    for p in (photo, png, tif, video, song, report, pdf_src, book):
        assert leaks(p), f"fixture {p.name} has no markers"
    return "all fixtures carry planted metadata"


def t_images():
    outs = []
    for src in (photo, png, tif):
        for target in ("jpg", "png", "webp", "heic", "avif", "tiff", "gif", "bmp", "ico", "pdf"):
            o = images.Options(target=target, strip_meta=True)
            outs += images.convert(src, OUT / "img", o, tools.Job(), noop, noop)
    for o in outs:
        if o.suffix == ".pdf":
            continue
        with Image.open(o) as im:
            assert not im.info.get("icc_profile"), f"{o.name} kept a colour profile"
            ex = im.getexif()
            assert not ex.get_ifd(0x8825) and 0x010F not in ex, f"{o.name} kept EXIF"
    return expect_clean(outs)


def t_images_kept_without_switch():
    o = images.convert(photo, OUT / "img", images.Options(target="jpg"), tools.Job(), noop, noop)[0]
    assert b"ZQXMAKE" in o.read_bytes(), "camera info should stay when the switch is off"
    return "switch off → camera info kept (as expected)"


def t_media():
    outs = []
    for mode, target, extra in (("video", "mp4", {}), ("video", "mkv", {}), ("video", "webm", {}),
                                ("video", "mov", {"fast": False}), ("video", "mp4", {"size_mb": 1}),
                                ("audio", "mp3", {}), ("audio", "m4a", {}), ("audio", "flac", {})):
        o = media.Options(mode=mode, target=target, private=True, **extra)
        outs.append(media.convert(video, OUT / "media", o, tools.Job(), noop, noop))
    outs.append(media.convert(song, OUT / "media", media.Options(mode="audio", target="ogg", private=True),
                              tools.Job(), noop, noop))
    return expect_clean(outs)


def t_editor():
    outs = []
    e = editing.Edit(start=0.5, end=2.5, private=True)
    outs.append(editing.export_media(video, e, OUT / "edit", tools.Job(), noop, noop))
    outs.append(editing.export_media(video, editing.Edit(reverse=True, private=True, fmt="MKV"), OUT / "edit",
                                     tools.Job(), noop, noop))
    outs.append(editing.export_media(song, editing.Edit(start=1, end=2, private=True), OUT / "edit",
                                     tools.Job(), noop, noop))
    outs.append(editing.save_frame(video, 1.0, editing.Edit(private=True), OUT / "edit", tools.Job()))
    e2 = editing.Edit(crop=(0.1, 0.1, 0.5, 0.5), private=True)
    outs.append(editing.export_image(photo, e2, OUT / "edit"))
    e3 = editing.Edit(private=True, fmt="PNG")
    e3.orient.rotate_cw()
    outs.append(editing.export_image(png, e3, OUT / "edit"))
    return expect_clean(outs)


def t_docs(src, targets):
    def run():
        outs = []
        for t in targets:
            outs.append(docs.convert(src, OUT / "docs", docs.Options(target=t, private=True), tools.Job(),
                                     noop, noop))
        md_files = [f for o in outs if o.suffix == ".md" for f in (o.parent / f"{o.stem}_files").rglob("*.*")]
        return expect_clean(outs + md_files)
    return run


def t_merge():
    a = docs.convert(report, OUT / "docs", docs.Options(target="pdf"), tools.Job(), noop, noop)  # not private
    assert leaks(a), "unscrubbed part should carry markers"
    out = docs.merge_pdfs([("Report", a), ("Paper", pdf_src)], OUT / "docs" / "Merged private.pdf", tools.Job(),
                          private=True)
    return expect_clean([out])


for sub in ("img", "media", "edit", "docs"):
    (OUT / sub).mkdir(exist_ok=True)
check("fixtures carry metadata", sanity)
check("IMAGE: 3 sources × 10 formats", t_images)
check("IMAGE: switch off keeps EXIF", t_images_kept_without_switch)
check("MEDIA: video/audio + size limit", t_media)
check("EDIT: video/audio/frame/photo", t_editor)
doc_targets = ["pdf", "epub", "odt", "rtf", "html", "md", "fb2", "txt"]
check("TEXT: DOCX → 8 formats", t_docs(report, doc_targets))
check("TEXT: PDF → 7 formats", t_docs(pdf_src, ["epub", "docx", "html", "md", "odt", "rtf", "txt"]))
check("TEXT: EPUB → 7 formats", t_docs(book, ["pdf", "docx", "html", "md", "odt", "rtf", "fb2"]))
if tools.calibre_path():
    check("TEXT: Kindle formats", t_docs(report, ["mobi", "azw3"]))
check("TEXT: merge PDFs", t_merge)

print("\n" + "=" * 100)
for ok, name, note, secs in results:
    print(f"{'PASS' if ok else 'FAIL'}  {name:<36} {secs:5.1f}s  {note}")
print(f"\n{sum(r[0] for r in results)}/{len(results)} passed")
