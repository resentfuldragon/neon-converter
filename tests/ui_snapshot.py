"""Drive the real window: load samples into every mode, run a few jobs, save screenshots.

    python tests/ui_snapshot.py <folder-with-samples> <screenshot-folder>

Screenshots are taken of the app's own window only (PrintWindow), so nothing else on screen
is captured and the window doesn't need focus.
"""
import ctypes
import ctypes.wintypes as wt
import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image  # noqa: E402

from neonconvert.gui import App  # noqa: E402

samples, shots = Path(sys.argv[1]), Path(sys.argv[2])
shots.mkdir(parents=True, exist_ok=True)
app = App()
report = []


class BIH(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
                ("biBitCount", wt.WORD), ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


def grab(name):
    app.update()
    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    hwnd = user32.GetParent(app.winfo_id())
    r = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    hdc_win = user32.GetWindowDC(hwnd)
    hdc = gdi32.CreateCompatibleDC(hdc_win)
    bmp = gdi32.CreateCompatibleBitmap(hdc_win, w, h)
    gdi32.SelectObject(hdc, bmp)
    user32.PrintWindow(hwnd, hdc, 2)
    bih = BIH(ctypes.sizeof(BIH), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(hdc, bmp, 0, h, buf, ctypes.byref(bih), 0)
    Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1).save(shots / f"{name}.png")
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(hdc)
    user32.ReleaseDC(hwnd, hdc_win)
    report.append(f"shot {name} {w}x{h}")


def wait_idle(panel, then, timeout=120):
    t0 = time.monotonic()

    def poll():
        if panel.running and time.monotonic() - t0 < timeout:
            app.after(300, poll)
        else:
            then()
    app.after(600, poll)


files = sorted(p for p in samples.iterdir() if p.is_file() and "(edited)" not in p.name)
pick = lambda *exts: [p for p in files if p.suffix.lower() in exts]  # noqa: E731
ed = app.panels["edit"]
steps = []


def add(fn, delay=1200):
    steps.append((fn, delay))


add(lambda: (app.select("text"), app.route_files(pick(".docx", ".epub", ".pdf", ".md"))))
add(lambda: app.panels["text"].start(), 200)
add(lambda: wait_idle(app.panels["text"], next_step), 0)
add(lambda: grab("1_text"))
add(lambda: (app.select("image"), app.route_files(pick(".png", ".jpg", ".gif", ".svg"))))
add(lambda: grab("2_image"))
add(lambda: (app.select("media"), app.route_files(pick(".mp4"))))
add(lambda: grab("3_media"))
# --- editor: video
add(lambda: (app.select("edit"), app.route_files(pick(".mp4")[:1])), 2500)
add(lambda: (ed.aspect_var.set("1:1"), ed._on_aspect(), ed.orient("rotate_cw")))
add(lambda: (setattr(ed.timeline, "pos", 1.0), ed.set_start(), setattr(ed.timeline, "pos", 2.5),
             ed.set_end(), ed.speed_var.set("2×"), ed._on_speed()), 1500)
add(lambda: grab("4_edit_video"))
add(lambda: ed.start(), 200)
add(lambda: wait_idle(ed, next_step), 0)
add(lambda: report.append(f"video saved: {ed.footer.last_output} | {ed.footer.status.cget('text')}"))
# --- editor: photo
add(lambda: app.route_files(pick(".jpg")[:1]), 1500)
add(lambda: (setattr(ed.edit, "crop", (0.1, 0.15, 0.6, 0.6)), ed.canvas.request_draw(), ed._update_labels()))
add(lambda: grab("5_edit_photo"))
add(lambda: ed.start(), 200)
add(lambda: wait_idle(ed, next_step), 0)
add(lambda: report.append(f"photo saved: {ed.footer.last_output} | {ed.footer.status.cget('text')}"))
add(lambda: app.destroy())

_i = [0]


def next_step():
    if _i[0] >= len(steps):
        return
    fn, delay = steps[_i[0]]
    _i[0] += 1
    fn()
    if delay:
        app.after(delay, next_step)


app.after(1500, next_step)
app.mainloop()
print("\n".join(report))
