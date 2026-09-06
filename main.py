import sys
import os
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFont
from PySide6.QtCore import Qt
from startup_splash import (
    create_startup_splash, set_active_startup_splash, update_startup_status,
)
from app_version import APP_VERSION


def main() -> None:
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv)
    app.setApplicationName("Crimson Desert Save Editor")
    app.setApplicationVersion(APP_VERSION)

    font = QFont("Consolas", 10)
    font.setStyleHint(QFont.Monospace)
    app.setFont(font)

    startup = create_startup_splash(APP_VERSION)
    set_active_startup_splash(startup)
    startup.show_centered()
    update_startup_status("Loading editor modules...", 15)

    from gui import MainWindow

    update_startup_status("Building main window...", 25)
    window = MainWindow()
    update_startup_status("Ready", 100)
    window.show()
    QApplication.processEvents()
    startup.close()
    set_active_startup_splash(None)

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
