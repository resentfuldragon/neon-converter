"""Video and audio, all through the bundled FFmpeg."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import privacy, tools
from .tools import ConversionError

VIDEO_OUTPUTS: list[tuple[str, str]] = [
    ("mp4", "MP4  (H.264)"),
    ("mkv", "MKV"),
    ("mov", "MOV"),
    ("webm", "WEBM  (VP9)"),
    ("avi", "AVI"),
    ("gif", "GIF  (animated)"),
]
AUDIO_OUTPUTS: list[tuple[str, str]] = [
    ("mp3", "MP3"),
    ("m4a", "M4A  (AAC)"),
    ("wav", "WAV"),
    ("flac", "FLAC  (lossless)"),
    ("ogg", "OGG  (Vorbis)"),
    ("opus", "OPUS"),
]
VIDEO_EXTS = {"mp4", "m4v", "mkv", "mov", "avi", "wmv", "flv", "webm", "mpg", "mpeg", "ts",
              "m2ts", "mts", "3gp", "ogv", "vob", "asf", "f4v", "divx", "gif"}
AUDIO_EXTS = {"mp3", "wav", "flac", "m4a", "aac", "ogg", "oga", "opus", "wma", "aiff", "aif",
              "amr", "ac3", "mka", "alac"}
INPUT_EXTS = VIDEO_EXTS | AUDIO_EXTS

QUALITIES = ["High", "Balanced", "Small file"]
RESOLUTIONS = ["Original", "2160p", "1440p", "1080p", "720p", "480p", "360p"]
BITRATES = ["320 kbps", "256 kbps", "192 kbps", "128 kbps", "96 kbps"]
SIZE_LIMITS = ["No limit", "8 MB", "10 MB", "25 MB", "50 MB", "100 MB", "500 MB"]
SIZED_TARGETS = {"mp4", "mkv", "mov", "webm"}

_X264_CRF = {"High": 18, "Balanced": 23, "Small file": 28}
_VP9_CRF = {"High": 24, "Balanced": 32, "Small file": 40}
_MPEG4_Q = {"High": 2, "Balanced": 4, "Small file": 7}
_AUDIO_BR = {"High": "192k", "Balanced": "160k", "Small file": "96k"}


@dataclass
class Options:
    mode: str = "video"  # "video" or "audio"
    target: str = "mp4"
    quality: str = "Balanced"
    resolution: str = "Original"
    fast: bool = True  # try copying streams without re-encoding first
    bitrate: str = "192 kbps"
    size_mb: int | None = None  # squeeze the video under this size (two-pass encode)
    private: bool = False  # remove location, device, dates, titles and encoder tags


@dataclass
class Probe:
    duration: float | None
    has_video: bool
    has_audio: bool
    vcodec: str
    acodec: str
    width: int = 0  # as displayed (phone rotation already applied)
    height: int = 0
    fps: float = 0.0


def fmt_of(path: str | Path) -> str:
    return Path(path).suffix.lower().lstrip(".")


def probe(src: Path, job: tools.Job) -> Probe:
    rc, out, err = job.run([tools.ffmpeg_path(), "-hide_banner", "-i", str(src)])
    text = err
    dur = None
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if m:
        dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    vids = [ln for ln in text.splitlines() if "Video:" in ln and "attached pic" not in ln]
    auds = [ln for ln in text.splitlines() if "Audio:" in ln]
    if not vids and not auds:
        raise ConversionError("FFmpeg couldn't read this file: " + tools.tail(text, 1))

    def codec(lines):
        if not lines:
            return ""
        mm = re.search(r"(?:Video|Audio):\s*([\w\-]+)", lines[0])
        return mm.group(1) if mm else ""

    w = h = 0
    fps = 0.0
    if vids:
        size = re.search(r",\s*(\d{2,5})x(\d{2,5})", vids[0])
        if size:
            w, h = int(size.group(1)), int(size.group(2))
        rate = re.search(r"([\d.]+)\s*fps", vids[0])
        if rate:
            fps = float(rate.group(1))
        rot = re.search(r"rotation of (-?[\d.]+) degrees", text)
        if rot and round(abs(float(rot.group(1)))) % 180 == 90:
            w, h = h, w  # FFmpeg auto-rotates portrait phone videos when decoding
    return Probe(dur, bool(vids), bool(auds), codec(vids), codec(auds), w, h, fps)


def _run_ffmpeg(args: list[str], duration: float | None, job: tools.Job,
                progress: Callable[[float], None], private: bool = False) -> tuple[int, str]:
    """Run FFmpeg; `args` ends with the output path. `private` scrubs identifying metadata."""
    if private:
        args = [*args[:-1], *privacy.FFMPEG, args[-1]]
    cmd = [tools.ffmpeg_path(), "-hide_banner", "-nostdin", "-y", "-progress", "pipe:1",
           "-nostats", *args]

    def on_line(line: str) -> None:
        if duration and (line.startswith("out_time_us=") or line.startswith("out_time_ms=")):
            try:
                us = int(line.split("=", 1)[1])
                progress(max(0.0, min(1.0, us / 1e6 / duration)))
            except ValueError:
                pass

    rc, _out, err = job.run(cmd, on_line=on_line)
    return rc, err


def _scale_filter(resolution: str, target: str) -> str:
    if target == "gif":
        if resolution == "Original":
            return "scale='min(640,iw)':-2:flags=lanczos"
        return f"scale=-2:'min({resolution[:-1]},ih)':flags=lanczos"
    if resolution == "Original":
        return "scale=trunc(iw/2)*2:trunc(ih/2)*2"  # H.264 needs even sizes
    return f"scale=-2:'min({resolution[:-1]},ih)'"


def _video_args(target: str, o: Options, info: Probe) -> list[str]:
    vf = _scale_filter(o.resolution, target)
    ab = _AUDIO_BR[o.quality]
    if target == "gif":
        return ["-an", "-vf", f"fps=15,{vf},split[a][b];[a]palettegen=stats_mode=diff[p];"
                              "[b][p]paletteuse=dither=bayer:bayer_scale=4", "-loop", "0"]
    maps = ["-map", "0:v:0", "-map", "0:a?"]
    if target in ("mp4", "mov", "mkv"):
        args = maps + ["-vf", vf, "-c:v", "libx264", "-preset", "medium", "-crf",
                       str(_X264_CRF[o.quality]), "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", ab]
        if target in ("mp4", "mov"):
            args += ["-movflags", "+faststart"]
        return args
    if target == "webm":
        return maps + ["-vf", vf, "-c:v", "libvpx-vp9", "-crf", str(_VP9_CRF[o.quality]), "-b:v", "0",
                       "-row-mt", "1", "-deadline", "good", "-cpu-used", "4",
                       "-c:a", "libopus", "-b:a", ab, "-ac", "2"]
    if target == "avi":
        return maps + ["-vf", vf, "-c:v", "mpeg4", "-vtag", "xvid", "-q:v", str(_MPEG4_Q[o.quality]),
                       "-c:a", "libmp3lame", "-b:a", ab]
    raise ConversionError(f"Unknown video format {target}")


def _audio_args(target: str, o: Options, info: Probe) -> list[str]:
    br = o.bitrate.split()[0] + "k"
    base = ["-map", "0:a:0", "-vn", "-sn", "-dn"]
    if target == "mp3":
        return base + ["-c:a", "libmp3lame", "-b:a", br]
    if target == "m4a":
        if info.acodec == "aac":
            return base + ["-c:a", "copy"]  # lossless and instant
        return base + ["-c:a", "aac", "-b:a", br]
    if target == "wav":
        return base + ["-c:a", "pcm_s16le"]
    if target == "flac":
        return base + ["-c:a", "flac"]
    if target == "ogg":
        return base + ["-c:a", "libvorbis", "-b:a", br]
    if target == "opus":
        return base + ["-c:a", "libopus", "-b:a", br, "-ac", "2"]
    raise ConversionError(f"Unknown audio format {target}")


def _encode_to_size(src: Path, out: Path, target: str, o: Options, info: Probe, job: tools.Job,
                    status: Callable[[str], None], progress: Callable[[float], None]) -> None:
    """Two-pass encode at the bitrate that lands just under o.size_mb."""
    import shutil
    import tempfile

    dur = info.duration
    if not dur:
        raise ConversionError("Can't aim for a file size: the video's length is unknown")
    budget = o.size_mb * 8388.608 * 0.93 / dur  # kbit/s for everything, with container headroom
    audio_k = 0 if not info.has_audio else (128 if budget > 1200 else 96 if budget > 500 else 64)
    video_k = int(budget - audio_k)
    if video_k < 100:
        mins = f"{dur / 60:.0f} min" if dur >= 90 else f"{dur:.0f} s"
        raise ConversionError(f"{mins} of video won't fit in {o.size_mb} MB at watchable quality — "
                              "pick a bigger limit, or shorten it in 04 // EDIT")
    # lower the resolution when the bitrate couldn't carry it cleanly
    cap = 480 if video_k < 600 else 720 if video_k < 1500 else 1080 if video_k < 4000 else None
    res = o.resolution
    if cap and (res == "Original" or int(res[:-1]) > cap):
        res = f"{cap}p"
    vf = _scale_filter(res, target)
    if target == "webm":
        vcodec = ["-c:v", "libvpx-vp9", "-b:v", f"{video_k}k", "-row-mt", "1", "-deadline", "good",
                  "-cpu-used", "4"]
        acodec = ["-c:a", "libopus", "-b:a", f"{audio_k}k", "-ac", "2"]
    else:
        vcodec = ["-c:v", "libx264", "-preset", "medium", "-b:v", f"{video_k}k", "-pix_fmt", "yuv420p"]
        acodec = ["-c:a", "aac", "-b:a", f"{audio_k}k"]
    tmp = Path(tempfile.mkdtemp(prefix="neon_2pass_"))
    try:
        log = str(tmp / "pass")
        status(f"Pass 1/2 → {o.size_mb} MB  ({video_k} kbps, {res})")
        rc, err = _run_ffmpeg(["-i", str(src), "-map", "0:v:0", "-vf", vf, *vcodec, "-pass", "1",
                               "-passlogfile", log, "-an", "-f", "null", "NUL"],
                              dur, job, lambda f: progress(f * 0.5))
        if rc != 0:
            raise ConversionError("FFmpeg failed (pass 1): " + tools.tail(err, 2))
        status(f"Pass 2/2 → {o.size_mb} MB")
        args = ["-i", str(src), "-map", "0:v:0", "-vf", vf, *vcodec, "-pass", "2", "-passlogfile", log]
        args += (["-map", "0:a:0", *acodec] if audio_k else ["-an"])
        if target in ("mp4", "mov"):
            args += ["-movflags", "+faststart"]
        rc, err = _run_ffmpeg([*args, str(out)], dur, job, lambda f: progress(0.5 + f * 0.5), o.private)
        if rc != 0:
            raise ConversionError("FFmpeg failed (pass 2): " + tools.tail(err, 2))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def convert(src: Path, out_dir: Path, opts: Options, job: tools.Job,
            status: Callable[[str], None], progress: Callable[[float], None]) -> Path:
    if not tools.ffmpeg_path():
        raise ConversionError("FFmpeg is missing")
    target = opts.target
    status("Analyzing…")
    info = probe(src, job)
    out = tools.unique_path(out_dir, src.stem, target)

    try:
        if opts.mode == "audio":
            if not info.has_audio:
                raise ConversionError("This file has no audio track")
            status(f"Extracting audio → {target.upper()}")
            rc, err = _run_ffmpeg(["-i", str(src), *_audio_args(target, opts, info), str(out)],
                                  info.duration, job, progress, opts.private)
        else:
            if not info.has_video:
                raise ConversionError("No video track — switch to “Extract audio” for audio files")
            rc = 1
            err = ""
            sized = bool(opts.size_mb) and target in SIZED_TARGETS
            already_small = sized and src.stat().st_size <= opts.size_mb * 1024 * 1024
            if opts.fast and target != "gif" and opts.resolution == "Original" and (not sized or already_small):
                status(f"Fast copy → {target.upper()}")
                args = ["-i", str(src), "-map", "0:v:0", "-map", "0:a?", "-c", "copy"]
                if target in ("mp4", "mov"):
                    args += ["-movflags", "+faststart"]
                rc, err = _run_ffmpeg([*args, str(out)], info.duration, job, progress, opts.private)
                if rc != 0:
                    out.unlink(missing_ok=True)  # codecs don't fit the container: re-encode
            if rc != 0 and sized:  # a size limit is a promise: aim the bitrate at it
                progress(0.0)
                _encode_to_size(src, out, target, opts, info, job, status, progress)
                rc = 0
            if rc != 0:
                status(f"Encoding → {target.upper()}  ({opts.quality.lower()})")
                progress(0.0)
                rc, err = _run_ffmpeg(["-i", str(src), *_video_args(target, opts, info), str(out)],
                                      info.duration, job, progress, opts.private)
        if rc != 0 or not out.exists() or out.stat().st_size == 0:
            raise ConversionError("FFmpeg failed: " + tools.tail(err, 2))
        progress(1.0)
        return out
    except BaseException:
        out.unlink(missing_ok=True)
        raise
