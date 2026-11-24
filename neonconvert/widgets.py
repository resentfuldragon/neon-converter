"""Shared neon widgets: buttons, menus, the holographic drop zone and the save/progress footer."""
from __future__ import annotations

import os
import subprocess
import tkinter as tk
from pathlib import Path
from tkinter import filedialog

import customtkinter as ctk

from . import theme as T


def short_name(name: str, limit: int = 64) -> str:
    if len(name) <= limit:
        return name
    keep = limit - 1
    return name[: keep // 2 + 4] + "…" + name[-(keep // 2 - 4):]


def reveal(path: Path) -> None:
    """Open Explorer with the file selected (or open the folder)."""
    try:
        if path.is_file():
            subprocess.Popen(["explorer", "/select,", str(path)])
        else:
            os.startfile(str(path if path.is_dir() else path.parent))
    except Exception:
        pass


def neon_button(master, app, text, command, width=120, primary=False, height=None):
    if primary:
        return ctk.CTkButton(master, text=text, command=command, width=width, height=height or 44,
                             corner_radius=6, fg_color=T.CYAN, hover_color=T.TEXT, text_color=T.BG,
                             font=app.f_btn)
    return ctk.CTkButton(master, text=text, command=command, width=width, height=height or 34,
                         corner_radius=6, fg_color="transparent", hover_color=T.CARD_HI, border_width=1,
                         border_color=T.BORDER, text_color=T.TEXT, font=app.f_mono_b)


def option_menu(master, app, values, variable, command=None, width=200):
    return ctk.CTkOptionMenu(
        master, values=values, variable=variable, command=command, width=width, height=32,
        corner_radius=6, fg_color=T.CARD, button_color=T.CARD_HI, button_hover_color=T.PURPLE,
        text_color=T.TEXT, dropdown_fg_color=T.PANEL, dropdown_hover_color=T.CARD_HI,
        dropdown_text_color=T.TEXT, font=app.f_body, dropdown_font=app.f_body, dynamic_resizing=False)


def neon_switch(master, app, text, variable, command=None):
    return ctk.CTkSwitch(master, text=text, variable=variable, command=command, onvalue=True,
                         offvalue=False, progress_color=T.PURPLE, button_color=T.TEXT,
                         button_hover_color=T.CYAN, fg_color=T.BORDER, text_color=T.TEXT, font=app.f_body)


def caption(master, app, text):
    return ctk.CTkLabel(master, text=text, font=app.f_mono_s, text_color=T.MUTED, anchor="w")


class DropZone(tk.Canvas):
    """Holographic drop target: faint grid, neon HUD corner brackets, click to browse."""

    def __init__(self, master, panel, scale: float, height: int = 120, title: str = "DROP FILES HERE") -> None:
        self.s = scale
        super().__init__(master, height=int(height * scale), bg=T.PANEL, highlightthickness=0,
                         cursor="hand2", bd=0)
        self.panel = panel
        self.title = title
        self.hot = False
        self.color = T.CYAN
        self.bind("<Configure>", lambda e: self.redraw())
        self.bind("<Button-1>", lambda e: panel.browse())
        self.bind("<Enter>", lambda e: self.set_hot(True))
        self.bind("<Leave>", lambda e: self.set_hot(False))

    def set_hot(self, hot: bool) -> None:
        if hot != self.hot:
            self.hot = hot
            self.redraw()

    def set_color(self, color: str) -> None:
        self.color = color
        self.itemconfigure("neon", fill=color)
        self.itemconfigure("glow_outline", outline=T.shade(color, 0.45 if self.hot else 0.22, T.PANEL))

    def redraw(self) -> None:
        draw_holo(self, self.s, self.color, self.hot,
                  "RELEASE TO ADD FILES" if self.hot and self.panel.app.dragging else self.title,
                  "or click to browse   ·   " + self.panel.formats_hint)


def draw_holo(cv: tk.Canvas, s: float, color: str, hot: bool, title: str, sub: str,
              glyph: str = T.GLYPH["upload"]) -> None:
    """The shared 'holographic' empty-state art used by drop zones and the editor preview."""
    w, h = cv.winfo_width(), cv.winfo_height()
    if w < 10:
        return
    cv.delete("all")
    step = int(22 * s)
    grid = T.mix(T.PANEL, T.PURPLE, 0.16 if hot else 0.08)
    for x in range(step // 2, w, step):
        cv.create_line(x, 0, x, h, fill=grid)
    for y in range(step // 2, h, step):
        cv.create_line(0, y, w, y, fill=grid)
    m, L, lw = int(6 * s), int(26 * s), max(2, int(3 * s))
    cv.create_rectangle(m, m, w - m, h - m, outline=T.shade(color, 0.45 if hot else 0.22, T.PANEL),
                        width=max(1, int(s)), tags="glow_outline")
    for (x, y, dx, dy) in ((m, m, 1, 1), (w - m, m, -1, 1), (m, h - m, 1, -1), (w - m, h - m, -1, -1)):
        cv.create_line(x, y, x + dx * L, y, fill=color, width=lw, tags="neon", capstyle="projecting")
        cv.create_line(x, y, x, y + dy * L, fill=color, width=lw, tags="neon", capstyle="projecting")
    cx, cy = w // 2, h // 2
    cv.create_text(cx, cy - int(30 * s), text=glyph, fill=color, tags="neon",
                   font=(T.FONT_ICON, -int(30 * s)))
    cv.create_text(cx, cy + int(10 * s), text=title, fill=T.TEXT, font=(T.FONT_UI_BOLD, -int(19 * s)))
    cv.create_text(cx, cy + int(36 * s), text=sub, fill=T.MUTED, font=(T.FONT_UI, -int(13 * s)))


class Footer(ctk.CTkFrame):
    """SAVE TO folder picker, the big action button, cancel / open-output, progress and status."""

    def __init__(self, master, app, st: dict, go_text: str, on_go, on_cancel) -> None:
        super().__init__(master, fg_color="transparent")
        self.app, self.st, self.go_text = app, st, go_text
        self.last_output: Path | None = None
        self.grid_columnconfigure(1, weight=1)

        caption(self, app, "SAVE TO").grid(row=0, column=0, sticky="w")
        self.out_var = tk.StringVar()
        self.out_menu = option_menu(self, app, [], self.out_var, self._on_out_choice, width=300)
        self.out_menu.grid(row=1, column=0, sticky="w", pady=(2, 0))
        self._refresh_out_menu()

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.grid(row=0, column=2, rowspan=2, sticky="e")
        self.open_btn = neon_button(btns, app, "OPEN OUTPUT", self.open_output, width=130)
        self.cancel_btn = ctk.CTkButton(btns, text="CANCEL", command=on_cancel, width=100, height=44,
                                        corner_radius=6, fg_color="transparent", border_width=2,
                                        border_color=T.PINK, text_color=T.PINK, hover_color=T.CARD_HI,
                                        font=app.f_btn)
        self.go_btn = neon_button(btns, app, go_text, on_go, width=170, primary=True)
        self.go_btn.pack(side="right")

        self.progress = ctk.CTkProgressBar(self, height=8, corner_radius=4, fg_color=T.CARD,
                                           progress_color=T.CYAN, border_width=0)
        self.progress.set(0)
        self.progress.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(12, 4))
        self.status = ctk.CTkLabel(self, text="SYSTEM READY", font=app.f_mono_s, text_color=T.MUTED, anchor="w")
        self.status.grid(row=3, column=0, columnspan=3, sticky="ew")

    # ----------------------------------------------------------------- save-to folder
    def _refresh_out_menu(self) -> None:
        custom = self.st.get("out_dir")
        values = ["Same folder as original"] + ([custom] if custom else []) + ["Choose folder…"]
        self.out_menu.configure(values=values)
        self.out_var.set(custom if custom and self.st.get("use_custom") else values[0])

    def _on_out_choice(self, choice: str) -> None:
        if choice == "Choose folder…":
            folder = filedialog.askdirectory(parent=self.app, title="Save files to…",
                                             initialdir=self.st.get("out_dir") or str(Path.home()))
            if folder:
                self.st["out_dir"] = str(Path(folder))
                self.st["use_custom"] = True
            self._refresh_out_menu()
        else:
            self.st["use_custom"] = choice != "Same folder as original"
        self.app.settings.save()

    def out_dir(self) -> Path | None:
        if self.st.get("use_custom") and self.st.get("out_dir"):
            return Path(self.st["out_dir"])
        return None

    def check_out_dir(self) -> bool:
        d = self.out_dir()
        if d and not d.is_dir():
            self.set_status(f"Output folder not found: {d}", T.RED)
            return False
        return True

    # ----------------------------------------------------------------- state
    def set_status(self, text: str, color: str = T.MUTED) -> None:
        self.status.configure(text=text, text_color=color)

    def set_running(self, running: bool, label: str = "RUNNING…") -> None:
        if running:
            self.last_output = None
            self.open_btn.pack_forget()
            self.go_btn.configure(state="disabled", text=label)
            self.cancel_btn.pack(side="right", padx=(0, 10))
            self.progress.set(0)
        else:
            self.cancel_btn.pack_forget()
            self.go_btn.configure(state="normal", text=self.go_text, fg_color=T.CYAN)
            if self.last_output:
                self.open_btn.pack(side="right", padx=(0, 10))

    def open_output(self) -> None:
        if self.last_output:
            reveal(self.last_output)

    def animate(self, phase: float, running: bool) -> None:
        self.progress.configure(progress_color=T.neon(phase + 0.3))
        c = T.neon(phase + 0.45)
        self.go_btn.configure(fg_color=T.shade(c, 0.35) if running else c)
