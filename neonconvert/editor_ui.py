"""04 // EDIT — crop, rotate, flip, trim, speed, reverse and mute with a live preview."""
from __future__ import annotations

import copy
import threading
import time
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import filedialog

import customtkinter as ctk
from PIL import Image, ImageEnhance, ImageTk

from . import editing, media, tools
from . import theme as T
from .editing import Edit
from .tools import Cancelled, ConversionError
from .widgets import Footer, draw_holo, neon_button, neon_switch, option_menu, short_name


# =========================================================================== preview + crop

class PreviewCanvas(tk.Canvas):
    """Shows the (rotated) picture; drag to draw a crop box, drag inside it to move it."""

    def __init__(self, master, panel: "EditPanel", scale: float) -> None:
        super().__init__(master, bg=T.PANEL, highlightthickness=0, bd=0)
        self.panel, self.s = panel, scale
        self.mode = "empty"  # empty | image | audio | loading
        self.base: Image.Image | None = None
        self.disp = self.dark = None
        self.box = (0, 0, 1, 1)  # ox, oy, dw, dh of the picture on the canvas
        self.color = T.CYAN
        self.hot = False
        self._tk = None
        self._drag = None
        self._pending = False
        self.audio_text = ("", "")
        self.bind("<Configure>", lambda e: self._layout())
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<B1-Motion>", self._motion)
        self.bind("<ButtonRelease-1>", self._release)

    # ----------------------------------------------------------------- content
    def show_empty(self) -> None:
        self.mode, self.base = "empty", None
        self.configure(cursor="hand2")
        self._layout()

    def show_loading(self) -> None:
        self.mode = "loading"
        self._layout()

    def show_image(self, im: Image.Image) -> None:
        self.mode, self.base = "image", im
        self.configure(cursor="crosshair")
        self._layout()

    def show_audio(self, name: str, detail: str, glyph: str = T.GLYPH["audio"]) -> None:
        """Placeholder for files without a picture (audio, or video we can't preview)."""
        self.mode, self.base, self.audio_text, self.glyph = "audio", None, (name, detail), glyph
        self.configure(cursor="")
        self._layout()

    def set_hot(self, hot: bool) -> None:
        if hot != self.hot:
            self.hot = hot
            self._layout()

    def set_color(self, color: str) -> None:
        self.color = color
        self.itemconfigure("neon", fill=color)
        self.itemconfigure("neon_line", outline=color)

    # ----------------------------------------------------------------- drawing
    def _layout(self) -> None:
        w, h = self.winfo_width(), self.winfo_height()
        if w < 20 or h < 20:
            return
        if self.mode in ("empty", "loading") or (self.mode == "image" and self.base is None):
            title = "LOADING…" if self.mode == "loading" else (
                "RELEASE TO OPEN" if self.hot and self.panel.app.dragging else "DROP A PHOTO, VIDEO OR AUDIO FILE")
            draw_holo(self, self.s, self.color, self.hot, title,
                      "or click to browse   ·   then crop, rotate, trim, speed up or reverse it")
            return
        if self.mode == "audio":
            draw_holo(self, self.s, self.color, self.hot, self.audio_text[0], self.audio_text[1],
                      glyph=getattr(self, "glyph", T.GLYPH["audio"]))
            return
        pad = int(14 * self.s)
        bw, bh = self.base.size
        f = min((w - 2 * pad) / bw, (h - 2 * pad) / bh)
        dw, dh = max(1, int(bw * f)), max(1, int(bh * f))
        self.box = ((w - dw) // 2, (h - dh) // 2, dw, dh)
        self.disp = self.base.resize((dw, dh), Image.Resampling.BILINEAR).convert("RGB")
        self.dark = ImageEnhance.Brightness(self.disp).enhance(0.32)
        self._draw()

    def request_draw(self) -> None:
        if not self._pending:
            self._pending = True
            self.after_idle(self._draw)

    def _draw(self) -> None:
        self._pending = False
        if self.mode != "image" or self.disp is None:
            return
        ox, oy, dw, dh = self.box
        crop = self.panel.edit.crop
        if crop:
            x0, y0 = int(crop[0] * dw), int(crop[1] * dh)
            x1, y1 = int((crop[0] + crop[2]) * dw), int((crop[1] + crop[3]) * dh)
            frame = self.dark.copy()
            if x1 > x0 and y1 > y0:
                frame.paste(self.disp.crop((x0, y0, x1, y1)), (x0, y0))
        else:
            frame = self.disp
        self._tk = ImageTk.PhotoImage(frame)
        self.delete("all")
        self.create_rectangle(ox - 1, oy - 1, ox + dw, oy + dh, outline=T.BORDER)
        self.create_image(ox, oy, image=self._tk, anchor="nw")
        if crop:
            X0, Y0, X1, Y1 = ox + x0, oy + y0, ox + x1, oy + y1
            for i in (1, 2):  # rule of thirds
                gx = X0 + (X1 - X0) * i / 3
                gy = Y0 + (Y1 - Y0) * i / 3
                self.create_line(gx, Y0, gx, Y1, fill="#ffffff", stipple="gray50")
                self.create_line(X0, gy, X1, gy, fill="#ffffff", stipple="gray50")
            self.create_rectangle(X0, Y0, X1, Y1, outline=self.color, width=2, tags="neon_line")
            L, lw = int(14 * self.s), max(3, int(4 * self.s))
            for (x, y, dx, dy) in ((X0, Y0, 1, 1), (X1, Y0, -1, 1), (X0, Y1, 1, -1), (X1, Y1, -1, -1)):
                self.create_line(x, y, x + dx * L, y, fill=self.color, width=lw, tags="neon")
                self.create_line(x, y, x, y + dy * L, fill=self.color, width=lw, tags="neon")
        if self.hot:
            self.create_rectangle(2, 2, self.winfo_width() - 3, self.winfo_height() - 3, outline=self.color,
                                  width=2, tags="neon_line")

    # ----------------------------------------------------------------- crop interaction
    def _frac(self, x: float, y: float) -> tuple[float, float]:
        ox, oy, dw, dh = self.box
        return min(1.0, max(0.0, (x - ox) / dw)), min(1.0, max(0.0, (y - oy) / dh))

    def _press(self, e) -> None:
        if self.mode in ("empty",):
            self.panel.browse()
            return
        if self.mode != "image":
            return
        fx, fy = self._frac(e.x, e.y)
        crop = self.panel.edit.crop
        ox, oy, dw, dh = self.box
        if crop:
            x, y, w, h = crop
            corners = {(x, y): (x + w, y + h), (x + w, y): (x, y + h),
                       (x, y + h): (x + w, y), (x + w, y + h): (x, y)}
            for (cx, cy), opposite in corners.items():
                if abs((cx - fx) * dw) <= 14 * self.s and abs((cy - fy) * dh) <= 14 * self.s:
                    self._drag = ("new", opposite)
                    return
            if x <= fx <= x + w and y <= fy <= y + h:
                self._drag = ("move", (fx - x, fy - y))
                return
        self._drag = ("new", (fx, fy))

    def _motion(self, e) -> None:
        if not self._drag or self.mode != "image":
            return
        kind, ref = self._drag
        fx, fy = self._frac(e.x, e.y)
        edit = self.panel.edit
        if kind == "move" and edit.crop:
            _, _, w, h = edit.crop
            nx = min(max(0.0, fx - ref[0]), 1 - w)
            ny = min(max(0.0, fy - ref[1]), 1 - h)
            edit.crop = (nx, ny, w, h)
        else:
            ax, ay = ref
            w, h = abs(fx - ax), abs(fy - ay)
            ratio = self.panel.aspect_ratio()
            if ratio:
                _, _, dw, dh = self.box
                k = ratio * dh / dw  # wanted width/height in fraction units
                if h == 0 or w / max(h, 1e-9) > k:
                    w = h * k
                else:
                    h = w / k
                max_w = ax if fx < ax else 1 - ax
                max_h = ay if fy < ay else 1 - ay
                shrink = min(1.0, max_w / w if w else 1.0, max_h / h if h else 1.0)
                w, h = w * shrink, h * shrink
            x = ax - w if fx < ax else ax
            y = ay - h if fy < ay else ay
            edit.crop = (x, y, w, h)
        self.panel.on_crop_changed(live=True)
        self.request_draw()

    def _release(self, e) -> None:
        if not self._drag:
            return
        self._drag = None
        crop = self.panel.edit.crop
        _, _, dw, dh = self.box
        if crop and (crop[2] * dw < 8 or crop[3] * dh < 8):
            self.panel.edit.crop = None  # a click, not a drag: clear the crop
        self.panel.on_crop_changed(live=False)
        self.request_draw()


# =========================================================================== timeline

class Timeline(tk.Canvas):
    """Trim range (two handles) and a playhead. Click/drag anywhere else to scrub."""

    def __init__(self, master, panel: "EditPanel", scale: float) -> None:
        self.s = scale
        super().__init__(master, height=int(46 * scale), bg=T.PANEL, highlightthickness=0, bd=0,
                         cursor="hand2", takefocus=1)
        self.panel = panel
        self.dur = 0.0
        self.a = self.b = self.pos = 0.0
        self.step = 1 / 25
        self.color = T.CYAN
        self._drag = None
        self.bind("<Configure>", lambda e: self.redraw())
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<B1-Motion>", self._motion)
        self.bind("<ButtonRelease-1>", lambda e: setattr(self, "_drag", None))
        for key, d in (("<Left>", -1), ("<Right>", 1)):
            self.bind(key, lambda e, d=d: self.nudge(d * self.step))
            self.bind(f"<Shift-{key[1:-1]}>", lambda e, d=d: self.nudge(d * 1.0))

    def set_duration(self, dur: float, fps: float) -> None:
        self.dur = max(0.0, dur)
        self.a, self.b, self.pos = 0.0, self.dur, 0.0
        self.step = 1 / fps if fps else 0.1
        self.redraw()

    def _pad(self) -> int:
        return int(12 * self.s)

    def x_of(self, t: float) -> float:
        p = self._pad()
        return p + (t / self.dur if self.dur else 0) * (self.winfo_width() - 2 * p)

    def t_of(self, x: float) -> float:
        p = self._pad()
        span = max(1, self.winfo_width() - 2 * p)
        return min(self.dur, max(0.0, (x - p) / span * self.dur))

    def set_color(self, c: str) -> None:
        self.color = c
        self.itemconfigure("range", fill=c)

    def redraw(self) -> None:
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 20 or not self.dur:
            return
        s, p = self.s, self._pad()
        mid = h // 2
        self.create_rectangle(p, mid - 5 * s, w - p, mid + 5 * s, fill=T.CARD, outline=T.BORDER)
        xa, xb = self.x_of(self.a), self.x_of(self.b)
        self.create_rectangle(xa, mid - 5 * s, xb, mid + 5 * s, fill=self.color, outline="", tags="range")
        for x in (xa, xb):
            self.create_rectangle(x - 5 * s, mid - 15 * s, x + 5 * s, mid + 15 * s, fill=T.TEXT, outline=T.BG)
            self.create_line(x, mid - 7 * s, x, mid + 7 * s, fill=T.BG, width=max(1, int(2 * s)))
        xp = self.x_of(self.pos)
        self.create_line(xp, 2, xp, h - 2, fill=T.PINK, width=max(2, int(2 * s)))
        self.create_polygon(xp - 6 * s, 2, xp + 6 * s, 2, xp, 10 * s, fill=T.PINK, outline="")

    def _press(self, e) -> None:
        self.focus_set()
        if not self.dur:
            return
        grab = 12 * self.s
        if abs(e.x - self.x_of(self.a)) <= grab:
            self._drag = "a"
        elif abs(e.x - self.x_of(self.b)) <= grab:
            self._drag = "b"
        else:
            self._drag = "pos"
        self._motion(e)

    def _motion(self, e) -> None:
        if not self._drag or not self.dur:
            return
        t = self.t_of(e.x)
        gap = min(0.1, self.dur / 2)
        if self._drag == "a":
            self.a = min(t, self.b - gap)
            self.pos = self.a
        elif self._drag == "b":
            self.b = max(t, self.a + gap)
            self.pos = self.b
        else:
            self.pos = t
        self.redraw()
        self.panel.on_timeline()

    def nudge(self, dt: float) -> None:
        if self.dur:
            self.pos = min(self.dur, max(0.0, self.pos + dt))
            self.redraw()
            self.panel.on_timeline()

    def set_start(self) -> None:
        self.a = min(self.pos, self.b - min(0.1, self.dur / 2))
        self.redraw()

    def set_end(self) -> None:
        self.b = max(self.pos, self.a + min(0.1, self.dur / 2))
        self.redraw()


class FrameGrabber:
    """Fetches preview frames on a worker thread, always serving only the newest request."""

    def __init__(self, app, on_frame) -> None:
        self.app, self.on_frame = app, on_frame
        self._req = None
        self._lock = threading.Lock()
        self._event = threading.Event()
        threading.Thread(target=self._loop, daemon=True).start()

    def request(self, path: Path, t: float) -> None:
        with self._lock:
            self._req = (path, t)
        self._event.set()

    def _loop(self) -> None:
        while True:
            self._event.wait()
            self._event.clear()
            with self._lock:
                req, self._req = self._req, None
            if req:
                try:
                    im = editing.grab_frame(req[0], req[1])
                except Exception:
                    im = None
                self.app.post(self.on_frame, req[0], req[1], im)


# =========================================================================== panel

class EditPanel(ctk.CTkFrame):
    key = "edit"
    number = "04"
    tag = "EDIT"
    title = "Quick Edit"
    subtitle = ("Crop, rotate and flip photos. Trim, speed up, slow down, reverse or mute videos and audio.\n"
                "Your original is never changed — edits are saved as a new file.")
    formats_hint = "JPG · PNG · HEIC · MP4 · MOV · MKV · GIF · MP3 …"
    exts = editing.IMAGE_EXTS | editing.VIDEO_EXTS | editing.AUDIO_EXTS
    offset = 0.82

    def __init__(self, master, app) -> None:
        super().__init__(master, fg_color=T.BG, corner_radius=0)
        self.app = app
        self.st = app.settings.setdefault(self.key, {})
        self.edit = Edit()
        self.path: Path | None = None
        self.kind = ""
        self.src_size = (0, 0)  # full-resolution size as displayed before edits
        self.preview_src: Image.Image | None = None  # un-edited preview picture / current frame
        self.info: media.Probe | None = None
        self.running = False
        self.job: tools.Job | None = None
        self.started = 0.0
        self.grabber = FrameGrabber(app, self._on_frame)
        self._build()
        self._show_sections()

    # ----------------------------------------------------------------- layout
    def _build(self) -> None:
        app, pad = self.app, 30
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=pad, pady=(14, 2))
        self.tag_label = ctk.CTkLabel(head, text=f"{self.number} // {self.tag}", font=app.f_mono_b,
                                      text_color=T.CYAN, anchor="w")
        self.tag_label.pack(anchor="w")
        ctk.CTkLabel(head, text=self.title, font=app.f_title, text_color=T.TEXT, anchor="w").pack(anchor="w")
        ctk.CTkLabel(head, text=self.subtitle, font=app.f_small, text_color=T.MUTED, anchor="w",
                     justify="left").pack(anchor="w")

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.grid(row=1, column=0, sticky="nsew", padx=pad, pady=(8, 0))
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(0, weight=1)

        stage = ctk.CTkFrame(body, fg_color=T.PANEL, corner_radius=10, border_width=1, border_color=T.BORDER)
        stage.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        stage.grid_columnconfigure(0, weight=1)
        stage.grid_rowconfigure(0, weight=1)
        self.canvas = PreviewCanvas(stage, self, app.scale)
        self.canvas.grid(row=0, column=0, sticky="nsew", padx=6, pady=6)
        self.drop = self.canvas  # the app highlights `drop` while files are dragged over

        self.tl_box = ctk.CTkFrame(stage, fg_color="transparent")
        self.tl_box.grid_columnconfigure(1, weight=1)
        self.timeline = Timeline(self.tl_box, self, app.scale)
        self.timeline.grid(row=0, column=0, columnspan=3, sticky="ew", padx=4)
        self.t_start = ctk.CTkLabel(self.tl_box, text="", font=app.f_mono_s, text_color=T.MUTED)
        self.t_start.grid(row=1, column=0, sticky="w", padx=10, pady=(0, 6))
        self.t_pos = ctk.CTkLabel(self.tl_box, text="", font=app.f_mono_b, text_color=T.PINK)
        self.t_pos.grid(row=1, column=1, pady=(0, 6))
        self.t_end = ctk.CTkLabel(self.tl_box, text="", font=app.f_mono_s, text_color=T.MUTED)
        self.t_end.grid(row=1, column=2, sticky="e", padx=10, pady=(0, 6))

        self.tools = ctk.CTkScrollableFrame(body, width=270, fg_color=T.PANEL, corner_radius=10,
                                            border_width=1, border_color=T.BORDER,
                                            scrollbar_button_color=T.BORDER, scrollbar_button_hover_color=T.PURPLE)
        self.tools.grid(row=0, column=1, sticky="ns")
        self._build_tools(self.tools)

        self.footer = Footer(self, app, self.st, "SAVE  ▶", self.start, self.cancel)
        self.footer.grid(row=2, column=0, sticky="ew", padx=pad, pady=(10, 12))

    def _section(self, parent, title: str) -> ctk.CTkFrame:
        f = ctk.CTkFrame(parent, fg_color="transparent")
        ctk.CTkLabel(f, text=f"// {title}", font=self.app.f_mono_b, text_color=T.MUTED,
                     anchor="w").pack(fill="x", pady=(10, 4))
        return f

    def _build_tools(self, tf) -> None:
        app = self.app
        s = {}
        f = s["file"] = ctk.CTkFrame(tf, fg_color="transparent")
        self.file_label = ctk.CTkLabel(f, text="No file loaded", font=app.f_body, text_color=T.TEXT,
                                       anchor="w", justify="left", wraplength=250)
        self.file_label.pack(fill="x", pady=(8, 0))
        self.file_info = ctk.CTkLabel(f, text="Drop a photo, video or audio file", font=app.f_mono_s,
                                      text_color=T.MUTED, anchor="w", justify="left", wraplength=250)
        self.file_info.pack(fill="x")
        neon_button(f, app, "OPEN FILE…", self.browse, width=250).pack(fill="x", pady=(8, 0))

        f = s["orient"] = self._section(tf, "ROTATE & FLIP")
        g = ctk.CTkFrame(f, fg_color="transparent")
        g.pack(fill="x")
        for i, (label, op) in enumerate((("↺  LEFT", "rotate_ccw"), ("↻  RIGHT", "rotate_cw"),
                                          ("⇆  MIRROR", "flip_h"), ("⇅  FLIP", "flip_v"))):
            neon_button(g, app, label, lambda op=op: self.orient(op), width=122).grid(
                row=i // 2, column=i % 2, padx=(0 if i % 2 == 0 else 6, 0), pady=3, sticky="ew")

        f = s["crop"] = self._section(tf, "CROP")
        row = ctk.CTkFrame(f, fg_color="transparent")
        row.pack(fill="x")
        self.aspect_var = tk.StringVar(value="Free")
        option_menu(row, app, list(editing.ASPECTS), self.aspect_var, self._on_aspect, width=122).pack(side="left")
        neon_button(row, app, "RESET", self.reset_crop, width=122).pack(side="left", padx=(6, 0))
        self.crop_label = ctk.CTkLabel(f, text="Drag on the picture to crop", font=app.f_mono_s,
                                       text_color=T.MUTED, anchor="w")
        self.crop_label.pack(fill="x", pady=(4, 0))

        f = s["trim"] = self._section(tf, "TRIM")
        row = ctk.CTkFrame(f, fg_color="transparent")
        row.pack(fill="x")
        neon_button(row, app, "⇤ SET START", self.set_start, width=122).pack(side="left")
        neon_button(row, app, "SET END ⇥", self.set_end, width=122).pack(side="left", padx=(6, 0))
        self.trim_label = ctk.CTkLabel(f, text="", font=app.f_mono_s, text_color=T.MUTED, anchor="w",
                                       justify="left", wraplength=250)
        self.trim_label.pack(fill="x", pady=(4, 0))
        ctk.CTkLabel(f, text="Drag the white handles. Click the timeline, then ← → to step a frame "
                             "(Shift = 1 s).", font=app.f_mono_s, text_color=T.DIM, anchor="w",
                     justify="left", wraplength=245).pack(fill="x")

        f = s["speed"] = self._section(tf, "SPEED & EFFECTS")
        self.speed_var = tk.StringVar(value="1×")
        self.speed_seg = ctk.CTkSegmentedButton(
            f, values=["0.25×", "0.5×", "1×", "1.5×", "2×", "4×"], variable=self.speed_var,
            command=lambda v: self._on_speed(), height=30, corner_radius=6, fg_color=T.CARD,
            selected_color=T.PURPLE, selected_hover_color=T.PINK, unselected_color=T.CARD,
            unselected_hover_color=T.CARD_HI, text_color=T.TEXT, font=app.f_mono_s)
        self.speed_seg.pack(fill="x")
        self.reverse_var = tk.BooleanVar(value=False)
        neon_switch(f, app, "Reverse (play backwards)", self.reverse_var, self._on_effects).pack(anchor="w", pady=(8, 0))
        self.mute_var = tk.BooleanVar(value=False)
        self.mute_sw = neon_switch(f, app, "Remove sound", self.mute_var, self._on_effects)
        self.mute_sw.pack(anchor="w", pady=(6, 0))

        f = s["frame"] = self._section(tf, "SNAPSHOT")
        neon_button(f, app, "SAVE THIS FRAME AS PNG", self.save_frame, width=250).pack(fill="x")

        f = s["output"] = self._section(tf, "SAVE AS")
        self.fmt_var = tk.StringVar(value="Same as original")
        self.fmt_menu = option_menu(f, app, editing.IMAGE_FORMATS, self.fmt_var, width=250)
        self.fmt_menu.pack(fill="x")
        self.private_var = tk.BooleanVar(value=self.st.get("private", False))
        neon_switch(f, app, "Remove all identifying info", self.private_var,
                    lambda: self.st.update(private=bool(self.private_var.get()))).pack(anchor="w", pady=(10, 0))
        ctk.CTkLabel(f, text="GPS, camera/phone model, dates, software and other tags.", font=app.f_mono_s,
                     text_color=T.DIM, anchor="w", justify="left", wraplength=245).pack(fill="x")
        neon_button(f, app, "RESET ALL EDITS", self.reset_all, width=250).pack(fill="x", pady=(14, 10))
        self.sections = s

    def _show_sections(self) -> None:
        order = {
            "": ["file"],
            "image": ["file", "orient", "crop", "output"],
            "video": ["file", "orient", "crop", "trim", "speed", "frame", "output"],
            "audio": ["file", "trim", "speed", "output"],
        }[self.kind]
        for f in self.sections.values():
            f.pack_forget()
        for name in order:
            self.sections[name].pack(fill="x", padx=10)
        if self.kind in ("video", "audio"):
            self.tl_box.grid(row=1, column=0, sticky="ew", padx=6, pady=(0, 4))
        else:
            self.tl_box.grid_forget()
        if self.kind == "video":
            self.mute_sw.pack(anchor="w", pady=(6, 0))
        else:
            self.mute_sw.pack_forget()
        fmts = {"image": editing.IMAGE_FORMATS, "video": editing.VIDEO_FORMATS,
                "audio": editing.AUDIO_FORMATS}.get(self.kind, editing.IMAGE_FORMATS)
        self.fmt_menu.configure(values=fmts)
        if self.fmt_var.get() not in fmts:
            self.fmt_var.set(fmts[0])

    # ----------------------------------------------------------------- app interface
    def accepts(self, path: Path) -> bool:
        return path.suffix.lower().lstrip(".") in self.exts

    def add_files(self, paths: list[Path]) -> int:
        files = [p for p in paths if p.is_file()]
        if not files:
            return 0
        self.load(files[0])
        if len(files) > 1:
            self.app.after(300, lambda: self.set_status(
                f"Opened {files[0].name} — the editor works on one file at a time", T.AMBER))
        return 1

    def browse(self) -> None:
        if self.running:
            return
        exts = " ".join(f"*.{e}" for e in sorted(self.exts))
        f = filedialog.askopenfilename(parent=self.app, title="Open a photo, video or audio file",
                                       filetypes=[("Photos, videos and audio", exts), ("All files", "*.*")],
                                       initialdir=self.st.get("last_browse") or str(Path.home()))
        if f:
            self.st["last_browse"] = str(Path(f).parent)
            self.load(Path(f))

    def set_status(self, text: str, color: str = T.MUTED) -> None:
        self.footer.set_status(text, color)

    def animate(self, phase: float, heavy: bool = True) -> None:
        self.tag_label.configure(text_color=T.neon(phase + self.offset))
        self.canvas.set_color(T.neon(phase + self.offset + 0.12))
        self.timeline.set_color(T.neon(phase + self.offset + 0.25))
        if heavy:
            self.footer.animate(phase + self.offset, self.running)

    # ----------------------------------------------------------------- loading
    def load(self, path: Path) -> None:
        if self.running:
            self.set_status("Wait for the current save to finish", T.AMBER)
            return
        try:
            kind = editing.kind_of(path)
        except ConversionError as exc:
            self.set_status(str(exc), T.RED)
            return
        self.path, self.kind = path, kind
        self.edit = Edit()
        self.aspect_var.set("Free")
        self.speed_var.set("1×")
        self.reverse_var.set(False)
        self.mute_var.set(False)
        self.preview_src, self.info = None, None
        self.footer.last_output = None  # forget the previous file's result
        self.footer.open_btn.pack_forget()
        self.footer.progress.set(0)
        self.file_label.configure(text=short_name(path.name, 40))
        self.file_info.configure(text="Reading…")
        self._show_sections()
        self.canvas.show_loading()
        token = path
        threading.Thread(target=self._load_worker, args=(path, kind, token), daemon=True).start()

    def _load_worker(self, path: Path, kind: str, token) -> None:
        try:
            if kind == "image":
                im = editing.load_image(path)
                size = im.size
                im.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
                self.app.post(self._loaded, token, im.convert("RGB"), size, None)
            else:
                info = media.probe(path, tools.Job())
                frame = editing.grab_frame(path, 0.0) if kind == "video" and info.has_video else None
                self.app.post(self._loaded, token, frame, (info.width, info.height), info)
        except ConversionError as exc:
            self.app.post(self._load_failed, token, str(exc))
        except Exception as exc:
            traceback.print_exc()
            self.app.post(self._load_failed, token, f"{exc.__class__.__name__}: {exc}")

    def _load_failed(self, token, msg: str) -> None:
        if token != self.path:
            return
        self.path, self.kind = None, ""
        self._show_sections()
        self.canvas.show_empty()
        self.file_label.configure(text="No file loaded")
        self.file_info.configure(text="Drop a photo, video or audio file")
        self.set_status(msg, T.RED)

    def _loaded(self, token, im, size, info) -> None:
        if token != self.path:
            return
        self.info = info
        self.src_size = size
        if self.kind == "video" and im is None:
            self.kind = "audio" if info and info.has_audio else self.kind
            self._show_sections()
        self.preview_src = im
        if info:
            self.edit.end = info.duration
            self.timeline.set_duration(info.duration or 0.0, info.fps)
        if self.kind == "audio":
            self.canvas.show_audio(self.path.name, f"Audio · {editing.fmt_time(info.duration or 0)}")
        elif im is not None:
            self._refresh_preview()
        else:  # a video we can't decode a preview frame from: editing still works
            self.canvas.show_audio(self.path.name, "No preview for this video — trims, speed and "
                                                   "rotation still apply", glyph=T.GLYPH["media"])
        self._update_labels()
        self.set_status(f"Loaded {self.path.name}", T.CYAN)

    # ----------------------------------------------------------------- editing actions
    def _refresh_preview(self) -> None:
        if self.preview_src is not None:
            self.canvas.show_image(self.edit.orient.apply(self.preview_src))

    def orient(self, op: str) -> None:
        if self.kind not in ("image", "video") or self.running:
            return
        getattr(self.edit.orient, op)()
        if self.edit.crop:
            self.edit.crop = {"rotate_cw": editing.crop_after_cw, "rotate_ccw": editing.crop_after_ccw,
                              "flip_h": editing.crop_after_flip_h, "flip_v": editing.crop_after_flip_v}[op](self.edit.crop)
        self._refresh_preview()
        self._update_labels()

    def aspect_ratio(self) -> float | None:
        r = editing.ASPECTS.get(self.aspect_var.get())
        if r == -1.0:
            w, h = self.edit.orient.size(*self.src_size)
            return w / h if h else None
        return r

    def _on_aspect(self, _value=None) -> None:
        r = self.aspect_ratio()
        if r and self.kind in ("image", "video"):
            w, h = self.edit.orient.size(*self.src_size)
            if w and h:
                self.edit.crop = editing.fit_aspect(r, w, h, self.edit.crop)
                self.canvas.request_draw()
                self._update_labels()

    def reset_crop(self) -> None:
        self.edit.crop = None
        self.aspect_var.set("Free")
        self.canvas.request_draw()
        self._update_labels()

    def on_crop_changed(self, live: bool) -> None:
        self._update_labels()

    def on_timeline(self) -> None:
        tl = self.timeline
        self.edit.start, self.edit.end = tl.a, tl.b
        self._update_labels()
        if self.kind == "video" and self.path:
            self.grabber.request(self.path, tl.pos)

    def _on_frame(self, path: Path, t: float, im) -> None:
        if path == self.path and im is not None and self.kind == "video":
            self.preview_src = im
            self._refresh_preview()

    def set_start(self) -> None:
        self.timeline.set_start()
        self.on_timeline()

    def set_end(self) -> None:
        self.timeline.set_end()
        self.on_timeline()

    def _on_speed(self) -> None:
        self.edit.speed = float(self.speed_var.get().rstrip("×"))
        self._update_labels()

    def _on_effects(self) -> None:
        self.edit.reverse = bool(self.reverse_var.get())
        self.edit.mute = bool(self.mute_var.get())
        self._update_labels()

    def reset_all(self) -> None:
        if self.path and not self.running:
            self.load(self.path)

    def _update_labels(self) -> None:
        e = self.edit
        if not self.path:
            return
        W, H = e.orient.size(*self.src_size)
        if self.kind in ("image", "video") and W:
            if e.crop:
                cw, ch = max(1, round(e.crop[2] * W)), max(1, round(e.crop[3] * H))
                self.crop_label.configure(text=f"{W}×{H}  →  {cw}×{ch} px", text_color=T.CYAN)
            else:
                self.crop_label.configure(text="Drag on the picture to crop", text_color=T.MUTED)
        details = []
        if W:
            details.append(f"{W}×{H}")
        if self.info and self.info.duration:
            details.append(editing.fmt_time(self.info.duration))
        details.append(self.path.suffix.lstrip(".").upper())
        try:
            details.append(tools.human_size(self.path.stat().st_size))
        except OSError:
            pass
        self.file_info.configure(text="  ·  ".join(details))
        if self.kind in ("video", "audio") and self.info and self.info.duration:
            tl = self.timeline
            length = max(0.0, e.end - e.start) if e.end is not None else 0.0
            out_len = length / e.speed
            text = f"{editing.fmt_time(e.start)} → {editing.fmt_time(e.end or 0)}   ({length:.1f} s)"
            if abs(e.speed - 1) > 1e-6:
                text += f"\nAt {self.speed_var.get()}: {out_len:.1f} s long"
            if e.reverse:
                text += "\nPlays backwards"
            self.trim_label.configure(text=text, text_color=T.CYAN)
            self.t_start.configure(text=f"START {editing.fmt_time(tl.a)}")
            self.t_end.configure(text=f"END {editing.fmt_time(tl.b)}")
            self.t_pos.configure(text=editing.fmt_time(tl.pos))

    # ----------------------------------------------------------------- saving
    def _dest(self) -> Path:
        return self.footer.out_dir() or self.path.parent

    def start(self) -> None:
        if self.running:
            return
        if not self.path or not self.kind:
            self.set_status("Open a file first", T.AMBER)
            return
        if not self.footer.check_out_dir():
            return
        self.edit.fmt = self.fmt_var.get()
        self.edit.private = bool(self.private_var.get())
        if self.kind == "image" and self.edit.orient.identity and not self.edit.crop \
                and self.edit.fmt == "Same as original" and not self.edit.private:
            self.set_status("Nothing to save yet — crop, rotate or flip first (or turn on "
                            "'Remove all identifying info' to save a clean copy)", T.AMBER)
            return
        # snapshot everything, so tweaking controls during the save can't change what gets written
        path, edit, dest, kind = self.path, copy.deepcopy(self.edit), self._dest(), self.kind
        self._run(lambda job, status, progress: (
            editing.export_image(path, edit, dest) if kind == "image"
            else editing.export_media(path, edit, dest, job, status, progress)), "SAVING…")

    def save_frame(self) -> None:
        if self.running or self.kind != "video" or not self.footer.check_out_dir():
            return
        self.edit.private = bool(self.private_var.get())
        path, edit, dest, t = self.path, copy.deepcopy(self.edit), self._dest(), self.timeline.pos
        self._run(lambda job, status, progress: editing.save_frame(path, t, edit, dest, job), "SAVING…")

    def cancel(self) -> None:
        if self.job:
            self.job.cancel()
            self.set_status("Cancelling…", T.AMBER)

    def _run(self, fn, label: str) -> None:
        self.running = True
        self.job = tools.Job()
        self.started = time.monotonic()
        self.footer.set_running(True, label)
        self.set_status("Working…", T.CYAN)
        job = self.job
        post = self.app.post

        def status(msg):
            post(self.set_status, msg, T.CYAN)

        last = [0.0]

        def progress(f):
            now = time.monotonic()
            if now - last[0] > 0.08 or f >= 1:
                last[0] = now
                post(self.footer.progress.set, max(0.0, min(1.0, f)))

        def work():
            try:
                out = fn(job, status, progress)
                post(self._done, out, None)
            except Cancelled:
                post(self._done, None, "Cancelled")
            except ConversionError as exc:
                post(self._done, None, str(exc))
            except PermissionError:
                post(self._done, None, "Windows blocked saving there — pick another SAVE TO folder")
            except Exception as exc:
                traceback.print_exc()
                post(self._done, None, f"{exc.__class__.__name__}: {exc}")

        threading.Thread(target=work, daemon=True).start()

    def _done(self, out: Path | None, error: str | None) -> None:
        self.running = False
        self.job = None
        if out:
            self.footer.last_output = out
            self.footer.progress.set(1.0)
            self.set_status(f"SAVED  //  {out.name}", T.GREEN)
        else:
            self.footer.progress.set(0)
            self.set_status(error or "Failed", T.AMBER if error == "Cancelled" else T.RED)
        self.footer.set_running(False)
        self.app.notify_done(time.monotonic() - self.started)
