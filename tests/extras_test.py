"""PDF merge, video size limit, metadata stripping, unusual file names, and cancelling.

    python tests/extras_test.py [output-folder]
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pymupdf  # noqa: E402
from PIL import Image  # noqa: E402

from neonconvert import docs, editing, images, media, tools  # noqa: E402
from neonconvert.tools import Cancelled  # noqa: E402

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="neon_extras_"))
OUT.mkdir(parents=True, exist_ok=True)
results = []
noop = lambda *a: None  # noqa: E731


def check(name, fn):
    t0 = time.time()
    try:
        note = fn() or ""
        results.append((True, name, note, time.time() - t0))
    except BaseException as exc:  # noqa: BLE001
        traceback.print_exc()
        results.append((False, name, f"{exc.__class__.__name__}: {exc}", time.time() - t0))


def expect(cond, msg):
    if not cond:
        raise AssertionError(msg)


def ff(*args):
    subprocess.run([tools.ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y", *args], check=True,
                   creationflags=tools.CREATE_NO_WINDOW)


# ---------------------------------------------------------------- fixtures
def md_doc(name, chapters):
    p = OUT / f"{name}.md"
    p.write_text("\n\n".join(f"# {c}\n\n" + "Text " * 300 for c in chapters), encoding="utf-8")
    return p


heavy = OUT / "heavy.mp4"  # noisy 720p clip that compresses badly → a big file
ff("-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=12,noise=alls=35:allf=t", "-f", "lavfi",
   "-i", "sine=frequency=300:duration=12", "-c:v", "libx264", "-crf", "16", "-pix_fmt", "yuv420p",
   "-c:a", "aac", "-shortest", str(heavy))


def merge():
    a = docs.convert(md_doc("Part A", ["Alpha 1", "Alpha 2"]), OUT, docs.Options(target="pdf"), tools.Job(), noop, noop)
    b = docs.convert(md_doc("Part B", ["Beta 1"]), OUT, docs.Options(target="pdf"), tools.Job(), noop, noop)
    out = docs.merge_pdfs([("Part A", a), ("Part B", b)], OUT / "Merged.pdf", tools.Job())
    with pymupdf.open(a) as da, pymupdf.open(b) as db, pymupdf.open(out) as dm:
        expect(len(dm) == len(da) + len(db), "page count")
        toc = dm.get_toc()
        tops = [t[1] for t in toc if t[0] == 1]
        expect(tops == ["Part A", "Part B"], f"top bookmarks {tops}")
        expect(any(t[0] == 2 and t[1] == "Beta 1" and t[2] > len(da) for t in toc), f"nested bookmarks {toc}")
        return f"{len(dm)} pages, bookmarks: {[(t[0], t[1], t[2]) for t in toc]}"


def size_limit():
    src_mb = heavy.stat().st_size / 2**20
    o = media.Options(mode="video", target="mp4", size_mb=2)
    out = media.convert(heavy, OUT, o, tools.Job(), noop, noop)
    mb = out.stat().st_size / 2**20
    expect(mb <= 2.0, f"{mb:.2f} MB is over the 2 MB limit")
    expect(mb >= 1.2, f"{mb:.2f} MB wastes the budget")
    p = media.probe(out, tools.Job())
    return f"{src_mb:.1f} MB -> {mb:.2f} MB, {p.width}x{p.height}, audio={p.has_audio}"


def size_limit_already_small():
    o = media.Options(mode="video", target="mkv", size_mb=100)
    out = media.convert(heavy, OUT, o, tools.Job(), noop, noop)
    return f"under limit -> fast copy {out.name} {out.stat().st_size / 2**20:.1f} MB"


def strip_meta():
    src = OUT / "camera.jpg"
    im = Image.new("RGB", (64, 48), (200, 50, 50))
    ex = Image.Exif()
    ex[0x010F] = "NeonCam"         # Make
    ex[0x0110] = "Model X"         # Model
    ex[0x0132] = "2026:01:01 10:00:00"
    im.save(src, exif=ex.tobytes())
    found = {}
    for target in ("jpg", "png", "webp", "heic", "avif"):
        for strip in (False, True):
            out = images.convert(src, OUT, images.Options(target=target, strip_meta=strip), tools.Job(),
                                 noop, noop)[0]
            with Image.open(out) as r:
                make = r.getexif().get(0x010F)
            found[(target, strip)] = make
            if strip:
                expect(make is None, f"{target}: camera info survived stripping ({make})")
    expect(found[("jpg", False)] == "NeonCam", "camera info should be kept when not stripping")
    return "stripped from " + ", ".join(t for (t, s), m in found.items() if s) + "; kept otherwise"


def weird_names():
    names = []
    src_doc = md_doc("Ünïcødé 日本 – test #1 (draft)", ["Kapitel Ä"])
    for t in ("pdf", "epub", "docx"):
        names.append(docs.convert(src_doc, OUT, docs.Options(target=t), tools.Job(), noop, noop).name)
    pic = OUT / "фото 🙂 & co.png"
    Image.new("RGB", (40, 40), "teal").save(pic)
    names.append(images.convert(pic, OUT, images.Options(target="jpg"), tools.Job(), noop, noop)[0].name)
    vid = OUT / "vidéo clip – été.mp4"
    shutil.copy(heavy, vid)
    names.append(media.convert(vid, OUT, media.Options(mode="audio", target="mp3"), tools.Job(), noop, noop).name)
    names.append(editing.export_media(vid, editing.Edit(start=1, end=2), OUT, tools.Job(), noop, noop).name)
    return " | ".join(names)


def cancel():
    job = tools.Job()
    err = []
    o = media.Options(mode="video", target="webm", quality="High", fast=False)

    def run():
        try:
            media.convert(heavy, OUT / "cancel", o, job, noop, noop)
        except BaseException as exc:  # noqa: BLE001
            err.append(exc)

    (OUT / "cancel").mkdir(exist_ok=True)
    t = threading.Thread(target=run)
    t.start()
    time.sleep(1.5)
    t0 = time.time()
    job.cancel()
    t.join(20)
    expect(not t.is_alive(), "worker did not stop")
    expect(err and isinstance(err[0], Cancelled), f"expected Cancelled, got {err}")
    left = list((OUT / "cancel").iterdir())
    expect(not left, f"partial files left behind: {left}")
    return f"stopped in {time.time() - t0:.2f}s, no partial file"


check("merge PDFs with nested bookmarks", merge)
check("video size limit (2 MB)", size_limit)
check("size limit, already small", size_limit_already_small)
check("strip location/camera info", strip_meta)
check("unicode / emoji file names", weird_names)
check("cancel mid-encode", cancel)

print("\n" + "=" * 100)
for ok, name, note, secs in results:
    print(f"{'PASS' if ok else 'FAIL'}  {name:<34} {secs:5.1f}s  {note}")
print(f"\n{sum(r[0] for r in results)}/{len(results)} passed")
