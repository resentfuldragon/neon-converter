"""UI responsiveness: time to queue many files, and idle CPU with/without the neon animation.

    python tests/perf_check.py
"""
import sys
import tempfile
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from neonconvert.gui import App  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="neon_perf_"))
files = []
for i in range(300):
    p = tmp / f"chapter {i:03d}.txt"
    p.write_text("hello", encoding="utf-8")
    files.append(p)

app = App()
app.select("text")
report = []


def measure_cpu(label, seconds, then):
    c0, w0 = time.process_time(), time.perf_counter()

    def done():
        cpu = time.process_time() - c0
        wall = time.perf_counter() - w0
        report.append(f"{label}: {100 * cpu / wall:.1f}% of one core")
        then()
    app.after(int(seconds * 1000), done)


def step1():
    panel = app.panels["text"]
    t0 = time.perf_counter()
    app.route_files(files)
    first = time.perf_counter() - t0
    # the window stays usable while the remaining rows are built in the background
    worst = [0.0]
    last = [time.perf_counter()]

    def watch():
        now = time.perf_counter()
        worst[0] = max(worst[0], now - last[0])
        last[0] = now
        if panel._pending_rows or panel._row_job:
            app.after(5, watch)
        else:
            report.append(f"queued {len(panel.items)} files: first rows after {first:.2f}s, all rows after "
                          f"{time.perf_counter() - t0:.2f}s, longest UI freeze {worst[0] * 1000:.0f} ms")
            panel.clear()
            app.update()
            measure_cpu("idle, animation ON", 5, step2)
    app.after(5, watch)


def step2():
    app.motion_var.set(False)
    app.after(500, lambda: measure_cpu("idle, animation OFF", 5, lambda: app.destroy()))


app.after(1500, step1)
app.mainloop()
print("\n".join(report))
