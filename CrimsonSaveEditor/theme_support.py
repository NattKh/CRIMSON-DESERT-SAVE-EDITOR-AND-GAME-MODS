from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QColorDialog, QDialog, QDialogButtonBox, QFrame, QGridLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QPushButton,
    QVBoxLayout,
)


DARK_COLORS = {
    "bg": "#1a1510", "panel": "#272018", "header": "#3d2e1a",
    "accent": "#daa850", "text": "#f0e6d4", "text_dim": "#b0a088",
    "selected": "#5c4320", "border": "#554430", "input_bg": "#1e1610",
    "panel_text": "#f0e6d4", "primary_text": "#f0e6d4", "accent_text": "#111111", "selection_text": "#FFFFFF", "success": "#9cc470", "warning": "#f0b040", "error": "#d44f40",
}
LIGHT_COLORS = {
    "bg": "#f5f5f5", "panel": "#ffffff", "header": "#d8d8d8",
    "accent": "#8B3A00", "text": "#111111", "text_dim": "#3d3d3d",
    "selected": "#b8d9ff", "border": "#a0a0a0", "input_bg": "#ffffff",
    "panel_text": "#111111", "primary_text": "#111111", "accent_text": "#FFFFFF", "selection_text": "#111111", "success": "#1b5e20", "warning": "#bf5500", "error": "#b71c1c",
}


def _contrast_text_raw(background):
    value = str(background).lstrip("#")
    red, green, blue = (int(value[i:i + 2], 16) for i in (0, 2, 4))
    return "#111111" if (red * 299 + green * 587 + blue * 114) / 1000 >= 150 else "#FFFFFF"


def _palette(bg, panel, header, accent, text, dim, selected, border, input_bg,
             success="#78d49a", warning="#e6ba59", error="#e26972"):
    return dict(bg=bg, panel=panel, header=header, accent=accent, text=text,
                text_dim=dim, selected=selected, border=border, input_bg=input_bg,
                panel_text=_contrast_text_raw(panel),
                primary_text=_contrast_text_raw(header),
                accent_text=_contrast_text_raw(accent),
                selection_text=_contrast_text_raw(selected), success=success,
                warning=warning, error=error)


THEME_PRESETS = {
    "dark": {"name": "Classic Gold", "description": "The original warm brown and gold Crimson theme.",
             "colors": DARK_COLORS, "tab": ("#2a3040", "#e0eaff", "#70a8ff")},
    "obsidian_blue": {"name": "Obsidian Blue", "description": "Deep navy panels with a clean electric-blue accent.",
        "colors": _palette("#0b111b", "#111c2b", "#172a42", "#4da3ff", "#e8f2ff", "#91a8c2", "#234d75", "#2e4968", "#0d1724"),
        "tab": ("#193b5d", "#e8f4ff", "#4da3ff")},
    "forest_emerald": {"name": "Forest Emerald", "description": "A subdued woodland palette with emerald highlights.",
        "colors": _palette("#0d1712", "#15241b", "#203a2a", "#55c983", "#e6f4e9", "#9ab6a1", "#285f3e", "#365c43", "#101c15"),
        "tab": ("#214e34", "#edfff2", "#55c983")},
    "royal_purple": {"name": "Royal Purple", "description": "Dark violet surfaces with bright amethyst accents.",
        "colors": _palette("#15101d", "#21182d", "#352348", "#b67cff", "#f2eaff", "#b6a3ca", "#57357b", "#574069", "#191222"),
        "tab": ("#482b66", "#f6edff", "#b67cff")},
    "crimson_night": {"name": "Crimson Night", "description": "Near-black burgundy with vivid crimson controls.",
        "colors": _palette("#170c0f", "#281318", "#431d24", "#e05262", "#f8e9eb", "#c0a0a5", "#702b37", "#63323a", "#1d0e12"),
        "tab": ("#5a222d", "#fff0f2", "#e05262")},
    "slate_cyan": {"name": "Slate Cyan", "description": "Neutral charcoal-slate panels with cool cyan highlights.",
        "colors": _palette("#101619", "#182327", "#24383e", "#4fc6c8", "#e6f3f3", "#9cb5b7", "#286267", "#36575c", "#121c20"),
        "tab": ("#214f54", "#efffff", "#4fc6c8")},
    "light": {"name": "Daylight", "description": "A bright, high-contrast theme for daytime use.",
              "colors": LIGHT_COLORS, "tab": ("#cfe3ff", "#103060", "#2277dd")},
}

DEFAULT_CUSTOM_COLORS = {
    "primary": DARK_COLORS["header"], "accent": DARK_COLORS["accent"],
    "background": DARK_COLORS["bg"],
}

DEFAULT_POPUP_BRANDING = "Crimson Save Editor Enhanced Update"
MAX_POPUP_BRANDING_LENGTH = 100
DEFAULT_STARTUP_SPLASH_TITLE = DEFAULT_POPUP_BRANDING


def normalize_popup_branding(value) -> str:
    """Return safe, non-empty branding text for the loading popup."""
    if not isinstance(value, str):
        return DEFAULT_POPUP_BRANDING
    value = value.strip()
    if value in {"Crimson Save Editor - JY's Version", "Crimson Save Editor — JY’s Version"}:
        return DEFAULT_POPUP_BRANDING
    if (not value or len(value) > MAX_POPUP_BRANDING_LENGTH
            or any(ord(character) < 32 for character in value)):
        return DEFAULT_POPUP_BRANDING
    return value


def normalize_startup_splash_title(value) -> str:
    return normalize_popup_branding(value)


def normalize_hex(value: str, fallback: str) -> str:
    value = str(value or "").strip()
    if not value.startswith("#"):
        value = "#" + value
    if len(value) == 4 and all(ch in "0123456789abcdefABCDEF" for ch in value[1:]):
        value = "#" + "".join(ch * 2 for ch in value[1:])
    if len(value) != 7 or any(ch not in "0123456789abcdefABCDEF" for ch in value[1:]):
        return fallback
    return value.upper()


def normalize_custom_colors(values: dict | None) -> dict:
    values = values or {}
    return {key: normalize_hex(values.get(key, default), default)
            for key, default in DEFAULT_CUSTOM_COLORS.items()}


def _rgb(value):
    value = normalize_hex(value, "#000000")
    return tuple(int(value[i:i + 2], 16) for i in (1, 3, 5))


def contrast_text(background):
    """Return readable button text for any user-selected background color."""
    return _contrast_text_raw(normalize_hex(background, "#000000"))


def _hex(rgb):
    return "#{:02X}{:02X}{:02X}".format(*(max(0, min(255, n)) for n in rgb))


def _blend(a, b, weight):
    return _hex(tuple(round(x * (1 - weight) + y * weight) for x, y in zip(_rgb(a), _rgb(b))))


def build_custom_palette(values):
    custom = normalize_custom_colors(values)
    primary, accent, background = custom["primary"], custom["accent"], custom["background"]
    r, g, b = (v / 255 for v in _rgb(background))
    light = 0.2126 * r + 0.7152 * g + 0.0722 * b > 0.58
    text = "#111111" if light else "#F4F4F4"
    semantic = LIGHT_COLORS if light else DARK_COLORS
    return _palette(background, _blend(background, primary, .18), primary, accent,
                    text, _blend(text, background, .38), _blend(primary, accent, .42),
                    _blend(primary, accent, .24), _blend(background, primary, .10),
                    semantic["success"], semantic["warning"], semantic["error"])


def resolve_theme_key(mode):
    key = {"classic": "dark", "default": "dark"}.get(mode, mode)
    return key if key in THEME_PRESETS or key == "custom" else "dark"


def get_theme_definition(mode, custom_colors=None):
    key = resolve_theme_key(mode)
    if key == "custom":
        colors = build_custom_palette(custom_colors)
        return {"name": "Custom Colors", "description": "Your saved primary, accent, and background colors.",
                "colors": colors, "tab": (colors["selected"], colors["text"], colors["accent"])}
    return THEME_PRESETS[key]


class AppearanceDialog(QDialog):
    """Preset browser with reversible live preview, matching the game-mod tool."""

    def __init__(self, current_theme, custom_colors, preview_fn, parent=None,
                 startup_splash_title=None, save_load_popup_title=None):
        super().__init__(parent)
        self.setWindowTitle("Appearance & Color Presets")
        self.setMinimumSize(780, 600)
        self._original = resolve_theme_key(current_theme)
        self._selected = self._original
        self._custom_colors = normalize_custom_colors(custom_colors)
        self._original_custom = dict(self._custom_colors)
        self._preview_fn = preview_fn
        root = QVBoxLayout(self)
        title = QLabel("Choose an application color preset")
        title.setStyleSheet("font-size: 17px; font-weight: bold; padding: 4px;")
        root.addWidget(title)
        root.addWidget(QLabel("Select a preset to preview it immediately. It is saved only when you press OK."))
        body = QHBoxLayout()
        self._list = QListWidget(); self._list.setMinimumWidth(220)
        for key, preset in THEME_PRESETS.items():
            item = QListWidgetItem(preset["name"]); item.setData(Qt.UserRole, key); self._list.addItem(item)
            if key == self._original: self._list.setCurrentItem(item)
        item = QListWidgetItem("Custom Colors"); item.setData(Qt.UserRole, "custom"); self._list.addItem(item)
        if self._original == "custom": self._list.setCurrentItem(item)
        self._list.currentItemChanged.connect(self._selection_changed)
        body.addWidget(self._list)
        right = QVBoxLayout(); self._description = QLabel(); self._description.setWordWrap(True); right.addWidget(self._description)
        self._preview = QFrame(); self._preview.setMinimumHeight(250); pv = QVBoxLayout(self._preview)
        self._preview_header = QLabel("PRESET PREVIEW"); self._preview_header.setAlignment(Qt.AlignCenter); pv.addWidget(self._preview_header)
        self._preview_panel = QFrame(); pp = QVBoxLayout(self._preview_panel)
        self._sample_title = QLabel("Crimson Desert Save Editor"); self._sample_title.setStyleSheet("font-size: 16px; font-weight: bold;"); pp.addWidget(self._sample_title)
        pp.addWidget(QLabel("Tables, panels, buttons, selections, and accent colors"))
        row = QHBoxLayout(); self._sample_primary = QPushButton("Primary Action"); self._sample_secondary = QPushButton("Secondary")
        row.addWidget(self._sample_primary); row.addWidget(self._sample_secondary); pp.addLayout(row)
        self._swatches = QLabel(); pp.addWidget(self._swatches); pv.addWidget(self._preview_panel); right.addWidget(self._preview, 1)
        custom = QGroupBox("Custom Theme Colors"); grid = QGridLayout(custom); self._color_edits = {}
        for row, (key, label) in enumerate((("primary", "Primary"), ("accent", "Accent"), ("background", "Background"))):
            grid.addWidget(QLabel(label + ":"), row, 0)
            edit = QLineEdit(self._custom_colors[key]); edit.setMaxLength(7); edit.setPlaceholderText("#RRGGBB")
            edit.editingFinished.connect(lambda k=key: self._hex_edited(k)); self._color_edits[key] = edit; grid.addWidget(edit, row, 1)
            pick = QPushButton("Pick Color..."); pick.clicked.connect(lambda _=False, k=key: self._pick_color(k)); grid.addWidget(pick, row, 2)
        copy = QPushButton("Use Selected Preset as Starting Colors"); copy.clicked.connect(self._copy_selected_preset); grid.addWidget(copy, 3, 0, 1, 2)
        reset = QPushButton("Reset Custom Values"); reset.clicked.connect(self._reset_custom); grid.addWidget(reset, 3, 2); right.addWidget(custom)
        branding = QGroupBox("Loading Popup Branding")
        branding_layout = QGridLayout(branding)
        branding_layout.addWidget(QLabel("EXE startup splash:"), 0, 0)
        self._startup_title_edit = QLineEdit(
            normalize_startup_splash_title(startup_splash_title)
        )
        self._startup_title_edit.setMaxLength(MAX_POPUP_BRANDING_LENGTH)
        self._startup_title_edit.setPlaceholderText(DEFAULT_STARTUP_SPLASH_TITLE)
        branding_layout.addWidget(self._startup_title_edit, 0, 1)
        branding_layout.addWidget(QLabel("Save Browser loading popup:"), 1, 0)
        self._save_load_title_edit = QLineEdit(
            normalize_popup_branding(save_load_popup_title)
        )
        self._save_load_title_edit.setMaxLength(MAX_POPUP_BRANDING_LENGTH)
        self._save_load_title_edit.setPlaceholderText(DEFAULT_POPUP_BRANDING)
        branding_layout.addWidget(self._save_load_title_edit, 1, 1)
        right.addWidget(branding)
        body.addLayout(right, 1); root.addLayout(body, 1)
        bottom = QHBoxLayout(); classic = QPushButton("Reset to Classic Gold"); classic.clicked.connect(lambda: self._select("dark")); bottom.addWidget(classic); bottom.addStretch()
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel); buttons.accepted.connect(self.accept); buttons.rejected.connect(self.reject); bottom.addWidget(buttons); root.addLayout(bottom)
        self._update_preview(self._original)

    @property
    def selected_theme(self): return self._selected
    @property
    def custom_colors(self): return dict(self._custom_colors)
    @property
    def startup_splash_title(self):
        return normalize_startup_splash_title(self._startup_title_edit.text())
    @property
    def save_load_popup_title(self):
        return normalize_popup_branding(self._save_load_title_edit.text())

    def _select(self, key):
        for i in range(self._list.count()):
            if self._list.item(i).data(Qt.UserRole) == key:
                self._list.setCurrentRow(i); return

    def _selection_changed(self, current, _previous=None):
        if current is None: return
        self._selected = current.data(Qt.UserRole); self._update_preview(self._selected); self._preview_fn(self._selected, self._custom_colors)

    def _update_preview(self, key):
        preset = get_theme_definition(key, self._custom_colors); c = preset["colors"]
        self._description.setText(f'{preset["name"]}\n\n{preset["description"]}')
        self._preview.setStyleSheet(f"QFrame {{ background:{c['bg']}; border:1px solid {c['border']}; border-radius:6px; }}")
        self._preview_header.setStyleSheet(f"background:{c['header']}; color:{c['accent']}; padding:10px; font-weight:bold;")
        self._preview_panel.setStyleSheet(f"QFrame {{ background:{c['panel']}; color:{c['text']}; padding:10px; }} QLabel {{ color:{c['text']}; border:none; }}")
        self._sample_primary.setStyleSheet(f"background:{c['accent']}; color:{c['accent_text']}; padding:8px;")
        self._sample_secondary.setStyleSheet(f"background:{c['header']}; color:{c['primary_text']}; border:1px solid {c['border']}; padding:8px;")
        self._swatches.setText(" ".join(f"<span style='background:{v};color:{v};'>###</span>" for v in (c['bg'], c['panel'], c['header'], c['accent'], c['selected'], c['text'])))

    def _hex_edited(self, key):
        edit = self._color_edits[key]; value = normalize_hex(edit.text(), self._custom_colors[key]); edit.setText(value); self._custom_colors[key] = value; self._select("custom")

    def _pick_color(self, key):
        color = QColorDialog.getColor(QColor(self._custom_colors[key]), self, f"Choose {key.title()} Color")
        if color.isValid():
            self._custom_colors[key] = color.name().upper(); self._color_edits[key].setText(self._custom_colors[key]); self._select("custom")

    def _copy_selected_preset(self):
        c = get_theme_definition(self._selected, self._custom_colors)["colors"]
        self._custom_colors = {"primary": c["header"], "accent": c["accent"], "background": c["bg"]}
        for key, edit in self._color_edits.items(): edit.setText(self._custom_colors[key])
        self._select("custom")

    def _reset_custom(self):
        self._custom_colors = dict(DEFAULT_CUSTOM_COLORS)
        for key, edit in self._color_edits.items(): edit.setText(self._custom_colors[key])
        self._select("custom")

    def reject(self):
        self._preview_fn(self._original, self._original_custom)
        super().reject()
