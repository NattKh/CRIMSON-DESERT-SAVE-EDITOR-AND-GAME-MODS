from __future__ import annotations

import json
import os
import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication, QDialog, QLabel, QProgressBar, QVBoxLayout,
)

from theme_support import (
    DEFAULT_STARTUP_SPLASH_TITLE,
    get_theme_definition,
    normalize_startup_splash_title,
)


def _relative_luminance(color: str) -> float:
    value = str(color).lstrip("#")
    if len(value) != 6:
        return 0.0
    channels = [int(value[index:index + 2], 16) / 255 for index in (0, 2, 4)]
    channels = [
        channel / 12.92 if channel <= 0.04045
        else ((channel + 0.055) / 1.055) ** 2.4
        for channel in channels
    ]
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def _readable_color(preferred: str, background: str, fallback: str) -> str:
    lighter = max(_relative_luminance(preferred), _relative_luminance(background))
    darker = min(_relative_luminance(preferred), _relative_luminance(background))
    return preferred if (lighter + 0.05) / (darker + 0.05) >= 4.5 else fallback


def startup_config_path() -> str:
    if getattr(sys, "frozen", False):
        base = os.path.dirname(os.path.abspath(sys.executable))
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "editor_config.json")


def load_startup_settings(config_path: str | None = None) -> tuple[str, dict]:
    config = {}
    try:
        with open(config_path or startup_config_path(), "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
            if isinstance(loaded, dict):
                config = loaded
    except (OSError, ValueError, TypeError):
        pass

    title = normalize_startup_splash_title(config.get("startup_splash_title"))
    theme = get_theme_definition(
        config.get("theme", "dark"), config.get("custom_theme_colors", {})
    )
    return title, theme["colors"]


class StartupSplashDialog(QDialog):
    """Theme-aware startup status shown while the main window is constructed."""

    def __init__(self, title: str, colors: dict, version: str):
        super().__init__(None)
        self._colors = dict(colors)
        self.setObjectName("startupSplash")
        self.setWindowTitle(normalize_startup_splash_title(title))
        self.setWindowFlags(
            Qt.WindowType.SplashScreen
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setFixedSize(500, 240)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(8)

        self._title = QLabel(self.windowTitle())
        self._title.setObjectName("startupTitle")
        layout.addWidget(self._title)

        self._subtitle = QLabel("Standalone save editing toolkit")
        self._subtitle.setObjectName("startupSubtitle")
        layout.addWidget(self._subtitle)

        self._version = QLabel(f"v{version}")
        self._version.setObjectName("startupVersion")
        layout.addWidget(self._version)

        layout.addStretch(1)
        self._status = QLabel("Starting up...")
        self._status.setObjectName("startupStatus")
        layout.addWidget(self._status)

        self._progress = QProgressBar()
        self._progress.setObjectName("startupProgress")
        self._progress.setRange(0, 100)
        self._progress.setValue(5)
        self._progress.setTextVisible(False)
        layout.addWidget(self._progress)

        self.setStyleSheet(self._build_stylesheet())

    def _build_stylesheet(self) -> str:
        colors = self._colors
        dim_text = _readable_color(colors["text_dim"], colors["bg"], colors["text"])
        version_text = _readable_color(colors["accent"], colors["bg"], colors["text"])
        return f"""
            QDialog#startupSplash {{
                background-color: {colors['bg']};
                border: 2px solid {colors['accent']};
                color: {colors['text']};
            }}
            QLabel {{ background: transparent; color: {colors['text']}; }}
            QLabel#startupTitle {{ font-size: 18px; font-weight: bold; }}
            QLabel#startupSubtitle {{ color: {dim_text}; }}
            QLabel#startupVersion {{ color: {version_text}; font-weight: bold; }}
            QLabel#startupStatus {{ font-weight: bold; }}
            QProgressBar#startupProgress {{
                min-height: 8px; max-height: 8px;
                background-color: {colors['input_bg']};
                border: 1px solid {colors['border']};
                border-radius: 4px;
            }}
            QProgressBar#startupProgress::chunk {{
                background-color: {colors['accent']};
                border-radius: 3px;
            }}
        """

    def show_centered(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is not None:
            frame = self.frameGeometry()
            frame.moveCenter(screen.availableGeometry().center())
            self.move(frame.topLeft())
        self.show()
        self.raise_()
        QApplication.processEvents()

    def set_status(self, text: str, progress: int | None = None) -> None:
        self._status.setText(str(text))
        if progress is None:
            progress = min(92, self._progress.value() + 12)
        self._progress.setValue(max(0, min(100, int(progress))))
        QApplication.processEvents()


_active_splash: StartupSplashDialog | None = None


def set_active_startup_splash(splash: StartupSplashDialog | None) -> None:
    global _active_splash
    _active_splash = splash


def update_startup_status(text: str, progress: int | None = None) -> None:
    if _active_splash is not None:
        _active_splash.set_status(text, progress)


def create_startup_splash(version: str) -> StartupSplashDialog:
    title, colors = load_startup_settings()
    return StartupSplashDialog(title, colors, version)
