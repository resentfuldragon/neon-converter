"""The window: a sidebar with four modes (TEXT / IMAGE / MEDIA / EDIT) and a neon, color-cycling UI."""
from __future__ import annotations

import json
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import traceback
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk
from PIL import ImageTk

from . import APP_NAME, __version__, docs, images, media, tools
from . import theme as T
from .editor_ui import EditPanel
from .tools import Cancelled, ConversionError
from .widgets import (DropZone, Footer, caption, neon_button, neon_switch, option_menu, reveal,
                      short_name)

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD

    DND_OK = True
except Exception:  # pragma: no cover
    DND_OK = False

ctk.set_appearance_mode("dark")


def friendly_error(exc: BaseException) -> str:
    if isinstance(exc, ConversionError):
        return str(exc)
    if isinstance(exc, PermissionError):
        return "Windows blocked access — the file may be open elsewhere, or pick another SAVE TO folder"
    if isinstance(exc, FileNotFoundError):
        return "File not found — was it moved or deleted?"
    if isinstance(exc, MemoryError):
        return "Ran out of memory on this file"
    return f"{exc.__class__.__name__}: {exc}"


class Settings(dict):
    def __init__(self) -> None:
        super().__init__()
        self.file = tools.data_dir() / "settings.json"
        try:
            self.update(json.loads(self.file.read_text(encoding="utf-8")))
        except Exception:
            pass

    def save(self) -> None:
        try:
            self.file.write_text(json.dumps(self, indent=2), encoding="utf-8")
        except Exception:
            pass


class Item:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.state = "ready"  # ready | working | done | error | cancelled
        self.message = ""
        self.outputs: list[Path] = []
        self.row: FileRow | None = None


# =========================================================================== queue rows

class FileRow(tk.Frame):
    STATE_COLORS = {"ready": T.DIM, "working": T.CYAN, "done": T.GREEN, "error": T.RED,
                    "cancelled": T.AMBER, "skipped": T.AMBER}
    STATE_LABELS = {"ready": "READY", "working": "WORKING", "done": "DONE  ↗", "error": "ERROR  ⓘ",
                    "cancelled": "CANCELLED", "skipped": "SKIPPED"}

    def __init__(self, master, panel: "ModePanel", item: Item) -> None:
        # Plain Tk widgets with sharp HUD-style edges: cheap enough to queue hundreds of files
        # without the window stalling (CustomTkinter widgets redraw a canvas each).
        app = panel.app
        bg = T.CARD
        super().__init__(master, bg=bg, highlightthickness=1, highlightbackground=T.BORDER,
                         highlightcolor=T.BORDER, bd=0)
        self.panel, self.item = panel, item
        self.grid_columnconfigure(2, weight=1)
        s = app.scale
        self.stripe = tk.Frame(self, width=max(3, int(4 * s)), bg=T.DIM)
        self.stripe.grid(row=0, column=0, rowspan=2, sticky="ns", padx=(0, 10))
        ext = item.path.suffix.lstrip(".").upper()[:5] or "?"
        tk.Label(self, text=ext, width=6, font=app.tf_mono_b, bg=T.BG, fg=T.CYAN, bd=0,
                 pady=int(8 * s)).grid(row=0, column=1, rowspan=2, padx=(0, 12), pady=int(8 * s))
        tk.Label(self, text=short_name(item.path.name), anchor="w", font=app.tf_body, bg=bg, fg=T.TEXT,
                 bd=0).grid(row=0, column=2, sticky="ew", pady=(int(8 * s), 0))
        self.sub = tk.Label(self, text="", anchor="w", font=app.tf_mono_s, bg=bg, fg=T.MUTED, bd=0)
        self.sub.grid(row=1, column=2, sticky="ew", pady=(int(2 * s), int(8 * s)))
        self.status = tk.Label(self, text="READY", anchor="e", font=app.tf_mono_b, bg=bg, fg=T.DIM, bd=0)
        self.status.grid(row=0, column=3, rowspan=2, padx=10)
        self.status.bind("<Button-1>", lambda e: self.on_status_click())
        self.remove = tk.Label(self, text=T.GLYPH["close"], font=(T.FONT_ICON, -round(12 * s)), bg=bg, fg=T.MUTED,
                               cursor="hand2", bd=0, padx=6, pady=4)
        self.remove.grid(row=0, column=4, rowspan=2, padx=(0, 12))
        self.remove.bind("<Button-1>", lambda e: panel.remove_item(item))
        self.remove.bind("<Enter>", lambda e: self.remove.configure(fg=T.PINK))
        self.remove.bind("<Leave>", lambda e: self.remove.configure(fg=T.MUTED))
        try:
            self.size = tools.human_size(item.path.stat().st_size)
        except OSError:
            self.size = "?"
        self.route = ""
        self.refresh_sub()

    def set_route(self, route: str) -> None:
        self.route = route
        self.refresh_sub()

    def refresh_sub(self) -> None:
        it = self.item
        if it.state == "done" and it.outputs:
            out = it.outputs[0]
            more = f"  (+{len(it.outputs) - 1} more)" if len(it.outputs) > 1 else ""
            text = f"→ {out.parent.name if len(it.outputs) > 1 else out.name}{more}"
        elif it.state in ("working", "error", "cancelled", "skipped") and it.message:
            text = it.message
        else:
            text = f"{self.size}   ·   {self.route}" if self.route else self.size
        self.sub.configure(text=short_name(text, 96), fg=T.RED if it.state == "error" else T.MUTED)

    def set_state(self, state: str, message: str = "") -> None:
        it = self.item
        it.state, it.message = state, message
        color = self.STATE_COLORS.get(state, T.DIM)
        self.status.configure(text=self.STATE_LABELS[state], fg=color,
                              cursor="hand2" if state in ("done", "error") else "")
        self.stripe.configure(bg=color)
        edge = T.shade(color, 0.6, T.CARD) if state in ("done", "error") else T.BORDER
        self.configure(highlightbackground=edge, highlightcolor=edge)
        self.remove.configure(fg=T.DIM if state == "working" else T.MUTED)
        self.refresh_sub()

    def pulse(self, color: str) -> None:
        if self.item.state == "working":
            self.stripe.configure(bg=color)
            self.status.configure(fg=color)

    def on_status_click(self) -> None:
        it = self.item
        if it.state == "done" and it.outputs:
            reveal(it.outputs[0])
        elif it.state == "error":
            messagebox.showerror(APP_NAME, f"{it.path.name}\n\n{it.message}", parent=self.panel.app)


# =========================================================================== batch panels

class ModePanel(ctk.CTkFrame):
    key = ""
    number = ""
    tag = ""
    title = ""
    subtitle = ""
    formats_hint = ""
    exts: set[str] = set()
    offset = 0.0  # where in the color cycle this mode sits

    def __init__(self, master, app: "App") -> None:
        super().__init__(master, fg_color=T.BG, corner_radius=0)
        self.app = app
        self.items: list[Item] = []
        self._pending_rows: list[Item] = []  # queued files whose rows aren't built yet
        self._row_job = None
        self.job: tools.Job | None = None
        self.running = False
        self.started = 0.0
        self.st = app.settings.setdefault(self.key, {})

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)
        pad = 30
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=pad, pady=(14, 2))
        self.tag_label = ctk.CTkLabel(head, text=f"{self.number} // {self.tag}", font=app.f_mono_b,
                                      text_color=T.CYAN, anchor="w")
        self.tag_label.pack(anchor="w")
        ctk.CTkLabel(head, text=self.title, font=app.f_title, text_color=T.TEXT, anchor="w").pack(anchor="w")
        ctk.CTkLabel(head, text=self.subtitle, font=app.f_small, text_color=T.MUTED, anchor="w",
                     justify="left").pack(anchor="w")

        self.drop = DropZone(self, self, app.scale)
        self.drop.grid(row=1, column=0, sticky="ew", padx=pad, pady=(8, 10))

        qh = ctk.CTkFrame(self, fg_color="transparent")
        qh.grid(row=2, column=0, sticky="ew", padx=pad)
        self.queue_label = ctk.CTkLabel(qh, text="// QUEUE  [0]", font=app.f_mono_b, text_color=T.MUTED)
        self.queue_label.pack(side="left")
        neon_button(qh, app, "CLEAR", self.clear, width=80).pack(side="right")
        neon_button(qh, app, "SORT A→Z", self.sort_queue, width=100).pack(side="right", padx=(8, 0))
        neon_button(qh, app, "+ ADD FILES", self.browse, width=120).pack(side="right", padx=8)

        self.list = ctk.CTkScrollableFrame(self, fg_color=T.PANEL, corner_radius=10, border_width=1,
                                           border_color=T.BORDER, scrollbar_button_color=T.BORDER,
                                           scrollbar_button_hover_color=T.PURPLE)
        self.list.grid(row=3, column=0, sticky="nsew", padx=pad, pady=(8, 12))
        self.empty = ctk.CTkLabel(self.list, text="QUEUE EMPTY  ·  DROP FILES ABOVE TO BEGIN",
                                  font=app.f_mono, text_color=T.DIM)
        self.empty.pack(pady=34)

        self.opt = ctk.CTkFrame(self, fg_color=T.PANEL, corner_radius=10, border_width=1, border_color=T.BORDER)
        self.opt.grid(row=4, column=0, sticky="ew", padx=pad)
        self.build_options(self.opt)

        self.footer = Footer(self, app, self.st, "CONVERT  ▶", self.start, self.cancel)
        self.footer.grid(row=5, column=0, sticky="ew", padx=pad, pady=(10, 12))

    # ----------------------------------------------------------------- subclasses fill in
    def build_options(self, frame) -> None:
        raise NotImplementedError

    def get_options(self):
        raise NotImplementedError

    def route_for(self, item: Item) -> str:
        return ""

    def convert_one(self, src, dest, opts, job, status, progress):
        raise NotImplementedError

    def accepts(self, path: Path) -> bool:
        return path.suffix.lower().lstrip(".") in self.exts

    def set_status(self, text: str, color: str = T.MUTED) -> None:
        self.footer.set_status(text, color)

    # ----------------------------------------------------------------- queue
    def add_files(self, paths: list[Path]) -> int:
        known = {it.path.resolve() for it in self.items}
        added = 0
        for p in paths:
            try:
                rp = p.resolve()
            except OSError:
                continue
            if rp in known or not p.is_file():
                continue
            known.add(rp)
            it = Item(p)
            self.items.append(it)
            self._pending_rows.append(it)
            added += 1
        if added:
            self.empty.pack_forget()
            if self._row_job:
                self.after_cancel(self._row_job)
            self._make_rows()
        self._update_count()
        return added

    def _make_rows(self, limit: int = 8) -> None:
        """Build queue rows a batch at a time so big drops never freeze the window."""
        self._row_job = None
        cache: dict[str, str] = {}
        batch, self._pending_rows = self._pending_rows[:limit], self._pending_rows[limit:]
        for it in batch:
            if it not in self.items or it.row:
                continue
            it.row = FileRow(self.list, self, it)
            it.row.pack(fill="x", padx=6, pady=3)
            key = it.path.suffix.lower()
            if key not in cache:
                cache[key] = self.route_for(it)
            it.row.set_route(cache[key])
            if it.state != "ready":
                it.row.set_state(it.state, it.message)
        if self._pending_rows:
            self._row_job = self.after(20, self._make_rows)  # let clicks and redraws through

    def _flush_rows(self) -> None:
        if self._row_job:
            self.after_cancel(self._row_job)
            self._row_job = None
        while self._pending_rows:
            self._make_rows(limit=200)
            if self._row_job:
                self.after_cancel(self._row_job)
                self._row_job = None

    def sort_queue(self) -> None:
        """Natural A→Z order ('Chapter 2' before 'Chapter 10'); merged PDFs follow this order."""
        if self.running:
            self.set_status("Can't reorder while converting", T.AMBER)
            return
        self._flush_rows()

        def key(it: Item):
            return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", it.path.name.lower())]

        self.items.sort(key=key)
        for it in self.items:
            if it.row:
                it.row.pack_forget()
        for it in self.items:
            if it.row:
                it.row.pack(fill="x", padx=6, pady=3)
        self.set_status(f"Sorted {len(self.items)} files A→Z", T.CYAN)

    def remove_item(self, item: Item) -> None:
        if item.state == "working":
            return
        if item in self.items:
            self.items.remove(item)
        if item.row:
            item.row.destroy()
        self._update_count()

    def clear(self) -> None:
        if self.running:
            self.set_status("Can't clear while converting", T.AMBER)
            return
        if self._row_job:
            self.after_cancel(self._row_job)
            self._row_job = None
        self._pending_rows = []
        for it in self.items:
            if it.row:
                it.row.destroy()
        self.items.clear()
        self.footer.progress.set(0)
        self.footer.last_output = None
        self.footer.open_btn.pack_forget()
        self._update_count()
        self.set_status("SYSTEM READY")

    def _update_count(self) -> None:
        self.queue_label.configure(text=f"// QUEUE  [{len(self.items)}]")
        if not self.items:
            self.empty.pack(pady=34)

    def refresh_routes(self) -> None:
        cache: dict[str, str] = {}
        for it in self.items:
            if it.row:
                key = it.path.suffix.lower()
                if key not in cache:
                    cache[key] = self.route_for(it)
                it.row.set_route(cache[key])

    def browse(self) -> None:
        exts = " ".join(f"*.{e}" for e in sorted(self.exts))
        files = filedialog.askopenfilenames(
            parent=self.app, title=f"Add files — {self.title}",
            filetypes=[(self.title, exts), ("All files", "*.*")],
            initialdir=self.st.get("last_browse") or str(Path.home()))
        if files:
            self.st["last_browse"] = str(Path(files[0]).parent)
            self.app.route_files([Path(f) for f in files], prefer=self)

    # ----------------------------------------------------------------- run
    def start(self) -> None:
        if self.running:
            return
        self._flush_rows()  # every queued file needs its row before the worker reports on it
        todo = [it for it in self.items if it.state in ("ready", "error", "cancelled", "skipped")]
        if not todo:
            self.set_status("Add some files first" if not self.items else
                            "Everything in the queue is converted — add more or press CLEAR", T.AMBER)
            return
        if not self.footer.check_out_dir():
            return
        opts = self.get_options()
        self.app.settings.save()
        self.job = tools.Job()
        self.running = True
        self.started = time.monotonic()
        for it in todo:
            it.row.set_state("ready")
        self.footer.set_running(True)
        self.set_status(f"Converting {len(todo)} file{'s' if len(todo) != 1 else ''}…", T.CYAN)
        threading.Thread(target=self._worker, args=(todo, opts, self.footer.out_dir(), self.job),
                         daemon=True).start()

    def cancel(self) -> None:
        if self.job:
            self.job.cancel()
            self.set_status("Cancelling…", T.AMBER)

    def _make_callbacks(self, it: Item, i: int, n: int):
        post = self.app.post
        last = [0.0]

        def status(msg):
            post(it.row.set_state, "working", msg)

        def progress(f):
            now = time.monotonic()
            if now - last[0] > 0.08 or f >= 1:
                last[0] = now
                post(self.footer.progress.set, min(1.0, (i + max(0.0, min(1.0, f))) / n))

        return status, progress

    def _worker(self, todo: list[Item], opts, dest: Path | None, job: tools.Job) -> None:
        post = self.app.post
        ok = fail = skipped = 0
        n = len(todo)
        for i, it in enumerate(todo):
            if job.cancelled:
                break
            post(it.row.set_state, "working", "Starting…")
            post(self.set_status, f"[{i + 1}/{n}]  {it.path.name}", T.CYAN)
            status, progress = self._make_callbacks(it, i, n)
            try:
                outs = self.convert_one(it.path, dest or it.path.parent, opts, job, status, progress)
                outs = outs if isinstance(outs, list) else [outs]
                it.outputs = outs
                if outs:
                    self.footer.last_output = outs[-1]
                ok += 1
                post(it.row.set_state, "done")
            except Cancelled:
                post(it.row.set_state, "cancelled", "Cancelled")
                break
            except tools.AlreadyTarget as exc:
                skipped += 1
                post(it.row.set_state, "skipped", str(exc))
            except Exception as exc:  # keep going with the rest of the batch
                fail += 1
                if not isinstance(exc, (ConversionError, OSError)):
                    traceback.print_exc()
                post(it.row.set_state, "error", friendly_error(exc))
            post(self.footer.progress.set, (i + 1) / n)
        post(self._finished, ok, fail, job.cancelled, skipped)

    def _finished(self, ok: int, fail: int, cancelled: bool, skipped: int = 0) -> None:
        self.running = False
        self.job = None
        self.footer.set_running(False)
        parts = [f"{ok} converted"]
        if skipped:
            parts.append(f"{skipped} skipped (already that format)")
        if fail:
            parts.append(f"{fail} failed — click ERROR for details")
        if cancelled:
            parts.append("cancelled")
        color = T.GREEN if ok and not fail and not cancelled else (T.RED if fail else T.AMBER)
        self.set_status("COMPLETE  //  " + "  ·  ".join(parts), color)
        self.app.notify_done(time.monotonic() - self.started)

    # ----------------------------------------------------------------- animation
    def animate(self, phase: float, heavy: bool = True) -> None:
        self.tag_label.configure(text_color=T.neon(phase + self.offset))
        self.drop.set_color(T.neon(phase + self.offset + 0.12))
        if heavy:  # the big button and progress bar are the costly redraws
            self.footer.animate(phase + self.offset, self.running)
        if self.running:
            wc = T.neon(phase * 3 + self.offset)
            for it in self.items:
                if it.state == "working" and it.row:
                    it.row.pulse(wc)


def _options_grid(f) -> None:
    for c in range(6):
        f.grid_columnconfigure(c, weight=0)
    f.grid_columnconfigure(5, weight=1)


def _place(widget, row, col, first=False, **kw):
    widget.grid(row=row, column=col, sticky="w", padx=(16 if first else 8, 8), **kw)


# --------------------------------------------------------------------------- TEXT

class TextPanel(ModePanel):
    key = "text"
    number = "01"
    tag = "TEXT"
    title = "Documents & E-books"
    subtitle = ("Batch-convert books and documents. Keeps chapters, headings, tables, images and covers;\n"
                "PDFs get clickable chapter bookmarks.")
    formats_hint = "EPUB · PDF · MOBI · AZW3 · DOCX · DOC · ODT · RTF · TXT · HTML · MD · FB2"
    exts = docs.INPUT_EXTS
    offset = 0.0

    def build_options(self, f) -> None:
        app = self.app
        _options_grid(f)
        self.target_var = tk.StringVar()
        _place(caption(f, app, "CONVERT TO"), 0, 0, True, pady=(12, 2))
        self.target_menu = option_menu(f, app, [], self.target_var, self._on_target, width=230)
        _place(self.target_menu, 1, 0, True, pady=(0, 10))
        _place(caption(f, app, "PAGE SIZE"), 0, 1, pady=(12, 2))
        if self.st.get("page_v") != 2:  # "Original" became the default in v1.1
            self.st.update(page_size="Original", page_v=2)
        self.page_var = tk.StringVar(value=self.st.get("page_size", "Original"))
        _place(option_menu(f, app, docs.PAGE_SIZES, self.page_var, lambda v: self._on_target(self.target_var.get()),
                           width=120), 1, 1, pady=(0, 10))
        sw = ctk.CTkFrame(f, fg_color="transparent")
        sw.grid(row=2, column=0, columnspan=6, sticky="w", padx=16, pady=(0, 4))
        self.toc_var = tk.BooleanVar(value=self.st.get("toc", True))
        neon_switch(sw, app, "Contents page", self.toc_var).pack(side="left")
        self.merge_var = tk.BooleanVar(value=self.st.get("merge", False))
        self.merge_sw = neon_switch(sw, app, "Merge into one PDF", self.merge_var,
                                    lambda: self._on_target(self.target_var.get()))
        self.merge_sw.pack(side="left", padx=(24, 0))
        self.private_var = tk.BooleanVar(value=self.st.get("private", False))
        neon_switch(sw, app, "Remove all identifying info", self.private_var,
                    lambda: self._on_target(self.target_var.get())).pack(side="left", padx=(24, 0))
        self.note = ctk.CTkLabel(f, text="", font=app.f_mono_s, text_color=T.MUTED, anchor="w", justify="left",
                                 wraplength=860)
        self.note.grid(row=3, column=0, columnspan=6, sticky="ew", padx=16, pady=(0, 10))
        self.calibre_btn = neon_button(f, app, "INSTALL CALIBRE", app.install_calibre, width=150)
        self.refresh_target_menu()

    def refresh_target_menu(self) -> None:
        values = []
        self.value_to_key = {}
        for key, label in docs.OUTPUTS:
            why = docs.missing_engine(key)
            text = f"{label}   ·  {why}" if why else label
            values.append(text)
            self.value_to_key[text] = key
        self.target_menu.configure(values=values)
        want = self.st.get("target", "pdf")
        self.target_var.set(next((v for v, k in self.value_to_key.items() if k == want), values[0]))
        self._on_target(self.target_var.get())

    TIPS = {
        "pdf": "PDF: chapters become clickable bookmarks; covers, pictures and styling are kept.",
        "epub": "EPUB: chapters split at headings with a clickable contents page; a PDF's first page becomes the cover.",
        "docx": "DOCX from PDF keeps the original layout, fonts, margins and tables. From e-books: headings map to Word styles.",
        "mobi": "MOBI for Kindle — cover, chapters and pictures are carried over.",
        "azw3": "AZW3 (KF8) for Kindle — cover, chapters and pictures are carried over.",
    }

    def _on_target(self, value: str) -> None:
        key = self.value_to_key.get(value, "pdf")
        self.st["target"] = key
        self.merge_sw.configure(state="normal" if key == "pdf" else "disabled")
        if docs.missing_engine(key) == "needs Calibre":
            self.note.configure(text="MOBI and AZW3 are written by Calibre, which isn't in this build. "
                                     "Install it once and these formats unlock.", text_color=T.AMBER)
            self.calibre_btn.grid(row=1, column=5, sticky="e", padx=16, pady=(0, 10))
        else:
            self.calibre_btn.grid_forget()
            tip = self.TIPS.get(key, "Headings, paragraphs, lists, tables and pictures are carried across "
                                     "where the format allows.")
            if key in ("pdf", "docx"):
                size = self.page_var.get() if hasattr(self, "page_var") else "Original"
                tip += ("  Page size: each file keeps its own (e-books get pages shaped like their cover)."
                        if size == "Original" else f"  Page size: {size} (Word/ODT files made with LibreOffice "
                                                   "keep their own).")
            color = T.MUTED
            if getattr(self, "private_var", None) is not None and self.private_var.get():
                tip = ("PRIVATE: removes authors, editors, company, dates, revision IDs, comment authors, template "
                       "paths, printer names and the GPS/camera data inside embedded photos. Kindle files keep "
                       "only the file name as title. The text itself is not changed.")
                color = T.CYAN
            self.note.configure(text=tip, text_color=color)
        self.refresh_routes()

    def get_options(self) -> docs.Options:
        self.st.update(page_size=self.page_var.get(), toc=bool(self.toc_var.get()),
                       merge=bool(self.merge_var.get()), private=bool(self.private_var.get()))
        return docs.Options(target=self.st["target"], page_size=self.page_var.get(),
                            toc=bool(self.toc_var.get()), private=bool(self.private_var.get()))

    def route_for(self, item: Item) -> str:
        sf, target = docs.fmt_of(item.path), self.st.get("target", "pdf")
        if sf == target:
            return "already PDF — will be merged" if target == "pdf" and self.merge_var.get() else f"already {target.upper()}"
        return docs.describe_route(sf, target) or f"{sf.upper()} → {target.upper()} unavailable"

    def convert_one(self, src, dest, opts, job, status, progress):
        return docs.convert(src, dest, opts, job, status, progress)

    def _worker(self, todo, opts, dest, job):
        if not (self.st.get("merge") and opts.target == "pdf"):
            return super()._worker(todo, opts, dest, job)
        # Merge mode: convert everything to PDF first, then join them with a bookmark per file
        post = self.app.post
        tmp = Path(tempfile.mkdtemp(prefix="neon_merge_"))
        parts: list[tuple[Item, Path]] = []
        fail = 0
        n = len(todo)
        try:
            for i, it in enumerate(todo):
                if job.cancelled:
                    break
                post(it.row.set_state, "working", "Preparing…")
                status, progress = self._make_callbacks(it, i, n + 1)
                try:
                    part = it.path if docs.fmt_of(it.path) == "pdf" else \
                        docs.convert(it.path, tmp, opts, job, status, progress)
                    parts.append((it, part))
                    post(it.row.set_state, "working", "Waiting to merge…")
                except Cancelled:
                    post(it.row.set_state, "cancelled", "Cancelled")
                    break
                except Exception as exc:
                    fail += 1
                    post(it.row.set_state, "error", friendly_error(exc))
            ok = 0
            if parts and not job.cancelled:
                folder = dest or todo[0].path.parent
                out = tools.unique_path(folder, "Merged", "pdf")
                post(self.set_status, f"Merging {len(parts)} files…", T.CYAN)
                try:
                    docs.merge_pdfs([(it.path.stem, p) for it, p in parts], out, job, private=opts.private)
                    self.footer.last_output = out
                    for it, _ in parts:
                        it.outputs = [out]
                        post(it.row.set_state, "done")
                    ok = len(parts)
                except Cancelled:
                    for it, _ in parts:
                        post(it.row.set_state, "cancelled", "Cancelled")
                except Exception as exc:
                    for it, _ in parts:
                        post(it.row.set_state, "error", friendly_error(exc))
                    fail += len(parts)
            elif job.cancelled:
                for it, _ in parts:
                    post(it.row.set_state, "cancelled", "Cancelled")
            post(self.footer.progress.set, 1.0)
            post(self._finished, ok, fail, job.cancelled)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- IMAGE

class ImagePanel(ModePanel):
    key = "image"
    number = "02"
    tag = "IMAGE"
    title = "Pictures"
    subtitle = ("Batch-convert photos, graphics and icons. Animated GIF/WEBP stay animated; phone photos stay\n"
                "upright; PDFs and SVGs can be turned into images.")
    formats_hint = "PNG · JPG · WEBP · GIF · HEIC · AVIF · TIFF · BMP · ICO · SVG · PDF"
    exts = images.INPUT_EXTS
    offset = 0.33

    def build_options(self, f) -> None:
        app = self.app
        _options_grid(f)
        self.value_to_key = {label: key for key, label in images.OUTPUTS}
        self.target_var = tk.StringVar(value=dict(images.OUTPUTS)[self.st.get("target", "png")])
        _place(caption(f, app, "CONVERT TO"), 0, 0, True, pady=(12, 2))
        _place(option_menu(f, app, [l for _, l in images.OUTPUTS], self.target_var, self._on_target,
                           width=200), 1, 0, True, pady=(0, 8))
        self.q_caption = caption(f, app, "")
        _place(self.q_caption, 0, 1, pady=(12, 2))
        # the quality you chose for lossy formats; lossless formats just show the slider pinned at 100
        self.lossy_q = int(self.st.get("quality", 90))
        self.q_var = tk.IntVar(value=self.lossy_q)
        self.q_slider = ctk.CTkSlider(f, from_=10, to=100, number_of_steps=90, variable=self.q_var,
                                      command=self._on_quality, width=170, progress_color=T.PURPLE,
                                      button_color=T.PINK, button_hover_color=T.TEXT, fg_color=T.BORDER)
        _place(self.q_slider, 1, 1, pady=(0, 8))
        _place(caption(f, app, "RESIZE"), 0, 2, pady=(12, 2))
        self.resize_var = tk.StringVar(value=self.st.get("resize", "Original size"))
        _place(option_menu(f, app, images.RESIZE_CHOICES, self.resize_var, width=150), 1, 2, pady=(0, 8))
        sw = ctk.CTkFrame(f, fg_color="transparent")
        sw.grid(row=2, column=0, columnspan=6, sticky="w", padx=16, pady=(2, 4))
        self.combine_var = tk.BooleanVar(value=self.st.get("combine", False))
        self.combine_sw = neon_switch(sw, app, "Combine all into one PDF", self.combine_var)
        self.combine_sw.pack(side="left")
        self.strip_var = tk.BooleanVar(value=self.st.get("strip", False))
        neon_switch(sw, app, "Remove all identifying info", self.strip_var, self._on_strip).pack(
            side="left", padx=(24, 0))
        self.note = ctk.CTkLabel(f, text="", font=app.f_mono_s, text_color=T.MUTED, anchor="w", justify="left")
        self.note.grid(row=3, column=0, columnspan=6, sticky="ew", padx=16, pady=(0, 10))
        self._on_target(self.target_var.get())

    def _lossy(self) -> bool:
        key = self.value_to_key.get(self.target_var.get(), "png")
        return key in images.QUALITY_FORMATS or key == "pdf"

    def _on_quality(self, value) -> None:
        if self._lossy():
            self.lossy_q = int(round(float(value)))
        self._q_text()

    def _q_text(self) -> None:
        key = self.value_to_key.get(self.target_var.get(), "png")
        if not self._lossy():
            text = "QUALITY  100 · LOSSLESS"
        elif key == "webp" and self.lossy_q >= 100:
            text = "QUALITY  100 · LOSSLESS WEBP"
        else:
            text = f"QUALITY  {self.lossy_q}"
        self.q_caption.configure(text=text)

    def _on_target(self, value: str) -> None:
        key = self.value_to_key.get(value, "png")
        self.st["target"] = key
        if self._lossy():  # adjustable, at the value you picked last time
            self.q_slider.configure(state="normal", button_color=T.PINK, progress_color=T.PURPLE)
            self.q_var.set(self.lossy_q)
        else:  # PNG, GIF, BMP, TIFF, ICO keep every pixel: nothing to choose
            self.q_var.set(100)
            self.q_slider.configure(state="disabled", button_color=T.MUTED,
                                    progress_color=T.shade(T.PURPLE, 0.55, T.PANEL))
        self.combine_sw.configure(state="normal" if key == "pdf" else "disabled")
        self._q_text()
        self._on_strip()
        self.refresh_routes()

    def _on_strip(self) -> None:
        if self.strip_var.get():
            self.note.configure(text="Removes GPS location, camera make/model/serials, dates, software, author, "
                                     "XMP/IPTC, comments, hidden thumbnails and color-profile device info. "
                                     "The picture itself (faces, places, reflections) can still give things away.",
                                text_color=T.CYAN, wraplength=840)
        else:
            self.note.configure(text="Camera info, location and color profiles are kept, like the original. "
                                     "Turn on 'Remove all identifying info' before sharing photos.",
                                text_color=T.MUTED)

    def get_options(self) -> images.Options:
        self.st.update(quality=self.lossy_q, resize=self.resize_var.get(),
                       combine=bool(self.combine_var.get()), strip=bool(self.strip_var.get()))
        return images.Options(target=self.st["target"], quality=self.lossy_q,
                              resize=self.resize_var.get(),
                              combine_pdf=bool(self.combine_var.get()) and self.st["target"] == "pdf",
                              strip_meta=bool(self.strip_var.get()))

    def route_for(self, item: Item) -> str:
        sf = images.fmt_of(item.path).upper()
        t = self.st.get("target", "png").upper()
        if sf == "PDF" and t != "PDF":
            return f"PDF pages → {t} (one image per page)"
        return f"{sf} → {t}"

    def convert_one(self, src, dest, opts, job, status, progress):
        return images.convert(src, dest, opts, job, status, progress)

    def _worker(self, todo, opts, dest, job):
        if not opts.combine_pdf:
            return super()._worker(todo, opts, dest, job)
        post = self.app.post
        folder = dest or todo[0].path.parent
        out = tools.unique_path(folder, "Combined", "pdf")
        n = len(todo)

        def on_file(i, p):
            post(todo[i].row.set_state, "working", "Adding to combined PDF…")
            post(self.footer.progress.set, i / n)
            if i:
                post(todo[i - 1].row.set_state, "done")

        try:
            images.combine_to_pdf([it.path for it in todo], out, opts, job, on_file)
            for it in todo:
                it.outputs = [out]
                post(it.row.set_state, "done")
            self.footer.last_output = out
            post(self.footer.progress.set, 1.0)
            post(self._finished, n, 0, False)
        except Cancelled:
            out.unlink(missing_ok=True)
            for it in todo:
                post(it.row.set_state, "cancelled", "Cancelled")
            post(self._finished, 0, 0, True)
        except Exception as exc:
            out.unlink(missing_ok=True)
            for it in todo:
                post(it.row.set_state, "error", friendly_error(exc))
            post(self._finished, 0, n, False)


# --------------------------------------------------------------------------- MEDIA

class MediaPanel(ModePanel):
    key = "media"
    number = "03"
    tag = "MEDIA"
    title = "Video & Audio"
    subtitle = ("Convert videos between formats, make GIFs, shrink videos to a size limit, or rip just the\n"
                "audio track out of a video. Audio files can be converted here too.")
    formats_hint = "MP4 · MKV · MOV · WEBM · AVI · WMV · GIF · MP3 · WAV · FLAC · M4A"
    exts = media.INPUT_EXTS
    offset = 0.66

    def build_options(self, f) -> None:
        app = self.app
        _options_grid(f)
        _place(caption(f, app, "MODE"), 0, 0, True, pady=(12, 2))
        self.mode_var = tk.StringVar(value="EXTRACT AUDIO" if self.st.get("mode") == "audio" else "CONVERT VIDEO")
        self.mode_seg = ctk.CTkSegmentedButton(
            f, values=["CONVERT VIDEO", "EXTRACT AUDIO"], variable=self.mode_var, command=self._on_mode,
            height=32, corner_radius=6, fg_color=T.CARD, selected_color=T.PURPLE,
            selected_hover_color=T.PINK, unselected_color=T.CARD, unselected_hover_color=T.CARD_HI,
            text_color=T.TEXT, font=app.f_mono_b)
        _place(self.mode_seg, 1, 0, True, pady=(0, 8))
        _place(caption(f, app, "CONVERT TO"), 0, 1, pady=(12, 2))
        self.target_var = tk.StringVar()
        self.target_menu = option_menu(f, app, [], self.target_var, self._on_target, width=160)
        _place(self.target_menu, 1, 1, pady=(0, 8))

        # video-only controls (second line)
        self.vbox = ctk.CTkFrame(f, fg_color="transparent")
        for col, (cap, values, key, default, width) in enumerate((
                ("QUALITY", media.QUALITIES, "quality", "Balanced", 120),
                ("RESOLUTION", media.RESOLUTIONS, "resolution", "Original", 110),
                ("SIZE LIMIT", media.SIZE_LIMITS, "size_limit", "No limit", 110))):
            caption(self.vbox, app, cap).grid(row=0, column=col, sticky="w", padx=(0 if col == 0 else 8, 8))
            var = tk.StringVar(value=self.st.get(key, default))
            setattr(self, f"{key}_var", var)
            option_menu(self.vbox, app, values, var, width=width, command=lambda v: self._on_target(
                self.target_var.get())).grid(row=1, column=col, sticky="w", padx=(0 if col == 0 else 8, 8))
        self.fast_var = tk.BooleanVar(value=self.st.get("fast", True))
        neon_switch(self.vbox, app, "Fast copy (no re-encode)", self.fast_var).grid(
            row=1, column=3, sticky="w", padx=(8, 0))
        # audio-only controls
        self.abox = ctk.CTkFrame(f, fg_color="transparent")
        caption(self.abox, app, "BITRATE  (MP3 / M4A / OGG / OPUS)").grid(row=0, column=0, sticky="w")
        self.br_var = tk.StringVar(value=self.st.get("bitrate", "192 kbps"))
        option_menu(self.abox, app, media.BITRATES, self.br_var, width=130).grid(row=1, column=0, sticky="w")

        self.private_var = tk.BooleanVar(value=self.st.get("private", False))
        neon_switch(f, app, "Remove all identifying info", self.private_var,
                    lambda: self._on_target(self.target_var.get())).grid(
            row=3, column=0, columnspan=6, sticky="w", padx=16, pady=(8, 0))
        self.note = ctk.CTkLabel(f, text="", font=app.f_mono_s, text_color=T.MUTED, anchor="w", justify="left",
                                 wraplength=860)
        self.note.grid(row=4, column=0, columnspan=6, sticky="ew", padx=16, pady=(4, 10))
        self._on_mode(self.mode_var.get())

    def _mode(self) -> str:
        return "audio" if self.mode_var.get() == "EXTRACT AUDIO" else "video"

    def _on_mode(self, _value=None) -> None:
        mode = self._mode()
        self.st["mode"] = mode
        outs = media.AUDIO_OUTPUTS if mode == "audio" else media.VIDEO_OUTPUTS
        self.value_to_key = {label: key for key, label in outs}
        self.target_menu.configure(values=[l for _, l in outs])
        want = self.st.get(f"target_{mode}", outs[0][0])
        self.target_var.set(dict(outs).get(want, outs[0][1]))
        box, other = (self.abox, self.vbox) if mode == "audio" else (self.vbox, self.abox)
        other.grid_forget()
        box.grid(row=2, column=0, columnspan=6, sticky="w", padx=16, pady=(2, 2))
        self._on_target(self.target_var.get())

    NOTES = {
        "gif": "GIF: 15 fps, palette-optimized; 'Original' resolution is capped at 640 px wide to keep files sane.",
        "mp4": "MP4 (H.264 + AAC) plays everywhere. Fast copy just re-wraps the streams when they already fit.",
        "webm": "WEBM (VP9 + Opus) is great for the web but slower to encode.",
        "m4a": "M4A copies the original AAC audio losslessly when the video already uses AAC.",
        "wav": "WAV is uncompressed — big files, perfect quality.",
        "flac": "FLAC is lossless and about half the size of WAV.",
    }

    def _on_target(self, value: str) -> None:
        mode = self._mode()
        key = self.value_to_key.get(value)
        self.st[f"target_{mode}"] = key
        limit = self.size_limit_var.get() if hasattr(self, "size_limit_var") else "No limit"
        if mode == "video" and limit != "No limit":
            if key in media.SIZED_TARGETS:
                self.note.configure(text=f"Two-pass encode aimed just under {limit} (great for Discord or email); "
                                         "resolution drops automatically if needed.", text_color=T.CYAN)
            else:
                self.note.configure(text=f"Size limit works with MP4, MKV, MOV and WEBM — ignored for {key.upper()}.",
                                    text_color=T.AMBER)
        else:
            self.note.configure(text=self.NOTES.get(key, "Audio and video stay in sync; subtitles and chapters "
                                                         "are dropped."), text_color=T.MUTED)
        if getattr(self, "private_var", None) is not None and self.private_var.get():
            self.note.configure(text="PRIVATE: removes GPS location, phone/camera make and model, recording dates, "
                                     "titles, artist/comment tags, chapters, GPS telemetry tracks and encoder "
                                     "version tags. What's in the picture and sound is not changed.",
                                text_color=T.CYAN)
        self.refresh_routes()

    def get_options(self) -> media.Options:
        mode = self._mode()
        limit = self.size_limit_var.get()
        self.st.update(quality=self.quality_var.get(), resolution=self.resolution_var.get(),
                       fast=bool(self.fast_var.get()), bitrate=self.br_var.get(), size_limit=limit,
                       private=bool(self.private_var.get()))
        return media.Options(mode=mode, target=self.st[f"target_{mode}"], quality=self.quality_var.get(),
                             resolution=self.resolution_var.get(), fast=bool(self.fast_var.get()),
                             bitrate=self.br_var.get(), private=bool(self.private_var.get()),
                             size_mb=None if limit == "No limit" else int(limit.split()[0]))

    def route_for(self, item: Item) -> str:
        mode = self._mode() if hasattr(self, "mode_var") else "video"
        t = (self.st.get(f"target_{mode}") or "").upper()
        src = item.path.suffix.lstrip(".").upper()
        return f"{src} → {t}" + ("  (audio only)" if mode == "audio" else "")

    def convert_one(self, src, dest, opts, job, status, progress):
        return media.convert(src, dest, opts, job, status, progress)


# =========================================================================== app

if DND_OK:
    class _Base(ctk.CTk, TkinterDnD.DnDWrapper):
        def __init__(self) -> None:
            super().__init__()
            try:
                self.TkdndVersion = TkinterDnD._require(self)
            except Exception:
                self.TkdndVersion = None
else:  # pragma: no cover
    class _Base(ctk.CTk):
        TkdndVersion = None


class NavButton(ctk.CTkFrame):
    ICONS = {"text": "text", "image": "image", "media": "media", "edit": "edit"}

    def __init__(self, master, app: "App", panel) -> None:
        super().__init__(master, fg_color="transparent", corner_radius=8, height=60, cursor="hand2")
        self.app, self.panel = app, panel
        self.grid_columnconfigure(2, weight=1)
        self.bar = ctk.CTkFrame(self, width=4, height=36, corner_radius=2, fg_color="transparent")
        self.bar.grid(row=0, column=0, rowspan=2, padx=(4, 10), pady=8, sticky="ns")
        self.icon = ctk.CTkLabel(self, text=T.GLYPH[self.ICONS[panel.key]], font=(T.FONT_ICON, 20),
                                 text_color=T.MUTED, width=28)
        self.icon.grid(row=0, column=1, rowspan=2, padx=(0, 10))
        self.title = ctk.CTkLabel(self, text=f"{panel.number}  {panel.tag}", font=app.f_nav, text_color=T.TEXT,
                                  anchor="w")
        self.title.grid(row=0, column=2, sticky="sw", pady=(8, 0))
        self.sub = ctk.CTkLabel(self, text=panel.title, font=app.f_small, text_color=T.MUTED, anchor="w")
        self.sub.grid(row=1, column=2, sticky="nw", pady=(0, 8))
        for w in (self, self.bar, self.icon, self.title, self.sub):
            w.bind("<Button-1>", lambda e: app.select(panel.key))
            w.bind("<Enter>", lambda e: self._hover(True))
            w.bind("<Leave>", lambda e: self._hover(False))
        self.active = False

    def _hover(self, on: bool) -> None:
        if not self.active:
            self.configure(fg_color=T.CARD if on else "transparent")

    def set_active(self, on: bool) -> None:
        self.active = on
        self.configure(fg_color=T.CARD if on else "transparent")
        self.bar.configure(fg_color=T.CYAN if on else "transparent")
        self.title.configure(text_color=T.TEXT if on else T.MUTED)
        self.icon.configure(text_color=T.CYAN if on else T.DIM)

    def animate(self, color: str) -> None:
        if self.active:
            self.bar.configure(fg_color=color)
            self.icon.configure(text_color=color)


class App(_Base):
    def __init__(self, files: list[str] | None = None) -> None:
        super().__init__()
        self.settings = Settings()
        self.ui_queue: queue.Queue = queue.Queue()
        self.dragging = False
        T.pick_fonts(set(tkfont.families(self)))
        self.scale = ctk.ScalingTracker.get_window_scaling(self)

        self.title(APP_NAME)
        sw, sh = self._work_area()
        w, h = int(min(1220, sw - 40)), int(min(840, sh - 50))
        geo = f"{w}x{h}+{max(0, int((sw - w) / 2))}+{max(0, int((sh - h) / 2) - 24)}"
        saved = re.fullmatch(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)", str(self.settings.get("geometry", "")))
        if saved:  # reopen where you left it, if that still fits this screen
            gw, gh, gx, gy = map(int, saved.groups())
            if 600 <= gw <= sw and 500 <= gh <= sh and -8 <= gx <= sw - 200 and -8 <= gy <= sh - 100:
                geo = f"{gw}x{gh}+{gx}+{gy}"
        self.geometry(geo)
        self.minsize(min(1000, w), min(680, h))
        if self.settings.get("maximized"):
            self.after(50, lambda: self.state("zoomed"))
        self.configure(fg_color=T.BG)
        icon = tools.resource_dir() / "assets" / "icon.ico"
        if icon.exists():
            try:
                self.iconbitmap(default=str(icon))  # dialogs and file pickers too
                self.iconbitmap(str(icon))
            except Exception:
                pass
            # Tk stretches one small frame for the big icon; give Windows the right sizes itself
            self.after(250, lambda: self._set_window_icons(icon))

        self.f_title = ctk.CTkFont(family=T.FONT_UI_BOLD, size=28)
        self.f_nav = ctk.CTkFont(family=T.FONT_UI_BOLD, size=16)
        self.f_btn = ctk.CTkFont(family=T.FONT_UI_BOLD, size=15)
        self.f_body = ctk.CTkFont(family=T.FONT_UI, size=14)
        self.f_small = ctk.CTkFont(family=T.FONT_UI, size=12)
        self.f_mono = ctk.CTkFont(family=T.FONT_MONO, size=12)
        self.f_mono_s = ctk.CTkFont(family=T.FONT_MONO, size=11)
        self.f_mono_b = ctk.CTkFont(family=T.FONT_MONO, size=12, weight="bold")
        # plain-Tk fonts (queue rows) need the display scaling applied by hand
        px = lambda n: -max(8, round(n * self.scale))  # noqa: E731
        self.tf_body = tkfont.Font(self, family=T.FONT_UI, size=px(14))
        self.tf_mono_s = tkfont.Font(self, family=T.FONT_MONO, size=px(11))
        self.tf_mono_b = tkfont.Font(self, family=T.FONT_MONO, size=px(12), weight="bold")

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self._build_sidebar()

        content = ctk.CTkFrame(self, fg_color=T.BG, corner_radius=0)
        content.grid(row=0, column=1, sticky="nsew")
        content.grid_columnconfigure(0, weight=1)
        content.grid_rowconfigure(1, weight=1)
        self.scan = tk.Canvas(content, height=max(3, int(3 * self.scale)), bg=T.BG, highlightthickness=0, bd=0)
        self.scan.grid(row=0, column=0, sticky="ew")
        self.scan.bind("<Configure>", self._scan_resize)
        self._scan_img = None
        self._scan_w = 0

        self.panels: dict = {}
        for cls in (TextPanel, ImagePanel, MediaPanel, EditPanel):
            p = cls(content, self)
            p.grid(row=1, column=0, sticky="nsew")
            self.panels[p.key] = p
        for p in self.panels.values():
            nb = NavButton(self.nav_frame, self, p)
            nb.pack(fill="x", padx=10, pady=2)
            self.nav[p.key] = nb
        self.current = ""
        self.select(self.settings.get("mode", "text"))

        if DND_OK and self.TkdndVersion:
            self.drop_target_register(DND_FILES)
            self.dnd_bind("<<DropEnter>>", self._drag_enter)
            self.dnd_bind("<<DropLeave>>", self._drag_leave)
            self.dnd_bind("<<Drop>>", self._on_drop)

        # keyboard shortcuts
        self.bind("<Control-o>", lambda e: self.panels[self.current].browse())
        self.bind("<Control-O>", lambda e: self.panels[self.current].browse())
        self.bind("<Control-Return>", lambda e: self.panels[self.current].start())
        for i, key in enumerate(self.panels, start=1):
            self.bind(f"<Control-Key-{i}>", lambda e, k=key: self.select(k))

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(40, self._pump)
        self.after(60, self._tick)
        if files:
            self.after(300, lambda: self.route_files([Path(f) for f in files]))

    def _set_window_icons(self, ico: Path) -> None:
        """Load the .ico frames that match this display's icon sizes (title bar, taskbar, Alt+Tab)."""
        try:
            import ctypes
            import ctypes.wintypes as wt

            user32 = ctypes.windll.user32
            user32.LoadImageW.restype = wt.HANDLE
            user32.LoadImageW.argtypes = [wt.HINSTANCE, wt.LPCWSTR, wt.UINT, ctypes.c_int, ctypes.c_int, wt.UINT]
            user32.SendMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
            user32.GetParent.restype = wt.HWND
            hwnd = user32.GetParent(self.winfo_id())
            self._hicons = []
            for which, metric in ((1, 11), (0, 49)):  # ICON_BIG / SM_CXICON, ICON_SMALL / SM_CXSMICON
                size = user32.GetSystemMetrics(metric)
                h = user32.LoadImageW(None, str(ico), 1, size, size, 0x10)  # IMAGE_ICON, LR_LOADFROMFILE
                if h:
                    user32.SendMessageW(hwnd, 0x80, which, h)  # WM_SETICON
                    self._hicons.append(h)
        except Exception:
            pass

    def _work_area(self) -> tuple[float, float]:
        """Usable desktop size (minus taskbar) in CustomTkinter's scaled units."""
        try:
            import ctypes
            import ctypes.wintypes

            r = ctypes.wintypes.RECT()
            if ctypes.windll.user32.SystemParametersInfoW(48, 0, ctypes.byref(r), 0):  # SPI_GETWORKAREA
                return (r.right - r.left) / self.scale, (r.bottom - r.top) / self.scale
        except Exception:
            pass
        return self.winfo_screenwidth(), self.winfo_screenheight()

    # ----------------------------------------------------------------- sidebar
    def _build_sidebar(self) -> None:
        side = ctk.CTkFrame(self, fg_color=T.SIDEBAR, corner_radius=0, width=250)
        side.grid(row=0, column=0, sticky="nsw")
        side.grid_propagate(False)
        side.grid_columnconfigure(0, weight=1)
        side.grid_rowconfigure(2, weight=1)
        self.logo_frames = [ImageTk.PhotoImage(im) for im in T.logo_frames(self.scale)]
        self.logo = tk.Label(side, image=self.logo_frames[0], bg=T.SIDEBAR, bd=0)
        self.logo.grid(row=0, column=0, sticky="w", padx=12, pady=(18, 6))
        self._logo_idx = 0

        self.nav_frame = ctk.CTkFrame(side, fg_color="transparent")
        self.nav_frame.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        self.nav: dict[str, NavButton] = {}

        hud = ctk.CTkFrame(side, fg_color=T.PANEL, corner_radius=10, border_width=1, border_color=T.BORDER)
        hud.grid(row=3, column=0, sticky="ew", padx=14, pady=(0, 8))
        ctk.CTkLabel(hud, text="// ENGINES", font=self.f_mono_b, text_color=T.MUTED).pack(anchor="w", padx=12,
                                                                                         pady=(8, 2))
        self.engine_rows: dict[str, ctk.CTkLabel] = {}
        for name in ("FFMPEG", "PANDOC", "MUPDF", "LIBREOFFICE", "CALIBRE"):
            lbl = ctk.CTkLabel(hud, text=name, font=self.f_mono_s, anchor="w", text_color=T.MUTED, height=20)
            lbl.pack(fill="x", padx=12)
            self.engine_rows[name] = lbl
        self.calibre_side_btn = ctk.CTkButton(hud, text="INSTALL CALIBRE", height=26, corner_radius=6,
                                              fg_color="transparent", border_width=1, border_color=T.PINK,
                                              text_color=T.PINK, hover_color=T.CARD_HI, font=self.f_mono_s,
                                              command=self.install_calibre)
        ctk.CTkLabel(hud, text="", height=4).pack()
        self.refresh_engines()

        bottom = ctk.CTkFrame(side, fg_color="transparent")
        bottom.grid(row=4, column=0, sticky="ew", padx=16, pady=(0, 12))
        self.motion_var = tk.BooleanVar(value=self.settings.get("animate", True))
        ctk.CTkSwitch(bottom, text="Neon animation", variable=self.motion_var, onvalue=True, offvalue=False,
                      command=self._on_motion, progress_color=T.PURPLE, button_color=T.TEXT,
                      button_hover_color=T.CYAN, fg_color=T.BORDER, text_color=T.MUTED,
                      font=self.f_mono_s).pack(anchor="w")
        ctk.CTkLabel(bottom, text=f"v{__version__}  //  100% OFFLINE", font=self.f_mono_s,
                     text_color=T.DIM).pack(anchor="w", pady=(4, 0))

    def refresh_engines(self) -> None:
        state = {"FFMPEG": bool(tools.ffmpeg_path()), "PANDOC": bool(tools.pandoc_path()), "MUPDF": True,
                 "LIBREOFFICE": bool(tools.libreoffice_path()), "CALIBRE": bool(tools.calibre_path())}
        builtin = {"FFMPEG", "PANDOC", "MUPDF"} | ({"CALIBRE"} if tools.bundled_calibre() else set())
        for name, ok in state.items():
            word = ("BUILT-IN" if name in builtin else "ONLINE") if ok else "OFFLINE"
            self.engine_rows[name].configure(text=f"{'●' if ok else '○'} {name:<12}{word}",
                                             text_color=T.GREEN if ok else T.PINK)
        if state["CALIBRE"]:
            self.calibre_side_btn.pack_forget()
        else:
            self.calibre_side_btn.pack(fill="x", padx=12, pady=(6, 0))

    # ----------------------------------------------------------------- navigation
    def select(self, key: str) -> None:
        if key not in self.panels:
            key = "text"
        self.current = key
        self.panels[key].tkraise()
        for k, nb in self.nav.items():
            nb.set_active(k == key)
        self.settings["mode"] = key

    def route_files(self, paths: list[Path], prefer=None) -> None:
        """Send each file to the mode that handles it (current mode wins ties, e.g. PDF or GIF)."""
        cur = prefer or self.panels[self.current]
        expanded: list[Path] = []
        for p in paths:
            if p.is_dir():
                expanded += sorted(x for x in p.rglob("*") if x.is_file())
            else:
                expanded.append(p)
        buckets: dict[str, list[Path]] = {k: [] for k in self.panels}
        skipped = 0
        for p in expanded:
            if cur.accepts(p):
                buckets[cur.key].append(p)
                continue
            for k, panel in self.panels.items():
                if k != "edit" and panel.accepts(p):
                    buckets[k].append(p)
                    break
            else:
                skipped += 1
        added = {k: self.panels[k].add_files(v) for k, v in buckets.items() if v}
        if not added.get(cur.key) and added:
            best = max(added, key=added.get)
            if added[best]:
                self.select(best)
        target = self.panels[self.current]
        total = sum(added.values())
        if self.current == "edit":
            return  # the editor reports its own status
        msg = f"Added {total} file{'s' if total != 1 else ''}"
        others = [f"{n} → {self.panels[k].tag}" for k, n in added.items() if n and k != self.current]
        if others:
            msg += f"  ({', '.join(others)})"
        if skipped:
            msg += f"  ·  skipped {skipped} unsupported"
        if not target.running:
            target.set_status(msg, T.CYAN if total else T.AMBER)

    # ----------------------------------------------------------------- drag & drop
    def _drag_enter(self, event):
        self.dragging = True
        self.panels[self.current].drop.set_hot(True)
        return event.action

    def _drag_leave(self, event):
        self.dragging = False
        self.panels[self.current].drop.set_hot(False)
        return event.action

    def _on_drop(self, event):
        self.dragging = False
        self.panels[self.current].drop.set_hot(False)
        try:
            paths = [Path(p) for p in self.tk.splitlist(event.data)]
        except Exception:
            paths = []
        self.route_files(paths)
        return event.action

    # ----------------------------------------------------------------- calibre (only for builds without it)
    def install_calibre(self) -> None:
        if tools.calibre_path():
            self._calibre_ready()
            return
        if not shutil.which("winget"):
            webbrowser.open("https://calibre-ebook.com/download_windows")
            return
        if not messagebox.askokcancel(
                APP_NAME, "Install Calibre (free, open-source e-book engine) using Windows Package Manager?\n\n"
                          "A console window will open; Windows may ask for permission. "
                          "MOBI and AZW3 unlock automatically when it finishes.", parent=self):
            return
        subprocess.Popen(
            ["cmd", "/c", "title Installing Calibre && winget install -e --id calibre.calibre --source winget"
             " && echo. && echo Calibre installed - you can close this window. && pause"],
            creationflags=subprocess.CREATE_NEW_CONSOLE)
        self.panels["text"].set_status("Installing Calibre… this updates by itself when done", T.AMBER)
        self._calibre_deadline = time.monotonic() + 20 * 60
        self.after(4000, self._poll_calibre)

    def _poll_calibre(self) -> None:
        if tools.calibre_path():
            self._calibre_ready()
        elif time.monotonic() < self._calibre_deadline:
            self.after(4000, self._poll_calibre)

    def _calibre_ready(self) -> None:
        self.refresh_engines()
        tp = self.panels["text"]
        tp.refresh_target_menu()
        tp.set_status("CALIBRE ONLINE  //  MOBI and AZW3 unlocked", T.GREEN)

    # ----------------------------------------------------------------- loop
    def post(self, fn, *args) -> None:
        self.ui_queue.put((fn, args))

    def _pump(self) -> None:
        try:
            for _ in range(500):
                fn, args = self.ui_queue.get_nowait()
                try:
                    fn(*args)
                except Exception:
                    traceback.print_exc()
        except queue.Empty:
            pass
        self.after(40, self._pump)

    def notify_done(self, seconds: float) -> None:
        """Flash the taskbar button when a long job finishes while you're in another window."""
        if seconds < 5:
            return
        try:
            import ctypes
            import ctypes.wintypes as wt

            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            if ctypes.windll.user32.GetForegroundWindow() == hwnd:
                return

            class FLASHWINFO(ctypes.Structure):
                _fields_ = [("cbSize", wt.UINT), ("hwnd", wt.HWND), ("dwFlags", wt.DWORD),
                            ("uCount", wt.UINT), ("dwTimeout", wt.DWORD)]

            info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), hwnd, 0x2 | 0xC, 0, 0)  # TRAY | TIMERNOFG
            ctypes.windll.user32.FlashWindowEx(ctypes.byref(info))
        except Exception:
            pass

    def _scan_resize(self, event) -> None:
        w = max(2, event.width)
        if w == self._scan_w:
            return
        self._scan_w = w
        h = max(3, int(3 * self.scale))
        strip = T.gradient(w, h)
        full = strip.resize((w * 2, h))
        full.paste(strip, (0, 0))
        full.paste(strip, (w, 0))
        self._scan_img = ImageTk.PhotoImage(full)
        self.scan.delete("all")
        self.scan.create_image(0, 0, image=self._scan_img, anchor="nw", tags="g")

    COLOR_STEPS = 64  # colors move in steps, so widgets only redraw when their color really changes

    def _on_motion(self) -> None:
        self.settings["animate"] = bool(self.motion_var.get())
        self.settings.save()
        self._last_step = None

    _last_step = None

    def _activity(self) -> str:
        """'active', 'background' (another app has focus) or 'hidden' (minimized)."""
        try:
            if self.state() == "iconic":
                return "hidden"
            return "active" if self.focus_displayof() is not None else "background"
        except Exception:
            return "active"

    def _tick(self) -> None:
        activity = self._activity()
        animate = self.motion_var.get() and activity != "hidden"
        raw = (time.monotonic() / T.CYCLE_SECONDS) % 1.0 if animate else 0.18
        try:
            if self._scan_w and animate:  # the scan line glides every frame (cheap canvas move)
                self.scan.coords("g", -int(raw * self._scan_w), 0)
            step = (int(raw * self.COLOR_STEPS), self.current) if animate else ("still", self.current,
                                                                                 self.panels[self.current].running)
            if step != self._last_step:
                self._last_step = step
                phase = step[0] / self.COLOR_STEPS if animate else raw
                idx = int(phase * len(self.logo_frames)) % len(self.logo_frames)
                if idx != self._logo_idx:
                    self._logo_idx = idx
                    self.logo.configure(image=self.logo_frames[idx])
                panel = self.panels[self.current]
                heavy = not animate or step[0] % 2 == 0
                panel.animate(phase, heavy)
                if heavy:
                    self.nav[self.current].animate(T.neon(phase + panel.offset))
        except Exception:
            traceback.print_exc()
        # full speed while you're using the window, gentle in the background, idle when minimized
        self.after(50 if animate and activity == "active" else 200 if animate else 400, self._tick)

    def _on_close(self) -> None:
        running = [p for p in self.panels.values() if p.running]
        if running and not messagebox.askyesno(APP_NAME, "A conversion is still running. Stop it and quit?",
                                               parent=self):
            return
        for p in running:
            if p.job:
                p.job.cancel()
        try:  # remember size and place for next time
            self.settings["maximized"] = self.state() == "zoomed"
            if not self.settings["maximized"]:
                self.settings["geometry"] = self.geometry()
        except Exception:
            pass
        self.settings.save()
        self.destroy()


def run(files: list[str] | None = None) -> None:
    app = App(files)
    app.mainloop()
