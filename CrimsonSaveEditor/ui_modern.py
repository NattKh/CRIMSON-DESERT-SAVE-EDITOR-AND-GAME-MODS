"""
Modern UI components for Crimson Save Editor.
Provides a clean, consistent design system and reusable widgets.
"""
from PySide6.QtCore import Qt, Signal, QSize
from PySide6.QtWidgets import (
    QWidget, QLabel, QPushButton, QVBoxLayout, QHBoxLayout,
    QFrame, QScrollArea, QApplication
)
from PySide6.QtGui import QFont, QPainter, QPalette, QColor

# =============================================================================
# DESIGN SYSTEM
# =============================================================================

# Dark theme color palette - professional desktop application feel
DS = {
    # Backgrounds
    'bg': '#0f0f14',
    'sidebar_bg': '#18181f',
    'surface': '#1e1e26',
    'surface_raised': '#262630',
    'surface_hover': '#2d2d3a',

    # Text
    'text_primary': '#e8e8ed',
    'text_secondary': '#9898a4',
    'text_muted': '#606070',

    # Accent - warm coral/orange
    'accent': '#e07050',
    'accent_hover': '#f08060',
    'accent_dim': '#5a4035',

    # Borders
    'border': '#303040',
    'border_hover': '#404055',

    # States
    'selected': '#353545',
    'hover': '#28283a',
    'sidebar_selected': '#252535',

    # Semantic
    'success': '#50b070',
    'warning': '#e0a040',
    'error': '#e05050',
    'info': '#5090d0',

    # Interactive
    'button_bg': '#252530',
    'button_hover': '#303040',
    'input_bg': '#1a1a22',
    'input_border': '#353545',
}

# Sidebar width
SIDEBAR_WIDTH = 240

# Spacing scale (8px base)
SP = {
    'xs': 4,
    'sm': 8,
    'md': 12,
    'lg': 16,
    'xl': 24,
    '2xl': 32,
}

# Border radius scale
BR = {
    'sm': 4,
    'md': 6,
    'lg': 8,
    'xl': 12,
}

# Font sizes
FS = {
    'xs': 10,
    'sm': 11,
    'md': 12,
    'lg': 14,
    'xl': 16,
    '2xl': 18,
}


# =============================================================================
# REUSABLE COMPONENTS
# =============================================================================

class SidebarItem(QWidget):
    """A clickable item in the sidebar navigation."""

    clicked = Signal(str)

    def __init__(self, icon: str, label: str, item_id: str, parent=None, indent: int = 0):
        super().__init__(parent)
        self._id = item_id
        self._icon = icon
        self._label = label
        self._indent = indent * 16
        self._selected = False
        self._hovered = False
        self._setup_ui()

    def _setup_ui(self):
        self.setFixedHeight(40)
        self.setCursor(Qt.PointingHandCursor)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16 + self._indent, 0, 16, 0)
        layout.setSpacing(10)

        # Icon
        self._icon_label = QLabel(self._icon, self)
        self._icon_label.setStyleSheet("background: transparent; border: none; font-size: 14px;")
        self._icon_label.setFixedWidth(20)
        layout.addWidget(self._icon_label)

        # Label
        self._text_label = QLabel(self._label, self)
        self._text_label.setStyleSheet(
            f"background: transparent; border: none; "
            f"color: {DS['text_secondary']}; font-size: {FS['sm']}px;"
        )
        layout.addWidget(self._text_label, 1)

        self._update_style()

    def _update_style(self):
        if self._selected:
            bg = DS['sidebar_selected']
            border = f"border-left: 3px solid {DS['accent']};"
            text_color = DS['text_primary']
            font_weight = "bold"
        elif self._hovered:
            bg = DS['hover']
            border = "border-left: 3px solid transparent;"
            text_color = DS['text_primary']
            font_weight = "normal"
        else:
            bg = "transparent"
            border = "border-left: 3px solid transparent;"
            text_color = DS['text_secondary']
            font_weight = "normal"

        self.setStyleSheet(f"""
            QWidget {{
                background: {bg};
                {border}
            }}
        """)
        self._text_label.setStyleSheet(
            f"background: transparent; border: none; "
            f"color: {text_color}; font-size: {FS['sm']}px; font-weight: {font_weight};"
        )

    def set_selected(self, selected: bool):
        self._selected = selected
        self._update_style()

    def is_selected(self) -> bool:
        return self._selected

    def enterEvent(self, event):
        self._hovered = True
        self._update_style()
        return super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = False
        self._update_style()
        return super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self._id)
        return super().mousePressEvent(event)


class SidebarSection(QWidget):
    """A collapsible section in the sidebar with a header and items."""

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self._title = title
        self._collapsed = False
        self._items = []
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(2)

        # Header
        header = QWidget(self)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(16, 4, 16, 4)
        header_layout.setSpacing(6)

        self._arrow = QLabel("▼", header)
        self._arrow.setStyleSheet(f"color: {DS['text_muted']}; font-size: 8px; background: transparent;")
        self._arrow.setFixedWidth(12)
        header_layout.addWidget(self._arrow)

        self._title_label = QLabel(self._title.upper(), header)
        self._title_label.setStyleSheet(f"""
            color: {DS['text_muted']};
            font-size: {FS['xs']}px;
            font-weight: bold;
            letter-spacing: 0.5px;
            background: transparent;
        """)
        header_layout.addWidget(self._title_label, 1)

        layout.addWidget(header)

        # Items container
        self._items_widget = QWidget(self)
        self._items_layout = QVBoxLayout(self._items_widget)
        self._items_layout.setContentsMargins(0, 0, 0, 0)
        self._items_layout.setSpacing(0)
        layout.addWidget(self._items_widget)

    def add_item(self, item: SidebarItem):
        self._items.append(item)
        self._items_layout.addWidget(item)

    def add_stretch(self):
        self._items_layout.addStretch()

    def set_collapsed(self, collapsed: bool):
        self._collapsed = collapsed
        self._items_widget.setVisible(not collapsed)
        self._arrow.setText("▶" if collapsed else "▼")


class PageHeader(QWidget):
    """Header for a content page with title and optional actions."""

    def __init__(self, title: str, subtitle: str = "", parent=None):
        super().__init__(parent)
        self._title = title
        self._subtitle = subtitle
        self._setup_ui()

    def _setup_ui(self):
        self.setFixedHeight(56)
        self.setStyleSheet(f"background: {DS['surface']}; border-bottom: 1px solid {DS['border']};")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(24, 0, 24, 0)
        layout.setSpacing(16)

        # Title section
        title_layout = QVBoxLayout()
        title_layout.setSpacing(2)

        self._title_label = QLabel(self._title, self)
        self._title_label.setStyleSheet(f"""
            color: {DS['text_primary']};
            font-size: {FS['xl']}px;
            font-weight: bold;
            background: transparent;
        """)
        title_layout.addWidget(self._title_label)

        if self._subtitle:
            self._subtitle_label = QLabel(self._subtitle, self)
            self._subtitle_label.setStyleSheet(f"""
                color: {DS['text_muted']};
                font-size: {FS['sm']}px;
                background: transparent;
            """)
            title_layout.addWidget(self._subtitle_label)
        else:
            self._subtitle_label = None

        layout.addLayout(title_layout, 1)

        # Actions container
        self._actions_layout = QHBoxLayout()
        self._actions_layout.setSpacing(8)
        layout.addLayout(self._actions_layout)

    def add_action(self, widget: QWidget):
        self._actions_layout.addWidget(widget)


class StatusBar(QWidget):
    """Status bar widget for the bottom of the window."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._setup_ui()

    def _setup_ui(self):
        self.setFixedHeight(32)
        self.setStyleSheet(f"""
            background: {DS['sidebar_bg']};
            border-top: 1px solid {DS['border']};
        """)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 0, 16, 0)
        layout.setSpacing(12)

        self._status_label = QLabel("Ready", self)
        self._status_label.setStyleSheet(f"""
            color: {DS['text_secondary']};
            font-size: {FS['xs']}px;
            background: transparent;
        """)
        layout.addWidget(self._status_label, 1)

        # Spacer for potential right-side info
        layout.addStretch()

    def set_status(self, text: str, status_type: str = "info"):
        """Set status text with optional type for coloring."""
        colors = {
            "info": DS['text_secondary'],
            "success": DS['success'],
            "warning": DS['warning'],
            "error": DS['error'],
        }
        color = colors.get(status_type, DS['text_secondary'])
        self._status_label.setStyleSheet(f"""
            color: {color};
            font-size: {FS['xs']}px;
            background: transparent;
        """)
        self._status_label.setText(text)


class ModernButton(QPushButton):
    """Stylized button for the modern UI."""

    def __init__(self, text: str = "", variant: str = "default", parent=None):
        super().__init__(text, parent)
        self._variant = variant
        self._setup_style()

    def _setup_style(self):
        if self._variant == "primary":
            self.setStyleSheet(f"""
                QPushButton {{
                    background: {DS['accent']};
                    color: #1a1a1a;
                    font-weight: bold;
                    border: none;
                    border-radius: {BR['md']}px;
                    padding: 8px 16px;
                    font-size: {FS['sm']}px;
                }}
                QPushButton:hover {{
                    background: {DS['accent_hover']};
                }}
                QPushButton:pressed {{
                    background: {DS['accent_dim']};
                }}
                QPushButton:disabled {{
                    background: {DS['accent_dim']};
                    color: {DS['text_muted']};
                }}
            """)
        elif self._variant == "danger":
            self.setStyleSheet(f"""
                QPushButton {{
                    background: {DS['error']};
                    color: white;
                    font-weight: bold;
                    border: none;
                    border-radius: {BR['md']}px;
                    padding: 8px 16px;
                    font-size: {FS['sm']}px;
                }}
                QPushButton:hover {{
                    background: #f06060;
                }}
                QPushButton:disabled {{
                    background: #603030;
                    color: {DS['text_muted']};
                }}
            """)
        else:
            self.setStyleSheet(f"""
                QPushButton {{
                    background: {DS['button_bg']};
                    color: {DS['text_primary']};
                    border: 1px solid {DS['border']};
                    border-radius: {BR['md']}px;
                    padding: 8px 16px;
                    font-size: {FS['sm']}px;
                }}
                QPushButton:hover {{
                    background: {DS['button_hover']};
                    border-color: {DS['border_hover']};
                }}
                QPushButton:pressed {{
                    background: {DS['surface']};
                }}
                QPushButton:disabled {{
                    background: {DS['surface']};
                    color: {DS['text_muted']};
                    border-color: {DS['border']};
                }}
            """)


class ModernToggle(QWidget):
    """A toggle switch widget."""

    def __init__(self, checked: bool = False, parent=None):
        super().__init__(parent)
        self._checked = checked
        self._setup_ui()

    def _setup_ui(self):
        self.setFixedSize(44, 24)
        self.setCursor(Qt.PointingHandCursor)
        self._update_style()

    def _update_style(self):
        track_color = DS['accent'] if self._checked else DS['text_muted']
        knob_color = "#ffffff" if self._checked else DS['text_secondary']

        self.setStyleSheet(f"""
            QWidget {{
                background: {track_color};
                border-radius: 12px;
            }}
        """)

        # Update knob position
        knob_x = 22 if self._checked else 2
        knob = QLabel(self)
        knob.setFixedSize(20, 20)
        knob.move(knob_x, 2)
        knob.setStyleSheet(f"""
            background: {knob_color};
            border-radius: 10px;
        """)
        knob.show()

    def isChecked(self) -> bool:
        return self._checked

    def setChecked(self, checked: bool):
        if self._checked != checked:
            self._checked = checked
            self._update_style()

    def toggle(self):
        self.setChecked(not self._checked)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.toggle()


class ModernInput(QLineEdit):
    """Stylized text input for the modern UI."""

    def __init__(self, placeholder: str = "", parent=None):
        super().__init__(parent)
        self.setPlaceholderText(placeholder)
        self._setup_style()

    def _setup_style(self):
        self.setStyleSheet(f"""
            QLineEdit {{
                background: {DS['input_bg']};
                color: {DS['text_primary']};
                border: 1px solid {DS['input_border']};
                border-radius: {BR['md']}px;
                padding: 8px 12px;
                font-size: {FS['sm']}px;
            }}
            QLineEdit:focus {{
                border-color: {DS['accent']};
            }}
            QLineEdit::placeholder {{
                color: {DS['text_muted']};
            }}
        """)


class ModernSelect(QWidget):
    """Stylized dropdown select widget."""

    def __init__(self, options: list, parent=None):
        super().__init__(parent)
        self._options = options
        self._selected_index = 0
        self._setup_ui()

    def _setup_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._label = QLabel(self._options[0] if self._options else "", self)
        self._label.setStyleSheet(f"""
            background: {DS['input_bg']};
            color: {DS['text_primary']};
            border: 1px solid {DS['input_border']};
            border-radius: {BR['md']}px;
            padding: 8px 12px;
            font-size: {FS['sm']}px;
        """)
        layout.addWidget(self._label, 1)

        self._dropdown_btn = QPushButton("▼", self)
        self._dropdown_btn.setFixedWidth(32)
        self._dropdown_btn.setStyleSheet(f"""
            QPushButton {{
                background: {DS['input_bg']};
                color: {DS['text_secondary']};
                border: 1px solid {DS['input_border']};
                border-left: none;
                border-radius: 0 {BR['md']}px {BR['md']}px 0;
                padding: 8px 4px;
            }}
            QPushButton:hover {{
                background: {DS['hover']};
            }}
        """)
        layout.addWidget(self._dropdown_btn)

        self.setFixedHeight(38)


class Section(QWidget):
    """A section container with a title and content."""

    def __init__(self, title: str = "", parent=None):
        super().__init__(parent)
        self._title = title
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        if self._title:
            title_label = QLabel(self._title, self)
            title_label.setStyleSheet(f"""
                color: {DS['text_secondary']};
                font-size: {FS['xs']}px;
                font-weight: bold;
                text-transform: uppercase;
                letter-spacing: 0.5px;
                background: transparent;
                padding-bottom: 4px;
            """)
            layout.addWidget(title_label)

        self._content = QWidget(self)
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(0, 0, 0, 0)
        self._content_layout.setSpacing(8)
        layout.addWidget(self._content)

    def add_row(self, widget: QWidget):
        """Add a widget as a row in the section."""
        self._content_layout.addWidget(widget)


class SettingRow(QWidget):
    """A row with a label and a control."""

    def __init__(self, label: str, control: QWidget, description: str = "", parent=None):
        super().__init__(parent)
        self._setup_ui(label, control, description)

    def _setup_ui(self, label: str, control: QWidget, description: str):
        self.setFixedHeight(48 if description else 40)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)

        # Label section
        label_layout = QVBoxLayout()
        label_layout.setSpacing(2)

        label_text = QLabel(label, self)
        label_text.setStyleSheet(f"""
            color: {DS['text_primary']};
            font-size: {FS['sm']}px;
            background: transparent;
        """)
        label_layout.addWidget(label_text)

        if description:
            desc_text = QLabel(description, self)
            desc_text.setStyleSheet(f"""
                color: {DS['text_muted']};
                font-size: {FS['xs']}px;
                background: transparent;
            """)
            label_layout.addWidget(desc_text)

        label_layout.addStretch()
        layout.addLayout(label_layout, 1)

        # Control
        layout.addWidget(control)


class DirtyIndicator(QWidget):
    """Indicator showing unsaved changes status."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._dirty = False
        self._setup_ui()

    def _setup_ui(self):
        self.setFixedHeight(24)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 0, 8, 0)
        layout.setSpacing(6)

        self._dot = QLabel("●", self)
        self._dot.setStyleSheet(f"""
            color: {DS['text_muted']};
            font-size: 10px;
            background: transparent;
        """)
        layout.addWidget(self._dot)

        self._text = QLabel("", self)
        self._text.setStyleSheet(f"""
            color: {DS['text_muted']};
            font-size: {FS['xs']}px;
            background: transparent;
        """)
        layout.addWidget(self._text)

        self._update_style()

    def _update_style(self):
        if self._dirty:
            self.setStyleSheet(f"""
                background: {DS['warning']}20;
                border: 1px solid {DS['warning']}40;
                border-radius: {BR['sm']}px;
            """)
            self._dot.setStyleSheet(f"""
                color: {DS['warning']};
                font-size: 10px;
                background: transparent;
            """)
            self._text.setStyleSheet(f"""
                color: {DS['warning']};
                font-size: {FS['xs']}px;
                background: transparent;
            """)
            self._text.setText("Unsaved changes")
        else:
            self.setStyleSheet(f"""
                background: transparent;
                border: none;
            """)
            self._dot.setStyleSheet(f"""
                color: {DS['text_muted']};
                font-size: 10px;
                background: transparent;
            """)
            self._text.setStyleSheet(f"""
                color: {DS['text_muted']};
                font-size: {FS['xs']}px;
                background: transparent;
            """)
            self._text.setText("")

    def set_dirty(self, dirty: bool):
        self._dirty = dirty
        self._update_style()

    def is_dirty(self) -> bool:
        return self._dirty
