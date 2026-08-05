import sys
import os

def _splash(text: str) -> None:
    try:
        import pyi_splash
        ver = globals().get('_APP_VER', '?')
        pyi_splash.update_text(f"v{ver} — {text}" if ver != '?' else text)
    except Exception:
        pass


def _splash_close() -> None:
    try:
        import pyi_splash
        pyi_splash.close()
    except Exception:
        pass
    try:
        import tkinter
        root = getattr(tkinter, "_default_root", None)
        if root is not None:
            try:
                root.destroy()
            except Exception:
                pass
            tkinter._default_root = None
    except Exception:
        pass


try:
    from updater import APP_VERSION as _APP_VER
except Exception:
    _APP_VER = "?"
_splash("Starting up...")

def _setup_file_logging() -> None:
    try:
        base = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) \
               else os.path.dirname(os.path.abspath(__file__))
        log_path = os.path.join(base, "logs.txt")
        log_file = open(log_path, "a", buffering=1, encoding="utf-8", errors="replace")

        class _Tee:
            def __init__(self, *streams): self._streams = [s for s in streams if s]
            def write(self, data):
                for s in self._streams:
                    try: s.write(data)
                    except Exception: pass
            def flush(self):
                for s in self._streams:
                    try: s.flush()
                    except Exception: pass
            def isatty(self): return False

        sys.stdout = _Tee(sys.__stdout__, log_file)
        sys.stderr = _Tee(sys.__stderr__, log_file)

        import logging, datetime
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            handlers=[logging.StreamHandler(sys.stdout)],
            force=True,
        )
        print(f"=== Session start {datetime.datetime.now().isoformat()} ===")

        def _excepthook(exc_type, exc, tb):
            import traceback
            traceback.print_exception(exc_type, exc, tb)
        sys.excepthook = _excepthook
    except Exception as e:
        try: print(f"[log setup failed] {e}")
        except Exception: pass

_setup_file_logging()

os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
os.environ.setdefault("QT_AUTO_SCREEN_SCALE_FACTOR", "1")

_splash("Loading Qt framework...")
from PySide6.QtCore import Qt, QLocale
from PySide6.QtGui import QFont, QGuiApplication
from PySide6.QtWidgets import QApplication

QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
    Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
)

_splash("Loading editor modules...")
from gui import MainWindow


_CJK_FONT_STACK = [
    "Consolas",
    "Segoe UI",
    "Microsoft YaHei",
    "Microsoft JhengHei",
    "Malgun Gothic",
    "Yu Gothic UI",
    "Noto Sans CJK SC",
    "sans-serif",
]


def _editor_config_path() -> str:
    base = (
        os.path.dirname(os.path.abspath(sys.executable))
        if getattr(sys, "frozen", False)
        else os.path.dirname(os.path.abspath(__file__))
    )
    primary = os.path.join(base, "editor_config.json")
    gui_alt = os.path.join(base, "gui", "editor_config.json")
    if not os.path.isfile(primary) and os.path.isfile(gui_alt):
        return gui_alt
    return primary


def _compute_startup_language() -> str:
    try:
        from gui_i18n import compute_startup_language as _compute
        return _compute(_editor_config_path())
    except Exception as e:
        print(f"[lang] compute_startup_language failed: {e}")
        return "en"


def _autodetect_locale_if_unset() -> None:
    try:
        try:
            from gui_i18n import current_language
            if current_language() and current_language() != "en":
                return
        except Exception:
            pass

        import json
        from localization import set_language, get_available_languages
        cfg_path = _editor_config_path()
        cfg = {}
        if os.path.isfile(cfg_path):
            try:
                with open(cfg_path, "r", encoding="utf-8") as f:
                    cfg = json.load(f) or {}
            except Exception:
                cfg = {}
        if cfg.get("default_lang") or cfg.get("language"):
            return

        sys_name = QLocale.system().name()
        base = sys_name.split("_")[0].lower()
        try:
            codes = {c for c, _ in get_available_languages()}
        except Exception:
            codes = set()
        chosen = "en"
        if sys_name in codes:
            chosen = sys_name
        elif base in codes:
            chosen = base
        elif base == "zh" and "zh_CN" in codes:
            chosen = "zh_CN"
        elif base == "ko" and "ko_KR" in codes:
            chosen = "ko_KR"
        set_language(chosen)
    except Exception:
        pass


def main() -> None:
    harvest_i18n = "--harvest-i18n" in sys.argv
    if harvest_i18n:
        sys.argv = [a for a in sys.argv if a != "--harvest-i18n"]

    app = QApplication(sys.argv)
    from updater import APP_VERSION
    try:
        from updater import APP_VARIANT as _variant
    except Exception:
        _variant = "full"
    app.setApplicationName({
        "gamemods":   "Crimson Desert Game Mods",
        "standalone": "Crimson Desert Save Editor",
    }.get(_variant, "Crimson Desert Save Editor"))
    app.setApplicationVersion(APP_VERSION)

    font = QFont()
    font.setFamilies(_CJK_FONT_STACK)
    font.setPointSize(10)
    font.setStyleHint(QFont.Monospace)
    app.setFont(font)

    chosen_language = _compute_startup_language()
    try:
        from gui_i18n import install as install_i18n
        install_i18n(app, lang=chosen_language)
    except Exception as e:
        print(f"[gui_i18n] install failed: {e}")
    _autodetect_locale_if_unset()

    _splash("Building main window...")
    window = MainWindow()
    _splash_close()
    window.show()

    try:
        from gui_i18n import needs_language_picker
        if needs_language_picker():
            pending_autoload = getattr(window, "_pending_autoload_path", None)
            window._pending_autoload_path = None
            try:
                from gui.language_picker import LanguagePickerDialog
                LanguagePickerDialog.run(
                    window,
                    config_path=_editor_config_path(),
                    config=window._config,
                    blocking=True,
                )
                try:
                    window._save_config()
                except Exception:
                    pass
            except Exception as e:
                print(f"[lang] first-run picker failed: {e}")
            if pending_autoload:
                from PySide6.QtCore import QTimer
                QTimer.singleShot(0, lambda p=pending_autoload: window._load_save(p))
    except Exception as e:
        print(f"[lang] first-run hookup failed: {e}")

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

    exit_code = app.exec()
    if harvest_i18n:
        try:
            from gui_i18n import dump_harvest
            out = os.path.join(
                os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
                else os.path.dirname(os.path.abspath(__file__)),
                "harvest_missing.json",
            )
            dump_harvest(out)
            print(f"[gui_i18n] wrote harvest -> {out}")
        except Exception as e:
            print(f"[gui_i18n] harvest dump failed: {e}")
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
