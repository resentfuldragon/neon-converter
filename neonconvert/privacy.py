"""Opsec: remove identifying metadata from finished files.

Only *metadata* can be removed. What a picture or document shows (faces, places, names in the
text, a camera's sensor-noise fingerprint) is content and stays.
"""
from __future__ import annotations

import base64
import io
import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

# FFmpeg: drop container/stream metadata (GPS, device, dates, titles), chapters, and the
# encoder version tags; bitexact also zeroes MP4/MOV creation times.
FFMPEG = ["-map_metadata", "-1", "-map_chapters", "-1", "-fflags", "+bitexact",
          "-flags:v", "+bitexact", "-flags:a", "+bitexact"]

# Pandoc: forget who wrote it before the output is built (title pages print these fields).
PANDOC_LUA = """
function Meta(m)
  for _, k in ipairs({"author", "authors", "date", "creator", "contributor", "publisher", "rights",
                      "institute", "email", "keywords", "subject", "description", "abstract", "identifier"}) do
    m[k] = nil
  end
  return m
end
"""

# Calibre: neutral book metadata.
CALIBRE = ["--authors", "Unknown", "--author-sort", "Unknown", "--book-producer", "Unknown",
           "--publisher", "Unknown", "--pubdate", "2000-01-01", "--timestamp", "2000-01-01"]

FIXED_DATE = "2000-01-01T00:00:00Z"


# =========================================================================== embedded pictures

def _jpeg_orientation(data: bytes) -> int:
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as im:
            return int(im.getexif().get(0x0112, 1) or 1)
    except Exception:
        return 1


def _orientation_app1(orientation: int) -> bytes:
    """A minimal EXIF block holding only the rotation flag (so phone photos stay upright)."""
    from PIL import Image

    ex = Image.Exif()
    ex[0x0112] = orientation
    payload = ex.tobytes()
    if not payload.startswith(b"Exif\x00\x00"):
        payload = b"Exif\x00\x00" + payload
    return b"\xff\xe1" + (len(payload) + 2).to_bytes(2, "big") + payload


def strip_jpeg(data: bytes) -> bytes:
    """Remove EXIF/XMP/IPTC/comments/thumbnails/MPF from a JPEG without re-encoding it.

    Only the orientation flag survives (in a fresh, minimal EXIF block), so pictures that rely on
    it are still shown the right way up.
    """
    if data[:2] != b"\xff\xd8":
        return data
    orientation = _jpeg_orientation(data)
    out = bytearray(b"\xff\xd8")
    if orientation != 1:
        out += _orientation_app1(orientation)
    i, n = 2, len(data)
    while i + 4 <= n:
        if data[i] != 0xFF:
            return data  # not a marker where one should be: leave the file alone
        while i + 1 < n and data[i + 1] == 0xFF:  # fill bytes
            i += 1
        marker = data[i + 1]
        if marker in (0xDA, 0xD9):  # start of scan / end: copy the image data as-is
            out += data[i:]
            return bytes(out)
        if 0xD0 <= marker <= 0xD7 or marker == 0x01:
            out += data[i:i + 2]
            i += 2
            continue
        length = int.from_bytes(data[i + 2:i + 4], "big")
        seg = data[i:i + 2 + length]
        keep = True
        if marker == 0xFE:  # COM
            keep = False
        elif marker == 0xE0:  # APP0: keep plain JFIF, drop JFXX thumbnails
            keep = seg[4:9] == b"JFIF\x00"
        elif marker == 0xE2:  # APP2: keep the colour profile, drop MPF (extra images, depth maps)
            keep = seg[4:16] == b"ICC_PROFILE\x00"
        elif marker == 0xEE:  # APP14 Adobe: needed to decode CMYK correctly
            keep = True
        elif 0xE1 <= marker <= 0xEF:  # APP1 EXIF/XMP, APP13 IPTC/Photoshop, maker data …
            keep = False
        if keep:
            out += seg
        i += 2 + length
    return data


_PNG_SIG = b"\x89PNG\r\n\x1a\n"
_PNG_DROP = {b"tEXt", b"zTXt", b"iTXt", b"eXIf", b"tIME", b"dSIG"}


def strip_png(data: bytes) -> bytes:
    """Remove text chunks (author, software, comments), EXIF and timestamps from a PNG, losslessly."""
    if data[:8] != _PNG_SIG:
        return data
    out = bytearray(_PNG_SIG)
    i = 8
    while i + 12 <= len(data):
        length = int.from_bytes(data[i:i + 4], "big")
        ctype = data[i + 4:i + 8]
        end = i + 12 + length
        if end > len(data):
            return data
        if ctype not in _PNG_DROP:
            out += data[i:end]
        i = end
        if ctype == b"IEND":
            break
    return bytes(out)


def strip_image_bytes(data: bytes) -> bytes:
    if data[:2] == b"\xff\xd8":
        return strip_jpeg(data)
    if data[:8] == _PNG_SIG:
        return strip_png(data)
    return data


def _strip_data_uris(text: str) -> str:
    def fix(m):
        try:
            raw = base64.b64decode(m.group(2))
        except Exception:
            return m.group(0)
        clean = strip_image_bytes(raw)
        return m.group(1) + base64.b64encode(clean).decode("ascii") if clean != raw else m.group(0)

    return re.sub(r"(data:image/(?:jpeg|jpg|png);base64,)([A-Za-z0-9+/=\s]+)", fix, text)


# =========================================================================== file types

def scrub(path: Path) -> None:
    """Scrub a finished output in place (unknown types are left untouched)."""
    ext = path.suffix.lower()
    fn = {".pdf": scrub_pdf, ".docx": scrub_docx, ".odt": scrub_odt, ".epub": scrub_epub,
          ".rtf": scrub_rtf, ".html": scrub_html, ".htm": scrub_html, ".fb2": scrub_fb2,
          ".md": scrub_md, ".jpg": scrub_raw_image, ".jpeg": scrub_raw_image, ".png": scrub_raw_image}.get(ext)
    if fn:
        fn(path)


def scrub_raw_image(path: Path) -> None:
    data = path.read_bytes()
    clean = strip_image_bytes(data)
    if clean != data:
        path.write_bytes(clean)


def _replace(path: Path, writer) -> None:
    fd, tmp = tempfile.mkstemp(prefix="neon_scrub_", suffix=path.suffix, dir=str(path.parent))
    os.close(fd)
    try:
        writer(Path(tmp))
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def scrub_pdf(path: Path) -> None:
    import pymupdf

    doc = pymupdf.open(str(path))
    try:
        doc.set_metadata({})  # author, creator app, producer, dates, title, keywords …
        try:
            doc.del_xml_metadata()
        except Exception:
            pass
        for xref in range(1, doc.xref_length()):
            try:
                for key in ("Metadata", "PieceInfo", "LastModified"):  # per-object XMP & app data
                    if doc.xref_get_key(xref, key)[0] != "null":
                        doc.xref_set_key(xref, key, "null")
                if doc.xref_get_key(xref, "Subtype")[1] == "/Image" and \
                        doc.xref_get_key(xref, "Filter")[1] == "/DCTDecode":
                    raw = doc.xref_stream_raw(xref)
                    clean = strip_jpeg(raw)
                    if clean != raw:  # EXIF/GPS inside an embedded photo
                        doc.update_stream(xref, clean, compress=False)
                        doc.xref_set_key(xref, "Filter", "/DCTDecode")
            except Exception:
                continue
        for page in doc:
            for annot in page.annots() or []:
                try:  # comment/highlight authors
                    annot.set_info(title="", subject="")
                    annot.update()
                except Exception:
                    pass
        fd, tmp = tempfile.mkstemp(prefix="neon_scrub_", suffix=".pdf", dir=str(path.parent))
        os.close(fd)
        doc.save(tmp, garbage=3, deflate=True)
    except BaseException:
        doc.close()
        raise
    doc.close()  # release the file before swapping it (Windows locks open files)
    try:
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


_CORE = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<cp:coreProperties '
         'xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
         'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
         'xmlns:dcmitype="http://purl.org/dc/dcmitype/" '
         'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"></cp:coreProperties>')
_APP = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Properties '
        'xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" '
        'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"></Properties>')
_CUSTOM = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Properties '
           'xmlns="http://schemas.openxmlformats.org/officeDocument/2006/custom-properties" '
           'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"></Properties>')
_IMG_EXT = (".jpg", ".jpeg", ".png", ".jpe", ".jfif")


def _rewrite_zip(path: Path, transform) -> None:
    """Copy a zip (DOCX/ODT/EPUB) entry by entry, keeping order and compression ('mimetype' first)."""
    def write(tmp: Path) -> None:
        with zipfile.ZipFile(path) as zin, zipfile.ZipFile(tmp, "w") as zout:
            for info in zin.infolist():
                data = zin.read(info.filename)
                new = transform(info.filename, data)
                if new is None:
                    continue
                zi = zipfile.ZipInfo(info.filename, date_time=(2000, 1, 1, 0, 0, 0))
                zi.compress_type = info.compress_type
                zi.external_attr = info.external_attr
                zout.writestr(zi, new)
    _replace(path, write)


def scrub_docx(path: Path) -> None:
    def transform(name: str, data: bytes):
        low = name.lower()
        if low == "docprops/core.xml":
            return _CORE.encode()
        if low == "docprops/app.xml":  # company, manager, template, editing time, app version
            return _APP.encode()
        if low == "docprops/custom.xml":
            return _CUSTOM.encode()
        if low.endswith(_IMG_EXT):
            return strip_image_bytes(data)
        if low.endswith(".rels"):  # e.g. template path C:\Users\<you>\…\Normal.dotm
            return re.sub(rb'Target="file:///[^"]*"', b'Target="Normal.dotm"', data)
        if low.endswith(".xml"):
            t = data.decode("utf-8", "ignore")
            t = re.sub(r'\s+w:rsid\w*="[0-9A-Fa-f]+"', "", t)  # editing-session IDs
            t = re.sub(r"<w:rsids>.*?</w:rsids>", "", t, flags=re.S)
            t = re.sub(r'(w(?:15)?:author)="[^"]*"', r'\1="Author"', t)  # comments, tracked changes
            t = re.sub(r'w:initials="[^"]*"', 'w:initials="A"', t)
            t = re.sub(r'\s+w:date="[^"]*"', "", t)
            t = re.sub(r'(w15:(?:userId|providerId))="[^"]*"', r'\1="None"', t)  # account IDs/emails
            return t.encode("utf-8")
        return data
    _rewrite_zip(path, transform)


_ODT_META = ('<?xml version="1.0" encoding="UTF-8"?>\n<office:document-meta '
             'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" office:version="1.3">'
             '<office:meta/></office:document-meta>')


def scrub_odt(path: Path) -> None:
    def transform(name: str, data: bytes):
        low = name.lower()
        if low == "meta.xml":  # creator, dates, editing cycles/time, generator, statistics
            return _ODT_META.encode()
        if low.endswith(_IMG_EXT):
            return strip_image_bytes(data)
        if low.endswith(".xml"):
            t = data.decode("utf-8", "ignore")
            t = re.sub(r"<dc:creator>[^<]*</dc:creator>", "<dc:creator>Author</dc:creator>", t)
            t = re.sub(r"<dc:date>[^<]*</dc:date>", "<dc:date>2000-01-01T00:00:00</dc:date>", t)
            if low == "settings.xml":  # printer names / driver setup
                t = re.sub(r'<config:config-item config:name="Printer(?:Name|Setup|PaperFromSetup)"[^>]*>.*?'
                           r"</config:config-item>", "", t, flags=re.S)
            return t.encode("utf-8")
        return data
    _rewrite_zip(path, transform)


_AUTHOR_ELEMENT = re.compile(r'<(\w+)\b[^>]*class="[^"]*\b(?:author|date)\b[^"]*"[^>]*>.*?</\1>', re.S | re.I)


def scrub_epub(path: Path) -> None:
    def transform(name: str, data: bytes):
        low = name.lower()
        if low.endswith(_IMG_EXT):
            return strip_image_bytes(data)
        if low.endswith((".xhtml", ".html", ".htm")):  # title pages print the author's name
            t = data.decode("utf-8", "ignore")
            new = _AUTHOR_ELEMENT.sub("", t)
            return new.encode("utf-8") if new != t else data
        if low.endswith(".ncx"):
            t = data.decode("utf-8", "ignore")
            return re.sub(r"<docAuthor>.*?</docAuthor>", "", t, flags=re.S).encode("utf-8")
        if low.endswith(".opf"):
            t = data.decode("utf-8", "ignore")
            for tag in ("creator", "contributor", "publisher", "rights", "date", "source", "description"):
                t = re.sub(rf"<dc:{tag}\b[^>]*>.*?</dc:{tag}>|<dc:{tag}\b[^>]*/>", "", t, flags=re.S)
            t = re.sub(r'<meta\b[^>]*(?:name|property)="calibre:[^"]*"[^>]*/>', "", t)
            t = re.sub(r'<meta\b[^>]*property="calibre:[^"]*"[^>]*>.*?</meta>', "", t, flags=re.S)
            t = re.sub(r'<meta\b[^>]*refines="#(?:creator|contributor|epub-creator|epub-contributor)[^"]*"'
                       r"[^>]*>.*?</meta>", "", t, flags=re.S)
            t = re.sub(r'(<meta\b[^>]*property="dcterms:modified"[^>]*>)[^<]*(</meta>)', rf"\g<1>{FIXED_DATE}\2", t)
            return t.encode("utf-8")
        return data
    _rewrite_zip(path, transform)


def _remove_rtf_group(text: str, start_word: str) -> str:
    while True:
        i = text.find(start_word)
        if i < 0:
            return text
        depth, j = 0, i
        while j < len(text):
            c = text[j]
            if c == "\\":
                j += 2
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        text = text[:i] + text[j + 1:]


def scrub_rtf(path: Path) -> None:
    t = path.read_bytes().decode("latin-1")
    for group in ("{\\info", "{\\*\\revtbl", "{\\*\\rsidtbl", "{\\*\\generator", "{\\*\\userprops"):
        t = _remove_rtf_group(t, group)
    t = re.sub(r"\\(?:insrsid|charrsid|pararsid|sectrsid|tblrsid|delrsid|styrsid)\d+", "", t)
    path.write_bytes(t.encode("latin-1"))


def scrub_html(path: Path) -> None:
    t = path.read_text(encoding="utf-8", errors="ignore")
    t = re.sub(r'<meta\s+[^>]*name="(?:author|generator|date|keywords|description|dcterms\.[\w.]+)"[^>]*>\s*',
               "", t, flags=re.I)
    t = _AUTHOR_ELEMENT.sub("", t)
    t = _strip_data_uris(t)
    path.write_text(t, encoding="utf-8")


def scrub_fb2(path: Path) -> None:
    t = path.read_text(encoding="utf-8", errors="ignore")
    t = re.sub(r"<document-info>.*?</document-info>", "", t, flags=re.S)
    t = re.sub(r"<author>.*?</author>", "<author><nickname>Unknown</nickname></author>", t, flags=re.S)

    def fix(m):
        try:
            raw = base64.b64decode(m.group(2))
        except Exception:
            return m.group(0)
        return m.group(1) + base64.b64encode(strip_image_bytes(raw)).decode("ascii") + m.group(3)

    t = re.sub(r'(<binary\b[^>]*>)([^<]+)(</binary>)', fix, t)
    path.write_text(t, encoding="utf-8")


def scrub_md(path: Path) -> None:
    media = path.parent / f"{path.stem}_files"
    if media.is_dir():
        for f in media.rglob("*"):
            if f.suffix.lower() in _IMG_EXT:
                scrub_raw_image(f)


def scrubbed_copy(src: Path, folder: Path) -> Path:
    """A scrubbed copy of an input (so tools like LibreOffice never see the original's metadata)."""
    dst = folder / src.name
    shutil.copyfile(src, dst)
    scrub(dst)
    return dst
