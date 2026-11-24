"""Checks for the EDIT engine: geometry math, image edits, video/audio trim, speed, reverse, crop.

    python tests/edit_test.py [output-folder]
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

from neonconvert import editing, media, tools  # noqa: E402
from neonconvert.editing import Edit, Orientation  # noqa: E402

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="neon_edit_"))
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


def ff(*args):
    subprocess.run([tools.ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y", *args], check=True,
                   creationflags=tools.CREATE_NO_WINDOW)


def probe(p):
    return media.probe(p, tools.Job())


# ---------------------------------------------------------------- fixtures
img = OUT / "marker.png"  # 300x200, red square in the top-left corner
im = Image.new("RGB", (300, 200), "white")
ImageDraw.Draw(im).rectangle([0, 0, 59, 39], fill="red")
im.save(img)
vid = OUT / "clip.mp4"  # 8 s, 320x240, 25 fps, with a tone
ff("-f", "lavfi", "-i", "testsrc=duration=8:size=320x240:rate=25", "-f", "lavfi", "-i",
   "sine=frequency=330:duration=8", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(vid))
song = OUT / "tone.mp3"
ff("-f", "lavfi", "-i", "sine=frequency=500:duration=6", "-c:a", "libmp3lame", str(song))


# ---------------------------------------------------------------- geometry
def geometry():
    # every sequence of ops must equal doing them one by one on real pixels
    import itertools
    ops = ["cw", "ccw", "fh", "fv"]
    T = Image.Transpose
    direct = {"cw": lambda i: i.transpose(T.ROTATE_270), "ccw": lambda i: i.transpose(T.ROTATE_90),
              "fh": lambda i: i.transpose(T.FLIP_LEFT_RIGHT), "fv": lambda i: i.transpose(T.FLIP_TOP_BOTTOM)}
    n = 0
    for seq in itertools.product(ops, repeat=3):
        o = Orientation()
        ref = im
        for op in seq:
            {"cw": o.rotate_cw, "ccw": o.rotate_ccw, "fh": o.flip_h, "fv": o.flip_v}[op]()
            ref = direct[op](ref)
        assert o.apply(im).tobytes() == ref.tobytes(), seq
        n += 1
    # crop box follows the content through a rotation
    c = (0.0, 0.0, 0.2, 0.2)
    o = Orientation()
    o.rotate_cw()
    c2 = editing.crop_after_cw(c)
    W, H = o.size(300, 200)
    rot = o.apply(im)
    box = (round(c2[0] * W), round(c2[1] * H), round((c2[0] + c2[2]) * W), round((c2[1] + c2[3]) * H))
    px = rot.crop(box).getpixel((box[2] - box[0] - 2, 2))
    assert px == (255, 0, 0), px
    return f"{n} op sequences match"


def image_edit():
    e = Edit()
    e.orient.rotate_cw()
    e.crop = (0.5, 0.0, 0.5, 0.5)  # after rotation the red block sits top-right
    out = editing.export_image(img, e, OUT)
    with Image.open(out) as r:
        assert r.size == (100, 150), r.size
        assert r.getpixel((95, 5)) == (255, 0, 0), r.getpixel((95, 5))
    e.fmt = "JPG"
    out2 = editing.export_image(img, e, OUT)
    return f"{out.name} {Image.open(out).size}, {out2.name}"


def video_edit(**kw):
    def run():
        e = Edit(**{k: v for k, v in kw.items() if k not in ("rotate", "fmt_in")})
        if kw.get("rotate"):
            e.orient.rotate_cw()
        out = editing.export_media(kw.get("fmt_in", vid), e, OUT, tools.Job(), noop, noop)
        p = probe(out)
        return f"{out.name} {p.width}x{p.height} {p.duration:.2f}s audio={p.has_audio}"
    return run


def chunked_reverse():
    # force small chunks so the join path is exercised on a short clip
    e = Edit(reverse=True, start=0.0, end=8.0)
    info = probe(vid)
    out = tools.unique_path(OUT, "clip chunked-reverse", "mp4")
    editing._reverse_in_chunks(vid, 0.0, 8.0, 3.0, e, info, "mp4", out, tools.Job(), noop, noop)
    p = probe(out)
    assert abs(p.duration - 8.0) < 0.3, p.duration
    # first frame of the reversed clip must match the last frame of the original
    a = editing.grab_frame(out, 0.0)
    b = editing.grab_frame(vid, 7.95)
    diff = sum(abs(x - y) for x, y in zip(a.resize((32, 24)).tobytes(), b.resize((32, 24)).tobytes()))
    assert diff / (32 * 24 * 3) < 12, diff / (32 * 24 * 3)
    return f"{out.name} {p.duration:.2f}s, frame match ok"


def frames():
    f = editing.grab_frame(vid, 3.0)
    e = Edit()
    e.orient.rotate_ccw()
    e.crop = (0.0, 0.0, 0.5, 0.5)
    out = editing.save_frame(vid, 3.0, e, OUT, tools.Job())
    with Image.open(out) as r:
        return f"preview {f.size}, saved {out.name} {r.size}"


check("geometry math", geometry)
check("image rotate+crop", image_edit)
check("video trim 2-5s", video_edit(start=2.0, end=5.0))
check("video 2x speed", video_edit(speed=2.0))
check("video 0.5x speed", video_edit(start=0, end=3, speed=0.5))
check("video rotate+crop 1:1", video_edit(rotate=True, crop=editing.fit_aspect(1.0, 240, 320)))
check("video reverse (short)", video_edit(start=1, end=4, reverse=True))
check("video reverse (chunked join)", chunked_reverse)
check("video mute -> webm", video_edit(mute=True, fmt="WEBM", end=3))
check("video -> gif 4x", video_edit(fmt="GIF", speed=4.0))
check("audio trim+reverse", video_edit(fmt_in=song, start=1, end=4, reverse=True))
check("audio 1.5x -> wav", video_edit(fmt_in=song, speed=1.5, fmt="WAV"))
check("grab + save frame", frames)
check("kind_of", lambda: str({p.name: editing.kind_of(p) for p in (img, vid, song)}))

print("\n" + "=" * 90)
for ok, name, note, secs in results:
    print(f"{'PASS' if ok else 'FAIL'}  {name:<30} {secs:5.1f}s  {note}")
print(f"\n{sum(r[0] for r in results)}/{len(results)} passed   (files in {OUT})")
