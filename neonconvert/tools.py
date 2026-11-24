"""Shared helpers: finding the bundled tools, running them, and naming output files."""
from __future__ import annotations

import functools
import importlib.util
import os
import shutil
import subprocess
import sys
import threading
from collections import deque
from pathlib import Path

IS_WIN = os.name == "nt"
CREATE_NO_WINDOW = 0x08000000 if IS_WIN else 0


class ConversionError(Exception):
    """A failure whose message is meant to be shown to the user."""


class AlreadyTarget(ConversionError):
    """The file is already in the requested format, so it was skipped (not a failure)."""


class Cancelled(BaseException):
    """Raised when the user presses Cancel.

    Derives from BaseException so library code that catches ``Exception``
    (pdf2docx does this per page) cannot swallow it.
    """


# --------------------------------------------------------------------------- paths

def resource_dir() -> Path:
    """Folder holding bundled files (PyInstaller's _internal dir, or the repo root)."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    """Per-user writable folder for settings, logs and the LibreOffice profile."""
    root = os.environ.get("LOCALAPPDATA") or str(Path.home())
    d = Path(root) / "NeonConverter"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _bundled(name: str) -> str | None:
    p = resource_dir() / "bin" / name
    return str(p) if p.is_file() else None


@functools.cache
def ffmpeg_path() -> str | None:
    found = _bundled("ffmpeg.exe")
    if found:
        return found
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return shutil.which("ffmpeg")


@functools.cache
def pandoc_path() -> str | None:
    found = _bundled("pandoc.exe")
    if found:
        return found
    spec = importlib.util.find_spec("pypandoc")
    if spec and spec.submodule_search_locations:
        for loc in spec.submodule_search_locations:
            for name in ("pandoc.exe", "pandoc"):
                p = Path(loc) / "files" / name
                if p.is_file():
                    return str(p)
    return shutil.which("pandoc")


def _program_dirs() -> list[Path]:
    dirs = []
    for var in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        v = os.environ.get(var)
        if v and Path(v) not in dirs:
            dirs.append(Path(v))
    return dirs


@functools.cache
def libreoffice_path() -> str | None:
    for base in _program_dirs():
        for exe in ("soffice.com", "soffice.exe"):
            p = base / "LibreOffice" / "program" / exe
            if p.is_file():
                return str(p)
    return shutil.which("soffice")


def bundled_calibre() -> str | None:
    """Calibre shipped inside the app (engines/calibre), put there by build.ps1.

    When running from source, NEONCONVERTER_CALIBRE may point at a Calibre folder instead.
    """
    for base in (resource_dir() / "engines" / "calibre", os.environ.get("NEONCONVERTER_CALIBRE")):
        if base:
            p = Path(base) / "ebook-convert.exe"
            if p.is_file():
                return str(p)
    return None


def calibre_path() -> str | None:
    """Built-in Calibre first, then a system install (not cached: it can appear while running)."""
    found = bundled_calibre()
    if found:
        return found
    for base in _program_dirs():
        p = base / "Calibre2" / "ebook-convert.exe"
        if p.is_file():
            return str(p)
    return shutil.which("ebook-convert")


# --------------------------------------------------------------------------- output names

def unique_path(folder: Path, stem: str, ext: str) -> Path:
    p = folder / f"{stem}.{ext}"
    i = 1
    while p.exists():
        p = folder / f"{stem} ({i}).{ext}"
        i += 1
    return p


def unique_dir(folder: Path, name: str) -> Path:
    p = folder / name
    i = 1
    while p.exists():
        p = folder / f"{name} ({i})"
        i += 1
    return p


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def tail(text: str, lines: int = 3) -> str:
    """Last few meaningful lines of a tool's output, for error messages."""
    keep = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return " | ".join(keep[-lines:])[:400]


# --------------------------------------------------------------------------- running tools

def _kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if IS_WIN:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
    else:
        proc.kill()


class Job:
    """One batch run. Holds the cancel flag and the tool process currently running."""

    def __init__(self) -> None:
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def cancel(self) -> None:
        self._cancel.set()
        with self._lock:
            proc = self._proc
        if proc is not None:
            _kill_tree(proc)

    def check(self) -> None:
        if self._cancel.is_set():
            raise Cancelled()

    def run(self, cmd, cwd=None, on_line=None, env=None) -> tuple[int, str, str]:
        """Run a tool without a console window. Returns (exit code, stdout, stderr tail)."""
        self.check()
        proc = subprocess.Popen(
            [str(c) for c in cmd],
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        with self._lock:
            self._proc = proc
        if self.cancelled:
            _kill_tree(proc)

        err: deque[str] = deque(maxlen=80)

        def drain() -> None:
            for line in proc.stderr:  # type: ignore[union-attr]
                err.append(line.rstrip())

        t = threading.Thread(target=drain, daemon=True)
        t.start()
        out: deque[str] = deque(maxlen=400)
        for line in proc.stdout:  # type: ignore[union-attr]
            line = line.rstrip("\r\n")
            out.append(line)
            if on_line:
                on_line(line)
        rc = proc.wait()
        t.join(5)
        with self._lock:
            self._proc = None
        self.check()
        return rc, "\n".join(out), "\n".join(err)
