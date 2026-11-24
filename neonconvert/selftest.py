"""Headless check that every bundled engine works inside the packaged app.

    NeonConverter.exe --selftest <folder-with-samples> <output-folder>

Converts whatever sample files it finds and writes report.txt to the output folder.
"""
from __future__ import annotations

import time
import traceback
from pathlib import Path

from . import docs, images, media, tools

PLAN = [
    ("docx", "text", ["pdf", "epub", "md", "mobi", "azw3"]),
    ("epub", "text", ["pdf", "docx", "txt", "mobi"]),
    ("pdf", "text", ["epub", "docx", "txt", "azw3"]),
    ("mobi", "text", ["epub", "pdf", "docx"]),
    ("azw3", "text", ["epub", "pdf"]),
    ("txt", "text", ["epub", "pdf"]),
    ("md", "text", ["pdf", "html"]),
    ("png", "image", ["jpg", "webp", "ico", "heic", "avif", "pdf"]),
    ("gif", "image", ["webp", "png"]),
    ("svg", "image", ["png"]),
    ("mp4", "video", ["mkv", "webm", "gif"]),
    ("mp4", "audio", ["mp3", "m4a", "flac"]),
]


def run(src_dir: str, out_dir: str) -> int:
    src, out = Path(src_dir), Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    lines = [f"ffmpeg={tools.ffmpeg_path()}", f"pandoc={tools.pandoc_path()}",
             f"libreoffice={tools.libreoffice_path()}", f"calibre={tools.calibre_path()}"]
    fails = 0
    noop = lambda *a: None  # noqa: E731
    for ext, kind, targets in PLAN:
        sample = next(iter(sorted(src.glob(f"*.{ext}"))), None)
        if not sample:
            continue
        for t in targets:
            t0 = time.time()
            try:
                if kind == "text":
                    r = docs.convert(sample, out, docs.Options(target=t), tools.Job(), noop, noop)
                elif kind == "image":
                    r = images.convert(sample, out, images.Options(target=t), tools.Job(), noop, noop)[0]
                else:
                    r = media.convert(sample, out, media.Options(mode=kind, target=t), tools.Job(), noop, noop)
                route = docs.describe_route(docs.fmt_of(sample), t) if kind == "text" else ""
                lines.append(f"PASS  {sample.name} -> {t:5} {time.time() - t0:5.1f}s  {r.name}  {route}")
            except BaseException as exc:  # noqa: BLE001
                fails += 1
                lines.append(f"FAIL  {sample.name} -> {t:5} {exc.__class__.__name__}: {exc}")
                lines.append(traceback.format_exc())
    # quick edits (04 // EDIT) and the extras
    from . import editing

    vid = next(iter(sorted(src.glob("*.mp4"))), None)
    pic = next(iter(sorted(src.glob("*.png"))), None)
    pdfs = sorted(src.glob("*.pdf"))[:2]
    checks = []
    if vid:
        e = editing.Edit(start=0.5, end=2.5, speed=2.0, reverse=True, crop=(0.25, 0.0, 0.5, 1.0))
        e.orient.rotate_cw()
        checks.append(("edit video trim+2x+reverse+rotate+crop",
                       lambda: editing.export_media(vid, e, out, tools.Job(), noop, noop)))
        checks.append(("video size limit 1 MB",
                       lambda: media.convert(vid, out, media.Options(target="mkv", size_mb=1, fast=False),
                                             tools.Job(), noop, noop)))
    if pic:
        e2 = editing.Edit(crop=(0.1, 0.1, 0.5, 0.5))
        e2.orient.flip_h()
        checks.append(("edit photo mirror+crop", lambda: editing.export_image(pic, e2, out)))
    if pdfs:
        checks.append(("merge PDFs", lambda: docs.merge_pdfs([(p.stem, p) for p in pdfs], out / "Merged.pdf",
                                                             tools.Job())))
    # privacy (needs the colour-management module and every scrubber inside the build)
    photos = sorted(src.glob("*.jpg")) + sorted(src.glob("*.png"))
    if photos:
        checks.append(("private photo -> jpg", lambda: images.convert(
            photos[0], out, images.Options(target="jpg", strip_meta=True), tools.Job(), noop, noop)[0]))
    word = next(iter(sorted(src.glob("*.docx"))), None)
    if word:
        for t in ("pdf", "epub", "odt"):
            checks.append((f"private docx -> {t}", lambda t=t: docs.convert(
                word, out, docs.Options(target=t, private=True), tools.Job(), noop, noop)))
    if vid:
        checks.append(("private video -> mp4", lambda: media.convert(
            vid, out, media.Options(target="mp4", private=True, fast=False), tools.Job(), noop, noop)))
    for name, fn in checks:
        t0 = time.time()
        try:
            r = fn()
            lines.append(f"PASS  {name:<40} {time.time() - t0:5.1f}s  {r.name}")
        except BaseException as exc:  # noqa: BLE001
            fails += 1
            lines.append(f"FAIL  {name} {exc.__class__.__name__}: {exc}")
            lines.append(traceback.format_exc())
    lines.append(f"{fails} failures")
    (out / "report.txt").write_text("\n".join(lines), encoding="utf-8")
    return 1 if fails else 0
