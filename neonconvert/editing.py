"""Quick edits: rotate / flip, crop, trim, speed, reverse and mute for photos, videos and audio.

Geometry is kept in one canonical form — rotate k quarter-turns clockwise, then optionally
mirror horizontally — so any sequence of button presses becomes at most two FFmpeg filters,
and the crop box is stored as fractions (0..1) of the *displayed* (already rotated) picture.
"""
from __future__ import annotations

import io
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from PIL import Image, ImageOps

from . import images, media, privacy, tools
from .tools import ConversionError

IMAGE_EXTS = images.INPUT_EXTS - {"pdf", "svg"}
VIDEO_EXTS = set(media.VIDEO_EXTS)
AUDIO_EXTS = set(media.AUDIO_EXTS)
SPEEDS = [0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0, 4.0]
ASPECTS: dict[str, float | None] = {
    "Free": None, "Original": -1.0, "1:1": 1.0, "4:3": 4 / 3, "3:4": 3 / 4,
    "16:9": 16 / 9, "9:16": 9 / 16, "3:2": 3 / 2, "2:3": 2 / 3,
}
IMAGE_FORMATS = ["Same as original", "PNG", "JPG", "WEBP"]
VIDEO_FORMATS = ["Same as original", "MP4", "MOV", "MKV", "WEBM", "GIF"]
AUDIO_FORMATS = ["Same as original", "MP3", "M4A", "WAV", "FLAC", "OGG"]

Crop = tuple[float, float, float, float]  # x, y, w, h as fractions of the displayed frame


# =========================================================================== geometry

@dataclass
class Orientation:
    k: int = 0          # quarter turns clockwise
    mirror: bool = False  # horizontal mirror, applied after the rotation

    def rotate_cw(self) -> None:
        self.k = (self.k + (-1 if self.mirror else 1)) % 4

    def rotate_ccw(self) -> None:
        self.k = (self.k + (1 if self.mirror else -1)) % 4

    def flip_h(self) -> None:
        self.mirror = not self.mirror

    def flip_v(self) -> None:  # vertical flip == mirror + half turn
        self.mirror = not self.mirror
        self.k = (self.k + 2) % 4

    @property
    def identity(self) -> bool:
        return self.k == 0 and not self.mirror

    def apply(self, im: Image.Image) -> Image.Image:
        T = Image.Transpose
        im = {0: im, 1: im.transpose(T.ROTATE_270), 2: im.transpose(T.ROTATE_180),
              3: im.transpose(T.ROTATE_90)}[self.k]
        return im.transpose(T.FLIP_LEFT_RIGHT) if self.mirror else im

    def size(self, w: int, h: int) -> tuple[int, int]:
        return (h, w) if self.k % 2 else (w, h)

    def filters(self) -> list[str]:
        f = {0: [], 1: ["transpose=1"], 2: ["hflip", "vflip"], 3: ["transpose=2"]}[self.k]
        return f + (["hflip"] if self.mirror else [])


def crop_after_cw(c: Crop) -> Crop:
    x, y, w, h = c
    return (1 - y - h, x, h, w)


def crop_after_ccw(c: Crop) -> Crop:
    x, y, w, h = c
    return (y, 1 - x - w, h, w)


def crop_after_flip_h(c: Crop) -> Crop:
    x, y, w, h = c
    return (1 - x - w, y, w, h)


def crop_after_flip_v(c: Crop) -> Crop:
    x, y, w, h = c
    return (x, 1 - y - h, w, h)


def fit_aspect(ratio: float, frame_w: int, frame_h: int, within: Crop | None = None) -> Crop:
    """Largest crop with the given pixel aspect ratio, centered in `within` (or the whole frame)."""
    x, y, w, h = within or (0.0, 0.0, 1.0, 1.0)
    pw, ph = w * frame_w, h * frame_h
    if pw / ph > ratio:
        nw = ph * ratio / frame_w
        return (x + (w - nw) / 2, y, nw, h)
    nh = pw / ratio / frame_h
    return (x, y + (h - nh) / 2, w, nh)


@dataclass
class Edit:
    orient: Orientation = field(default_factory=Orientation)
    crop: Crop | None = None
    start: float = 0.0
    end: float | None = None
    speed: float = 1.0
    reverse: bool = False
    mute: bool = False
    fmt: str = "Same as original"
    private: bool = False  # remove identifying metadata from what gets saved

    def crop_filter(self) -> list[str]:
        if not self.crop:
            return []
        x, y, w, h = self.crop
        return [f"crop=trunc(iw*{w:.6f}/2)*2:trunc(ih*{h:.6f}/2)*2:trunc(iw*{x:.6f}):trunc(ih*{y:.6f})"]


def kind_of(path: Path) -> str:
    """'image', 'video' or 'audio' (animated GIFs count as video)."""
    ext = path.suffix.lower().lstrip(".")
    if ext in AUDIO_EXTS:
        return "audio"
    if ext == "gif":
        try:
            with Image.open(path) as im:
                return "video" if getattr(im, "n_frames", 1) > 1 else "image"
        except Exception:
            return "video"
    if ext in VIDEO_EXTS:
        return "video"
    if ext in IMAGE_EXTS:
        return "image"
    raise ConversionError(f"{path.suffix or 'This file'} can't be edited here")


def edited_stem(src: Path) -> str:
    """'clip' -> 'clip (edited)'; editing 'clip (edited) (2)' again doesn't stack the suffix."""
    import re

    return re.sub(r" \(edited\)( \(\d+\))?$", "", src.stem) + " (edited)"


def fmt_time(t: float) -> str:
    t = max(0.0, t)
    m, s = divmod(t, 60)
    h, m = divmod(int(m), 60)
    return f"{h}:{m:02d}:{s:04.1f}" if h else f"{m:02d}:{s:04.1f}"


# =========================================================================== images

def load_image(path: Path) -> Image.Image:
    try:
        im = Image.open(path)
        im.load()
    except Exception as exc:
        raise ConversionError(f"Couldn't read this image ({exc.__class__.__name__})") from None
    return ImageOps.exif_transpose(im)


def export_image(src: Path, edit: Edit, out_dir: Path) -> Path:
    im = edit.orient.apply(load_image(src))
    if edit.crop:
        x, y, w, h = edit.crop
        W, H = im.size
        box = (round(x * W), round(y * H), round((x + w) * W), round((y + h) * H))
        box = (max(0, box[0]), max(0, box[1]), min(W, box[2]), min(H, box[3]))
        if box[2] - box[0] < 1 or box[3] - box[1] < 1:
            raise ConversionError("The crop area is empty")
        im = im.crop(box)
    src_fmt = images.fmt_of(src)
    if edit.fmt == "Same as original":
        target = src_fmt if src_fmt in {"png", "jpg", "webp", "bmp", "tiff", "gif", "heic", "avif"} else "png"
    else:
        target = edit.fmt.lower()
    out = tools.unique_path(out_dir, edited_stem(src), target)
    try:
        pic = images.Picture([im], info={k: v for k, v in im.info.items() if k == "icc_profile"})
        if edit.private:
            images._strip(pic)
        images._save(pic, out, target, 92)
    except ConversionError:
        raise
    except Exception as exc:
        out.unlink(missing_ok=True)
        raise ConversionError(f"Couldn't save the image: {exc}") from None
    return out


# =========================================================================== video & audio

def grab_frame(src: Path, t: float, max_w: int = 1280) -> Image.Image | None:
    """A preview frame (no edits applied). Returns None if FFmpeg can't decode one."""
    for when in (t, max(0.0, t - 0.5), 0.0):
        r = subprocess.run(
            [tools.ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-ss", f"{when:.3f}", "-i", str(src),
             "-frames:v", "1", "-vf", f"scale='min({max_w},iw)':-2", "-f", "image2pipe", "-vcodec", "png", "-"],
            capture_output=True, stdin=subprocess.DEVNULL, creationflags=tools.CREATE_NO_WINDOW)
        if r.returncode == 0 and r.stdout:
            try:
                im = Image.open(io.BytesIO(r.stdout))
                im.load()
                return im.convert("RGB")
            except Exception:
                pass
    return None


def save_frame(src: Path, t: float, edit: Edit, out_dir: Path, job: tools.Job) -> Path:
    vf = edit.orient.filters() + edit.crop_filter()
    out = tools.unique_path(out_dir, f"{src.stem} frame {t:.2f}s", "png")
    args = [tools.ffmpeg_path(), "-hide_banner", "-y", "-ss", f"{t:.3f}", "-i", str(src), "-frames:v", "1"]
    if vf:
        args += ["-vf", ",".join(vf)]
    if edit.private:
        args += privacy.FFMPEG
    rc, _out, err = job.run(args + [str(out)])
    if rc != 0 or not out.exists():
        out.unlink(missing_ok=True)
        raise ConversionError("Couldn't grab that frame: " + tools.tail(err, 1))
    return out


def _atempo(speed: float) -> list[str]:
    parts = []
    while speed > 2.0:
        parts.append("atempo=2.0")
        speed /= 2.0
    while speed < 0.5:
        parts.append("atempo=0.5")
        speed /= 0.5
    if abs(speed - 1.0) > 1e-6:
        parts.append(f"atempo={speed:.6f}")
    return parts


def _video_codec(fmt: str) -> list[str]:
    if fmt in ("mp4", "mov", "mkv"):
        args = ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p"]
        return args + (["-movflags", "+faststart"] if fmt != "mkv" else [])
    if fmt == "webm":
        return ["-c:v", "libvpx-vp9", "-crf", "30", "-b:v", "0", "-row-mt", "1", "-deadline", "good",
                "-cpu-used", "4"]
    if fmt == "avi":
        return ["-c:v", "mpeg4", "-vtag", "xvid", "-q:v", "2"]
    raise ConversionError(f"Can't write {fmt.upper()} video")


def _audio_codec(fmt: str) -> list[str]:
    return {
        "mp4": ["-c:a", "aac", "-b:a", "192k"], "mov": ["-c:a", "aac", "-b:a", "192k"],
        "mkv": ["-c:a", "aac", "-b:a", "192k"], "m4a": ["-c:a", "aac", "-b:a", "192k"],
        "webm": ["-c:a", "libopus", "-b:a", "128k", "-ac", "2"],
        "avi": ["-c:a", "libmp3lame", "-b:a", "192k"], "mp3": ["-c:a", "libmp3lame", "-b:a", "192k"],
        "wav": ["-c:a", "pcm_s16le"], "flac": ["-c:a", "flac"], "ogg": ["-c:a", "libvorbis", "-b:a", "192k"],
        "opus": ["-c:a", "libopus", "-b:a", "128k", "-ac", "2"],
    }[fmt]


def _target_format(src: Path, edit: Edit, kind: str) -> str:
    ext = src.suffix.lower().lstrip(".")
    if edit.fmt != "Same as original":
        return edit.fmt.lower()
    if kind == "audio":
        return ext if ext in {"mp3", "m4a", "wav", "flac", "ogg", "opus"} else "mp3"
    return ext if ext in {"mp4", "mov", "mkv", "webm", "gif", "avi"} else "mp4"


AUDIO_OUT = {"mp3", "m4a", "wav", "flac", "ogg", "opus"}
GIF_TAIL = ("fps=15,scale='min(640,iw)':-2:flags=lanczos,split[a][b];"
            "[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4")
# near-lossless intermediates for chunked reversing; PCM audio keeps the joins gap-free
_MID_VIDEO = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "14", "-pix_fmt", "yuv420p"]
_MID_AUDIO = ["-c:a", "pcm_s16le"]


def _segment_args(src: Path, a: float, b: float, edit: Edit, info: media.Probe, fmt: str,
                  reverse: bool, vcodec: list[str] | None = None,
                  acodec: list[str] | None = None) -> list[str]:
    """FFmpeg arguments (after the global ones) that render [a, b] of the source with all edits.

    `b` may be None when the source's length is unknown: then everything from `a` is used.
    """
    args = ["-ss", f"{a:.3f}"] + (["-t", f"{max(0.05, b - a):.3f}"] if b is not None else []) + ["-i", str(src)]
    audio = info.has_audio and not edit.mute and fmt != "gif"
    if info.has_video and fmt not in AUDIO_OUT:
        vf = edit.orient.filters() + edit.crop_filter()
        if abs(edit.speed - 1.0) > 1e-6:
            vf.append(f"setpts=PTS/{edit.speed:.6f}")
        if reverse:
            vf.append("reverse")
        if fmt == "gif":
            return args + ["-map", "0:v:0", "-an", "-vf", ",".join(vf + [GIF_TAIL]), "-loop", "0"]
        vf.append("scale=trunc(iw/2)*2:trunc(ih/2)*2")
        args += ["-map", "0:v:0", "-vf", ",".join(vf)] + (vcodec or _video_codec(fmt))
    else:
        args += ["-vn"]
    if audio:
        af = _atempo(edit.speed) + (["areverse"] if reverse else [])
        args += ["-map", "0:a:0"] + (["-af", ",".join(af)] if af else []) + (acodec or _audio_codec(fmt))
    else:
        args += ["-an"]
    return args


def export_media(src: Path, edit: Edit, out_dir: Path, job: tools.Job,
                 status: Callable[[str], None], progress: Callable[[float], None]) -> Path:
    kind = kind_of(src)
    info = media.probe(src, job)
    if kind == "video" and not info.has_video:
        kind = "audio"
    dur = info.duration or 0.0
    start = max(0.0, edit.start)
    if dur:
        end = min(edit.end if edit.end is not None else dur, dur)
        if end - start < 0.05:
            raise ConversionError("The trimmed clip is empty — move the end marker after the start")
    else:
        end = None  # length unknown (some streams): keep everything after the start
    fmt = _target_format(src, edit, kind)
    if kind == "audio" and fmt in ("mp4", "mov", "mkv", "webm", "gif", "avi"):
        raise ConversionError("Audio files can only be saved as audio formats")
    if kind == "audio" and not info.has_audio:
        raise ConversionError("This file has no audio track")
    out = tools.unique_path(out_dir, edited_stem(src), fmt)
    out_len = (end - start) / edit.speed if end is not None else None

    try:
        frame_bytes = max(1, info.width * info.height) * 1.5
        fps = info.fps or 30.0
        # `reverse` keeps every decoded frame in memory, so long clips are reversed in chunks
        chunk = max(1.0, min(10.0, 500e6 / (frame_bytes * fps))) if kind == "video" else 60.0
        if edit.reverse and end is not None and (end - start) > chunk * 1.5:
            _reverse_in_chunks(src, start, end, chunk, edit, info, fmt, out, job, status, progress)
        else:
            status("Rendering…" if kind == "video" else "Processing audio…")
            rc, err = media._run_ffmpeg(
                _segment_args(src, start, end, edit, info, fmt, edit.reverse) + [str(out)],
                out_len, job, progress, edit.private)
            if rc != 0:
                raise ConversionError("FFmpeg failed: " + tools.tail(err, 2))
        if not out.exists() or out.stat().st_size == 0:
            raise ConversionError("FFmpeg produced no output")
        progress(1.0)
        return out
    except BaseException:
        out.unlink(missing_ok=True)
        raise


def _reverse_in_chunks(src, start, end, chunk, edit, info, fmt, out, job, status, progress) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="neon_rev_"))
    try:
        bounds = []
        a = start
        while a < end - 1e-3:
            b = min(end, a + chunk)
            bounds.append((a, b))
            a = b
        parts = []
        has_video_out = info.has_video and fmt not in AUDIO_OUT
        for i, (a, b) in enumerate(reversed(bounds)):
            job.check()
            status(f"Reversing part {i + 1}/{len(bounds)}…")
            part = tmp / f"part{i:04d}.mkv"
            mid_fmt = "mkv" if has_video_out else "wav"
            args = _segment_args(src, a, b, edit, info, mid_fmt, reverse=True,
                                 vcodec=_MID_VIDEO, acodec=_MID_AUDIO)
            rc, err = media._run_ffmpeg(
                args + [str(part)], (b - a) / edit.speed, job,
                lambda f, i=i: progress(0.85 * (i + f) / len(bounds)))
            if rc != 0:
                raise ConversionError("FFmpeg failed while reversing: " + tools.tail(err, 2))
            parts.append(part)
        listing = tmp / "parts.txt"
        listing.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8")
        status("Joining…")
        final = ["-f", "concat", "-safe", "0", "-i", str(listing)]
        audio = info.has_audio and not edit.mute and fmt != "gif"
        if fmt == "gif":
            final += ["-map", "0:v:0", "-an", "-vf", GIF_TAIL, "-loop", "0"]
        else:
            final += (["-map", "0:v:0"] + _video_codec(fmt)) if has_video_out else ["-vn"]
            final += (["-map", "0:a:0"] + _audio_codec(fmt)) if audio else ["-an"]
        rc, err = media._run_ffmpeg(final + [str(out)], (end - start) / edit.speed, job,
                                    lambda f: progress(0.85 + 0.15 * f), edit.private)
        if rc != 0:
            raise ConversionError("FFmpeg failed while joining: " + tools.tail(err, 2))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
