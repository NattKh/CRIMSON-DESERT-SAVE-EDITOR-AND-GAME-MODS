from __future__ import annotations

import datetime
import json
import logging
import os
import shutil
import struct
import sys
import traceback

log = logging.getLogger(__name__)
from typing import List, Optional, Tuple

from PySide6.QtCore import Qt, QTimer, QSortFilterProxyModel, Signal, QSize
from PySide6.QtGui import (
    QAction, QActionGroup, QColor, QFont, QIcon, QKeySequence, QBrush, QShortcut,
)
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QTabWidget, QTableWidget, QTableWidgetItem, QHeaderView,
    QLabel, QPushButton, QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox,
    QFileDialog, QMessageBox, QStatusBar, QMenuBar, QMenu,
    QGroupBox, QSplitter, QFrame, QAbstractItemView,
    QListWidget, QListWidgetItem, QDialog, QDialogButtonBox,
    QProgressBar, QTextEdit, QCheckBox, QApplication, QDockWidget,
)

from models import SaveItem, SaveData, UndoEntry, QuestState
from save_crypto import load_save_file, load_raw_stream, write_save_file
from item_scanner import (
    scan_items, apply_stack_edit, apply_enchant_edit,
    apply_endurance_edit, apply_sharpness_edit, apply_item_swap, apply_item_swap_all,
    enrich_items_with_parc, smart_item_swap,
    apply_itemno_edit, get_max_itemno,
)
from item_db import ItemNameDB
from equipment_sets import SetManager, EquipmentSet, SetItem, StatOperation
from item_packs import PackManager, ItemPack, PackItem, PackIndexEntry
from paz_patcher import (
    PazPatchManager, PazPatch, get_all_patches,
    ItemBuffPatcher, ItemRecord, StatTriplet, BUFF_HASHES, BUFF_NAMES,
    VehiclePatcher, StoragePatcher, ItemEffectPatcher,
)
try:
    from parc_inserter3 import insert_item_to_inventory, insert_item_to_store, clone_block_section, insert_items_batch
except Exception:
    insert_item_to_inventory = insert_item_to_store = clone_block_section = insert_items_batch = None
from updater import APP_VERSION, check_for_update, download_update, apply_update_and_restart
from icon_cache import IconCache, ICON_SIZE
from localization import tr, set_language, get_language, get_available_languages


from gui.theme import (
    COLORS, CATEGORY_COLORS, _TAB_SELECTED_BG, _TAB_SELECTED_COLOR,
    _TAB_SELECTED_BORDER, DARK_STYLESHEET, LIGHT_STYLESHEET, apply_theme,
)
from gui.utils import _num_item


def find_save_files() -> List[dict]:
    results = []
    local_app = os.environ.get("LOCALAPPDATA", "")

    search_dirs = []
    if local_app:
        steam_dir = os.path.join(local_app, "Pearl Abyss", "CD", "save")
        epic_dir = os.path.join(local_app, "Pearl Abyss", "CD_Epic", "save")
        search_dirs.extend([steam_dir, epic_dir])

    if sys.platform != "win32":
        import pathlib
        user = os.getenv("USER", "")
        if user:
            linux_steam = pathlib.Path(
                f"/home/{user}/.local/share/Steam/steamapps/compatdata"
                f"/3321460/pfx/drive_c/users/steamuser/AppData/Local"
                f"/Pearl Abyss/CD/save"
            )
            if linux_steam.is_dir():
                search_dirs.append(str(linux_steam))

    for base_dir in search_dirs:
        if not os.path.isdir(base_dir):
            continue
        for user_id in os.listdir(base_dir):
            user_dir = os.path.join(base_dir, user_id)
            if not os.path.isdir(user_dir):
                continue
            for slot in os.listdir(user_dir):
                slot_dir = os.path.join(user_dir, slot)
                save_path = os.path.join(slot_dir, "save.save")
                if os.path.isfile(save_path):
                    mtime = os.path.getmtime(save_path)
                    size = os.path.getsize(save_path)
                    platform = "Steam" if "CD" in base_dir and "Epic" not in base_dir else "Epic"
                    results.append({
                        "path": save_path,
                        "user_id": user_id,
                        "slot": slot,
                        "platform": platform,
                        "mtime": mtime,
                        "size": size,
                        "display": (
                            f"[{platform}] {user_id}/{slot} - "
                            f"{datetime.datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M:%S')} "
                            f"({size:,} bytes)"
                        ),
                    })

    results.sort(key=lambda x: x["mtime"], reverse=True)
    return results


from gui.tabs.items import DatabaseBrowserTab
from gui.tabs.buffs_v319 import ItemBuffsTab
from gui.tabs.stacker import StackerTab
try:
    from gui.tabs.dmm_webview import DmmWebViewTab, HAS_WEBENGINE
except ImportError:
    DmmWebViewTab = None
    HAS_WEBENGINE = False
from iteminfo_reader import ItemInfoCache
from gui.tabs.world import (
    DropsetTab, SpawnTab, StoreEditorTab,
)
from gui.tabs.patches import GamePatchesTab
from gui.tabs.field_edit import FieldEditTab
from gui.tabs.bagspace import BagSpaceTab
from gui.tabs.skill_tree import SkillTreeTab
from gui.tabs.reserveslot import ReserveSlotTab
from gui.tabs.load_manager import LoadManagerTab
from gui.dialogs import (
    _FloatingTabWindow, DetachableTabWidget,
    GiveItemDialog, AddItemDialog, QuestEditorWindow,
    DescriptionSearchDialog, ItemSearchDialog,
    ApplyPackDialog, CreatePackDialog,
)


def _enable_drag_drop_under_uipi(hwnd: int) -> None:
    """Allow drag-drop from unelevated Explorer into this elevated window.

    Windows UIPI (User Interface Privilege Isolation) blocks messages
    from lower-integrity processes to higher-integrity ones by default.
    When the app is launched via "Run as administrator" and the user
    drags from a regular Explorer, the drag enters but the drop is
    silently filtered. Calling ChangeWindowMessageFilterEx with
    MSGFLT_ALLOW on the relevant messages lets them through.

    The three messages that matter:
      WM_DROPFILES     (0x0233) — legacy file-drop
      WM_COPYDATA      (0x004A) — used internally by some drag handlers
      WM_COPYGLOBALDATA(0x0049) — undocumented, required by OLE drop

    No-op on non-Windows platforms. No-op if user32.dll can't be loaded.
    """
    if not sys.platform.startswith("win"):
        return
    import ctypes
    user32 = ctypes.windll.user32
    # The function is available on Vista+ and still present on Win 11.
    try:
        func = user32.ChangeWindowMessageFilterEx
    except AttributeError:
        return
    MSGFLT_ALLOW = 1
    for msg in (0x0233, 0x004A, 0x0049):
        # Signature: BOOL ChangeWindowMessageFilterEx(HWND, UINT, DWORD, PCHANGEFILTERSTRUCT)
        # 4th arg is optional — pass NULL. Return value ignored; if the
        # call fails (e.g. running unelevated, where UIPI doesn't
        # apply), the message was never filtered to begin with.
        func(ctypes.c_void_p(hwnd), msg, MSGFLT_ALLOW, None)


class MainWindow(QMainWindow):

    _icon_ready = Signal(int)
    _parc_step = Signal(int)
    _parc_done = Signal(int, str)
    _parse_cache_ready = Signal()

    _CONFIG_FILE = "editor_config.json"

    def __init__(self) -> None:
        super().__init__()
        try:
            from updater import APP_VARIANT as _variant
        except Exception:
            _variant = "full"
        if _variant == "gamemods":
            self.setWindowTitle("Crimson Desert - Game Mods")
        elif _variant == "standalone":
            self.setWindowTitle("Crimson Desert - Save Editor")
        else:
            self.setWindowTitle("Crimson Desert - Save Editor & Game Mods")
        self._ui_scale = 1.0
        self.resize(1400, 800)
        self.setMinimumSize(800, 500)

        self._save_data: Optional[SaveData] = None
        self._items: List[SaveItem] = []
        self._name_db = ItemNameDB()
        self._pack_mgr = PackManager()
        self._set_mgr = SetManager()
        self._icon_cache = IconCache()

        self._max_enchant_map: dict = {}
        try:
            from data_db import get_connection as _get_db
            self._max_enchant_map = {
                str(row['item_key']): row['max_level']
                for row in _get_db().execute(
                    "SELECT item_key, max_level FROM item_enchant_limits"
                )
            }
        except Exception:
            pass
        self._icon_ready.connect(
            lambda key: self._inventory_tab._apply_icon_to_table(key)
            if hasattr(self, '_inventory_tab') else None
        )
        self._parc_step.connect(self._on_parc_step)
        self._parc_done.connect(self._finish_parc_enrich)
        self._parse_cache_ready.connect(self._on_parse_cache_ready)
        self._undo_stack: List[UndoEntry] = []
        self._loaded_path: str = ""
        self._dirty: bool = False
        self._parc_status: str = ""
        self._tab_loaders: dict = {}
        self._loaded_tabs: set = set()
        self._config: dict = self._load_config()

        saved_lang = (
            self._config.get("default_lang")
            or self._config.get("language")
            or "en"
        )
        set_language(saved_lang)

        self._experimental_mode: bool = self._config.get("experimental_mode", False)
        self._icons_enabled: bool = self._config.get("show_icons", False)

        # CRITICAL — apply theme BEFORE building any widgets, so the COLORS
        # dict is already mutated to light/dark and inline setStyleSheet
        # calls during widget construction bake the correct palette.
        saved_theme = self._config.get("theme", "dark")
        app = QApplication.instance()
        if app is not None:
            apply_theme(app, saved_theme)

        db_path = self._name_db.load_auto()
        self._name_db.apply_localization()

        self._build_menu()
        self._build_main_layout()
        self._setup_lazy_tab_loading()
        self._build_status_bar()

        # Re-apply after construction in case a lazy widget creation path
        # changed COLORS. No-op if already applied, safe either way.
        if app is not None:
            apply_theme(app, saved_theme)
        else:
            self.setStyleSheet(LIGHT_STYLESHEET if saved_theme == "light" else DARK_STYLESHEET)

        saved_scale = self._config.get("font_scale")
        if saved_scale and saved_scale != 1.0:
            self._set_font_scale(saved_scale)

        saved_widget_scale = self._config.get("widget_scale")
        if saved_widget_scale and saved_widget_scale != 1.0:
            self._set_widget_scale(saved_widget_scale)

        self._base_point_size = float(QApplication.instance().font().pointSizeF())
        if self._base_point_size <= 0:
            self._base_point_size = 10.0
        self._zoom_factor = float(self._config.get("ui_zoom", 1.0))
        self._zoom_factor = max(0.5, min(2.0, self._zoom_factor))
        QShortcut(QKeySequence("Ctrl+="), self).activated.connect(self._zoom_in)
        QShortcut(QKeySequence("Ctrl++"), self).activated.connect(self._zoom_in)
        QShortcut(QKeySequence("Ctrl+-"), self).activated.connect(self._zoom_out)
        QShortcut(QKeySequence("Ctrl+0"), self).activated.connect(self._zoom_reset)
        if abs(self._zoom_factor - 1.0) > 0.001:
            QTimer.singleShot(0, self._apply_zoom)

        if not self._config.get("show_gamepath_bar", True):
            self._global_hide_btn.setChecked(True)
            self._toggle_global_info(True)

        self._refresh_sidebar()

        try:
            from updater import APP_VARIANT as _variant
        except Exception:
            _variant = "full"
        _autoload = _variant != "gamemods"

        last_path = self._config.get("last_save_path", "")
        if _autoload and last_path and os.path.isfile(last_path):
            try:
                from gui_i18n import needs_language_picker
                if needs_language_picker():
                    self._pending_autoload_path = last_path
                    self._update_status("Waiting for language choice…")
                else:
                    self._update_status(f"Loading: {os.path.basename(last_path)}...")
                    QTimer.singleShot(0, lambda: self._load_save(last_path))
            except Exception:
                self._update_status(f"Loading: {os.path.basename(last_path)}...")
                QTimer.singleShot(0, lambda: self._load_save(last_path))
        elif _variant == "gamemods":
            self._update_status(
                f"Ready. Item DB: {len(self._name_db.items)} items. "
                "Load a save only when needed — e.g. 'My Inventory' in ItemBuffs."
            )
        else:
            self._update_status(
                f"Ready. Item DB: {len(self._name_db.items)} items"
                + (f" from {os.path.basename(db_path)}" if db_path else " (not found)")
                + "  |  Select a save from the sidebar or File > Open"
            )


    def _get_config_path(self) -> str:
        import sys
        if getattr(sys, 'frozen', False):
            return os.path.join(os.path.dirname(os.path.abspath(sys.executable)), self._CONFIG_FILE)
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), self._CONFIG_FILE)

    def _load_config(self) -> dict:
        path = self._get_config_path()
        if os.path.isfile(path):
            try:
                with open(path, "r") as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    def _save_config(self) -> None:
        try:
            with open(self._get_config_path(), "w") as f:
                json.dump(self._config, f, indent=2)
        except OSError:
            pass


    def _build_main_layout(self) -> None:
        from PySide6.QtWidgets import QDockWidget

        sidebar = QFrame()
        sidebar.setMinimumWidth(40)
        sidebar.setStyleSheet(f"background-color: {COLORS['panel']}; border-right: 1px solid {COLORS['border']};")
        sb_layout = QVBoxLayout(sidebar)
        sb_layout.setContentsMargins(8, 8, 8, 8)
        sb_layout.setSpacing(4)

        hdr_row = QHBoxLayout()
        self._sb_collapse_btn = QPushButton("\u25C0")
        self._sb_collapse_btn.setFixedSize(22, 22)
        self._sb_collapse_btn.setToolTip("Collapse Save Browser")
        self._sb_collapse_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {COLORS['accent']}; "
            f"font-size: 12px; border: 1px solid {COLORS['border']}; border-radius: 3px; }}"
            f"QPushButton:hover {{ background: {COLORS['selected']}; }}"
        )
        self._sb_collapse_btn.clicked.connect(self._toggle_save_sidebar)
        hdr_row.addWidget(self._sb_collapse_btn)
        hdr = QLabel("Save Browser")
        hdr.setStyleSheet(f"font-size: 13px; font-weight: bold; color: {COLORS['accent']}; padding: 4px 0;")
        hdr_row.addWidget(hdr)
        hdr_row.addStretch()

        home_btn = QPushButton("Home")
        home_btn.setFixedHeight(22)
        home_btn.setToolTip("Go to Inventory tab (Tab 1)")
        home_btn.clicked.connect(lambda: self._tabs.setCurrentIndex(0))
        hdr_row.addWidget(home_btn)

        backup_nav_btn = QPushButton("Backup")
        backup_nav_btn.setFixedHeight(22)
        backup_nav_btn.setToolTip("Go to Backup/Restore tab")
        backup_nav_btn.clicked.connect(lambda: self._tabs.setCurrentIndex(self._tabs.count() - 1))
        hdr_row.addWidget(backup_nav_btn)

        sb_layout.addLayout(hdr_row)

        path_row = QHBoxLayout()
        self._save_root_label = QLabel("(auto-detect)")
        self._save_root_label.setStyleSheet(f"color: {COLORS['text_dim']}; font-size: 10px;")
        path_row.addWidget(self._save_root_label, 1)
        browse_btn = QPushButton("...")
        browse_btn.setFixedWidth(30)
        browse_btn.setToolTip("Set save folder path")
        browse_btn.clicked.connect(self._browse_save_root)
        path_row.addWidget(browse_btn)
        self._global_icons_btn = QPushButton("Hide Icons" if self._config.get("show_icons", False) else "Show Icons")
        self._global_icons_btn.setFixedHeight(22)
        self._global_icons_btn.setToolTip("Toggle item icons on all tabs")
        self._global_icons_btn.clicked.connect(self._toggle_icons)
        path_row.addWidget(self._global_icons_btn)
        sb_layout.addLayout(path_row)

        from PySide6.QtWidgets import QTreeWidget, QTreeWidgetItem
        self._save_tree = QTreeWidget()
        self._save_tree.setHeaderHidden(True)
        self._save_tree.setIndentation(16)
        self._save_tree.setStyleSheet(
            f"QTreeWidget {{ background: {COLORS['bg']}; color: {COLORS['text']}; border: 1px solid {COLORS['border']}; }}"
            f"QTreeWidget::item:selected {{ background: {COLORS['selected']}; }}"
            f"QTreeWidget::item:hover {{ background: {COLORS['header']}; }}"
        )
        self._save_tree.itemDoubleClicked.connect(self._on_tree_double_click)
        self._save_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self._save_tree.customContextMenuRequested.connect(self._on_tree_context_menu)
        sb_layout.addWidget(self._save_tree, 1)

        ref_btn = QPushButton("Refresh")
        ref_btn.clicked.connect(self._refresh_sidebar)
        sb_layout.addWidget(ref_btn)

        self._quick_save_btn = QPushButton("SAVE EDIT TO SELECTED FILE")
        self._quick_save_btn.setStyleSheet(
            f"QPushButton {{ background-color: {COLORS['accent']}; color: white; font-weight: bold; "
            f"padding: 8px; border-radius: 4px; font-size: 11px; }}"
            f"QPushButton:hover {{ background-color: #ff5577; }}"
            f"QPushButton:disabled {{ background-color: #555; color: #888; }}"
        )
        self._quick_save_btn.setEnabled(False)
        self._quick_save_btn.clicked.connect(self._save_file)
        sb_layout.addWidget(self._quick_save_btn)

        backup_local_btn = QPushButton("Backup to Local Folder")
        backup_local_btn.setToolTip("Copy current save to backups/ folder next to the editor exe")
        backup_local_btn.clicked.connect(self._backup_to_local)
        sb_layout.addWidget(backup_local_btn)

        settings_btn = QPushButton("Settings")
        settings_btn.setStyleSheet(
            f"font-size: 12px; font-weight: bold; color: #4FC3F7; "
            f"border: 1px solid #4FC3F7; border-radius: 4px; padding: 4px 8px;"
        )
        settings_btn.clicked.connect(self._open_settings)
        sb_layout.addWidget(settings_btn)

        self._last_edit_label = QLabel("")
        self._last_edit_label.setStyleSheet(f"color: {COLORS['warning']}; font-size: 10px; padding: 2px;")
        self._last_edit_label.setWordWrap(True)
        sb_layout.addWidget(self._last_edit_label)

        self._save_sidebar = sidebar
        self._save_dock = QDockWidget("Save Browser", self)
        self._save_dock.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        self._save_dock.setFeatures(
            QDockWidget.DockWidgetMovable |
            QDockWidget.DockWidgetFloatable |
            QDockWidget.DockWidgetClosable
        )
        self._save_dock.setStyleSheet(
            f"QDockWidget::title {{ background: {COLORS['accent']}; padding: 4px; "
            f"color: white; font-weight: bold; }}"
            f"QDockWidget {{ border: 1px solid {COLORS['accent']}; }}")
        self._save_dock.setWidget(sidebar)
        self._save_dock.visibilityChanged.connect(
            lambda v: self.__dict__.update({'_sb_collapsed': not v})
        )
        self.addDockWidget(Qt.LeftDockWidgetArea, self._save_dock)

        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        self._center_status = QLabel("Ready — Select a save from the sidebar or File > Open")
        self._center_status.setAlignment(Qt.AlignCenter)
        self._center_status.setStyleSheet(
            f"background-color: {COLORS['panel']}; "
            f"color: {COLORS['accent']}; "
            f"font-size: 13px; font-weight: bold; "
            f"padding: 8px; "
            f"border-bottom: 1px solid {COLORS['border']};"
        )
        self._center_status.setFixedHeight(36)
        right_layout.addWidget(self._center_status)

        self._global_info_widget = QWidget()
        info_layout = QHBoxLayout(self._global_info_widget)
        info_layout.setContentsMargins(0, 0, 0, 0)
        info_layout.setSpacing(4)
        gp_icon = QLabel("Game:")
        gp_icon.setStyleSheet(f"color: {COLORS['text_dim']}; font-weight: bold; padding: 0 4px;")
        info_layout.addWidget(gp_icon)

        self._global_game_path = QLabel("Not set — click Browse")
        self._global_game_path.setStyleSheet(
            f"color: {COLORS['accent']}; padding: 2px 6px; "
            f"border: 1px solid {COLORS['border']}; border-radius: 3px; "
            f"background-color: {COLORS['input_bg']};"
        )
        self._global_game_path.setToolTip("Game installation path used by Game Data, ItemBuffs, and Stores")
        info_layout.addWidget(self._global_game_path, 1)

        gp_browse = QPushButton("Browse")
        gp_browse.setMinimumWidth(90)
        gp_browse.clicked.connect(self._global_browse_game_path)
        info_layout.addWidget(gp_browse)

        gp_detect = QPushButton("Auto-Detect")
        gp_detect.setMinimumWidth(110)
        gp_detect.setToolTip("Auto-detect game installation")
        gp_detect.clicked.connect(self._global_auto_detect_path)
        info_layout.addWidget(gp_detect)

        self._global_hide_btn = QPushButton("▲")
        self._global_hide_btn.setFixedSize(24, 24)
        self._global_hide_btn.setCheckable(True)
        self._global_hide_btn.setToolTip("Hide game path bar (▲ collapse / ▼ expand)")
        self._global_hide_btn.setStyleSheet(
            f"QPushButton {{ background: {COLORS['accent']}; color: white; "
            f"font-weight: bold; border-radius: 12px; font-size: 14px; }}"
            f"QPushButton:checked {{ background: {COLORS['accent']}; }}")
        self._global_hide_btn.clicked.connect(self._toggle_global_info)
        info_layout.addWidget(self._global_hide_btn)

        right_layout.addWidget(self._global_info_widget)

        self._paz_game_path = QLineEdit()
        self._paz_game_path.setVisible(False)
        self._paz_manager = PazPatchManager()
        self._paz_patches = []
        saved_gp = self._config.get("game_install_path", "")
        if saved_gp:
            self._paz_game_path.setText(saved_gp)
            self._paz_manager.game_path = saved_gp
            self._global_game_path.setText(saved_gp)
            self._global_game_path.setToolTip(saved_gp)

        self._tabs = QTabWidget()
        right_layout.addWidget(self._tabs, 1)
        self.setCentralWidget(right_panel)

        from PySide6.QtWidgets import QTreeWidget, QTreeWidgetItem
        pack_sidebar = QFrame()
        pack_sidebar.setMinimumWidth(40)
        pack_sidebar.setStyleSheet(f"background-color: {COLORS['panel']}; border-left: 1px solid {COLORS['border']};")
        ps_layout = QVBoxLayout(pack_sidebar)
        ps_layout.setContentsMargins(6, 6, 6, 6)
        ps_layout.setSpacing(3)

        ps_hdr_row = QHBoxLayout()
        ps_hdr = QLabel("Pack Browser")
        ps_hdr.setStyleSheet(f"font-size: 13px; font-weight: bold; color: {COLORS['accent']}; padding: 2px 0;")
        ps_hdr_row.addWidget(ps_hdr)
        ps_hdr_row.addStretch()
        self._ps_collapse_btn = QPushButton("\u25B6")
        self._ps_collapse_btn.setFixedSize(22, 22)
        self._ps_collapse_btn.setToolTip("Collapse Pack Browser")
        self._ps_collapse_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {COLORS['accent']}; "
            f"font-size: 12px; border: 1px solid {COLORS['border']}; border-radius: 3px; }}"
            f"QPushButton:hover {{ background: {COLORS['selected']}; }}"
        )
        self._ps_collapse_btn.clicked.connect(self._toggle_pack_sidebar)
        ps_hdr_row.addWidget(self._ps_collapse_btn)
        ps_layout.addLayout(ps_hdr_row)

        new_grp = QFrame()
        new_grp.setStyleSheet(f"border: 1px solid {COLORS['border']}; border-radius: 3px; padding: 2px;")
        new_lay = QVBoxLayout(new_grp)
        new_lay.setContentsMargins(4, 4, 4, 4)
        new_lay.setSpacing(2)

        self._pack_name_input = QLineEdit()
        self._pack_name_input.setPlaceholderText("Pack name...")
        self._pack_name_input.setStyleSheet(f"border: 1px solid {COLORS['border']}; padding: 3px;")
        new_lay.addWidget(self._pack_name_input)

        type_row = QHBoxLayout()
        self._pack_type_combo = QComboBox()
        self._pack_type_combo.addItems(["Knowledge", "Quest"])
        self._pack_type_combo.setFixedWidth(100)
        type_row.addWidget(self._pack_type_combo)

        ps_create_btn = QPushButton("Create")
        ps_create_btn.setStyleSheet("font-weight: bold;")
        ps_create_btn.clicked.connect(self._pack_browser_create)
        type_row.addWidget(ps_create_btn)
        new_lay.addLayout(type_row)
        ps_layout.addWidget(new_grp)

        self._pack_tree = QTreeWidget()
        self._pack_tree.setHeaderHidden(True)
        self._pack_tree.setIndentation(16)
        self._pack_tree.setStyleSheet(
            f"QTreeWidget {{ background: {COLORS['bg']}; color: {COLORS['text']}; border: 1px solid {COLORS['border']}; }}"
            f"QTreeWidget::item:selected {{ background: {COLORS['selected']}; }}"
            f"QTreeWidget::item:hover {{ background: {COLORS['header']}; }}"
        )
        self._pack_tree.itemDoubleClicked.connect(self._pack_browser_navigate)
        ps_layout.addWidget(self._pack_tree, 1)


        ps_inject_btn = QPushButton("Inject Selected Pack")
        ps_inject_btn.clicked.connect(self._pack_browser_inject)
        ps_layout.addWidget(ps_inject_btn)

        ps_delete_btn = QPushButton("Delete Selected Pack")
        ps_delete_btn.clicked.connect(self._pack_browser_delete)
        ps_layout.addWidget(ps_delete_btn)

        ps_refresh_btn = QPushButton("Refresh")
        ps_refresh_btn.clicked.connect(self._pack_browser_refresh)
        ps_layout.addWidget(ps_refresh_btn)

        ps_open_btn = QPushButton("Open Folder")
        ps_open_btn.setStyleSheet(f"font-size: 10px; color: {COLORS['text_dim']};")
        ps_open_btn.clicked.connect(self._pack_browser_open_folder)
        ps_layout.addWidget(ps_open_btn)

        self._pack_sidebar = pack_sidebar
        self._pack_dock = QDockWidget("Pack Browser", self)
        self._pack_dock.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        self._pack_dock.setFeatures(
            QDockWidget.DockWidgetMovable |
            QDockWidget.DockWidgetFloatable |
            QDockWidget.DockWidgetClosable
        )
        self._pack_dock.setStyleSheet(
            f"QDockWidget::title {{ background: {COLORS['accent']}; padding: 4px; "
            f"color: white; font-weight: bold; }}"
            f"QDockWidget {{ border: 1px solid {COLORS['accent']}; }}")
        self._pack_dock.setWidget(pack_sidebar)
        self._pack_dock.visibilityChanged.connect(
            lambda v: self.__dict__.update({'_ps_collapsed': not v})
        )
        self.addDockWidget(Qt.RightDockWidgetArea, self._pack_dock)

        self.resizeDocks([self._save_dock, self._pack_dock], [260, 260], Qt.Horizontal)

        self._sb_collapsed = False
        self._ps_collapsed = False
        self._sb_saved_width = 260
        self._ps_saved_width = 260

        self._pack_data = {}

        self._save_dock.hide()
        self._pack_dock.hide()
        self._sb_collapsed = True
        self._ps_collapsed = True

        self._save_dock.setFloating(True)
        self._save_dock.setAllowedAreas(Qt.NoDockWidgetArea)
        self._save_dock.setFeatures(
            QDockWidget.DockWidgetClosable | QDockWidget.DockWidgetMovable
        )

        _corner = QWidget()
        _corner_layout = QHBoxLayout(_corner)
        _corner_layout.setContentsMargins(0, 0, 4, 0)
        _corner_layout.setSpacing(4)

        self._btn_toggle_save_browser = QPushButton("Save Browser")
        self._btn_toggle_save_browser.setCheckable(True)
        self._btn_toggle_save_browser.setToolTip(
            "Open the Save Browser as a floating window"
        )
        self._btn_toggle_save_browser.clicked.connect(
            lambda checked: (
                self._save_dock.setFloating(True),
                self._save_dock.setVisible(checked),
                self._save_dock.raise_() if checked else None,
            )
        )
        self._save_dock.visibilityChanged.connect(
            self._btn_toggle_save_browser.setChecked
        )
        _corner_layout.addWidget(self._btn_toggle_save_browser)

        self._tabs.setCornerWidget(_corner, Qt.TopRightCorner)


        self._mods_tabs = QTabWidget()
        self._mods_tabs.setTabPosition(QTabWidget.South)
        self._tabs.addTab(self._mods_tabs, tr("tab.game_mods"))

        # if DmmWebViewTab is not None:
        #     self._dmm_webview_tab = DmmWebViewTab(config=self._config)
        #     self._dmm_webview_tab.status_message.connect(self._update_status)
        #     self._tabs.addTab(self._dmm_webview_tab, "Mod Manager")

        self._items_tabs = QTabWidget()
        self._items_tabs.setTabPosition(QTabWidget.South)
        self._tabs.addTab(self._items_tabs, tr("tab.items"))

        self._load_manager_tab = LoadManagerTab(config=self._config)
        self._load_manager_tab.status_message.connect(self._update_status)
        self._load_manager_tab.config_save_requested.connect(self._save_config)
        _saved_gp_lm = self._config.get("game_install_path", "")
        if _saved_gp_lm:
            try:
                self._load_manager_tab.set_game_path(_saved_gp_lm)
            except Exception:
                pass
        self._tabs.addTab(self._load_manager_tab, "Load Manager")

        self._save_tabs = QTabWidget()
        self._world_tabs = QTabWidget()

        _real_tabs = self._tabs

        self._tabs = self._mods_tabs
        self._patches_tab = GamePatchesTab(
            config=self._config,
            paz_manager=self._paz_manager,
            experimental_mode=self._experimental_mode,
            show_guide_fn=self._show_guide,
        )
        self._patches_tab.status_message.connect(self._update_status)
        self._patches_tab.game_path_changed.connect(self._set_game_path)
        self._patches_tab.config_save_requested.connect(self._save_config)

        self._field_edit_tab_obj = FieldEditTab(
            config=self._config,
            rebuild_papgt_fn=self._rebuild_papgt_without,
            show_guide_fn=self._show_guide,
        )
        self._field_edit_tab_obj.status_message.connect(self._update_status)
        self._field_edit_tab_obj.config_save_requested.connect(self._save_config)
        _saved_gp_fe = self._config.get("game_install_path", "")
        if _saved_gp_fe:
            try:
                self._field_edit_tab_obj.set_game_path(_saved_gp_fe)
            except Exception:
                pass
        self._mods_tabs.addTab(self._field_edit_tab_obj, tr("FieldEdit"))

        self._iteminfo_cache = ItemInfoCache()
        self._iteminfo_cache.set_game_path(self._config.get("game_install_path", ""))

        self._buffs_tab = ItemBuffsTab(
            name_db=self._name_db,
            icon_cache=self._icon_cache,
            config=self._config,
            show_guide_fn=self._show_guide,
            paz_manager=self._paz_manager,
            set_manager=getattr(self, "_set_manager", None),
        )
        self._buffs_tab.status_message.connect(self._update_status)
        self._buffs_tab.config_save_requested.connect(self._save_config)
        self._buffs_tab.paz_refresh_requested.connect(
            lambda: self._patches_tab._paz_refresh_status() if hasattr(self, "_patches_tab") else None
        )
        self._buffs_tab.dirty.connect(lambda: setattr(self, "_dirty", True))
        if hasattr(self, "_undo_stack"):
            self._buffs_tab.undo_entry_added.connect(self._undo_stack.append)
        self._buffs_tab.navigate_requested.connect(self._on_tab_navigate_requested)
        if hasattr(self, "_scan_items"):
            self._buffs_tab.scan_requested.connect(self._scan_items)

        def _pop_save_browser() -> None:
            if hasattr(self, "_save_dock"):
                self._save_dock.setFloating(True)
                self._save_dock.show()
                self._save_dock.raise_()
                self._save_dock.activateWindow()
        self._buffs_tab.open_save_browser_requested.connect(_pop_save_browser)
        _saved_gp_for_buffs = self._config.get("game_install_path", "")
        if _saved_gp_for_buffs:
            try:
                self._buffs_tab.set_game_path(_saved_gp_for_buffs)
            except Exception:
                pass
        self._mods_tabs.addTab(self._buffs_tab, tr("tab.itembuffs"))

        # Stacker Tool — multi-mod iteminfo.pabgb merger. Single sink for
        # all mod changes targeting iteminfo: external mods (folder PAZ,
        # loose pabgb, legacy JSON) + ItemBuffs tab edits pulled on demand.
        # Produces ONE overlay instead of N fighting-each-other overlays.
        self._stacker_tab = StackerTab(
            name_db=self._name_db,
            icon_cache=self._icon_cache,
            config=self._config,
            show_guide_fn=self._show_guide,
            buffs_tab=self._buffs_tab,
        )
        self._stacker_tab.status_message.connect(self._update_status)
        self._stacker_tab.config_save_requested.connect(self._save_config)
        _saved_gp_for_stacker = self._config.get("game_install_path", "")
        if _saved_gp_for_stacker:
            try:
                self._stacker_tab.set_game_path(_saved_gp_for_stacker)
            except Exception:
                pass
        self._mods_tabs.addTab(self._stacker_tab, "Stacker Tool")

        # self._store_tab = StoreEditorTab(
        #     name_db=self._name_db,
        #     icon_cache=self._icon_cache,
        #     config=self._config,
        #     rebuild_papgt_fn=self._rebuild_papgt_without,
        #     show_guide_fn=self._show_guide,
        # )
        # self._store_tab.status_message.connect(self._update_status)
        # self._store_tab.config_save_requested.connect(self._save_config)
        # self._store_tab.paz_refresh_requested.connect(
        #     lambda: self._patches_tab._paz_refresh_status() if hasattr(self, "_patches_tab") else None
        # )
        # _saved_gp = self._config.get("game_install_path", "")
        # if _saved_gp:
        #     try:
        #         self._store_tab.set_game_path(_saved_gp)
        #     except Exception:
        #         pass
        # self._mods_tabs.addTab(self._store_tab, tr("tab.stores"))

        self._bagspace_tab = BagSpaceTab(
            config=self._config,
            rebuild_papgt_fn=self._rebuild_papgt_without,
        )
        self._bagspace_tab.status_message.connect(self._update_status)
        self._bagspace_tab.config_save_requested.connect(self._save_config)
        _saved_gp_bagspace = self._config.get("game_install_path", "")
        if _saved_gp_bagspace:
            try:
                self._bagspace_tab.set_game_path(_saved_gp_bagspace)
            except Exception:
                pass
        self._mods_tabs.addTab(self._bagspace_tab, "BagSpace")

        self._dropset_tab = DropsetTab(
            name_db=self._name_db,
            icon_cache=self._icon_cache,
            config=self._config,
            rebuild_papgt_fn=self._rebuild_papgt_without,
        )
        self._dropset_tab.status_message.connect(self._update_status)
        self._mods_tabs.addTab(self._dropset_tab, "DropSets")

        self._spawn_tab = SpawnTab(config=self._config, show_guide_fn=self._show_guide)
        self._spawn_tab.status_message.connect(self._update_status)
        self._mods_tabs.addTab(self._spawn_tab, "SpawnEdit")

        self._skill_tree_tab = SkillTreeTab(
            config=self._config,
            rebuild_papgt_fn=self._rebuild_papgt_without,
        )
        self._skill_tree_tab.status_message.connect(self._update_status)
        self._skill_tree_tab.config_save_requested.connect(self._save_config)
        _saved_gp_st = self._config.get("game_install_path", "")
        if _saved_gp_st:
            try:
                self._skill_tree_tab.set_game_path(_saved_gp_st)
            except Exception:
                pass
        self._mods_tabs.addTab(self._skill_tree_tab, "SkillTree")

        self._reserveslot_tab = ReserveSlotTab(
            config=self._config,
            game_path_getter=lambda: self._config.get("game_install_path", ""),
            rebuild_papgt_fn=self._rebuild_papgt_without,
        )
        self._reserveslot_tab.status_message.connect(self._update_status)
        self._reserveslot_tab.config_save_requested.connect(self._save_config)
        self._mods_tabs.addTab(self._reserveslot_tab, "ReserveSlot")

        if self._experimental_mode:
            try:
                from gui.tabs.mercpets import MercPetsTab
                self._mercpets_tab = MercPetsTab(
                    self._config,
                    lambda: self._config.get("game_install_path", ""),
                )
                self._mercpets_tab.config_save_requested.connect(self._save_config)
                self._mods_tabs.addTab(self._mercpets_tab, "MercPets (dev)")
            except Exception as e:
                log.warning("MercPets tab load failed: %s", e)

        self._tabs = _real_tabs

        self._tabs = self._items_tabs
        self._database_tab = DatabaseBrowserTab(
            name_db=self._name_db,
            icon_cache=self._icon_cache,
            get_items_fn=lambda: self._items,
            icons_enabled=self._icons_enabled,
            goto_knowledge_fn=None,
            goto_quest_fn=None,
            app_dir_fn=self._app_dir,
            show_guide_fn=self._show_guide,
        )
        self._database_tab.toggle_icons_requested.connect(self._toggle_icons)
        self._database_tab.status_message.connect(self._update_status)
        self._database_tab.items_changed.connect(self._scan_and_populate)
        self._items_tabs.addTab(self._database_tab, tr("tab.database"))
        self._db_tab_widget = self._database_tab

        self._tabs = _real_tabs
        self._real_tabs = _real_tabs

        if hasattr(self, '_stacker_tab'):
            _mt = {}
            if hasattr(self, '_mercpets_tab'):
                _mt['mercpets'] = self._mercpets_tab
            if hasattr(self, '_bagspace_tab'):
                _mt['bagspace'] = self._bagspace_tab
            if hasattr(self, '_skill_tree_tab'):
                _mt['skilltree'] = self._skill_tree_tab
            if hasattr(self, '_reserveslot_tab'):
                _mt['reserveslot'] = self._reserveslot_tab
            if hasattr(self, '_field_edit_tab_obj'):
                _mt['fieldedit'] = self._field_edit_tab_obj
            self._stacker_tab._mod_tabs = _mt

        self._update_experimental_tabs()
        self._pack_browser_refresh()

        if hasattr(self, '_view_menu'):
            self._rebuild_view_tab_list()
            self._tabs.currentChanged.connect(self._on_tab_changed_view_menu)

        for i in range(min(12, self._tabs.count())):
            QShortcut(QKeySequence(f"F{i+1}"), self).activated.connect(
                (lambda idx: lambda: self._tabs.setCurrentIndex(idx))(i)
            )

        if self._config.get("ui_scale", 100) != 100 or self._config.get("compact_mode", False):
            self._apply_ui_settings()

    def _toggle_save_sidebar(self) -> None:
        if self._save_dock.isVisible():
            self._save_dock.hide()
            self._sb_collapse_btn.setText("\u25B6")
            self._sb_collapse_btn.setToolTip("Show Save Browser")
        else:
            self._save_dock.show()
            self._sb_collapse_btn.setText("\u25C0")
            self._sb_collapse_btn.setToolTip("Collapse Save Browser")

    def _toggle_pack_sidebar(self) -> None:
        if self._pack_dock.isVisible():
            self._pack_dock.hide()
            self._ps_collapse_btn.setText("\u25C0")
            self._ps_collapse_btn.setToolTip("Show Pack Browser")
        else:
            self._pack_dock.show()
            self._ps_collapse_btn.setText("\u25B6")
            self._ps_collapse_btn.setToolTip("Collapse Pack Browser")

    def _browse_save_root(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "Select Save Root Folder",
            self._config.get("save_root", r"C:\Users")
        )
        if path:
            self._config["save_root"] = path
            self._save_config()
            self._refresh_sidebar()

    @staticmethod
    def _friendly_slot_name(slot_dir: str) -> str:
        try:
            num = int(slot_dir.replace("slot", ""))
            if num < 100:
                return f"Auto Save {num + 1}"
            return f"Save Slot {num - 99}"
        except (ValueError, AttributeError):
            return slot_dir

    def _refresh_sidebar(self) -> None:
        from PySide6.QtWidgets import QTreeWidgetItem
        self._save_tree.clear()

        roots = []
        custom = self._config.get("save_root", "")
        if custom and os.path.isdir(custom):
            roots.append(custom)

        localappdata_paths = [os.environ.get("LOCALAPPDATA", "")]
        custom_appdata = self._config.get("custom_localappdata", "")
        if custom_appdata and os.path.isdir(custom_appdata):
            localappdata_paths.insert(0, custom_appdata)

        if sys.platform != "win32":
            user = os.getenv("USER", "")
            if user:
                linux_steam = os.path.join(
                    f"/home/{user}/.local/share/Steam/steamapps/compatdata",
                    "3321460/pfx/drive_c/users/steamuser/AppData/Local",
                    "Pearl Abyss/CD/save",
                )
                if os.path.isdir(linux_steam) and linux_steam not in roots:
                    roots.append(linux_steam)

        for local in localappdata_paths:
            if not local:
                continue
            for sub in ["Pearl Abyss/CD/save", "Pearl Abyss/CD_Epic/save", "Pearl Abyss/CD_GamePass/save"]:
                p = os.path.join(local, sub)
                if os.path.isdir(p) and p not in roots:
                    roots.append(p)

            packages_dir = os.path.join(local, "Packages")
            if os.path.isdir(packages_dir):
                try:
                    for pkg in os.listdir(packages_dir):
                        if pkg.startswith("PearlAbyss.CrimsonDesert"):
                            wgs_base = os.path.join(packages_dir, pkg, "SystemAppData", "wgs")
                            if os.path.isdir(wgs_base):
                                for user_dir in os.listdir(wgs_base):
                                    wgs_user = os.path.join(wgs_base, user_dir)
                                    if os.path.isdir(wgs_user) and user_dir != "t":
                                        if wgs_user not in roots:
                                            roots.append(("gamepass_wgs", wgs_user, pkg))
                except OSError:
                    pass

        if not roots:
            self._save_root_label.setText("No saves found")
            return

        last_save = self._config.get("last_save_path", "")
        last_slot = self._config.get("last_slot", "")
        newest_mtime = 0
        newest_node = None

        for root in roots:
            if isinstance(root, tuple) and root[0] == "gamepass_wgs":
                _, wgs_path, pkg_name = root
                platform = "Game Pass"
                self._save_root_label.setText(wgs_path)

                user_node = QTreeWidgetItem(self._save_tree, [f"{platform} — {pkg_name}"])
                user_node.setExpanded(True)

                try:
                    slot_idx = 0
                    for guid_dir in sorted(os.listdir(wgs_path)):
                        guid_path = os.path.join(wgs_path, guid_dir)
                        if not os.path.isdir(guid_path):
                            continue
                        best_file = None
                        best_size = 0
                        for fname in os.listdir(guid_path):
                            fpath = os.path.join(guid_path, fname)
                            if not os.path.isfile(fpath) or fname.startswith("container"):
                                continue
                            try:
                                sz = os.path.getsize(fpath)
                                if sz > best_size:
                                    with open(fpath, "rb") as f:
                                        magic = f.read(4)
                                    if magic == b"SAVE" and sz > 1000:
                                        best_size = sz
                                        best_file = fpath
                            except OSError:
                                continue

                        if best_file:
                            st = os.stat(best_file)
                            mtime = datetime.datetime.fromtimestamp(st.st_mtime).strftime("%m/%d %H:%M")
                            label = f"Save {slot_idx} ({mtime}, {best_size//1024}KB)"
                            slot_node = QTreeWidgetItem(user_node, [label])
                            slot_node.setData(0, Qt.UserRole, best_file)
                            if best_file == last_save:
                                slot_node.setForeground(0, QBrush(QColor(COLORS["accent"])))
                                font = slot_node.font(0)
                                font.setBold(True)
                                slot_node.setFont(0, font)
                            slot_idx += 1
                except OSError:
                    pass
                continue

            if "CD_Epic" in root:
                platform = "Epic"
            elif "CD_GamePass" in root:
                platform = "Game Pass"
            else:
                platform = "Steam"
            self._save_root_label.setText(os.path.dirname(root))

            try:
                user_dirs = sorted(os.listdir(root))
            except OSError:
                continue

            for user_id in user_dirs:
                user_path = os.path.join(root, user_id)
                if not os.path.isdir(user_path):
                    continue

                user_node = QTreeWidgetItem(self._save_tree, [f"{platform} — {user_id}"])
                user_node.setExpanded(True)

                try:
                    slots = sorted(
                        os.listdir(user_path),
                        key=lambda s: int(s.replace("slot", "")) if s.startswith("slot") and s[4:].isdigit() else 999999,
                    )
                except OSError:
                    continue

                for slot_name in slots:
                    slot_path = os.path.join(user_path, slot_name)
                    if not os.path.isdir(slot_path):
                        continue

                    save_file = os.path.join(slot_path, "save.save")
                    lobby_file = os.path.join(slot_path, "lobby.save")
                    has_save = os.path.isfile(save_file)
                    has_lobby = os.path.isfile(lobby_file)

                    if not has_save and not has_lobby:
                        continue

                    mtime = ""
                    if has_lobby:
                        st = os.stat(lobby_file)
                        mtime = datetime.datetime.fromtimestamp(st.st_mtime).strftime("%m/%d %H:%M")
                    elif has_save:
                        st = os.stat(save_file)
                        mtime = datetime.datetime.fromtimestamp(st.st_mtime).strftime("%m/%d %H:%M")

                    display_name = self._friendly_slot_name(slot_name)
                    label = f"{display_name}  ({mtime})" if mtime else display_name

                    slot_node = QTreeWidgetItem(user_node, [label])
                    slot_node.setData(0, Qt.UserRole, save_file if has_save else lobby_file)

                    check_file = lobby_file if has_lobby else save_file
                    try:
                        file_mtime = os.stat(check_file).st_mtime
                        if file_mtime > newest_mtime:
                            newest_mtime = file_mtime
                            newest_node = slot_node
                    except OSError:
                        pass

                    if display_name.startswith("Auto Save"):
                        slot_node.setForeground(0, QBrush(QColor(COLORS["warning"])))

                    if save_file == last_save:
                        slot_node.setForeground(0, QBrush(QColor(COLORS["accent"])))
                        font = slot_node.font(0)
                        font.setBold(True)
                        slot_node.setFont(0, font)

        if newest_node and (not last_save or newest_node.data(0, Qt.UserRole) != last_save):
            newest_node.setForeground(0, QBrush(QColor(COLORS["success"])))
            font = newest_node.font(0)
            font.setBold(True)
            newest_node.setFont(0, font)

        if last_slot:
            self._last_edit_label.setText(f"Last edited: {last_slot}")
        elif last_save:
            self._last_edit_label.setText(f"Last edited: {os.path.basename(os.path.dirname(last_save))}")

    def _on_tree_double_click(self, item, column) -> None:
        path = item.data(0, Qt.UserRole)
        if path and os.path.isfile(path):
            self._load_save(path)

    def _on_tree_context_menu(self, pos) -> None:
        item = self._save_tree.itemAt(pos)
        if not item:
            return
        path = item.data(0, Qt.UserRole)
        if not path:
            return

        from PySide6.QtWidgets import QMenu
        menu = QMenu(self)

        if os.path.isfile(path):
            load_action = menu.addAction("Load Save")
            load_action.triggered.connect(lambda: self._load_save(path))

            folder = os.path.dirname(path)
            open_folder_action = menu.addAction("Open File Location")
            open_folder_action.triggered.connect(lambda: self._open_folder(folder))

            copy_path_action = menu.addAction("Copy File Path")
            copy_path_action.triggered.connect(lambda: QApplication.clipboard().setText(path))
        elif os.path.isdir(path):
            open_folder_action = menu.addAction("Open Folder")
            open_folder_action.triggered.connect(lambda: self._open_folder(path))

        menu.exec(self._save_tree.viewport().mapToGlobal(pos))

    @staticmethod
    def _open_folder(folder: str) -> None:
        import subprocess
        if sys.platform == 'win32':
            os.startfile(folder)
        elif sys.platform == 'darwin':
            subprocess.Popen(['open', folder])
        else:
            subprocess.Popen(['xdg-open', folder])

    def _open_language_picker(self) -> None:
        try:
            from gui.language_picker import LanguagePickerDialog
        except Exception as e:
            QMessageBox.warning(self, "Language",
                f"Language picker failed to load:\n{e}")
            return

        def _on_applied(code: str) -> None:
            self._config["default_lang"] = code
            self._config["language"] = code
            self._save_config()

        LanguagePickerDialog.run(
            self,
            config_path=self._get_config_path(),
            config=dict(self._config),
            on_applied=_on_applied,
            blocking=False,
        )

    def _open_settings(self) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("Settings")
        dlg.setMinimumWidth(500)
        layout = QVBoxLayout(dlg)

        layout.addWidget(QLabel("Custom AppData / Save Root Path:"))
        desc = QLabel(
            "If your game saves are on a different drive (D:, E:, etc.) or a custom location,\n"
            "set the path here. Leave empty for auto-detect.\n\n"
            "Default: %LOCALAPPDATA%\\Pearl Abyss\\CD\\save"
        )
        desc.setStyleSheet(f"color: {COLORS['text_dim']}; font-size: 10px;")
        layout.addWidget(desc)

        path_row = QHBoxLayout()
        self._settings_path = QLineEdit()
        self._settings_path.setText(self._config.get("save_root", ""))
        self._settings_path.setPlaceholderText("e.g. D:\\AppData\\Local\\Pearl Abyss\\CD\\save")
        path_row.addWidget(self._settings_path, 1)
        browse = QPushButton("Browse...")
        browse.clicked.connect(lambda: self._settings_browse_path())
        path_row.addWidget(browse)
        layout.addLayout(path_row)

        layout.addWidget(QLabel("Custom LOCALAPPDATA Path (for non-C: drive installs):"))
        self._settings_appdata = QLineEdit()
        self._settings_appdata.setText(self._config.get("custom_localappdata", ""))
        self._settings_appdata.setPlaceholderText("e.g. D:\\Users\\YourName\\AppData\\Local")
        layout.addWidget(self._settings_appdata)

        self._settings_use_current = QCheckBox("Set currently loaded save folder as default path")
        self._settings_use_current.setToolTip(
            "If checked, the folder of the currently loaded save file will be saved\n"
            "as the default path. The tool will always look here first on startup.\n"
            "Useful for Linux users or custom install locations."
        )
        if self._loaded_path:
            current_folder = os.path.dirname(os.path.dirname(self._loaded_path))
            self._settings_use_current.setText(
                f"Set currently loaded folder as default:\n{current_folder}"
            )
        else:
            self._settings_use_current.setEnabled(False)
            self._settings_use_current.setText("Set currently loaded folder as default (no save loaded)")
        layout.addWidget(self._settings_use_current)

        layout.addWidget(QLabel(""))
        layout.addWidget(QLabel("Language / 言語:"))

        pick_lang_btn = QPushButton("🌐 Pick Language…")
        pick_lang_btn.setToolTip(
            "Open the language picker (downloads packs on demand from GitHub).\n"
            "Use this anytime to switch languages or pick one for the first time.")
        pick_lang_btn.setStyleSheet(
            "QPushButton { font-weight: bold; padding: 8px 16px; "
            "background: #FF4466; color: white; border-radius: 4px; }"
            "QPushButton:hover { background: #ff5577; }")
        pick_lang_btn.clicked.connect(self._open_language_picker)
        layout.addWidget(pick_lang_btn)

        if not (self._config.get("default_lang") or self._config.get("language")):
            hint = QLabel(
                "👆 No default language set yet — click above to choose one. "
                "You can change this anytime."
            )
            hint.setWordWrap(True)
            hint.setStyleSheet(
                "QLabel { color: #FF4466; font-style: italic; "
                "padding: 4px; border: 1px dashed #FF4466; border-radius: 4px; }")
            layout.addWidget(hint)

        lang_row = QHBoxLayout()
        self._settings_lang = QComboBox()
        available = get_available_languages()
        current_lang = (
            self._config.get("default_lang")
            or self._config.get("language")
            or "en"
        )
        for code, name in available:
            self._settings_lang.addItem(f"{name} ({code})", code)
            if code == current_lang:
                self._settings_lang.setCurrentIndex(self._settings_lang.count() - 1)
        lang_row.addWidget(self._settings_lang, 1)
        layout.addLayout(lang_row)

        lang_btn_row = QHBoxLayout()
        export_lang_btn = QPushButton("Export English Template")
        export_lang_btn.setToolTip("Export the base English strings as a .json file for translators")
        export_lang_btn.clicked.connect(lambda: self._settings_export_lang_template(dlg))
        lang_btn_row.addWidget(export_lang_btn)

        import_lang_btn = QPushButton("Import Translation")
        import_lang_btn.setToolTip("Import a community .json translation file")
        import_lang_btn.clicked.connect(lambda: self._settings_import_lang(dlg))
        lang_btn_row.addWidget(import_lang_btn)

        dl_lang_btn = QPushButton("Download Name Pack")
        dl_lang_btn.setToolTip("Download translated item/quest/knowledge names for the selected language from GitHub")
        dl_lang_btn.clicked.connect(lambda: self._settings_download_name_pack(dlg))
        lang_btn_row.addWidget(dl_lang_btn)

        dl_all_btn = QPushButton("Download All Packs")
        dl_all_btn.setToolTip(
            "Download ALL language packs (UI + names) from GitHub.\n"
            "Available: de, es, es-mx, fr, it, ja, ko, pl, pt-br, ru, tr, zh, zh-tw")
        dl_all_btn.clicked.connect(lambda: self._settings_download_all_lang_packs(dlg))
        lang_btn_row.addWidget(dl_all_btn)

        open_lang_btn = QPushButton("Open Locale Folder")
        open_lang_btn.setStyleSheet(f"font-size: 10px; color: {COLORS['text_dim']};")
        open_lang_btn.clicked.connect(lambda: os.startfile(os.path.join(self._app_dir(), 'locale')))
        lang_btn_row.addWidget(open_lang_btn)

        lang_btn_row.addStretch()
        layout.addLayout(lang_btn_row)

        lang_hint = QLabel(
            "Restart required after changing language.\n"
            "UI translations go in locale/ folder. Name packs go in locale/ or language/ folder.\n"
            "Community can contribute translations on GitHub."
        )
        lang_hint.setStyleSheet(f"color: {COLORS['text_dim']}; font-size: 10px;")
        lang_hint.setWordWrap(True)
        layout.addWidget(lang_hint)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        layout.addWidget(bb)

        if dlg.exec() == QDialog.Accepted:
            root = self._settings_path.text().strip()
            appdata = self._settings_appdata.text().strip()

            if self._settings_use_current.isChecked() and self._loaded_path:
                root = os.path.dirname(os.path.dirname(self._loaded_path))

            if root:
                self._config["save_root"] = root
            elif "save_root" in self._config:
                del self._config["save_root"]
            if appdata:
                self._config["custom_localappdata"] = appdata
            elif "custom_localappdata" in self._config:
                del self._config["custom_localappdata"]
            new_lang = self._settings_lang.currentData()
            current_lang = (
                self._config.get("default_lang")
                or self._config.get("language")
                or "en"
            )
            if new_lang and new_lang != current_lang:
                self._config["language"] = new_lang
                self._config["default_lang"] = new_lang
                set_language(new_lang)
                try:
                    import gui_i18n as _gi
                    _gi.set_language(new_lang)
                except Exception:
                    pass
                QMessageBox.information(self, "Language",
                    f"Language set to {self._settings_lang.currentText()}.\n\n"
                    f"Restart the editor for full effect.")

            self._save_config()
            self._refresh_sidebar()

    def _settings_export_lang_template(self, parent_dlg) -> None:
        from localization import export_template
        locale_dir = os.path.join(self._app_dir(), 'locale')
        os.makedirs(locale_dir, exist_ok=True)

        path, _ = QFileDialog.getSaveFileName(
            parent_dlg, "Export Language Template",
            os.path.join(locale_dir, "template.json"),
            "JSON Files (*.json)")
        if not path:
            return

        export_template(path)
        QMessageBox.information(parent_dlg, "Template Exported",
            f"English template exported to:\n{path}\n\n"
            f"How to translate:\n"
            f"1. Open the .json file in any text editor\n"
            f"2. Change \"_language_name\" to your language\n"
            f"3. Translate the values (right side), NOT the keys (left side)\n"
            f"4. Save as <language_code>.json (e.g. ja.json, ko.json, fr.json)\n"
            f"5. Put it in the locale/ folder\n"
            f"6. Share with the community!")

    def _settings_import_lang(self, parent_dlg) -> None:
        path, _ = QFileDialog.getOpenFileName(
            parent_dlg, "Import Translation File", "",
            "JSON Files (*.json)")
        if not path:
            return

        try:
            import json as _json
            with open(path, 'r', encoding='utf-8') as f:
                data = _json.load(f)

            if not isinstance(data, dict):
                QMessageBox.warning(parent_dlg, "Invalid File", "Not a valid translation file.")
                return

            lang_name = data.get('_language_name', '')
            if not lang_name:
                QMessageBox.warning(parent_dlg, "Invalid File",
                    "Missing \"_language_name\" field.\n\n"
                    "The .json file must have a \"_language_name\" key\n"
                    "with the language display name (e.g. \"日本語\").")
                return

            locale_dir = os.path.join(self._app_dir(), 'locale')
            os.makedirs(locale_dir, exist_ok=True)

            fname = os.path.basename(path)
            dest = os.path.join(locale_dir, fname)
            import shutil
            shutil.copy2(path, dest)

            self._settings_lang.clear()
            from localization import get_available_languages
            for code, name in get_available_languages():
                self._settings_lang.addItem(f"{name} ({code})", code)

            QMessageBox.information(parent_dlg, "Translation Imported",
                f"Imported '{lang_name}' translation.\n\n"
                f"File: {dest}\n\n"
                f"Select it from the Language dropdown and restart.")

        except Exception as e:
            QMessageBox.critical(parent_dlg, "Import Error", str(e))

    def _settings_download_name_pack(self, parent_dlg) -> None:
        lang_code = self._settings_lang.currentData()
        if not lang_code or lang_code == 'en':
            QMessageBox.information(parent_dlg, "Download",
                "English is built-in — no download needed.\n"
                "Select a different language first.")
            return

        filename = f"names_{lang_code}.json"
        url = f"https://raw.githubusercontent.com/NattKh/CRIMSON-DESERT-SAVE-EDITOR/main/language/{filename}"

        locale_dir = os.path.join(self._app_dir(), 'locale')
        os.makedirs(locale_dir, exist_ok=True)
        dest = os.path.join(locale_dir, filename)

        if os.path.isfile(dest):
            reply = QMessageBox.question(parent_dlg, "Download",
                f"{filename} already exists.\n\nRedownload and overwrite?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply != QMessageBox.Yes:
                return

        self._update_status(f"Downloading {filename}...")
        QApplication.processEvents()

        try:
            from PySide6.QtNetwork import QNetworkAccessManager, QNetworkRequest
            from PySide6.QtCore import QUrl, QEventLoop

            manager = QNetworkAccessManager()
            request = QNetworkRequest(QUrl(url))
            request.setRawHeader(b"User-Agent", b"CrimsonSaveEditor")
            reply_obj = manager.get(request)

            loop = QEventLoop()
            reply_obj.finished.connect(loop.quit)
            loop.exec()

            if reply_obj.error():
                import urllib.request
                urllib.request.urlretrieve(url, dest)
            else:
                data = reply_obj.readAll().data()
                with open(dest, 'wb') as f:
                    f.write(data)

            if os.path.isfile(dest) and os.path.getsize(dest) > 100:
                size_kb = os.path.getsize(dest) / 1024
                self._update_status(f"Downloaded {filename} ({size_kb:.0f}KB)")
                QMessageBox.information(parent_dlg, "Download Complete",
                    f"Downloaded {filename} ({size_kb:.0f}KB)\n\n"
                    f"17,267 translated names (items, quests, knowledge).\n"
                    f"Restart the editor for changes to take effect.")
            else:
                QMessageBox.warning(parent_dlg, "Download Failed",
                    f"Could not download {filename}.\n\n"
                    f"Try downloading manually from:\n{url}\n\n"
                    f"Place it in: {locale_dir}")

        except Exception as e:
            try:
                import urllib.request
                self._update_status(f"Downloading {filename} (fallback)...")
                QApplication.processEvents()
                urllib.request.urlretrieve(url, dest)
                size_kb = os.path.getsize(dest) / 1024
                QMessageBox.information(parent_dlg, "Download Complete",
                    f"Downloaded {filename} ({size_kb:.0f}KB)\n\nRestart for changes.")
            except Exception as e2:
                QMessageBox.critical(parent_dlg, "Download Error",
                    f"Failed: {e2}\n\nDownload manually from:\n{url}")

    def _settings_download_all_lang_packs(self, parent_dlg) -> None:
        import urllib.request

        LANGS = ['de', 'es', 'es-mx', 'fr', 'it', 'ja', 'ko', 'pl', 'pt-br', 'ru', 'tr', 'zh', 'zh-tw']
        GITHUB_BASE = "https://raw.githubusercontent.com/NattKh/CRIMSON-DESERT-SAVE-EDITOR/main"

        locale_dir = os.path.join(self._app_dir(), 'locale')
        os.makedirs(locale_dir, exist_ok=True)

        reply = QMessageBox.question(parent_dlg, "Download All Language Packs",
            f"Download {len(LANGS)} language packs from GitHub?\n\n"
            "Languages: " + ", ".join(LANGS) + "\n\n"
            "This downloads both UI translations (locale/) and name packs.\n"
            "Existing files will be overwritten with latest versions.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply != QMessageBox.Yes:
            return

        downloaded = 0
        errors = 0
        files_to_download = []

        for lang in LANGS:
            files_to_download.append((
                f"{GITHUB_BASE}/language/names_{lang}.json",
                os.path.join(locale_dir, f"names_{lang}.json"),
                f"names_{lang}.json"
            ))
        for lang in LANGS:
            files_to_download.append((
                f"{GITHUB_BASE}/locale/{lang}.json",
                os.path.join(locale_dir, f"{lang}.json"),
                f"{lang}.json"
            ))

        for url, dest, fname in files_to_download:
            self._update_status(f"Downloading {fname}...")
            QApplication.processEvents()
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "CrimsonSaveEditor"})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = resp.read()
                if data and len(data) > 50:
                    with open(dest, 'wb') as f:
                        f.write(data)
                    downloaded += 1
            except Exception:
                errors += 1

        self._update_status(f"Downloaded {downloaded} language files ({errors} not available)")
        QMessageBox.information(parent_dlg, "Download Complete",
            f"Downloaded {downloaded} language files.\n"
            f"{errors} files not available on GitHub yet (UI translations pending).\n\n"
            f"Restart the editor to apply.\n"
            f"Select your language in Settings after restart.")

    def _settings_browse_path(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Select Save Folder")
        if path:
            self._settings_path.setText(path)


    @staticmethod
    def _make_wip_banner() -> QLabel:
        wip = QLabel("WORK IN PROGRESS")
        wip.setAlignment(Qt.AlignCenter)
        wip.setStyleSheet(
            "color: #ff3333; font-size: 28px; font-weight: bold; "
            "padding: 12px; border: 3px solid #ff3333; border-radius: 8px; "
            "background-color: rgba(255, 50, 50, 0.1);"
        )
        return wip

    def _build_menu(self) -> None:
        menu_bar = self.menuBar()

        file_menu = menu_bar.addMenu("File")

        open_save = QAction("Open Save File (.save)...", self)
        open_save.setShortcut(QKeySequence("Ctrl+O"))
        open_save.triggered.connect(self._open_save_file)
        file_menu.addAction(open_save)

        open_raw = QAction("Open Raw Stream (.bin)...", self)
        open_raw.triggered.connect(self._open_raw_stream)
        file_menu.addAction(open_raw)

        auto_find = QAction("Auto-Find Save Files...", self)
        auto_find.triggered.connect(self._auto_find_save)
        file_menu.addAction(auto_find)

        file_menu.addSeparator()

        save_act = QAction("Save", self)
        save_act.setShortcut(QKeySequence("Ctrl+S"))
        save_act.triggered.connect(self._save_file)
        file_menu.addAction(save_act)

        save_as = QAction("Save As...", self)
        save_as.setShortcut(QKeySequence("Ctrl+Shift+S"))
        save_as.triggered.connect(self._save_file_as)
        file_menu.addAction(save_as)

        file_menu.addSeparator()

        exit_act = QAction("Exit", self)
        exit_act.setShortcut(QKeySequence("Alt+F4"))
        exit_act.triggered.connect(self.close)
        file_menu.addAction(exit_act)

        edit_menu = menu_bar.addMenu("Edit")

        undo_act = QAction("Undo", self)
        undo_act.setShortcut(QKeySequence("Ctrl+Z"))
        undo_act.triggered.connect(self._undo)
        edit_menu.addAction(undo_act)

        edit_menu.addSeparator()
        font_menu = edit_menu.addMenu("Font Size")
        for label, scale in [("100% (Default)", 1.0), ("125%", 1.25), ("150%", 1.5), ("200%", 2.0)]:
            act = QAction(label, self)
            act.setData(scale)
            act.triggered.connect(lambda checked, s=scale: self._set_font_scale(s))
            font_menu.addAction(act)

        widget_menu = edit_menu.addMenu("UI Scale")
        for label, scale in [("75% (Compact)", 0.75), ("100% (Default)", 1.0), ("125%", 1.25), ("150%", 1.5), ("200%", 2.0)]:
            act = QAction(label, self)
            act.triggered.connect(lambda checked, s=scale: self._set_widget_scale(s))
            widget_menu.addAction(act)

        help_menu = menu_bar.addMenu("Help")
        about_act = QAction("About", self)
        about_act.triggered.connect(self._show_about)
        help_menu.addAction(about_act)

        change_lang_act = QAction("Change Language...", self)
        change_lang_act.setToolTip("Download a language pack and switch the UI language.")
        change_lang_act.triggered.connect(self._open_language_picker)
        help_menu.addAction(change_lang_act)

        discord_act = QAction("💬 Discord (community help)", self)
        discord_act.setToolTip("Opens the Crimson Desert Modding Discord — "
                               "support, mod sharing, bug reports.")
        discord_act.triggered.connect(lambda: self._open_discord())
        help_menu.addAction(discord_act)

        # Top-level Discord shortcut in the menu bar itself — more
        # discoverable than burying it inside Help.
        discord_top = QAction("Discord", self)
        discord_top.setToolTip(
            "Join the Crimson Desert Modding community Discord "
            "(https://discord.gg/nBqSzunyFS).")
        discord_top.triggered.connect(lambda: self._open_discord())
        menu_bar.addAction(discord_top)

        update_menu = menu_bar.addMenu("Update")
        check_update_act = QAction(f"Check for Updates  (current: v{APP_VERSION})", self)
        check_update_act.triggered.connect(self._check_for_update)
        update_menu.addAction(check_update_act)

        changelog_act = QAction("Changelog / Releases", self)
        changelog_act.triggered.connect(lambda: __import__('webbrowser').open(
            "https://github.com/NattKh/CRIMSON-DESERT-SAVE-EDITOR-AND-GAME-MODS/releases"))
        update_menu.addAction(changelog_act)

        view_menu = menu_bar.addMenu("View")
        self._global_icons_action = QAction("Show Icons", self)
        self._global_icons_action.setCheckable(True)
        self._global_icons_action.setChecked(False)
        self._global_icons_action.triggered.connect(self._global_toggle_icons)
        view_menu.addAction(self._global_icons_action)
        view_menu.addSeparator()

        self._compact_action = QAction("Compact Mode", self)
        self._compact_action.setCheckable(True)
        self._compact_action.setChecked(self._config.get("compact_mode", False))
        self._compact_action.triggered.connect(self._toggle_compact_mode)
        view_menu.addAction(self._compact_action)

        scale_menu = view_menu.addMenu("UI Scale")
        self._scale_group = QActionGroup(self)
        saved_scale = self._config.get("ui_scale", 100)
        for pct in [50, 60, 70, 80, 90, 100, 110, 120]:
            act = QAction(f"{pct}%", self)
            act.setCheckable(True)
            act.setChecked(pct == saved_scale)
            act.triggered.connect(lambda checked, p=pct: self._set_ui_scale(p))
            self._scale_group.addAction(act)
            scale_menu.addAction(act)

        view_menu.addSeparator()

        zoom_in_act = QAction("Zoom In", self)
        zoom_in_act.setShortcut(QKeySequence("Ctrl+="))
        zoom_in_act.triggered.connect(self._zoom_in)
        view_menu.addAction(zoom_in_act)

        zoom_out_act = QAction("Zoom Out", self)
        zoom_out_act.setShortcut(QKeySequence("Ctrl+-"))
        zoom_out_act.triggered.connect(self._zoom_out)
        view_menu.addAction(zoom_out_act)

        zoom_reset_act = QAction("Reset Zoom", self)
        zoom_reset_act.setShortcut(QKeySequence("Ctrl+0"))
        zoom_reset_act.triggered.connect(self._zoom_reset)
        view_menu.addAction(zoom_reset_act)

        view_menu.addSeparator()

        theme_menu = view_menu.addMenu("Theme")
        theme_group = QActionGroup(self)
        cur_theme = self._config.get("theme", "dark")
        for label, key in [("Dark (default)", "dark"), ("Light (high contrast)", "light")]:
            act = QAction(label, self)
            act.setCheckable(True)
            act.setChecked(cur_theme == key)
            act.triggered.connect(lambda checked, k=key: self._set_theme(k))
            theme_group.addAction(act)
            theme_menu.addAction(act)

        view_menu.addSeparator()

        panels_menu = view_menu.addMenu("Show / Hide Panels")

        self._show_gamepath_action = QAction("Game Path Bar", self)
        self._show_gamepath_action.setCheckable(True)
        self._show_gamepath_action.setChecked(
            self._config.get("show_gamepath_bar", True))
        self._show_gamepath_action.triggered.connect(self._menu_toggle_gamepath)
        panels_menu.addAction(self._show_gamepath_action)

        view_menu.addSeparator()

        self._view_menu = view_menu
        self._tab_actions = []

        guides_menu = menu_bar.addMenu("Guides")
        for guide_name, guide_key in [
            ("Inventory", "inventory"),
            ("Item Swap", "swap"),
            ("Repurchase (Vendor Swap)", "repurchase"),
            ("Equipment (Enchanting)", "equipment"),
            ("Sockets (Abyss Gear)", "sockets"),
            ("Item Packs", "packs"),
            ("Community Mapping", "community"),
            ("ItemBuffs (Stat Editing)", "itembuffs"),
            ("Nexus Gate Locations", "waypoint"),
            ("Game Data Editor", "gamedata"),
            ("Knowledge Editor", "knowledge"),
        ]:
            act = QAction(guide_name, self)
            act.triggered.connect(lambda checked, k=guide_key: self._show_guide(k))
            guides_menu.addAction(act)

        guides_menu.addSeparator()

        _video_guides = [
            ("VIDEO: Drop Rate & Loot Table Editor + Packs Guide",
             "https://www.youtube.com/watch?v=-yZ3EtZGFf4&t=3s"),
            ("VIDEO: How to Change In-Game Drop Rate & Loot Table",
             "https://www.youtube.com/watch?v=oMUQ1w0DZqk&t=5s"),
            ("VIDEO: How to Modify Base Stats on Any Armor & Weapon",
             "https://www.youtube.com/watch?v=Fxc3Wn2dImk"),
            ("VIDEO: Item Editor — Mod Any Item",
             "https://www.youtube.com/watch?v=nDOP6OKI1_E"),
            ("VIDEO: How to Dye Any Color Without Unlocking",
             "https://www.youtube.com/watch?v=W7S3YWWYspw"),
        ]
        for title, url in _video_guides:
            act = QAction(title, self)
            act.triggered.connect(lambda checked, u=url: __import__('webbrowser').open(u))
            guides_menu.addAction(act)

        dev_menu = menu_bar.addMenu("Dev")
        self._experimental_action = QAction("Enable Experimental Mode", self)
        self._experimental_action.setCheckable(True)
        self._experimental_action.setChecked(self._experimental_mode)
        self._experimental_action.triggered.connect(self._toggle_experimental_mode)
        dev_menu.addAction(self._experimental_action)


    def _rebuild_view_tab_list(self) -> None:
        if not hasattr(self, '_tabs'):
            return
        for action in self._tab_actions:
            self._view_menu.removeAction(action)
        self._tab_actions.clear()

        _real_tabs = self._tabs
        sub_groups = [
            ("Save Editor", self._save_tabs),
            ("Game Mods", self._mods_tabs),
            ("Items", self._items_tabs),
            ("World", self._world_tabs),
        ]

        for group_name, sub_widget in sub_groups:
            group_idx = _real_tabs.indexOf(sub_widget)

            header = QAction(f"--- {group_name} ---", self)
            header.setEnabled(False)
            self._view_menu.addAction(header)
            self._tab_actions.append(header)

            for si in range(sub_widget.count()):
                if hasattr(sub_widget, "isTabVisible") and not sub_widget.isTabVisible(si):
                    continue
                sub_name = sub_widget.tabText(si)
                action = QAction(f"    {sub_name}", self)
                action.triggered.connect(
                    lambda checked, g=group_idx, s=si, sw=sub_widget:
                    (_real_tabs.setCurrentIndex(g), sw.setCurrentIndex(s))
                )
                self._view_menu.addAction(action)
                self._tab_actions.append(action)

        self._view_menu.addSeparator()
        for ti in range(_real_tabs.count()):
            name = _real_tabs.tabText(ti)
            if name not in [g[0] for g in sub_groups]:
                action = QAction(name, self)
                action.triggered.connect(
                    lambda checked, idx=ti: _real_tabs.setCurrentIndex(idx)
                )
                self._view_menu.addAction(action)
                self._tab_actions.append(action)

    def _goto_subtab(self, group_widget, tab_widget) -> None:
        _real_tabs = getattr(self, '_real_tabs', None)
        if not _real_tabs:
            for attr in ['_save_tabs', '_mods_tabs', '_items_tabs', '_world_tabs']:
                parent = getattr(self, attr, None)
                if parent and parent.indexOf(tab_widget) >= 0:
                    group_widget = parent
                    break
            _real_tabs = self._tabs

        group_idx = _real_tabs.indexOf(group_widget)
        sub_idx = group_widget.indexOf(tab_widget)
        if group_idx >= 0:
            _real_tabs.setCurrentIndex(group_idx)
        if sub_idx >= 0:
            group_widget.setCurrentIndex(sub_idx)

    def _on_tab_changed_view_menu(self, index: int) -> None:
        if len(self._tab_actions) != self._tabs.count():
            self._rebuild_view_tab_list()


    def _set_ui_scale(self, pct: int) -> None:
        self._config["ui_scale"] = pct
        self._save_config()
        self._apply_ui_settings()

    def _set_theme(self, mode: str) -> None:
        self._config["theme"] = mode
        self._save_config()
        app = QApplication.instance()
        if app is None:
            return
        # Mutate the COLORS dict to the new palette.
        apply_theme(app, mode)
        # _apply_ui_settings rebuilds the ENTIRE stylesheet from COLORS
        # values live — this is what makes the UI-scale menu also change
        # colors (we piggyback on that path for theme changes too).
        try:
            self._apply_ui_settings()
        except Exception as e:
            log.warning("theme _apply_ui_settings failed: %s", e)
        # Force all widgets to re-query style, in case any sub-panel holds
        # its own inline stylesheet that needs repolish.
        try:
            style = app.style()
            for w in app.allWidgets():
                try:
                    style.unpolish(w)
                    style.polish(w)
                    w.update()
                except Exception:
                    pass
        except Exception:
            pass

    def _toggle_compact_mode(self, checked: bool) -> None:
        self._config["compact_mode"] = checked
        self._save_config()
        self._apply_ui_settings()

    def _zoom_in(self) -> None:
        self._zoom_factor = min(2.0, round(self._zoom_factor + 0.1, 2))
        self._apply_zoom()

    def _zoom_out(self) -> None:
        self._zoom_factor = max(0.5, round(self._zoom_factor - 0.1, 2))
        self._apply_zoom()

    def _zoom_reset(self) -> None:
        self._zoom_factor = 1.0
        self._apply_zoom()

    def _apply_zoom(self) -> None:
        try:
            app = QApplication.instance()
            if app is None:
                return
            f = app.font()
            f.setPointSizeF(self._base_point_size * self._zoom_factor)
            app.setFont(f)
            for w in app.allWidgets():
                try:
                    w.setFont(f)
                    w.updateGeometry()
                except Exception:
                    pass
            try:
                from PySide6.QtWidgets import QToolBar
                icon_size = int(24 * self._zoom_factor)
                for tb in self.findChildren(QToolBar):
                    tb.setIconSize(QSize(icon_size, icon_size))
            except Exception:
                pass
            self._config["ui_zoom"] = self._zoom_factor
            self._save_config()
            try:
                self._update_status(f"Zoom: {int(self._zoom_factor * 100)}%")
            except Exception:
                pass
        except Exception as e:
            log.warning("apply_zoom failed: %s", e)

    def _apply_ui_settings(self) -> None:
        scale = self._config.get("ui_scale", 100) / 100.0
        compact = self._config.get("compact_mode", False)

        if compact:
            font_main = 11
            font_table = 10
            pad_btn_v, pad_btn_h = 3, 10
            pad_input = 3
            pad_tab_v, pad_tab_h = 4, 10
            pad_cell_v, pad_cell_h = 1, 4
            pad_header_v, pad_header_h = 3, 5
            pad_group_top = 8
            pad_group_margin = 6
            checkbox_sz = 13
            scrollbar_w = 8
        else:
            font_main = 13
            font_table = 12
            pad_btn_v, pad_btn_h = 6, 16
            pad_input = 5
            pad_tab_v, pad_tab_h = 8, 18
            pad_cell_v, pad_cell_h = 3, 6
            pad_header_v, pad_header_h = 5, 8
            pad_group_top = 14
            pad_group_margin = 10
            checkbox_sz = 16
            scrollbar_w = 12

        def s(v):
            return max(1, int(v * scale))

        sheet = f"""
QMainWindow, QWidget {{
    background-color: {COLORS['bg']};
    color: {COLORS['text']};
    font-family: Consolas, 'Courier New', monospace;
    font-size: {s(font_main)}px;
}}
QMenuBar {{
    background-color: {COLORS['header']};
    color: {COLORS['text']};
    border-bottom: 1px solid {COLORS['border']};
    padding: {s(2)}px;
}}
QMenuBar::item:selected {{
    background-color: {COLORS['selected']};
}}
QMenu {{
    background-color: {COLORS['panel']};
    color: {COLORS['text']};
    border: 1px solid {COLORS['border']};
}}
QMenu::item:selected {{
    background-color: {COLORS['selected']};
}}
QTabWidget::pane {{
    border: 1px solid {COLORS['border']};
    background-color: {COLORS['bg']};
}}
QTabBar::tab {{
    background-color: {COLORS['panel']};
    color: {COLORS['text']};
    padding: {s(pad_tab_v)}px {s(pad_tab_h)}px;
    margin-right: {s(2)}px;
    border-top-left-radius: {s(4)}px;
    border-top-right-radius: {s(4)}px;
    border: 1px solid {COLORS['border']};
    border-bottom: none;
}}
QTabBar::tab:selected {{
    background-color: {_TAB_SELECTED_BG};
    color: {_TAB_SELECTED_COLOR};
    border-bottom: {s(3)}px solid {_TAB_SELECTED_BORDER};
    font-weight: bold;
}}
QTabBar::tab:hover {{
    background-color: {COLORS['selected']};
}}
QTableWidget {{
    background-color: {COLORS['panel']};
    color: {COLORS['text']};
    gridline-color: {COLORS['border']};
    selection-background-color: {COLORS['selected']};
    selection-color: white;
    border: 1px solid {COLORS['border']};
    font-family: Consolas, monospace;
    font-size: {s(font_table)}px;
}}
QTableWidget::item {{
    padding: {s(pad_cell_v)}px {s(pad_cell_h)}px;
}}
QHeaderView::section {{
    background-color: {COLORS['header']};
    color: {COLORS['text']};
    padding: {s(pad_header_v)}px {s(pad_header_h)}px;
    border: 1px solid {COLORS['border']};
    font-weight: bold;
}}
QPushButton {{
    background-color: {COLORS['header']};
    color: {COLORS['text']};
    border: 1px solid {COLORS['border']};
    padding: {s(pad_btn_v)}px {s(pad_btn_h)}px;
    border-radius: {s(3)}px;
    font-weight: bold;
}}
QPushButton:hover {{
    background-color: {COLORS['selected']};
    border-color: {COLORS['accent']};
}}
QPushButton:pressed {{
    background-color: {COLORS['accent']};
}}
QPushButton#accentBtn {{
    background-color: {COLORS['accent']};
    color: white;
}}
QPushButton#accentBtn:hover {{
    background-color: #e8b85e;
}}
QLineEdit, QSpinBox, QComboBox {{
    background-color: {COLORS['input_bg']};
    color: {COLORS['text']};
    border: 1px solid {COLORS['border']};
    padding: {s(pad_input)}px {s(pad_input + 3)}px;
    border-radius: {s(3)}px;
}}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus {{
    border-color: {COLORS['accent']};
}}
QComboBox::drop-down {{
    border: none;
    background-color: {COLORS['header']};
    width: {s(24)}px;
}}
QComboBox QAbstractItemView {{
    background-color: {COLORS['panel']};
    color: {COLORS['text']};
    selection-background-color: {COLORS['selected']};
    border: 1px solid {COLORS['border']};
}}
QGroupBox {{
    color: {COLORS['text']};
    border: 1px solid {COLORS['border']};
    border-radius: {s(4)}px;
    margin-top: {s(pad_group_margin)}px;
    padding-top: {s(pad_group_top)}px;
    font-weight: bold;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: {s(10)}px;
    padding: 0 {s(5)}px;
}}
QStatusBar {{
    background-color: {COLORS['header']};
    color: {COLORS['text']};
    border-top: 1px solid {COLORS['border']};
}}
QListWidget {{
    background-color: {COLORS['panel']};
    color: {COLORS['text']};
    border: 1px solid {COLORS['border']};
    selection-background-color: {COLORS['selected']};
}}
QTextEdit {{
    background-color: {COLORS['panel']};
    color: {COLORS['text']};
    border: 1px solid {COLORS['border']};
}}
QScrollBar:vertical {{
    background-color: {COLORS['bg']};
    width: {s(scrollbar_w)}px;
    border: none;
}}
QScrollBar::handle:vertical {{
    background-color: {COLORS['border']};
    border-radius: {s(4)}px;
    min-height: {s(30)}px;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0px;
}}
QScrollBar:horizontal {{
    background-color: {COLORS['bg']};
    height: {s(scrollbar_w)}px;
    border: none;
}}
QScrollBar::handle:horizontal {{
    background-color: {COLORS['border']};
    border-radius: {s(4)}px;
    min-width: {s(30)}px;
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0px;
}}
QCheckBox {{
    color: {COLORS['text']};
    spacing: {s(6)}px;
}}
QCheckBox::indicator {{
    width: {s(checkbox_sz)}px;
    height: {s(checkbox_sz)}px;
}}
"""
        self.setStyleSheet(sheet)

        min_w = max(400, int(800 * scale))
        min_h = max(300, int(600 * scale))
        self.setMinimumSize(min_w, min_h)

        if hasattr(self, '_save_dock'):
            scaled_sidebar = max(140, int(260 * scale))
            docks, widths = [], []
            if not self._sb_collapsed:
                self._sb_saved_width = scaled_sidebar
                docks.append(self._save_dock); widths.append(scaled_sidebar)
            if not self._ps_collapsed:
                self._ps_saved_width = scaled_sidebar
                docks.append(self._pack_dock); widths.append(scaled_sidebar)
            if docks:
                self.resizeDocks(docks, widths, Qt.Horizontal)

        row_h = max(16, int(22 * scale))
        if compact:
            row_h = max(14, int(18 * scale))
        for table in self.findChildren(QTableWidget):
            table.verticalHeader().setDefaultSectionSize(row_h)

        from PySide6.QtWidgets import QTreeWidget
        for tree in self.findChildren(QTreeWidget):
            tree.setIndentation(max(6, int(16 * scale)))

        for btn in self.findChildren(QPushButton):
            h = btn.maximumHeight()
            if 20 <= h <= 24:
                btn.setFixedHeight(max(16, int(22 * scale)))

        if hasattr(self, '_center_status'):
            self._center_status.setFixedHeight(max(20, int(36 * scale)))


    def _global_toggle_icons(self, checked: bool) -> None:
        if self._icons_enabled != checked:
            self._toggle_icons()
        if hasattr(self, '_buffs_tab'):
            self._buffs_tab.set_icons_enabled(checked)
        if hasattr(self, '_store_tab'):
            self._store_tab.set_icons_enabled(checked)
        self._global_icons_action.setChecked(checked)


    def _toggle_experimental_mode(self, checked: bool) -> None:
        if checked:
            reply = QMessageBox.warning(
                self, "Enable Advanced / Dev Mode",
                "Advanced mode unlocks experimental features and export options.\n\n"
                "By enabling this you agree to the following:\n\n"
                "  - Experimental features may corrupt saves or crash the game\n"
                "  - Export as Mod / CDUMM / JSON are UNSUPPORTED\n"
                "  - We do NOT provide help for exported mod packages\n"
                "  - Contact the mod loader developer for loader issues\n"
                "  - Bug reports about exported mods will be closed\n\n"
                "Apply to Game remains the supported deployment method.\n\n"
                "Do you want to continue?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                self._experimental_action.setChecked(False)
                return

        self._experimental_mode = checked
        self._config["experimental_mode"] = checked
        self._save_config()
        self._experimental_action.setChecked(checked)
        self._update_experimental_tabs()
        self._update_status(
            "Experimental mode ENABLED — safeguards lifted"
            if checked else "Experimental mode disabled"
        )

    def _update_experimental_tabs(self) -> None:
        if hasattr(self, '_buffs_tab'):
            self._buffs_tab.set_experimental_mode(self._experimental_mode)
        if hasattr(self, '_community_tab'):
            self._community_tab.set_experimental_mode(self._experimental_mode)
        if hasattr(self, '_inventory_tab'):
            self._inventory_tab.set_experimental_mode(self._experimental_mode)
        if hasattr(self, '_swap_tab'):
            self._swap_tab.set_experimental_mode(self._experimental_mode)
        if hasattr(self, '_btn_know'):
            self._btn_know.setVisible(self._experimental_mode)
        if hasattr(self, '_storage_apply_btn'):
            self._storage_apply_btn.setVisible(self._experimental_mode)
        if hasattr(self, '_storage_check_btn'):
            self._storage_check_btn.setVisible(self._experimental_mode)
        if hasattr(self, '_repurchase_tab'):
            self._repurchase_tab.set_experimental_mode(self._experimental_mode)
        if hasattr(self, '_effect_grp'):
            self._effect_grp.setVisible(self._experimental_mode)
        if hasattr(self, '_inf_dura_check'):
            self._inf_dura_check.setVisible(self._experimental_mode)
        if hasattr(self, '_ride_grp'):
            self._ride_grp.setVisible(self._experimental_mode)
        if hasattr(self, '_ride_patch_btn'):
            self._ride_patch_btn.setVisible(self._experimental_mode)
        if hasattr(self, '_ride_check_btn'):
            self._ride_check_btn.setVisible(self._experimental_mode)
        if hasattr(self, '_unlock_all_dev_btn'):
            self._unlock_all_dev_btn.setVisible(self._experimental_mode)
        if hasattr(self, '_dye_tab'):
            self._dye_tab.set_experimental_mode(self._experimental_mode)
        # Game Mods tab export buttons (dev-gated, unsupported)
        for tab_attr in ('_field_edit_tab_obj', '_patches_tab', '_store_tab',
                         '_dropset_tab', '_spawn_tab', '_mercpets_tab'):
            tab = getattr(self, tab_attr, None)
            if tab and hasattr(tab, 'set_experimental_mode'):
                tab.set_experimental_mode(self._experimental_mode)
        for w in getattr(self, '_dev_mount_widgets', []):
            w.setVisible(self._experimental_mode)
        for attr, label_key in [
            ('_pabgb_browser_tab_widget', 'tab.pabgb_browser'),
        ]:
            widget = getattr(self, attr, None)
            if widget is not None:
                idx = self._tabs.indexOf(widget)
                if self._experimental_mode:
                    if idx < 0:
                        self._tabs.addTab(widget, tr(label_key))
                else:
                    if idx >= 0:
                        self._tabs.removeTab(idx)

        if hasattr(self, '_faction_tab_widget') and self._faction_tab_widget is not None:
            parent = self._world_tabs if hasattr(self, '_world_tabs') else self._tabs
            idx = parent.indexOf(self._faction_tab_widget)
            if self._experimental_mode:
                if idx < 0:
                    parent.addTab(self._faction_tab_widget, 'Faction')
            else:
                if idx >= 0:
                    parent.removeTab(idx)
        if hasattr(self, '_view_menu'):
            self._rebuild_view_tab_list()


    def _check_for_update(self) -> None:
        self._update_status("Checking for updates...")
        QApplication.processEvents()

        available, remote_ver, url = check_for_update()

        if not available:
            if remote_ver:
                QMessageBox.information(
                    self, "No Update Available",
                    f"You are on the latest version.\n\n"
                    f"Current: v{APP_VERSION}\nRemote: v{remote_ver}",
                )
            else:
                QMessageBox.warning(
                    self, "Update Check Failed",
                    "Could not reach the update server.\n"
                    "Check your internet connection.",
                )
            self._update_status("Update check complete.")
            return

        if sys.platform != 'win32':
            from PySide6.QtGui import QDesktopServices
            from PySide6.QtCore import QUrl
            reply = QMessageBox.information(
                self, "Update Available",
                f"A new version is available!\n\n"
                f"Current: v{APP_VERSION}\n"
                f"New: v{remote_ver}\n\n"
                f"Auto-update is only available on Windows.\n"
                f"Please download the Linux build manually from GitHub Releases.",
                QMessageBox.Open | QMessageBox.Cancel,
                QMessageBox.Open,
            )
            if reply == QMessageBox.Open:
                QDesktopServices.openUrl(QUrl("https://github.com/NattKh/CRIMSON-DESERT-SAVE-EDITOR/releases"))
            self._update_status("Update check complete.")
            return

        reply = QMessageBox.question(
            self, "Update Available",
            f"A new version is available!\n\n"
            f"Current: v{APP_VERSION}\n"
            f"New: v{remote_ver}\n\n"
            f"Download and install now?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if reply != QMessageBox.Yes:
            return

        self._update_status(f"Downloading v{remote_ver}...")
        QApplication.processEvents()

        def on_progress(downloaded, total):
            if total > 0:
                pct = downloaded * 100 // total
                self._update_status(f"Downloading v{remote_ver}... {pct}%  ({downloaded // 1024}KB)")
            else:
                self._update_status(f"Downloading v{remote_ver}... {downloaded // 1024}KB")
            QApplication.processEvents()

        update_path = download_update(url, progress_callback=on_progress)

        if not update_path:
            QMessageBox.critical(
                self, "Download Failed",
                "Failed to download the update. Try again later.",
            )
            self._update_status("Update download failed.")
            return

        self._update_status(f"Update downloaded. Restarting...")
        QApplication.processEvents()

        QMessageBox.information(
            self, "Update Ready",
            f"v{remote_ver} downloaded successfully.\n\n"
            "The editor will now close and update.\n\n"
            "Please reopen CrimsonSaveEditor.exe after it closes.",
        )

        apply_update_and_restart(update_path)


    def _enrich_vendor_names(self) -> None:
        store_items = [it for it in self._items if it.source == "Sold to Vendor"]
        if not store_items:
            return

        if not hasattr(self, '_repurchase_tab'):
            return
        repurch = self._repurchase_tab.scan_repurchase_items()
        vendor_by_offset = {it.offset: it.source for it in repurch}

        for item in store_items:
            if item.offset in vendor_by_offset:
                item.source = vendor_by_offset[item.offset]

    def _nav_to_swap(self) -> None:
        self._tabs.setCurrentIndex(self._tabs.indexOf(self._save_tabs))
        if hasattr(self, '_swap_tab'):
            swap_idx = self._save_tabs.indexOf(self._swap_tab)
            if swap_idx >= 0:
                self._save_tabs.setCurrentIndex(swap_idx)

    def _open_quest_editor(self) -> None:
        if not self._save_data:
            QMessageBox.warning(self, "Quest Editor", "Load a save file first.")
            return

        dlg = QuestEditorWindow(self._save_data, self._loaded_path, self)
        dlg.exec()
        if dlg.dirty:
            self._dirty = True
            self._update_status("Quest changes made — save with Ctrl+S")


    def _set_game_path(self, path: str) -> None:
        self._config["game_install_path"] = path
        self._save_config()
        self._iteminfo_cache.set_game_path(path)
        if hasattr(self, '_paz_game_path'):
            self._paz_game_path.setText(path)
        if hasattr(self, '_paz_manager'):
            self._paz_manager.game_path = path
        if hasattr(self, '_global_game_path'):
            self._global_game_path.setText(path)
            self._global_game_path.setToolTip(path)
        if hasattr(self, '_gamedata_tab'):
            self._gamedata_tab.set_game_path(path)
            self._gd_editor = getattr(self._gamedata_tab, '_gd_editor', None)
        if hasattr(self, '_store_tab'):
            self._store_tab.set_game_path(path)
            self._store_parser_v2 = getattr(self._store_tab, '_store_parser_v2', None)
        self._cmod_loader = None
        self._asi_manager = None
        if hasattr(self, '_cmod_refresh'):
            self._cmod_refresh()
        if hasattr(self, '_asi_refresh'):
            self._asi_refresh()
        if hasattr(self, '_patches_tab'):
            self._patches_tab.set_game_path(path)
        if hasattr(self, '_skills_tab_obj'):
            self._skills_tab_obj.set_game_path(path)
        if hasattr(self, '_buffs_tab'):
            self._buffs_tab.set_game_path(path)
        if hasattr(self, '_field_edit_tab_obj'):
            self._field_edit_tab_obj.set_game_path(path)
        if hasattr(self, '_bagspace_tab'):
            self._bagspace_tab.set_game_path(path)
        if hasattr(self, '_load_manager_tab'):
            self._load_manager_tab.set_game_path(path)

    def _validate_game_path(self, path: str) -> bool:
        paz = os.path.join(path, "0008", "0.paz")
        paz_bak = os.path.join(path, "0008", "0.paz.sebak")
        if os.path.isfile(paz) or os.path.isfile(paz_bak):
            return True
        return False

    def _toggle_global_info(self, checked):
        self._center_status.setVisible(not checked)
        for w in self._global_info_widget.findChildren(QWidget):
            if w is not self._global_hide_btn:
                w.setVisible(not checked)
        self._global_hide_btn.setText("▼" if checked else "▲")
        self._global_hide_btn.setToolTip(
            ("Show game path bar (▼ expand)" if checked
             else "Hide game path bar (▲ collapse)"))
        if hasattr(self, '_show_gamepath_action'):
            self._show_gamepath_action.setChecked(not checked)
        self._config["show_gamepath_bar"] = not checked
        self._save_config()

    def _menu_toggle_gamepath(self, checked):
        self._global_hide_btn.setChecked(not checked)
        self._toggle_global_info(not checked)

    def _global_browse_game_path(self) -> None:
        current = self._config.get("game_install_path", "")
        path = QFileDialog.getExistingDirectory(
            self, "Select Crimson Desert Install Folder",
            current or "C:\\"
        )
        if not path:
            return
        if not self._validate_game_path(path):
            QMessageBox.warning(
                self, "Invalid Path",
                f"Could not find game files in:\n{path}\n\n"
                f"Expected: 0008/0.paz\n\n"
                f"Select the Crimson Desert root folder\n"
                f"(the one containing 0008/, meta/, bin64/)."
            )
            return
        self._set_game_path(path)

    def _global_auto_detect_path(self) -> None:
        detected = PazPatchManager.find_game_path()
        if detected:
            self._set_game_path(detected)
        else:
            QMessageBox.warning(self, "Not Found",
                "Could not auto-detect Crimson Desert.\n"
                "Use Browse to set the path manually.")


    def _on_tab_navigate_requested(self, selector: str) -> None:
        try:
            if selector == "stacker":
                stacker = getattr(self, '_stacker_tab', None)
                if stacker is not None:
                    idx = self._mods_tabs.indexOf(stacker)
                    if idx >= 0:
                        self._mods_tabs.setCurrentIndex(idx)
                        return
            elif selector.startswith("tab_index:"):
                tab = getattr(self, '_buffs_tab', None)
                if tab is not None:
                    idx = self._mods_tabs.indexOf(tab)
                    if idx >= 0:
                        self._mods_tabs.setCurrentIndex(idx)
        except Exception:
            pass

    def _open_transmog_tab(self) -> None:
        try:
            tab = getattr(self, '_buffs_tab', None)
            if tab is None:
                return
            idx = self._mods_tabs.indexOf(tab)
            if idx >= 0:
                self._mods_tabs.setCurrentIndex(idx)
            try:
                parent_idx = self._tabs.indexOf(self._mods_tabs)
                if parent_idx >= 0:
                    self._tabs.setCurrentIndex(parent_idx)
            except Exception:
                pass
        except Exception:
            pass

    def _rebuild_papgt_without(self, game_path: str, group_to_remove: str) -> str:
        try:
            import crimson_rs
            papgt_path = os.path.join(game_path, "meta", "0.papgt")
            if not os.path.isfile(papgt_path):
                return "PAPGT not found"

            papgt = crimson_rs.parse_papgt_file(papgt_path)
            original_count = len(papgt['entries'])
            papgt['entries'] = [
                e for e in papgt['entries']
                if e['group_name'] != group_to_remove
            ]
            new_count = len(papgt['entries'])

            if new_count == original_count:
                return f"PAPGT: {group_to_remove} was not registered"

            crimson_rs.write_papgt_file(papgt, papgt_path)
            remaining = [e['group_name'] for e in papgt['entries'] if int(e['group_name']) >= 36]
            extra = f" (other overlays still active: {', '.join(remaining)})" if remaining else ""
            return f"PAPGT: removed {group_to_remove} entry{extra}"
        except Exception as e:
            sebak = os.path.join(game_path, "meta", "0.papgt.sebak")
            if os.path.isfile(sebak):
                try:
                    import shutil
                    shutil.copy2(sebak, papgt_path)
                    return f"PAPGT: fell back to .sebak restore ({e})"
                except Exception as e2:
                    e = e2
            vanilla = os.path.join(game_path, "meta", "0.papgt.vanilla")
            if os.path.isfile(vanilla):
                try:
                    import shutil
                    shutil.copy2(vanilla, papgt_path)
                    return f"PAPGT: fell back to .vanilla restore ({e})"
                except Exception as e3:
                    return f"PAPGT rebuild failed (all tiers): {e} / vanilla={e3}"
            return f"PAPGT rebuild failed: {e}"


    def _set_refresh_local(self) -> None:
        sets = self._set_mgr.scan_local()
        table = self._set_table
        table.setRowCount(len(sets))
        for row, es in enumerate(sets):
            table.setItem(row, 0, QTableWidgetItem(es.name))
            table.setItem(row, 1, QTableWidgetItem(es.author))
            table.setItem(row, 2, QTableWidgetItem(str(len(es.items))))
            table.setItem(row, 3, QTableWidgetItem(es.description))
        self._set_status.setText(f"{len(sets)} local sets")

    def _set_get_selected(self) -> Optional[EquipmentSet]:
        rows = self._set_table.selectionModel().selectedRows()
        if not rows:
            return None
        idx = rows[0].row()
        sets = self._set_mgr.scan_local()
        return sets[idx] if idx < len(sets) else None

    def _set_preview(self) -> None:
        es = self._set_get_selected()
        if not es:
            QMessageBox.information(self, "No Set", "Select a set first.")
            return

        from paz_patcher import BUFF_NAMES
        lines = [f"Set: {es.name}\nAuthor: {es.author}\n{es.description}\n"]
        for si in es.items:
            lines.append(f"\n{si.item_name} (key={si.item_key}):")
            for op in si.operations:
                if op.operation == "set_value":
                    val_str = f"Lv {op.value}" if op.size_class == "rate" else f"{op.value:,}"
                    lines.append(f"  {op.stat_name} = {val_str}")
                elif op.operation == "swap_hash":
                    target_name = BUFF_NAMES.get(op.target_hash, f"0x{op.target_hash:08X}")
                    lines.append(f"  {op.stat_name} -> {target_name} = {op.value:,}")

        QMessageBox.information(self, f"Set Preview: {es.name}", "\n".join(lines))

    def _set_create_new(self) -> None:
        from PySide6.QtWidgets import QInputDialog
        import datetime
        name, ok = QInputDialog.getText(self, "New Equipment Set", "Set name:")
        if not ok or not name.strip():
            return
        author, ok2 = QInputDialog.getText(self, "New Equipment Set", "Author:")
        if not ok2:
            author = ""
        desc, ok3 = QInputDialog.getText(self, "New Equipment Set", "Description:")
        if not ok3:
            desc = ""
        es = EquipmentSet(
            name=name.strip(), author=author.strip(),
            description=desc.strip(),
            created=datetime.date.today().isoformat(),
        )
        self._set_mgr.save_set(es)
        self._set_refresh_local()
        self._set_status.setText(f"Created set '{es.name}'")

    def _set_delete(self) -> None:
        es = self._set_get_selected()
        if not es:
            return
        reply = QMessageBox.question(
            self, "Delete Set", f"Delete '{es.name}'?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            self._set_mgr.delete_set(es.filename)
            self._set_refresh_local()

    def _set_import(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import Equipment Set", "", "JSON (*.json)")
        if not path:
            return
        es = self._set_mgr.load_set_file(path)
        if es:
            self._set_mgr.save_set(es)
            self._set_refresh_local()
            self._set_status.setText(f"Imported '{es.name}'")
        else:
            QMessageBox.warning(self, "Import Failed", "Could not parse set file.")

    def _set_export(self) -> None:
        es = self._set_get_selected()
        if not es:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Equipment Set", es.filename or f"{es.name}.json", "JSON (*.json)",
        )
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self._set_mgr.export_set_json(es))
            self._set_status.setText(f"Exported '{es.name}' to {os.path.basename(path)}")

    def _set_refresh_github(self) -> None:
        self._set_status.setText("Fetching community sets...")
        QApplication.processEvents()
        ok, msg = self._set_mgr.fetch_remote_index()
        if not ok:
            self._set_status.setText(msg)
            return
        remote = self._set_mgr.get_remote_index()
        downloaded = 0
        for entry in remote:
            local_path = os.path.join(self._set_mgr.local_dir, entry.filename)
            if not os.path.isfile(local_path):
                es, dl_msg = self._set_mgr.download_set(entry.filename)
                if es:
                    downloaded += 1
        self._set_refresh_local()
        self._set_status.setText(f"{msg} Downloaded {downloaded} new sets.")

    def _backup_to_local(self) -> None:
        if not self._save_data or not self._loaded_path:
            QMessageBox.warning(self, "Backup", "Load a save file first.")
            return
        if not os.path.isfile(self._loaded_path):
            QMessageBox.warning(self, "Backup", "Save file not found on disk.")
            return

        try:
            import shutil, datetime
            slot_name = os.path.basename(os.path.dirname(self._loaded_path))
            backup_dir = os.path.join(self._app_dir(), "backups", slot_name)
            os.makedirs(backup_dir, exist_ok=True)

            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            base = os.path.basename(self._loaded_path)
            backup_name = f"{base}.{timestamp}.bak"
            backup_path = os.path.join(backup_dir, backup_name)

            shutil.copy2(self._loaded_path, backup_path)

            existing = sorted(
                [f for f in os.listdir(backup_dir) if f.endswith(".bak")],
                key=lambda f: os.path.getmtime(os.path.join(backup_dir, f)),
                reverse=True,
            )
            for old in existing[10:]:
                os.remove(os.path.join(backup_dir, old))

            self._update_status(f"Backed up to: {backup_path}")
            QMessageBox.information(self, "Backup",
                f"Save backed up to local folder.\n\n{backup_path}")
        except Exception as e:
            QMessageBox.critical(self, "Backup Error", str(e))


    @staticmethod
    def _app_dir() -> str:
        if getattr(sys, 'frozen', False):
            return os.path.dirname(sys.executable)
        return os.path.dirname(os.path.abspath(__file__))

    def _get_pack_dirs(self) -> list:
        base = self._app_dir()
        dirs = []
        for folder in ['quest_packs', 'knowledge_packs']:
            p = os.path.join(base, folder)
            os.makedirs(p, exist_ok=True)
            dirs.append(p)
        return dirs

    def _pack_browser_refresh(self) -> None:
        from PySide6.QtWidgets import QTreeWidgetItem
        self._pack_tree.clear()
        self._pack_data = {}

        import json as _json

        for pack_dir in self._get_pack_dirs():
            folder_name = os.path.basename(pack_dir)
            cat_label = "Quest Packs" if "quest" in folder_name else "Knowledge Packs"
            cat_node = QTreeWidgetItem([cat_label])
            cat_node.setForeground(0, QBrush(QColor(COLORS['accent'])))
            self._pack_tree.addTopLevelItem(cat_node)

            try:
                for fname in sorted(os.listdir(pack_dir)):
                    if not fname.endswith('.json'):
                        continue
                    path = os.path.join(pack_dir, fname)
                    try:
                        with open(path, 'r') as f:
                            pack = _json.load(f)
                        pack_name = pack.get('name', fname.replace('.json', ''))
                        pack_type = pack.get('type', 'quest' if 'quest' in folder_name else 'knowledge')
                        entries = pack.get('quests', pack.get('entries', []))
                        count = len(entries)

                        pack_node = QTreeWidgetItem([f"{pack_name} ({count})"])
                        pack_node.setData(0, Qt.UserRole, path)
                        pack_node.setData(0, Qt.UserRole + 1, pack_type)
                        cat_node.addChild(pack_node)
                        self._pack_data[path] = pack

                        for entry in entries:
                            key = entry.get('key', '')
                            display = entry.get('display', entry.get('name', ''))
                            label = f"{key}: {display}" if display else str(key)
                            child = QTreeWidgetItem([label])
                            child.setData(0, Qt.UserRole, path)
                            child.setData(0, Qt.UserRole + 1, pack_type)
                            child.setData(0, Qt.UserRole + 2, key)
                            child.setForeground(0, QBrush(QColor(COLORS['text_dim'])))
                            pack_node.addChild(child)

                    except Exception:
                        pass
            except OSError:
                pass

            cat_node.setExpanded(True)

    def _pack_browser_create(self) -> None:
        pack_name = self._pack_name_input.text().strip()
        if not pack_name:
            self._pack_name_input.setFocus()
            self._pack_name_input.setPlaceholderText("Enter a name first!")
            return

        is_knowledge = self._pack_type_combo.currentText() == "Knowledge"
        folder = 'knowledge_packs' if is_knowledge else 'quest_packs'
        pack_dir = os.path.join(self._app_dir(), folder)
        os.makedirs(pack_dir, exist_ok=True)

        safe_name = "".join(c if c.isalnum() or c in ' _-' else '_' for c in pack_name)
        path = os.path.join(pack_dir, f"{safe_name}.json")

        if is_knowledge:
            pack = {'name': pack_name, 'type': 'knowledge', 'count': 0, 'entries': []}
        else:
            pack = {'name': pack_name, 'type': 'quest', 'quest_count': 0, 'quests': []}

        import json as _json
        with open(path, 'w') as f:
            _json.dump(pack, f, indent=2)

        self._pack_name_input.clear()
        self._pack_name_input.setPlaceholderText("Pack name...")
        self._pack_browser_refresh()
        self._update_status(f"Created pack: '{pack_name}'")

    def _pack_browser_inject(self) -> None:
        item = self._pack_tree.currentItem()
        if not item:
            return
        path = item.data(0, Qt.UserRole)
        pack_type = item.data(0, Qt.UserRole + 1)
        if not path or path not in self._pack_data:
            return

        pack = self._pack_data[path]
        pack_name = pack.get('name', '')

        if pack_type == 'knowledge':
            entries = pack.get('entries', [])
            keys = [e['key'] for e in entries]
            to_inject = [k for k in keys if k not in getattr(self, '_know_learned_keys', set())]
            if not to_inject:
                QMessageBox.information(self, "Pack", f"All entries from '{pack_name}' already learned.")
                return
            reply = QMessageBox.question(self, f"Inject '{pack_name}'",
                f"Inject {len(to_inject)} knowledge entries?\n({len(keys) - len(to_inject)} already learned)",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply == QMessageBox.Yes:
                self._know_inject_keys(to_inject)
        elif pack_type == 'quest':
            self._quest_db_tab._qdb_mark_pack_complete(pack)

    def _pack_browser_delete(self) -> None:
        item = self._pack_tree.currentItem()
        if not item:
            return
        path = item.data(0, Qt.UserRole)
        if not path or not os.path.isfile(path):
            return

        pack_name = self._pack_data.get(path, {}).get('name', os.path.basename(path))
        reply = QMessageBox.question(self, "Delete Pack",
            f"Delete '{pack_name}'?\n\n{path}",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply == QMessageBox.Yes:
            try:
                os.remove(path)
                self._pack_browser_refresh()
            except Exception as e:
                QMessageBox.critical(self, "Error", str(e))

    def _pack_browser_navigate(self, item, column) -> None:
        key = item.data(0, Qt.UserRole + 2)
        pack_type = item.data(0, Qt.UserRole + 1)

        if key is None:
            return

        key_str = str(key)

        if pack_type == 'knowledge':
            world_idx = self._tabs.indexOf(self._world_tabs)
            if world_idx >= 0:
                self._tabs.setCurrentIndex(world_idx)
            if hasattr(self, '_know_table'):
                know_idx = self._world_tabs.indexOf(self._know_table.parent())
                if know_idx >= 0:
                    self._world_tabs.setCurrentIndex(know_idx)
            if hasattr(self, '_know_search'):
                self._know_search.setText(key_str)

        elif pack_type == 'quest':
            world_idx = self._tabs.indexOf(self._world_tabs)
            if world_idx >= 0:
                self._tabs.setCurrentIndex(world_idx)
            if hasattr(self, '_qdb_table'):
                qdb_idx = self._world_tabs.indexOf(self._qdb_table.parent())
                if qdb_idx >= 0:
                    self._world_tabs.setCurrentIndex(qdb_idx)
            if hasattr(self, '_qdb_search'):
                self._qdb_search.setText(key_str)

    def _pack_browser_open_folder(self) -> None:
        for pack_dir in self._get_pack_dirs():
            if os.path.isdir(pack_dir):
                os.startfile(pack_dir)
                return
        pack_dir = os.path.join(self._app_dir(), 'knowledge_packs')
        os.makedirs(pack_dir, exist_ok=True)
        os.startfile(pack_dir)


    def _setup_lazy_tab_loading(self) -> None:
        entries = [
            ('_buffs_tab',        lambda: self._buffs_tab.load(self._save_data, self._items)),
            ('_repurchase_tab',   lambda: self._repurchase_tab.load(self._save_data, self._items)),
            ('_dye_tab',          lambda: self._dye_tab.load(self._save_data, self._items)),
            ('_sockets_tab',      lambda: self._sockets_tab.load(self._save_data, self._items)),
            ('_swap_tab',         lambda: self._swap_tab.load(self._save_data, self._items)),
            ('_quest_editor_tab', lambda: self._quest_editor_tab.load(self._save_data, self._loaded_path)),
            ('_waypoint_tab',     lambda: self._waypoint_tab.load(self._save_data)),
            ('_teleport_tab',     lambda: self._teleport_tab.load(self._save_data)),
            ('_knowledge_tab',    lambda: self._knowledge_tab.load(self._save_data)),
            ('_quest_db_tab',     lambda: self._quest_db_tab.load(self._save_data)),
            ('_player_tab',       lambda: self._player_tab.load(self._save_data)),
            ('_faction_tab',      lambda: self._faction_tab.load(self._save_data)),
            ('_backup_tab',       lambda: self._backup_tab._refresh_backups()),
        ]
        for attr, loader in entries:
            if hasattr(self, attr):
                self._tab_loaders[getattr(self, attr)] = loader

        for tw in [self._tabs, self._save_tabs, self._mods_tabs, self._items_tabs, self._world_tabs]:
            tw.currentChanged.connect(lambda idx, t=tw: self._on_tab_activated(t, idx))

    def _on_tab_activated(self, tab_widget, index: int) -> None:
        if not self._save_data:
            return
        widget = tab_widget.widget(index)
        if widget is None or widget in self._loaded_tabs:
            return
        loader = self._tab_loaders.get(widget)
        if loader:
            try:
                loader()
                self._loaded_tabs.add(widget)
            except Exception as e:
                log.warning("Lazy tab load failed for %s: %s", widget.__class__.__name__, e)

    def _reload_visible_tabs(self) -> None:
        for tw in [self._tabs, self._save_tabs, self._mods_tabs, self._items_tabs, self._world_tabs]:
            self._on_tab_activated(tw, tw.currentIndex())

    def _build_status_bar(self) -> None:
        self._status = self.statusBar()
        self._status_file_label = QLabel("No file loaded")
        self._status_items_label = QLabel("Items: 0")
        self._status_parc_label = QLabel("")
        self._status_parc_label.setStyleSheet(f"color: {COLORS['text_dim']}; padding: 0 8px;")
        self._status_action_label = QLabel("")

        self._parc_progress = QProgressBar()
        self._parc_progress.setRange(0, 4)
        self._parc_progress.setValue(0)
        self._parc_progress.setTextVisible(True)
        self._parc_progress.setFormat("Scanning offsets...")
        self._parc_progress.setFixedHeight(16)
        self._parc_progress.setFixedWidth(200)
        self._parc_progress.setStyleSheet(
            f"QProgressBar {{ border: 1px solid {COLORS['border']}; border-radius: 4px;"
            f" background: {COLORS['bg']}; color: {COLORS['text']}; font-size: 10px; }}"
            f"QProgressBar::chunk {{ background: {COLORS['accent']}; border-radius: 3px; }}"
        )
        self._parc_progress.hide()

        self._status.addWidget(self._status_file_label, 2)
        self._status.addWidget(self._status_items_label, 1)
        self._status.addWidget(self._status_parc_label, 1)
        self._status.addWidget(self._parc_progress)
        self._status.addPermanentWidget(self._status_action_label)

        discord_btn = QPushButton()
        discord_btn.setToolTip("Join the Crimson Desert Modding Discord")
        discord_btn.setCursor(Qt.PointingHandCursor)
        discord_btn.setFlat(True)
        discord_btn.setFixedSize(26, 26)
        try:
            _dc_path = os.path.join(
                getattr(sys, '_MEIPASS', os.path.dirname(__file__)), "icons", "discord.png"
            )
            if os.path.isfile(_dc_path):
                discord_btn.setIcon(QIcon(_dc_path))
                discord_btn.setIconSize(QSize(20, 20))
        except Exception:
            discord_btn.setText("DC")
        discord_btn.clicked.connect(lambda: __import__('PySide6.QtGui', fromlist=['QDesktopServices']).QDesktopServices.openUrl(
            __import__('PySide6.QtCore', fromlist=['QUrl']).QUrl("https://discord.gg/6wxX5xPS")
        ))
        self._status.addPermanentWidget(discord_btn)

    def _update_status(self, action: str = "") -> None:
        if self._loaded_path:
            name = os.path.basename(self._loaded_path)
            dirty_mark = " *" if self._dirty else ""
            self._status_file_label.setText(f"File: {name}{dirty_mark}")
        else:
            self._status_file_label.setText("No file loaded")

        self._status_items_label.setText(f"Items: {len(self._items)}")

        if action:
            self._status_action_label.setText(action)
            if hasattr(self, '_center_status'):
                self._center_status.setText(action)


    def _open_save_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Save File", "",
            "Save Files (*.save);;All Files (*)"
        )
        if not path:
            return
        self._load_save(path)

    def _open_raw_stream(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Raw Stream", "",
            "Binary Files (*.bin);;All Files (*)"
        )
        if not path:
            return
        try:
            self._save_data = load_raw_stream(path)
            self._loaded_path = path
            self._dirty = False
            self._undo_stack.clear()
            self._scan_and_populate()
            self._update_status(f"Loaded raw stream: {os.path.basename(path)}")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to load raw stream:\n{e}")

    def _auto_find_save(self) -> None:
        saves = find_save_files()
        if not saves:
            QMessageBox.information(
                self, "Auto-Find",
                "No save files found in known locations.\n\n"
                "Searched:\n"
                "  %LOCALAPPDATA%/Pearl Abyss/CD/save/\n"
                "  %LOCALAPPDATA%/Pearl Abyss/CD_Epic/save/"
            )
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("Select Save File")
        dialog.resize(700, 400)
        dlg_layout = QVBoxLayout(dialog)

        dlg_layout.addWidget(QLabel("Found save files:"))

        list_widget = QListWidget()
        for s in saves:
            list_widget.addItem(s["display"])
        dlg_layout.addWidget(list_widget, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        dlg_layout.addWidget(buttons)

        list_widget.setCurrentRow(0)

        if dialog.exec() == QDialog.Accepted and list_widget.currentRow() >= 0:
            idx = list_widget.currentRow()
            self._load_save(saves[idx]["path"])

    def _load_save(self, path: str) -> None:
        from PySide6.QtWidgets import QProgressDialog
        progress = QProgressDialog("Loading save file...", None, 0, 5, self)
        progress.setWindowTitle("Loading")
        progress.setMinimumDuration(0)
        progress.setWindowModality(Qt.WindowModal)
        progress.setValue(0)
        QApplication.processEvents()

        def _step(msg, val):
            progress.setLabelText(msg)
            progress.setValue(val)
            if hasattr(self, '_center_status'):
                self._center_status.setText(msg)
            QApplication.processEvents()

        try:
            _step("Decrypting save file...", 1)
            self._save_data = load_save_file(path)
            self._loaded_path = path
            self._dirty = False
            self._undo_stack.clear()

            _step("Creating backup...", 2)
            pristine = self._create_pristine_backup(path)
            if pristine:
                log.info("Pristine backup created: %s", pristine)

            _step("Scanning items...", 3)
            self._scan_and_populate()

            _step("Populating UI...", 4)

            slot_dir = os.path.basename(os.path.dirname(path))
            friendly = self._friendly_slot_name(slot_dir)
            self._config["last_save_path"] = path
            self._config["last_slot"] = friendly
            self._save_config()
            self._refresh_sidebar()

            self._quick_save_btn.setEnabled(True)
            self._quick_save_btn.setToolTip(f"Save to: {path}")

            self.setWindowTitle(f"Crimson Desert Save Editor — {friendly}")
            self._update_status(f"Loaded: {friendly} ({slot_dir})")

            if hasattr(self, '_backup_tab'):
                self._backup_tab.set_loaded_path(path)

            progress.setValue(5)
        except Warning as w:
            self._update_status(f"Loaded (HMAC warning): {os.path.basename(path)}")
            progress.close()
        except Exception as e:
            progress.close()
            err_msg = str(e)
            hint = ""
            if "byte must be in range" in err_msg or "chacha20" in traceback.format_exc().lower():
                hint = (
                    "\n\nThis file appears to be corrupted or is not a valid save file.\n"
                    "If this is a backup, it may have been created from an already-broken save.\n"
                    "Try loading a different save or restoring from an earlier backup."
                )
            QMessageBox.critical(
                self, "Error",
                f"Failed to load save file:\n\n{e}{hint}\n\n{traceback.format_exc()}"
            )

    def _save_file(self) -> None:
        if not self._save_data or not self._loaded_path:
            QMessageBox.warning(self, "Save", "No file loaded.")
            return
        if self._save_data.is_raw_stream:
            QMessageBox.warning(
                self, "Save",
                "Cannot save as .save from a raw stream.\nUse 'Save As' to choose a .save path."
            )
            return
        self._do_save(self._loaded_path)

    def _save_file_as(self) -> None:
        if not self._save_data:
            QMessageBox.warning(self, "Save As", "No file loaded.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save As", "",
            "Save Files (*.save);;All Files (*)"
        )
        if not path:
            return
        self._do_save(path)

    def _do_save(self, path: str) -> None:
        try:
            reply = QMessageBox.question(
                self, "Backup Save?",
                "Create a backup of your current save before writing changes?\n\n"
                "Highly recommended — you can restore from Backup/Restore tab if anything goes wrong.",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
                QMessageBox.Yes,
            )
            if reply == QMessageBox.Cancel:
                return
            if reply == QMessageBox.Yes:
                backup_path = self._create_backup(path)
                if backup_path:
                    self._update_status(f"Backup created: {os.path.basename(backup_path)}")

            write_save_file(
                path,
                bytes(self._save_data.decompressed_blob),
                self._save_data.raw_header if self._save_data.raw_header else None,
            )
            self._loaded_path = path
            self._dirty = False

            slot_dir = os.path.basename(os.path.dirname(path))
            friendly = self._friendly_slot_name(slot_dir)
            self._config["last_save_path"] = path
            self._config["last_slot"] = friendly
            self._save_config()

            self._update_status(f"Saved: {friendly} ({slot_dir})")
            if hasattr(self, '_backup_tab'):
                self._backup_tab._refresh_backups()
            self._refresh_sidebar()
            QMessageBox.warning(
                self, "Save",
                f"Save file written successfully.\n{path}\n\n"
                "WARNING: It is recommended to save again in game after loading\n"
                "the changes to have a new clean save to work with, before\n"
                "applying another change."
            )
        except Exception as e:
            QMessageBox.critical(
                self, "Save Error",
                f"Failed to save:\n\n{e}\n\n{traceback.format_exc()}"
            )


    def _fix_duplicate_item_nos(self) -> None:
        if not self._items or not self._save_data:
            return

        from collections import Counter
        no_counts = Counter(it.item_no for it in self._items)
        duplicated_nos = {no for no, count in no_counts.items() if count > 1}

        if not duplicated_nos:
            return

        max_no = get_max_itemno(self._items)
        next_no = max_no + 1
        fixed = 0

        for dup_no in duplicated_nos:
            sharing = [it for it in self._items if it.item_no == dup_no]
            for item in sharing[1:]:
                apply_itemno_edit(
                    self._save_data.decompressed_blob, item, next_no
                )
                next_no += 1
                fixed += 1

        if fixed > 0:
            self._dirty = True
            self._update_status(
                f"Fixed {fixed} duplicate ItemNo(s) across "
                f"{len(duplicated_nos)} group(s) — each item now has a unique ID."
            )

    def _scan_and_populate(self) -> None:
        if not self._save_data:
            return

        self._quest_entries = []
        self._mission_entries = []
        self._items = scan_items(self._save_data.decompressed_blob)

        for item in self._items:
            item.name = self._name_db.get_name(item.item_key)
            item.category = self._name_db.get_category(item.item_key)

        self._fix_duplicate_item_nos()

        self._loaded_tabs.clear()

        self._status_parc_label.hide()
        self._parc_progress.setValue(0)
        self._parc_progress.setFormat("Scanning offsets...")
        self._parc_progress.show()
        if hasattr(self, '_inventory_tab'):
            self._inventory_tab.load(self._save_data, self._items)
            self._inventory_tab._populate_inventory()
            self._loaded_tabs.add(self._inventory_tab)
        if hasattr(self, '_equipment_tab'):
            self._equipment_tab.load(self._save_data, self._items)
            self._equipment_tab._populate_equipment()
            self._loaded_tabs.add(self._equipment_tab)
        if hasattr(self, '_buffs_tab'):
            self._buffs_tab.load(self._save_data, self._items)
            self._loaded_tabs.add(self._buffs_tab)

        from PySide6.QtCore import QTimer
        QTimer.singleShot(100, self._deferred_parc_enrich)

    def _deferred_parc_enrich(self) -> None:
        if not self._save_data or not self._items:
            return

        import threading

        blob = self._save_data.decompressed_blob
        items = self._items

        def _on_progress(step: int, _total: int) -> None:
            self._parc_step.emit(step)

        def _do_enrich() -> None:
            try:
                enriched, parc_status = enrich_items_with_parc(blob, items, _on_progress)
            except Exception:
                enriched, parc_status = 0, "Legacy mode: pattern-based scanning"
            self._parc_done.emit(enriched, parc_status)

        threading.Thread(target=_do_enrich, daemon=True).start()

    _PARC_STEP_LABELS = {
        0: "Scanning offsets...",
        1: "Parsing PARC blob...",
        2: "Building bag map...",
        3: "Assigning bags...",
        4: "Done",
    }

    def _on_parc_step(self, step: int) -> None:
        self._parc_progress.setValue(step)
        self._parc_progress.setFormat(self._PARC_STEP_LABELS.get(step, "..."))

    def _finish_parc_enrich(self, enriched: int, parc_status: str) -> None:
        self._parc_progress.hide()
        self._parc_status = parc_status
        if enriched > 0:
            self._status_parc_label.setText(parc_status)
            self._status_parc_label.setStyleSheet(f"color: {COLORS['success']}; padding: 0 8px;")
        else:
            self._status_parc_label.setText("Legacy mode: pattern-based scanning")
            self._status_parc_label.setStyleSheet(f"color: {COLORS['text_dim']}; padding: 0 8px;")
        self._status_parc_label.show()

        self._enrich_vendor_names()

        self._loaded_tabs.clear()
        if hasattr(self, '_inventory_tab'):
            self._inventory_tab._populate_inventory()
            self._inventory_tab._inv_count_label.setText(str(len(self._items)))
            self._loaded_tabs.add(self._inventory_tab)
        if hasattr(self, '_equipment_tab'):
            self._equipment_tab._populate_equipment()
            self._loaded_tabs.add(self._equipment_tab)

        self._reload_visible_tabs()

        self._prefetch_parse_cache()

    def _prefetch_parse_cache(self) -> None:
        sd = self._save_data
        if not sd or not sd.decompressed_blob:
            return
        if sd.parse_cache is not None:
            return

        import threading

        blob_snapshot = bytes(sd.decompressed_blob)

        def _worker() -> None:
            try:
                from parc_inserter2 import parse_and_collect
                from models import ParseCache
                result, offset_positions, trailing_sizes = parse_and_collect(blob_snapshot)
                target_sd = self._save_data
                if target_sd is None or bytes(target_sd.decompressed_blob) != blob_snapshot:
                    return
                target_sd.parse_cache = ParseCache(
                    offset_positions=list(offset_positions),
                    trailing_sizes=list(trailing_sizes),
                    schema_end=result['raw']['schema_end'],
                    toc_entries=list(result['toc']['entries']),
                )
                self._parse_cache_ready.emit()
            except Exception as exc:
                log.debug("parse cache prefetch failed: %s", exc)

        threading.Thread(target=_worker, daemon=True).start()

    def _on_parse_cache_ready(self) -> None:
        log.info("PARC parse cache warmed")


    def _toggle_icons(self) -> None:
        self._icons_enabled = not self._icons_enabled
        self._config["show_icons"] = self._icons_enabled
        self._save_config()

        btn_text = "Hide Icons" if self._icons_enabled else "Show Icons"
        row_h = max(ICON_SIZE + 2, 24) if self._icons_enabled else 24

        if hasattr(self, '_global_icons_btn'):
            self._global_icons_btn.setText(btn_text)

        if self._icons_enabled and self._icon_cache.coverage < 100:
            reply = QMessageBox.question(
                self, "Download Icons",
                f"Only {self._icon_cache.coverage} icons cached locally.\n"
                f"Download all 6,000+ item icons + mount portraits from GitHub?\n\n"
                f"This requires internet (first time only, ~70 MB).\n"
                f"Already downloaded icons will be skipped.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
            )
            if reply == QMessageBox.Yes:
                if hasattr(self, '_inventory_tab'):
                    self._inventory_tab._bulk_download_icons()

        if hasattr(self, '_inventory_tab'):
            self._inventory_tab.set_icons_enabled(self._icons_enabled)
        if hasattr(self, '_equipment_tab'):
            self._equipment_tab.set_icons_enabled(self._icons_enabled)
        if hasattr(self, '_swap_tab'):
            self._swap_tab.set_icons_enabled(self._icons_enabled)
        if hasattr(self, '_database_tab'):
            self._database_tab.update_icons(self._icons_enabled)
        if hasattr(self, '_repurchase_tab'):
            self._repurchase_tab._filter_repurchase()
        if hasattr(self, '_mercenary_tab'):
            self._mercenary_tab.set_icons_enabled(self._icons_enabled)


    def _on_icon_loaded(self, item_key: int, pixmap) -> None:
        self._icon_ready.emit(item_key)


    def _create_backup(self, save_path: str) -> str:
        if not os.path.isfile(save_path):
            return ""

        backup_dir = os.path.join(os.path.dirname(save_path), "backups")
        os.makedirs(backup_dir, exist_ok=True)

        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        base = os.path.basename(save_path)
        backup_name = f"{base}.{timestamp}.bak"
        backup_path = os.path.join(backup_dir, backup_name)

        shutil.copy2(save_path, backup_path)

        try:
            existing = sorted(
                [f for f in os.listdir(backup_dir)
                 if f.endswith(".bak") and ".PRISTINE." not in f],
                key=lambda f: os.path.getmtime(os.path.join(backup_dir, f)),
                reverse=True,
            )
            for old in existing[10:]:
                os.remove(os.path.join(backup_dir, old))
        except OSError:
            pass

        try:
            slot_name = os.path.basename(os.path.dirname(save_path))
            exe_dir = self._app_dir()
            global_dir = os.path.join(exe_dir, "backups", slot_name)
            os.makedirs(global_dir, exist_ok=True)

            global_path = os.path.join(global_dir, backup_name)
            shutil.copy2(save_path, global_path)

            g_existing = sorted(
                [f for f in os.listdir(global_dir) if f.endswith(".bak")],
                key=lambda f: os.path.getmtime(os.path.join(global_dir, f)),
                reverse=True,
            )
            for old in g_existing[10:]:
                os.remove(os.path.join(global_dir, old))
        except Exception:
            pass

        return backup_path

    def _create_pristine_backup(self, save_path: str) -> str:
        if not os.path.isfile(save_path):
            return ""

        backup_dir = os.path.join(os.path.dirname(save_path), "backups")
        os.makedirs(backup_dir, exist_ok=True)

        base = os.path.basename(save_path)
        pristine_name = f"{base}.PRISTINE.bak"
        pristine_path = os.path.join(backup_dir, pristine_name)

        if os.path.isfile(pristine_path):
            return ""

        try:
            from save_crypto import load_save_file
            sd = load_save_file(save_path)
            if not sd or not sd.decompressed_blob or len(sd.decompressed_blob) < 100:
                log.warning("Skipping PRISTINE backup — save appears invalid")
                return ""
        except Exception as e:
            log.warning("Skipping PRISTINE backup — save failed validation: %s", e)
            return ""

        shutil.copy2(save_path, pristine_path)
        return pristine_path


    def _set_font_scale(self, scale: float) -> None:
        import re
        def _scale_px(m):
            orig = int(m.group(1))
            return f"font-size: {max(8, int(orig * scale))}px"
        scaled_ss = re.sub(r'font-size:\s*(\d+)px', _scale_px, DARK_STYLESHEET)
        self.setStyleSheet(scaled_ss)
        self._config["font_scale"] = scale
        self._save_config()
        self._update_status(f"Font size: {int(scale * 100)}%")

    def _set_widget_scale(self, scale: float) -> None:
        import re

        base_ss = self.styleSheet() or DARK_STYLESHEET

        def _scale_padding(m):
            vals = m.group(1).split()
            scaled = []
            for v in vals:
                if v.endswith('px'):
                    num = int(v[:-2])
                    scaled.append(f"{max(1, int(num * scale))}px")
                else:
                    try:
                        num = int(v)
                        scaled.append(str(max(1, int(num * scale))))
                    except ValueError:
                        scaled.append(v)
            return f"padding: {' '.join(scaled)}"

        scaled_ss = re.sub(r'padding:\s*([^;]+)', _scale_padding, base_ss)

        default_row = max(18, int(22 * scale))
        for table in self.findChildren(QTableWidget):
            table.verticalHeader().setDefaultSectionSize(default_row)

        if hasattr(self, '_buff_items_table'):
            t = self._buff_items_table
            t.setColumnWidth(1, max(80, int(180 * scale)))
            t.setColumnWidth(2, max(40, int(70 * scale)))
            t.setColumnWidth(4, max(40, int(70 * scale)))
        if hasattr(self, '_buff_stats_table'):
            t = self._buff_stats_table
            t.setColumnWidth(0, max(120, int(240 * scale)))
            t.setColumnWidth(1, max(60, int(100 * scale)))

        from PySide6.QtWidgets import QTreeWidget
        for tree in self.findChildren(QTreeWidget):
            tree.setIndentation(max(8, int(16 * scale)))

        if hasattr(self, '_save_dock'):
            scaled_sidebar = max(180, int(260 * scale))
            docks, widths = [], []
            if not self._sb_collapsed:
                self._sb_saved_width = scaled_sidebar
                docks.append(self._save_dock); widths.append(scaled_sidebar)
            if not self._ps_collapsed:
                self._ps_saved_width = scaled_sidebar
                docks.append(self._pack_dock); widths.append(scaled_sidebar)
            if docks:
                self.resizeDocks(docks, widths, Qt.Horizontal)

        if hasattr(self, '_center_status'):
            self._center_status.setFixedHeight(max(24, int(36 * scale)))

        self.setStyleSheet(scaled_ss)
        self._config["widget_scale"] = scale
        self._save_config()
        self._update_status(f"UI scale: {int(scale * 100)}%")

    def _undo(self) -> None:
        if not self._undo_stack or not self._save_data:
            self._update_status("Nothing to undo.")
            return

        entry = self._undo_stack.pop()
        blob = self._save_data.decompressed_blob

        for offset, old_bytes, _new_bytes in entry.patches:
            blob[offset:offset + len(old_bytes)] = old_bytes

        self._scan_and_populate()
        self._dirty = bool(self._undo_stack)
        self._update_status(f"Undone: {entry.description}")


    _GUIDES = {
        "inventory": (
            "Inventory",
            "View and edit all items in your save file.\n\n"
            "HOW TO USE:\n"
            "1. Load a save file from the sidebar\n"
            "2. Use Search to filter by name, key, or category\n"
            "3. Use Group dropdown to filter by source (Equipment, Inventory, Vendor, etc.)\n"
            "4. Select an item and change its stack count with the Set Stack button\n"
            "5. Use 'Give Item' to transform a donor item into something else\n"
            "6. Use 'Delete Item' to zero out unwanted items\n\n"
            "ADD NEW ITEM (PARC Insert):\n"
            "Creates a brand new item in your inventory without needing a donor.\n"
            "Currently limited to Narima's Horn (Dragon cooldown reset).\n"
            "Select the item, set quantity, and save. Usually appears after\n"
            "one reload, may occasionally require two.\n\n"
            "NOTE: The save stores items differently than the game displays them.\n"
            "A stack of 90 stones is ONE entry in the save — the game splits it\n"
            "into multiple visual slots at runtime.\n\n"
            "TIP: Right-click an item for quick actions like Swap."
        ),
        "swap": (
            "Item Swap",
            "Transform one item into another by changing its item key.\n\n"
            "HOW TO USE:\n"
            "1. Select an item from the Inventory tab first\n"
            "2. Go to the Swap tab\n"
            "3. Use Category filter or Search to find your target item\n"
            "4. Click 'Swap (Single Item)' to change just this one instance\n"
            "5. Or 'Swap All (Global)' to change EVERY copy in the save\n"
            "6. Save and load in-game\n\n"
            "WARNING: Swapping equipment in the inventory may cause items to not\n"
            "appear in-game. For reliable equipment swaps, use the Repurchase tab\n"
            "(sell a junk item to a vendor, swap it there, buy it back).\n\n"
            "IMPORTANT: Cross-type swaps (e.g. shield to glove) will show the\n"
            "wrong 3D model in-game. Stick to same-type swaps when possible."
        ),
        "repurchase": (
            "Repurchase (Vendor Swap)",
            "The MOST RELIABLE way to get new gear.\n\n"
            "SWAP SELECTED ITEM:\n"
            "1. In-game: sell a junk item to any NPC vendor\n"
            "2. Save your game\n"
            "3. Open the save in this editor\n"
            "4. Go to Repurchase tab — your sold item appears here\n"
            "5. Select it and click 'Swap Selected Item'\n"
            "6. Pick the item you want from the list\n"
            "7. Save the file\n"
            "8. Load in-game and buy the swapped item back from the vendor\n\n"
            "CLONE SELECTED TO VENDOR:\n"
            "Creates a NEW vendor buyback entry from the selected item.\n"
            "1. Select any sold item in the repurchase list\n"
            "2. Click 'Clone Selected to Vendor'\n"
            "3. Search for the target item by name\n"
            "4. Save, reload, and buy it back from the vendor\n\n"
            "IMPORTANT — TYPE-FOR-TYPE RULE:\n"
            "Swaps MUST be same type: Glove->Glove, Helm->Helm, Sword->Sword.\n"
            "Cross-type swaps (Glove->Helm) produce unequippable items.\n"
            "The game gains knowledge for cross-type items but they cannot be equipped.\n\n"
            "WORKAROUND for wrong-type items:\n"
            "If you swapped the wrong type and got an unequippable item,\n"
            "sell it to a vendor, then Clone it to the correct type. The game\n"
            "will fix the template when you buy it back.\n\n"
            "WHY THIS WORKS: Items bought from vendors are 'clean' —\n"
            "the game creates fresh data, so icons and stats are correct.\n\n"
            "TIP: Sell multiple junk items to set up several swaps at once."
        ),
        "equipment": (
            "Equipment (Enchanting)",
            "Edit enchantment levels, endurance, sharpness, and duplicate gear.\n\n"
            "HOW TO USE:\n"
            "1. Only items that have been enchanted at least once appear here\n"
            "2. If an item is missing, enchant it once at a blacksmith in-game\n"
            "3. Select an item and set Enchant Level (0-10 is safe)\n"
            "4. Change Endurance or Sharpness values\n\n"
            "DUPLICATING GEAR:\n"
            "1. Click 'Duplicate All Equipment' (sets stack to 2-3 copies)\n"
            "2. Save, load in-game\n"
            "3. Unequip gear — extra copies drop into your inventory\n"
            "4. Now you can swap the copies into different items\n\n"
            "WARNING: Enchant levels above 10 may crash the game.\n"
            "Always back up before making changes."
        ),
        "sockets": (
            "Sockets (Abyss Gear)",
            "Install, swap, or remove Abyss Gear gems in your equipment sockets.\n\n"
            "HOW TO USE:\n"
            "1. Select an equipment item from the dropdown\n"
            "2. Each socket shows a dropdown with available gems\n"
            "3. Use the Category filter or Search box to find specific gems\n"
            "4. Choose a gem for each socket you want to change:\n"
            "   - Empty sockets can be filled directly without installing in-game first\n"
            "   - Set a filled socket to empty to remove its gem\n"
            "5. Click 'Apply Socket Changes'\n"
            "6. Save and load in-game\n\n"
            "189 Abyss Gears available including combat skills, stat buffs,\n"
            "resistances, banes, gathering bonuses, and more."
        ),
        "packs": (
            "Item Packs",
            "Download and apply curated item collections from the community.\n\n"
            "HOW TO USE:\n"
            "1. Click 'Refresh from GitHub' to see available community packs\n"
            "2. Select a pack and click 'Download Selected Pack'\n"
            "3. Downloaded packs appear in 'My Packs' section\n"
            "4. Load your save file\n"
            "5. Select a pack and click 'Apply Pack to Save'\n"
            "6. A dialog maps each pack item to a donor from your inventory\n"
            "7. Use 'Auto-Pick Donors' for automatic matching\n"
            "8. Confirm — donor items are transformed into pack items\n"
            "9. Save and load in-game\n\n"
            "CREATING PACKS:\n"
            "Click 'Create New Pack' to build your own collection.\n"
            "Use 'Export for Sharing' to save as JSON for others."
        ),
        "community": (
            "Community Mapping",
            "Help map every item in Crimson Desert!\n\n"
            "This tool collects real item templates from player saves to build a\n"
            "complete database of all items and their binary structures.\n"
            "The more saves we scan, the more items the editor can support.\n\n"
            "HOW TO CONTRIBUTE:\n"
            "1. Click 'Sync with Community' (does everything in one click):\n"
            "   - Downloads the latest master database\n"
            "   - Scans your loaded save for new item templates\n"
            "   - Uploads any new discoveries to the community\n\n"
            "OR step by step:\n"
            "1. Click 'Download Latest DB' to get the newest master\n"
            "2. Click 'Scan Loaded Save' or 'Scan All Saves'\n"
            "3. Click 'Upload New Templates' to share your findings\n\n"
            "The coverage table shows mapping progress by item category.\n"
            "Green = 90%+, Yellow = 50%+, Red = under 50%."
        ),
        "itembuffs": (
            "ItemBuffs (Stat & Buff Editing)",
            "Modify stats, buffs, and passives on ANY item in the game.\n\n"
            "══════════════════════════════════════\n"
            "  TWO EXPORT MODES\n"
            "══════════════════════════════════════\n\n"
            "EXPORT JSON PATCH (value-only edits):\n"
            "  + Change stat values (DDD 5000 -> 999999)\n"
            "  + Swap stat hashes within same size class\n"
            "  + Max stack size changes\n"
            "  - CANNOT add new buffs, stats, or passives\n"
            "  - CANNOT change file size\n"
            "  Use with: CD JSON Mod Manager or CDUMM\n\n"
            "EXPORT AS MOD (structural edits):\n"
            "  + Everything above, PLUS:\n"
            "  + Add NEW equipment buffs (Fire Res, Ice Res, etc)\n"
            "  + Add NEW stats that don't exist on the item\n"
            "  + Add passive skills (Invincible, Great Thief, etc)\n"
            "  + God Mode injection\n"
            "  Outputs: 0036/ + meta/ + modinfo.json\n"
            "  Use with: CDUMM (import folder) or copy to game dir\n\n"
            "══════════════════════════════════════\n"
            "  HOW TO EDIT STATS (value-only)\n"
            "══════════════════════════════════════\n\n"
            "1. Click 'Extract (Rust)' to load item data\n"
            "2. Search for an item (e.g. 'Earring', 'Sword')\n"
            "3. Select it — stats appear on the right\n"
            "4. Pick a preset (Max All, Max DDD, Custom, etc)\n"
            "5. Click 'Apply Preset to Item'\n"
            "6. Click 'Export JSON Patch'\n\n"
            "STAT TYPES:\n"
            "  Flat stats (DDD, DPV, HP): large values like 999,999\n"
            "  Rate stats (Speed, Crit, AtkSpd): 0-15 where 15 = max\n\n"
            "══════════════════════════════════════\n"
            "  HOW TO ADD BUFFS / EFFECTS\n"
            "══════════════════════════════════════\n\n"
            "1. Click 'Extract (Rust)' (REQUIRED for structural edits)\n"
            "2. Search for your item and select it\n"
            "3. In the 'Equip Buff' row, pick a buff:\n"
            "   e.g. Fire Resistance, Ice Resistance, Attack Power Up\n"
            "4. Set the level (15 = max for most buffs)\n"
            "5. Click 'Add Buff' (adds to ALL enchant levels)\n"
            "6. Click 'Export as Mod'\n\n"
            "COMMON BUFFS:\n"
            "  Fire Resistance (1000012)\n"
            "  Ice Resistance (1000013)\n"
            "  Lightning Resistance (1000014)\n"
            "  Attack Power Up (1000071)\n"
            "  Defense Up (1000072)\n"
            "  Max HP Up (1000073)\n"
            "  Attack Speed Up (1000091)\n"
            "  Move Speed Up (1000093)\n\n"
            "══════════════════════════════════════\n"
            "  GOD MODE / PASSIVES\n"
            "══════════════════════════════════════\n\n"
            "God Mode: Select an item, click 'God Mode' button.\n"
            "  Injects Invincible + Great Thief passives, max stats,\n"
            "  max regen, max speed/crit/resist, 8 equipment buffs.\n"
            "  Then 'Export as Mod' to write.\n\n"
            "Set Passive: Pick a passive skill from the dropdown\n"
            "  (e.g. Fire Resistance, Invincible) and click 'Set Passive'.\n"
            "  Then 'Export as Mod' to write.\n\n"
            "══════════════════════════════════════\n"
            "  SAVE / LOAD CONFIG\n"
            "══════════════════════════════════════\n\n"
            "Save Config: Saves your edits as a reusable recipe file.\n"
            "  Stores WHAT you changed (not raw bytes), so it works\n"
            "  across game updates. Share with others!\n\n"
            "Load Config: Loads a config file and re-applies edits\n"
            "  to fresh game data. Tweak and re-export.\n\n"
            "══════════════════════════════════════\n"
            "  RESTORE / UNDO\n"
            "══════════════════════════════════════\n\n"
            "To undo: delete the 0036/ folder from your game directory,\n"
            "or use CDUMM to disable the mod. Steam > Verify Integrity\n"
            "also restores all original files.\n\n"
            "Credits: Potter420 & LukeFZ (crimson-rs parser)"
        ),
        "waypoint": (
            "Nexus Gate Locations",
            "Show all nexus/abyss gate locations on your map.\n\n"
            "TWO-STEP PROCESS:\n"
            "Step 1: Click 'Reveal Entire Map' to remove fog of war.\n"
            "   Save (Ctrl+S) and reload in-game.\n\n"
            "Step 2: Click 'Show All Nexus Gates' to inject community\n"
            "   knowledge entries. Save (Ctrl+S) and reload in-game.\n"
            "   All nexus gates appear as ??? markers on your map.\n\n"
            "IMPORTANT: Do Step 1 first, save and reload, THEN do Step 2.\n"
            "Doing both at once may not work on all saves.\n\n"
            "Gates are visible as ??? but not yet useable for teleport.\n"
            "Gate activation research is ongoing."
        ),
        "gamedata": (
            "Game Data Editor",
            "Browse and edit ANY game data file (PABGB) directly.\n\n"
            "AVAILABLE FILES:\n"
            "  buffinfo — Buff durations, stat values, levels (275 buffs)\n"
            "  skill — Skill cooldowns, damage, SP costs (1,900 skills)\n"
            "  dropsetinfo — Drop rates and loot tables (11,383 drop sets)\n"
            "  characterinfo — NPC/enemy HP, damage, behavior (6,872 NPCs)\n"
            "  faction — Faction reputation thresholds (135 factions)\n"
            "  conditioninfo — Unlock conditions and triggers (8,437 conditions)\n"
            "  knowledgeinfo — Knowledge unlock requirements (5,513 entries)\n"
            "  equipslotinfo — Equipment slot definitions (13 slots)\n"
            "  equiptypeinfo — Weapon/armor type rules (110 types)\n\n"
            "HOW TO USE:\n"
            "1. Set your game path using Browse at the top\n"
            "2. Select a file from the dropdown and click 'Load'\n"
            "3. Search records by name (e.g. 'Kliff', 'FireResistance')\n"
            "4. Select a record to view its hex data\n"
            "5. Edit bytes directly in the hex editor\n"
            "6. Click 'Apply to Game' to write changes in-place\n\n"
            "PATCHING METHOD:\n"
            "Uses in-place PAZ patching (same as community tools).\n"
            "Decompresses from original PAZ, applies edits, recompresses\n"
            "with LZ4 HC, writes back at the same offset.\n"
            "All checksums (PAMT + PAPGT) updated automatically.\n\n"
            "RESTORE: Click 'Restore' to revert from .sebak backup.\n\n"
            "REQUIRES: Administrator privileges and game restart."
        ),
        "knowledge": (
            "Knowledge Editor",
            "Browse and edit knowledge entries in your save.\n\n"
            "Knowledge controls: skill unlocks, map discovery, recipes,\n"
            "dye formulas, crafting manuals, faction data, and more.\n\n"
            "HOW TO USE:\n"
            "1. Load a save file\n"
            "2. Click 'Scan Save' to extract learned knowledge\n"
            "3. Browse entries — green = learned, dim = not learned\n"
            "4. Use Category filter to find specific types\n"
            "5. Select entries and click 'Learn Selected' to inject them\n\n"
            "Bulk 'Learn All' shortcuts are not offered here — they can freeze\n"
            "the UI during huge injects. Filter by category and use Learn Selected.\n\n"
            "IMPORTANT: Learning all knowledge reveals the entire map\n"
            "and cannot be undone with Re-Fog.\n\n"
            "Save (Ctrl+S) after making changes."
        ),
        "skills": (
            "Skill Editor — Modify Skill Parameters",
            "Browse and edit skill.pabgb — change cooldowns, use counts,\n"
            "damage values, and other parameters for any skill.\n\n"
            "══════════════════════════════════════\n"
            "  HOW TO USE\n"
            "══════════════════════════════════════\n\n"
            "1. Click 'Extract Skills' to load skill data from the game\n"
            "2. Search for a skill by name (e.g. JiJeongTa, Slash)\n"
            "3. Select a skill — its decoded fields appear on the right\n"
            "4. Double-click a field to edit its value\n"
            "5. Click 'Export as Mod' to create a PAZ mod folder\n\n"
            "══════════════════════════════════════\n"
            "  FIELD TYPES\n"
            "══════════════════════════════════════\n\n"
            "u32: Integer value (cooldown, counts, references)\n"
            "f32: Float value (multipliers, ranges, timings)\n"
            "hash: Stat/skill hash reference (0x000F42xx range)\n"
            "string: Embedded text (descriptions, names)\n"
            "  - Strings CANNOT be edited (would change file size)\n"
            "  - Only u32, f32, and hash fields can be edited\n\n"
            "══════════════════════════════════════\n"
            "  KNOWN FIELDS\n"
            "══════════════════════════════════════\n\n"
            "Fields are decoded heuristically — labels are best guesses.\n"
            "Common patterns found across skills:\n"
            "  - 'cooldown_ms?' — values like 1000, 2000 (milliseconds)\n"
            "  - 'small_int' 1-10 — levels, counts, tiers\n"
            "  - Floats 0.1-100 — damage multipliers, ranges\n"
            "  - Hash 0x000F42xx — stat references\n\n"
            "══════════════════════════════════════\n"
            "  SAVE / LOAD CONFIG\n"
            "══════════════════════════════════════\n\n"
            "Save Config: saves your edits as a JSON recipe\n"
            "Load Config: reloads edits onto fresh game data\n"
            "Configs store absolute offsets — may break after game updates\n\n"
            "══════════════════════════════════════\n"
            "  LIMITATIONS\n"
            "══════════════════════════════════════\n\n"
            "- Value-only edits (cannot add/remove fields)\n"
            "- Field labels are heuristic (some may be wrong)\n"
            "- Skills and ItemBuffs both use the 0036/ mod slot\n"
            "  — only one can be active at a time (use CDUMM to merge)\n"
            "- Restore: delete 0036/ from game dir, or Steam Verify Integrity"
        ),
        "stores": (
            "Vendor Stores",
            "Modify what vendors sell in-game (storeinfo.pabgb).\n\n"
            "HOW TO USE:\n"
            "1. Set your game path using Browse at the top\n"
            "2. Click 'Load Store Data' to extract vendor data\n"
            "3. Browse stores on the left, items on the right\n"
            "4. Select item(s) and click 'Swap Selected' to change them\n"
            "5. Click 'Export JSON Patch' to save your changes\n"
            "6. Use CD JSON Mod Manager to apply the patch\n"
            "7. Restart the game and visit the vendor\n\n"
            "SWAP vs ADD:\n"
            "  Swap: Replaces an existing vendor item (safest, proven)\n"
            "  Add: Clones an entry and changes its key (experimental)\n\n"
            "PURCHASE LIMITS:\n"
            "  Each item has a buy limit (e.g. 1, 5, 999)\n"
            "  Select items > set limit > click 'Apply Limit'\n"
            "  Or 'Set ALL Limits' to max out the entire store\n\n"
            "IMPORT/EXPORT JSON:\n"
            "  Import community patches (Pldada format)\n"
            "  Export your changes as shareable JSON files\n"
            "  Compatible with CD JSON Mod Manager\n\n"
            "RESTORE:\n"
            "  Use CD JSON Mod Manager to remove mods\n"
            "  Or use Steam > Verify Integrity of Game Files\n\n"
            "NOTE: Only 'Standard' format stores support editing.\n"
            "'Special' stores (Camp, Church, Bank) use different formats."
        ),
        "database": (
            "Item Database",
            "Browse all 6000+ known items in Crimson Desert.\n\n"
            "Use the search bar to find items by name, key, or category.\n"
            "Click an item to see its details.\n\n"
            "This database powers all swap/search dialogs across the editor."
        ),
        "quests": (
            "Quest Editor — Fixing Bugged Quests",
            "══════════════════════════════════════\n"
            "  BASIC: FIX A STUCK QUEST\n"
            "══════════════════════════════════════\n\n"
            "1. Load your save, then click 'Load Quests'\n"
            "2. Search for the bugged quest by name\n"
            "3. Select it and click 'Mark Completed'\n"
            "4. Save (Ctrl+S), reload in-game\n\n"
            "IF STILL STUCK — try the next quest in the chain:\n"
            "  Search the quest prefix (e.g. 'Sanctum' shows\n"
            "  Sanctum_01, _02, _03...) and complete the next one.\n\n"
            "══════════════════════════════════════\n"
            "  SET QUEST STATE (manual override)\n"
            "══════════════════════════════════════\n\n"
            "Use the state dropdown + 'Set Quest State' button to\n"
            "force any quest to a specific state:\n\n"
            "  Locked (0x0D01) — quest not yet available\n"
            "  Available (0x0902) — quest ready to start\n"
            "  In Progress (0x0905) — quest started\n"
            "  In Progress+ (0x1102) — quest advanced\n"
            "  Completed (0x1105) — quest done\n"
            "  Fully Completed (0x1905) — quest fully done\n\n"
            "Use this to fix corrupted states (invalid hex values)\n"
            "or to manually control quest progression.\n\n"
            "══════════════════════════════════════\n"
            "  INSERTING A MISSING QUEST\n"
            "══════════════════════════════════════\n\n"
            "If a quest doesn't appear in your save at all:\n"
            "1. Go to Quest Database tab\n"
            "2. Search for the quest by name or key\n"
            "3. Select it and click 'Add Quest to Save as Complete'\n"
            "This uses PARC insertion to add a new quest entry.\n\n"
            "══════════════════════════════════════\n"
            "  DIAGNOSTIC TOOLS\n"
            "══════════════════════════════════════\n\n"
            "DIAGNOSE QUEST:\n"
            "  Select a quest and click 'Diagnose Quest'.\n"
            "  Shows all PARC fields with raw hex values.\n"
            "  Flags: corrupted states, missing timestamps,\n"
            "  duplicate entries. Not all fields are required —\n"
            "  _completedTime only matters if state = Completed.\n"
            "  _branchedTime only matters if quest was started.\n\n"
            "SCAN ALL SAVE SLOTS:\n"
            "  Compares this quest's state across every save slot.\n"
            "  Helps find a clean version (autosaves often have\n"
            "  a working copy you can compare against).\n\n"
            "QUEST HEALTH CHECK:\n"
            "  Scans ALL quests in the save for problems:\n"
            "  - Corrupted state values (not a valid quest state)\n"
            "  - Duplicate entries (same key appears twice)\n"
            "  - Completed quests with no timestamp\n\n"
            "ADVANCED EDIT:\n"
            "  Opens a raw field editor for the selected quest.\n"
            "  Shows every PARC field with hex values in a table.\n"
            "  Edit the 'Raw Hex' column to change any value.\n"
            "  Values are little-endian (e.g. state 0x1905 = '0519').\n"
            "  If a quest has duplicates, you pick which entry to edit.\n"
            "  This is the most powerful quest editing tool — you can\n"
            "  fix any field: state, timestamps, counts, flags.\n\n"
            "══════════════════════════════════════\n\n"
            "WARNING: Completing quests you haven't started may\n"
            "cause unexpected side effects. Always back up first."
        ),
        "debug": (
            "Debug Tools",
            "Advanced save inspection and repair tools.\n\n"
            "PARC Tree: View the save file's internal structure\n"
            "Hex Viewer: Inspect raw bytes at any offset\n"
            "Block Inspector: Browse individual data blocks\n\n"
            "For developers and advanced troubleshooting only."
        ),
        "backup": (
            "Backup / Restore",
            "Manage save file backups.\n\n"
            "Auto-backup creates a timestamped copy before every save.\n"
            "Use 'Restore from Backup' to revert to any previous state.\n\n"
            "Backup location: same folder as your save files.\n"
            "Backups use .bak extension and include the timestamp."
        ),
        "faction": (
            "Faction Editor",
            "Change your character's faction alignment.\n\n"
            "WARNING: This is experimental. Changing factions may\n"
            "break quest progression or cause unexpected behavior.\n"
            "Always back up your save first."
        ),
        "gpatch": (
            "GPatch (Game Patches)",
            "Apply optional patches to game files (PAZ archives).\n\n"
            "HOW TO USE:\n"
            "1. Click 'Auto-Detect' to find your game installation\n"
            "2. Review available patches and their descriptions\n"
            "3. Select a patch and click 'Apply Selected'\n"
            "4. A backup is created automatically\n"
            "5. Use 'Restore from Backup' to undo any patch\n\n"
            "COMMUNITY MOD LOADER:\n"
            "Load community JSON mods alongside Save Editor patches.\n"
            "1. Drop .json mod files into the Json folder\n"
            "   (click 'Open Mods Folder' to find it)\n"
            "2. Enable/disable mods with checkboxes\n"
            "3. Click 'Apply Community Mods'\n"
            "4. Conflicts with our patches are detected automatically\n\n"
            "ASI PLUGIN MANAGER:\n"
            "1. Drop .asi files into SEModLoad/ASI folder\n"
            "2. Click 'Install Selected' to copy to bin64/\n"
            "3. Enable/disable with checkboxes\n"
            "4. ASI loader DLL (winmm.dll) is auto-detected\n\n"
            "WARNING: This modifies GAME FILES, not save files.\n"
            "If the game updates, patches may need to be re-applied.\n"
            "If something goes wrong:\n"
            "  - Click 'Restore All from Backup'\n"
            "  - Or use Steam > Verify Integrity of Game Files\n\n"
            "REQUIRES: Administrator privileges."
        ),
    }

    def _make_help_btn(self, guide_key: str) -> QPushButton:
        btn = QPushButton("?")
        btn.setFixedSize(28, 28)
        btn.setToolTip("Show help for this tab")
        btn.setStyleSheet(
            f"QPushButton {{ background-color: {COLORS['error']}; color: white; "
            f"font-weight: bold; font-size: 14px; border: 2px solid {COLORS['error']}; "
            f"border-radius: 14px; padding: 0; }}"
            f"QPushButton:hover {{ background-color: #ff6655; border-color: #ff6655; }}"
        )
        btn.clicked.connect(lambda: self._show_guide(guide_key))
        return btn

    def _make_scope_label(self, scope: str) -> QLabel:
        if scope == "save":
            text = "This tab modifies your SAVE FILE"
            color = "#4FC3F7"
            bg = "rgba(79,195,247,0.08)"
        elif scope == "game":
            text = "This tab modifies GAME FILES (requires admin + restart)"
            color = "#FFB74D"
            bg = "rgba(255,183,77,0.08)"
        elif scope == "readonly":
            text = "This tab is READ-ONLY (browse only)"
            color = "#B0A088"
            bg = "rgba(176,160,136,0.05)"
        else:
            text = scope
            color = "#4FC3F7"
            bg = "rgba(79,195,247,0.08)"
        lbl = QLabel(text)
        lbl.setStyleSheet(
            f"color: {color}; font-size: 11px; padding: 3px 8px; "
            f"border: 1px solid {color}; border-radius: 3px; "
            f"background-color: {bg}; font-weight: bold;"
        )
        lbl.setFixedHeight(22)
        return lbl

    def _show_guide(self, key: str) -> None:
        title, text = self._GUIDES.get(key, ("Unknown", "No guide available."))
        QMessageBox.information(self, f"Guide: {title}", text)

    def _open_discord(self) -> None:
        import webbrowser
        webbrowser.open("https://discord.gg/nBqSzunyFS")

    def _show_about(self) -> None:
        QMessageBox.about(
            self,
            "About Crimson Desert Save Editor",
            f"Crimson Desert - Offline Save Editor\n"
            f"Version {APP_VERSION}\n\n"
            "View and edit Crimson Desert save files.\n\n"
            "Community item database: 6000+ items\n"
            "Item packs for easy sharing\n"
            "Auto-backup before every save\n\n"
            "Credits:\n"
            "  @Gek — save decryption logic\n"
            "  @Potter420 & @LukeFZ — pycrimson/crimson-rs parser\n\n"
            "Made by the Crimson Desert modding community.\n"
        )


    def _on_sub_tab_changed(self, tab_widget, index: int) -> None:
        widget = tab_widget.widget(index)
        if hasattr(self, '_community_tab_widget') and widget is self._community_tab_widget:
            self._update_community_status()
        elif hasattr(self, '_waypoint_tab_widget') and widget is self._waypoint_tab_widget:
            if hasattr(self, '_waypoint_tab'): self._waypoint_tab._populate_waypoints()
        elif hasattr(self, '_swap_tab_widget') and widget is self._swap_tab_widget:
            if hasattr(self, '_swap_tab'):
                self._swap_tab._on_tab_changed_swap()

    def _on_tab_changed(self, index: int) -> None:
        pass

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._tabs.currentChanged.connect(self._on_tab_changed)
        for sub in [self._save_tabs, self._mods_tabs, self._items_tabs, self._world_tabs]:
            sub.currentChanged.connect(lambda idx, t=sub: self._on_sub_tab_changed(t, idx))
        # Windows UIPI fix: when the app runs elevated (Run as admin) and
        # Explorer is not, Windows silently blocks drag-drop messages
        # coming from the unelevated process. Call
        # ChangeWindowMessageFilterEx on WM_DROPFILES / WM_COPYDATA /
        # 0x0049 (WM_COPYGLOBALDATA) so they pass through the integrity
        # boundary and reach our drop zones (Stacker, Mods, etc.).
        # No-op on non-Windows builds.
        try:
            _enable_drag_drop_under_uipi(int(self.winId()))
        except Exception as exc:
            log.debug("UIPI drop filter not applied: %s", exc)

        # Shared state / overlay coordinator disabled — not used yet.
        # QTimer.singleShot(2000, self._run_startup_audit)
