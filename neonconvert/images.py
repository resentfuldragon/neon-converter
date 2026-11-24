"""Pictures: Pillow for raster formats, MuPDF for SVG and PDF pages, pillow-heif for HEIC."""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

from PIL import Image, ImageOps, ImageSequence

from . import tools
from .tools import ConversionError

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIF_OK = True
except Exception:  # pragma: no cover
    HEIF_OK = False

Image.MAX_IMAGE_PIXELS = 400_000_000  # allow big scans/panoramas, still guard against bombs

OUTPUTS: list[tuple[str, str]] = [
    ("png", "PNG"),
    ("jpg", "JPG / JPEG"),
    ("webp", "WEBP"),
    ("gif", "GIF"),
    ("bmp", "BMP"),
    ("tiff", "TIFF"),
    ("ico", "ICO  (Windows icon)"),
    ("avif", "AVIF"),
    ("heic", "HEIC"),
    ("pdf", "PDF"),
]
INPUT_EXTS = {
    "png", "jpg", "jpeg", "jpe", "jfif", "gif", "bmp", "dib", "tif", "tiff", "webp", "ico",
    "heic", "heif", "avif", "svg", "pdf", "psd", "tga", "ppm", "pgm", "pbm", "pcx", "apng",
}
ALIASES = {"jpeg": "jpg", "jpe": "jpg", "jfif": "jpg", "tif": "tiff", "heif": "heic",
           "dib": "bmp", "apng": "png"}
QUALITY_FORMATS = {"jpg", "webp", "avif", "heic"}
ANIMATED_OUT = {"gif", "webp"}
LOSSY_IN = {"jpg", "webp", "heic", "avif"}

RESIZE_CHOICES = ["Original size", "75%", "50%", "25%", "Fit 3840 px", "Fit 1920 px",
                  "Fit 1280 px", "Fit 800 px", "Fit 256 px"]


@dataclass
class Options:
    target: str = "png"
    quality: int = 90
    resize: str = "Original size"
    combine_pdf: bool = False
    strip_meta: bool = False  # remove everything identifying (see _strip)


# info keys that only describe how to *display* the picture: safe to keep when stripping
_DISPLAY_KEYS = {"transparency", "duration", "loop", "background", "gamma"}


def _to_srgb(im: Image.Image, icc: bytes) -> Image.Image:
    """Bake an embedded colour profile into plain sRGB pixels, so dropping it doesn't shift colours."""
    try:
        from PIL import ImageCms

        if im.mode not in ("RGB", "RGBA", "CMYK"):
            return im
        src = ImageCms.ImageCmsProfile(io.BytesIO(icc))
        dst = ImageCms.createProfile("sRGB")
        out_mode = "RGBA" if im.mode == "RGBA" else "RGB"
        return ImageCms.profileToProfile(im, src, dst, outputMode=out_mode) or im
    except Exception:
        return im.convert("RGB") if im.mode == "CMYK" else im


def _strip(pic: "Picture") -> None:
    """Opsec: rebuild every frame from bare pixels.

    Drops EXIF (GPS, camera make/model/serials, lens, owner, dates, software, embedded
    thumbnail), XMP/IPTC (creator, location, edit history, document IDs), JPEG/GIF comments,
    PNG text chunks, TIFF tags and the colour profile (after converting colours to sRGB).
    """
    icc = pic.info.get("icc_profile")
    clean_frames = []
    for f in pic.frames:
        prof = f.info.get("icc_profile") or icc
        g = _to_srgb(f, prof) if prof else f
        g = g.copy()  # a plain image: no TIFF tag directory, PNG text or maker notes ride along
        g.info = {k: v for k, v in f.info.items() if k in _DISPLAY_KEYS}
        clean_frames.append(g)
    pic.frames = clean_frames
    pic.info = {k: v for k, v in pic.info.items() if k in _DISPLAY_KEYS}


@dataclass
class Picture:
    frames: list[Image.Image]
    durations: list[int] = field(default_factory=list)
    loop: int = 0
    info: dict = field(default_factory=dict)
    label: str = ""  # e.g. "page 003" for multi-page sources


def fmt_of(path: str | Path) -> str:
    ext = Path(path).suffix.lower().lstrip(".")
    return ALIASES.get(ext, ext)


# =========================================================================== loading

def page_count(src: Path) -> int:
    """How many pictures `load` will produce (pages for PDFs, else 1)."""
    if src.suffix.lower() != ".pdf":
        return 1
    import pymupdf

    try:
        with pymupdf.open(str(src)) as doc:
            return len(doc)
    except Exception as exc:
        raise ConversionError(f"Couldn't open the PDF: {exc}") from None


def _render_mupdf(src: Path, job: tools.Job) -> Iterator[Picture]:
    """One page at a time, so a 300-page PDF never sits in memory all at once."""
    import pymupdf

    try:
        doc = pymupdf.open(str(src))
    except Exception as exc:
        raise ConversionError(f"Couldn't open the file: {exc}") from None
    try:
        if doc.needs_pass:
            raise ConversionError("This PDF is password-protected")
        for i, page in enumerate(doc):
            job.check()
            if src.suffix.lower() == ".svg":
                longest = max(page.rect.width, page.rect.height) or 1
                zoom = max(1.0, 1024 / longest)
            else:
                zoom = 200 / 72  # 200 dpi for PDF pages
            pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=src.suffix.lower() == ".svg")
            mode = "RGBA" if pix.alpha else "RGB"
            im = Image.frombytes(mode, (pix.width, pix.height), pix.samples)
            yield Picture([im], label=f"page {i + 1:03d}" if len(doc) > 1 else "")
    finally:
        doc.close()


def load(src: Path, keep_animation: bool, job: tools.Job) -> Iterator[Picture]:
    if src.suffix.lower() in (".svg", ".pdf"):
        return _render_mupdf(src, job)
    return iter(_load_raster(src, keep_animation))


def _load_raster(src: Path, keep_animation: bool) -> list[Picture]:
    try:
        im = Image.open(src)
        im.load()
    except Exception as exc:
        if fmt_of(src) == "heic" and not HEIF_OK:
            raise ConversionError("HEIC support isn't available") from None
        raise ConversionError(f"Couldn't read this image ({exc.__class__.__name__})") from None
    info = dict(im.info)
    n_frames = getattr(im, "n_frames", 1)
    if keep_animation and n_frames > 1 and fmt_of(src) in ("gif", "webp", "png"):
        frames, durations = [], []
        for fr in ImageSequence.Iterator(im):
            frames.append(fr.convert("RGBA"))
            durations.append(int(fr.info.get("duration", info.get("duration", 100)) or 100))
        return [Picture(frames, durations, int(info.get("loop", 0) or 0), info)]
    im.seek(0)
    try:
        im = ImageOps.exif_transpose(im)
    except Exception:
        pass
    return [Picture([im], info=info)]


# =========================================================================== transforms

def _resize(im: Image.Image, choice: str) -> Image.Image:
    if choice.endswith("%"):
        f = int(choice[:-1]) / 100
        size = (max(1, round(im.width * f)), max(1, round(im.height * f)))
    elif choice.startswith("Fit"):
        limit = int(choice.split()[1])
        longest = max(im.width, im.height)
        if longest <= limit:
            return im
        f = limit / longest
        size = (max(1, round(im.width * f)), max(1, round(im.height * f)))
    else:
        return im
    if im.mode in ("P", "1"):
        im = im.convert("RGBA")
    return im.resize(size, Image.Resampling.LANCZOS)


def _normalize(im: Image.Image) -> Image.Image:
    """Convert exotic modes (16-bit, CMYK, float) to RGB/RGBA/L."""
    if im.mode in ("I;16", "I;16B", "I;16L", "I", "F"):
        return im.convert("I").point(lambda v: v * (1 / 256)).convert("L")
    if im.mode == "CMYK":
        return im.convert("RGB")
    if im.mode in ("LA", "PA", "RGBa", "La") or (im.mode == "P" and "transparency" in im.info):
        return im.convert("RGBA")
    if im.mode in ("YCbCr", "LAB", "HSV"):
        return im.convert("RGB")
    return im


def _has_alpha(im: Image.Image) -> bool:
    return im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)


def _flatten(im: Image.Image, bg=(255, 255, 255)) -> Image.Image:
    im = _normalize(im)
    if _has_alpha(im):
        rgba = im.convert("RGBA")
        base = Image.new("RGB", rgba.size, bg)
        base.paste(rgba, mask=rgba.getchannel("A"))
        return base
    return im if im.mode in ("RGB", "L") else im.convert("RGB")


def _rgb_or_rgba(im: Image.Image) -> Image.Image:
    im = _normalize(im)
    if _has_alpha(im):
        return im.convert("RGBA")
    return im if im.mode == "RGB" else im.convert("RGB")


# =========================================================================== saving

def _save(pic: Picture, path: Path, target: str, q: int) -> None:
    im = pic.frames[0]
    icc = pic.info.get("icc_profile")
    exif = im.info.get("exif") or pic.info.get("exif")
    extra = {k: v for k, v in (("icc_profile", icc),) if v}
    animated = len(pic.frames) > 1

    if target == "jpg":
        if exif:
            extra["exif"] = exif
        _flatten(im).save(path, "JPEG", quality=q, optimize=True, progressive=True,
                          subsampling=0 if q >= 90 else 2, **extra)
    elif target == "png":
        _normalize(im).save(path, "PNG", **extra)
    elif target == "webp":
        if animated:
            pic.frames[0].save(path, "WEBP", save_all=True, append_images=pic.frames[1:],
                               duration=pic.durations, loop=pic.loop, quality=q, method=4)
        else:
            if exif:
                extra["exif"] = exif
            _rgb_or_rgba(im).save(path, "WEBP", quality=q, method=4, lossless=q >= 100, **extra)
    elif target == "gif":
        if animated:
            pic.frames[0].save(path, "GIF", save_all=True, append_images=pic.frames[1:],
                               duration=pic.durations, loop=pic.loop, disposal=2, optimize=False)
        else:
            _normalize(im).save(path, "GIF")
    elif target == "bmp":
        _flatten(im).save(path, "BMP")
    elif target == "tiff":
        _normalize(im).save(path, "TIFF", compression="tiff_lzw", **extra)
    elif target == "ico":
        rgba = _normalize(im).convert("RGBA")
        side = max(rgba.size)
        square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        square.paste(rgba, ((side - rgba.width) // 2, (side - rgba.height) // 2))
        if side > 256:
            square = square.resize((256, 256), Image.Resampling.LANCZOS)
            side = 256
        sizes = [(s, s) for s in (16, 24, 32, 48, 64, 128, 256) if s <= side] or [(side, side)]
        square.save(path, "ICO", sizes=sizes)
    elif target == "avif":
        _rgb_or_rgba(im).save(path, "AVIF", quality=q, **extra)
    elif target == "heic":
        if not HEIF_OK:
            raise ConversionError("HEIC support isn't available")
        _rgb_or_rgba(im).save(path, "HEIF", quality=q, **extra)
    else:
        raise ConversionError(f"Unknown output format {target}")


def _image_to_pdf_page(pdf, im: Image.Image, lossy_source: bool, q: int) -> None:
    """Add one image as a page; the long side is A4's (842 pt) so pages read naturally."""
    import pymupdf

    rgb = _flatten(im)
    buf = io.BytesIO()
    if lossy_source:
        rgb.save(buf, "JPEG", quality=q, optimize=True)
    else:
        rgb.save(buf, "PNG")
    scale = 842 / max(rgb.width, rgb.height)
    w, h = rgb.width * scale, rgb.height * scale
    page = pdf.new_page(width=w, height=h)
    page.insert_image(pymupdf.Rect(0, 0, w, h), stream=buf.getvalue())


# =========================================================================== public API

def convert(src: Path, out_dir: Path, opts: Options, job: tools.Job,
            status: Callable[[str], None], progress: Callable[[float], None]) -> list[Path]:
    import pymupdf

    target = opts.target
    if target == "pdf" and fmt_of(src) == "pdf":
        raise tools.AlreadyTarget("Already a PDF file — skipped")
    status("Reading…")
    total = page_count(src)
    pics = load(src, keep_animation=target in ANIMATED_OUT, job=job)
    progress(0.1)

    if target == "pdf":
        out = tools.unique_path(out_dir, src.stem, "pdf")
        pdf = pymupdf.open()
        try:
            for pic in pics:
                if opts.strip_meta:
                    _strip(pic)
                _image_to_pdf_page(pdf, _resize(pic.frames[0], opts.resize),
                                   fmt_of(src) in LOSSY_IN, opts.quality)
            if opts.strip_meta:
                pdf.set_metadata({})
            pdf.save(str(out), garbage=3, deflate=True)
        except BaseException:
            out.unlink(missing_ok=True)
            raise
        finally:
            pdf.close()
        progress(1.0)
        return [out]

    folder = out_dir
    if total > 1:
        folder = tools.unique_dir(out_dir, f"{src.stem} pages")
        folder.mkdir(parents=True)
    outs: list[Path] = []
    for i, pic in enumerate(pics):
        job.check()
        status(f"Writing {target.upper()}" + (f" ({i + 1}/{total})" if total > 1 else ""))
        if opts.strip_meta:
            _strip(pic)
        pic.frames = [_resize(f, opts.resize) for f in pic.frames]
        stem = pic.label or src.stem
        out = tools.unique_path(folder, stem, target)
        try:
            _save(pic, out, target, opts.quality)
        except ConversionError:
            raise
        except Exception as exc:
            out.unlink(missing_ok=True)
            raise ConversionError(f"Couldn't write {target.upper()}: {exc}") from None
        outs.append(out)
        progress(0.1 + 0.9 * (i + 1) / total)
    return outs


def combine_to_pdf(sources: list[Path], out_path: Path, opts: Options, job: tools.Job,
                   on_file: Callable[[int, Path], None]) -> Path:
    """Put every image (and every page of any PDFs) into one PDF, in list order."""
    import pymupdf

    pdf = pymupdf.open()
    try:
        for idx, src in enumerate(sources):
            job.check()
            on_file(idx, src)
            for pic in load(src, keep_animation=False, job=job):
                if opts.strip_meta:
                    _strip(pic)
                _image_to_pdf_page(pdf, _resize(pic.frames[0], opts.resize),
                                   fmt_of(src) in LOSSY_IN, opts.quality)
        if len(pdf) == 0:
            raise ConversionError("Nothing to combine")
        if opts.strip_meta:
            pdf.set_metadata({})
        pdf.save(str(out_path), garbage=3, deflate=True)
    finally:
        pdf.close()
    return out_path
