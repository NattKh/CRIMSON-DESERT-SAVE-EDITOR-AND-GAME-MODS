import atexit
import datetime
import faulthandler
import os
import logging
import platform
import sys
import threading
import traceback


def _application_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _open_log_stream() -> tuple[str, object]:
    """Open the persistent log before importing Qt or editor modules."""
    preferred = os.path.join(_application_dir(), "logs.txt")
    try:
        return preferred, open(preferred, "a", encoding="utf-8", buffering=1)
    except OSError:
        fallback_dir = os.path.join(
            os.environ.get("LOCALAPPDATA", os.getcwd()),
            "CrimsonSaveEditor",
        )
        os.makedirs(fallback_dir, exist_ok=True)
        fallback = os.path.join(fallback_dir, "logs.txt")
        return fallback, open(fallback, "a", encoding="utf-8", buffering=1)


LOG_PATH, _LOG_STREAM = _open_log_stream()

# PyInstaller windowed applications normally have no console streams. Redirect
# both Python streams and, where Windows permits it, file descriptors 1 and 2 so
# tracebacks and native-library diagnostics are retained too.
try:
    os.dup2(_LOG_STREAM.fileno(), 1)
    os.dup2(_LOG_STREAM.fileno(), 2)
except OSError:
    pass
sys.stdout = _LOG_STREAM
sys.stderr = _LOG_STREAM

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d %(levelname)s %(threadName)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(_LOG_STREAM)],
    force=True,
)
log = logging.getLogger("startup")

try:
    faulthandler.enable(file=_LOG_STREAM, all_threads=True)
except (OSError, RuntimeError):
    pass


def _log_unhandled(exc_type, exc_value, exc_traceback) -> None:
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    logging.getLogger("crash").critical(
        "Unhandled exception",
        exc_info=(exc_type, exc_value, exc_traceback),
    )
    _LOG_STREAM.flush()


def _log_thread_exception(args) -> None:
    logging.getLogger("crash.thread").critical(
        "Unhandled exception in thread %s",
        getattr(args.thread, "name", "unknown"),
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
    )
    _LOG_STREAM.flush()


sys.excepthook = _log_unhandled
threading.excepthook = _log_thread_exception


def _log_shutdown() -> None:
    log.info("Application shutdown")
    _LOG_STREAM.flush()


atexit.register(_log_shutdown)

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFont
from PySide6.QtCore import Qt, QtMsgType, qInstallMessageHandler
from startup_splash import (
    create_startup_splash, set_active_startup_splash, update_startup_status,
)
from app_version import APP_VERSION


def _qt_message_handler(message_type, context, message) -> None:
    levels = {
        QtMsgType.QtDebugMsg: logging.DEBUG,
        QtMsgType.QtInfoMsg: logging.INFO,
        QtMsgType.QtWarningMsg: logging.WARNING,
        QtMsgType.QtCriticalMsg: logging.ERROR,
        QtMsgType.QtFatalMsg: logging.CRITICAL,
    }
    location = ""
    if context is not None and (context.file or context.function):
        location = f" [{context.file or '?'}:{context.line} {context.function or ''}]"
    logging.getLogger("qt").log(
        levels.get(message_type, logging.INFO), "%s%s", message, location
    )


def main() -> int:
    qInstallMessageHandler(_qt_message_handler)
    log.info("=" * 72)
    log.info("Crimson Save Editor %s starting", APP_VERSION)
    log.info("Log file: %s", LOG_PATH)
    log.info("Executable: %s", os.path.abspath(sys.executable))
    log.info("Python: %s", platform.python_version())
    log.info("Working directory: %s", os.getcwd())
    log.info("Started UTC: %s", datetime.datetime.now(datetime.timezone.utc).isoformat())

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
                    log.exception("Failed to load raw stream %s: %s", path, e)

    return app.exec()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        exc_type, exc_value, exc_traceback = sys.exc_info()
        _log_unhandled(exc_type, exc_value, exc_traceback)
        raise SystemExit(1)
