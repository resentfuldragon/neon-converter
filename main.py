"""Neon Converter entry point.  Run:  python main.py [files...]"""
import multiprocessing
import os
import sys


def _prepare() -> None:
    from neonconvert import tools

    # A windowed .exe has no console: send stray prints/tracebacks to a log file instead.
    if sys.stdout is None or sys.stderr is None or getattr(sys, "frozen", False):
        log = open(tools.data_dir() / "log.txt", "w", encoding="utf-8", buffering=1)  # fresh each run
        sys.stdout = sys.stdout if sys.stdout is not None and not getattr(sys, "frozen", False) else log
        sys.stderr = sys.stderr if sys.stderr is not None and not getattr(sys, "frozen", False) else log
    if os.name == "nt" and not getattr(sys, "frozen", False):
        try:  # running from source: show our icon in the taskbar instead of python.exe's
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("NeonConverter.App")
        except Exception:
            pass


def main() -> None:
    multiprocessing.freeze_support()
    _prepare()
    import warnings

    warnings.filterwarnings("ignore")
    if len(sys.argv) == 4 and sys.argv[1] == "--selftest":
        from neonconvert import selftest

        sys.exit(selftest.run(sys.argv[2], sys.argv[3]))

    from neonconvert.gui import run

    run(sys.argv[1:])


if __name__ == "__main__":
    main()
