import sys
import os
import logging
import traceback
import ctypes


def _splash(text: str) -> None:
    # Kept as a no-op so startup steps can remain readable in source builds.
    # Calling pyi_splash without an active splash IPC channel can deadlock a
    # one-file executable before the main window is shown.
    return


def _splash_close() -> None:
    return


def _apply_windows_dark_titlebar(window) -> None:
    """Match the native Windows title bar to the editor's dark UI."""
    if sys.platform != "win32":
        return
    try:
        enabled = ctypes.c_int(1)
        hwnd = ctypes.c_void_p(int(window.winId()))
        # 20 is supported by current Windows 10/11; 19 covers older builds.
        dwmapi = ctypes.windll.dwmapi
        result = dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(enabled), ctypes.sizeof(enabled))
        if result != 0:
            dwmapi.DwmSetWindowAttribute(hwnd, 19, ctypes.byref(enabled), ctypes.sizeof(enabled))
    except Exception:
        pass


_splash("Starting up...")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)

_splash("Loading Qt framework...")
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFont
from PySide6.QtCore import Qt

_splash("Loading editor modules...")
from gui import MainWindow


def main() -> None:
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv)
    from updater import APP_VERSION
    app.setApplicationName("Crimson Desert Save Editor")
    app.setApplicationVersion(APP_VERSION)

    font = QFont("Consolas", 10)
    font.setStyleHint(QFont.Monospace)
    app.setFont(font)

    _splash("Building main window...")
    try:
        window = MainWindow()
    except Exception:
        # Windowed builds have no console. Keep a concrete diagnostic beside
        # the executable instead of leaving users on a loading screen.
        try:
            base_dir = os.path.dirname(os.path.abspath(sys.executable))
            with open(os.path.join(base_dir, "startup-error.log"), "w", encoding="utf-8") as log_file:
                log_file.write(traceback.format_exc())
        except OSError:
            pass
        _splash_close()
        raise
    _apply_windows_dark_titlebar(window)
    _splash_close()
    window.show()

    if len(sys.argv) > 1:
        path = sys.argv[1]
        if os.path.isfile(path):
            if path.lower().endswith(".save"):
                window._load_save(path)
            elif path.lower().endswith(".bin"):
                from save_crypto import load_raw_stream
                try:
                    window._save_data = load_raw_stream(path)
                    window._loaded_path = path
                    window._scan_and_populate()
                    window._update_status(f"Loaded: {os.path.basename(path)}")
                except Exception as e:
                    print(f"Error loading {path}: {e}")

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
