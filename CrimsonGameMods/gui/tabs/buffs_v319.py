from __future__ import annotations

import datetime
import json
import logging
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import traceback
import textwrap
from typing import Callable, List, Optional, Tuple

from PySide6.QtCore import Qt, QSize, QTimer, Signal
from PySide6.QtGui import QAction, QBrush, QColor, QFont, QIcon
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog,
    QDialogButtonBox, QFileDialog, QFrame, QGridLayout, QGroupBox,
    QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMenu, QMessageBox, QPushButton,
    QScrollArea, QSizePolicy, QSpinBox, QSplitter, QTableWidget,
    QTableWidgetItem, QTabWidget, QTextEdit, QToolButton, QVBoxLayout,
    QWidget,
)

from gui.theme import COLORS, CATEGORY_COLORS
from gui.iteminfo_index import IteminfoIndex
from models import SaveItem, SaveData, UndoEntry
from item_db import ItemNameDB
from equipment_sets import SetManager, EquipmentSet, SetItem, StatOperation
from paz_patcher import (
    PazPatchManager, PazPatch,
    ItemBuffPatcher, ItemRecord, StatTriplet, BUFF_HASHES, BUFF_NAMES,
    ItemEffectPatcher,
)
from icon_cache import IconCache, ICON_SIZE

try:
    from gui.utils import make_help_btn
except Exception:
    def make_help_btn(topic, fn=None):
        btn = QPushButton("?")
        btn.setFixedSize(22, 22)
        if fn:
            btn.clicked.connect(lambda: fn(topic))
        return btn

log = logging.getLogger(__name__)


def _can_write_game_dir(game_path: str) -> bool:
    try:
        _t = os.path.join(game_path, ".se_write_test")
        with open(_t, "w") as _f:
            _f.write("t")
        os.remove(_t)
        return True
    except Exception:
        return False


def _is_game_running() -> bool:
    try:
        out = subprocess.check_output(
            ["tasklist", "/FI", "IMAGENAME eq CrimsonDesert.exe", "/FO", "CSV", "/NH"],
            stderr=subprocess.DEVNULL, text=True, timeout=3,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return "CrimsonDesert.exe" in out
    except Exception:
        return False


class ItemBuffsTab(QWidget):

    dirty = Signal()
    status_message = Signal(str)
    config_save_requested = Signal()
    paz_refresh_requested = Signal()
    undo_entry_added = Signal(object)
    scan_requested = Signal()
    navigate_requested = Signal(str)
    open_save_browser_requested = Signal()

    def __init__(
        self,
        name_db: Optional[ItemNameDB] = None,
        icon_cache=None,
        config: Optional[dict] = None,
        show_guide_fn=None,
        paz_manager: Optional["PazPatchManager"] = None,
        set_manager: Optional["SetManager"] = None,
        parent=None,
    ):
        super().__init__(parent)
        self._name_db = name_db
        self._icon_cache = icon_cache
        self._config = config if config is not None else {}
        self._show_guide_fn = show_guide_fn
        self._paz_manager = paz_manager
        self._set_manager = set_manager if set_manager is not None else SetManager()
        self._save_data: Optional[SaveData] = None
        self._items: List[SaveItem] = []
        self._game_path: str = self._config.get("game_install_path", "")
        self._buff_patcher: Optional[ItemBuffPatcher] = None
        self._buff_rust_lookup = {}
        self._index: Optional[IteminfoIndex] = None  # built lazily after extract
        self._buff_icons_enabled = True
        self._buff_modified = False
        self._buff_item_limits = {}
        self._experimental_mode: bool = bool(self._config.get("experimental_mode", False))
        self._favorite_items: List[dict] = self._config.setdefault("favorite_items", [])
        self._build_ui()

    def _safely_replace_buff_item(self, target_key: int, new_dict: dict) -> None:
        new_dict['key'] = target_key
        replaced = False
        for i, it in enumerate(self._buff_rust_items):
            if it.get('key') == target_key:
                self._buff_rust_items[i] = new_dict
                replaced = True
                break
        if not replaced:
            self._buff_rust_items.append(new_dict)
        if hasattr(self, '_buff_rust_lookup'):
            self._buff_rust_lookup[target_key] = new_dict

    def _buff_parse_to_lookup(self, raw_bytes: bytes) -> dict:
        """Parse iteminfo bytes into {key: item_dict} using per-entry fallback."""
        import crimson_rs
        try:
            items = crimson_rs.parse_iteminfo_from_bytes(raw_bytes)
            return {it['key']: it for it in items}
        except Exception:
            pass
        import struct as _st
        game_path = getattr(self._buff_patcher, 'game_path', None) if hasattr(self, '_buff_patcher') else None
        if not game_path:
            raise RuntimeError("Cannot parse: no game path for pabgh extraction")
        pabgh = bytes(crimson_rs.extract_file(
            game_path, '0008', 'gamedata/binary__/client/bin', 'iteminfo.pabgh'))
        countA = _st.unpack_from('<H', pabgh, 0)[0]
        rs = (len(pabgh) - 2) // countA if countA else 8
        entries = []
        for _i in range(countA):
            rec = 2 + _i * rs
            if rec + rs > len(pabgh): break
            _soff = _st.unpack_from('<I', pabgh, rec + (rs - 4))[0]
            if _soff + 8 <= len(raw_bytes):
                entries.append(_soff)
        entries.sort()
        result = {}
        for _idx, _soff in enumerate(entries):
            _nxt = entries[_idx + 1] if _idx + 1 < len(entries) else len(raw_bytes)
            try:
                _parsed = crimson_rs.parse_iteminfo_from_bytes(raw_bytes[_soff:_nxt])
                if _parsed:
                    result[_parsed[0]['key']] = _parsed[0]
            except Exception:
                pass
        return result

    def _rebuild_full_iteminfo(self) -> bytearray:
        """Serialize all parsed items + raw unparsed items in vanilla order.

        Returns a complete iteminfo.pabgb with all items present and a
        matching pabgh stored in self._buff_rebuilt_pabgh.
        """
        import crimson_rs
        game_path = self._buff_patcher.game_path

        # ALWAYS use vanilla pabgb + vanilla pabgh as the ordering reference.
        # self._buff_data might be overlay data with different offsets — using
        # vanilla pabgh offsets into overlay data reads garbage keys.
        _van_raw = bytes(crimson_rs.extract_file(
            game_path, '0008', 'gamedata/binary__/client/bin', 'iteminfo.pabgb'))
        _pabgh = bytes(crimson_rs.extract_file(
            game_path, '0008', 'gamedata/binary__/client/bin', 'iteminfo.pabgh'))
        _pcount = struct.unpack_from('<H', _pabgh, 0)[0]
        _prs = (len(_pabgh) - 2) // _pcount if _pcount else 8

        _order = []
        for _pi in range(_pcount):
            _prec = 2 + _pi * _prs
            if _prec + _prs > len(_pabgh):
                break
            _psoff = struct.unpack_from('<I', _pabgh, _prec + (_prs - 4))[0]
            if _psoff + 4 <= len(_van_raw):
                _pk = struct.unpack_from('<I', _van_raw, _psoff)[0]
                _pnxt_idx = _pi + 1
                _pnxt = len(_van_raw)
                if _pnxt_idx < _pcount:
                    _pnxt_rec = 2 + _pnxt_idx * _prs
                    if _pnxt_rec + _prs <= len(_pabgh):
                        _pnxt = struct.unpack_from('<I', _pabgh, _pnxt_rec + (_prs - 4))[0]
                _order.append((_pk, _psoff, _pnxt - _psoff))

        _parsed_ser = {}
        for it in self._buff_rust_items:
            _parsed_ser[it['key']] = crimson_rs.serialize_iteminfo([it])

        _unparsed_map = {}
        for _raw in getattr(self, '_buff_unparsed_raw', []) or []:
            _uk = struct.unpack_from('<I', _raw, 0)[0]
            _unparsed_map[_uk] = _raw

        final = bytearray()
        _new_entries = []
        for _pk, _psoff, _psize in _order:
            _new_entries.append((_pk, len(final)))
            if _pk in _parsed_ser:
                final.extend(_parsed_ser[_pk])
            elif _pk in _unparsed_map:
                final.extend(_unparsed_map[_pk])
            else:
                final.extend(_van_raw[_psoff:_psoff + _psize])

        _new_pabgh = bytearray(struct.pack('<H', len(_new_entries)))
        for _pk, _poff in _new_entries:
            _new_pabgh.extend(struct.pack('<II', _pk, _poff))
        self._buff_rebuilt_pabgh = bytes(_new_pabgh)
        return final

    def set_experimental_mode(self, enabled: bool) -> None:
        self._experimental_mode = bool(enabled)
        # UP v1 is dev-only (kept for research)
        for w in getattr(self, '_dev_buff_widgets', []):
            w.setVisible(self._experimental_mode)

    def _require_dev_mode(self, action_name: str = "Export") -> bool:
        if self._experimental_mode:
            return True
        reply = QMessageBox.question(
            self, f"{action_name} requires Dev Mode",
            f"{action_name} is an advanced feature that requires Dev Mode.\n\n"
            "By enabling Dev Mode you agree to the following:\n\n"
            "  - Experimental features may corrupt saves or crash the game\n"
            "  - Legacy export formats (Export as Mod / CDUMM / JSON Patch)\n"
            "    have been removed. These used byte offsets that break on\n"
            "    every game update.\n"
            "  - Export as Field JSON v3 is the only supported export format.\n"
            "    It uses field names and survives game updates.\n"
            "  - JMM and CDUMM mod managers do not yet support Field JSON v3.\n"
            "    Until they adopt it, exported mods will only work with the\n"
            "    Stacker Tool or mod loaders that support Format 3.\n"
            "  - Apply to Game remains the recommended deployment method\n"
            "    for personal use.\n\n"
            "Enable Dev Mode now?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return False
        self._experimental_mode = True
        main_win = self.window()
        if hasattr(main_win, '_experimental_mode'):
            main_win._experimental_mode = True
            main_win._config["experimental_mode"] = True
            main_win._save_config()
            if hasattr(main_win, '_experimental_action'):
                main_win._experimental_action.setChecked(True)
            if hasattr(main_win, '_update_experimental_tabs'):
                main_win._update_experimental_tabs()
        return True


    def load(self, save_data: SaveData, items: List[SaveItem]) -> None:
        self._save_data = save_data
        self._items = items if items is not None else []

        buf_data   = getattr(self, "_buff_data", None)
        buf_items  = getattr(self, "_buff_items", None)
        if (items and buf_data is not None and buf_items
                and hasattr(self, "_buff_items_table")):
            try:
                self._buff_show_my_inventory(silent=True)
            except Exception:
                pass

    def unload(self) -> None:
        self._save_data = None
        self._items = []

    def set_game_path(self, path: str) -> None:
        self._game_path = path or ""
        if hasattr(self, "_buff_game_path") and self._buff_game_path is not None:
            self._buff_game_path.setText(self._game_path)
            self._buff_game_path.setToolTip(self._game_path)

    def set_icons_enabled(self, enabled: bool) -> None:
        if hasattr(self, "_buff_icons_enabled") and self._buff_icons_enabled != enabled:
            self._buff_toggle_icons()


    def _make_collapsible(self, label: str, content: QWidget,
                          start_open: bool = True,
                          config_key: str = None) -> QWidget:
        accent = COLORS.get("accent", "#daa850")
        if config_key and self._config.get(config_key) is not None:
            start_open = self._config[config_key]
        wrapper = QWidget()
        vbox = QVBoxLayout(wrapper)
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.setSpacing(0)

        toggle = QPushButton(("▾ " if start_open else "▸ ") + label)
        toggle.setStyleSheet(
            f"QPushButton {{ text-align: left; font-weight: bold; font-size: 11px;"
            f" padding: 3px 8px; background: transparent;"
            f" color: {accent}; border: none; border-bottom: 1px solid {accent}; }}"
            f"QPushButton:hover {{ background: rgba(218,168,80,0.10); }}")
        toggle.setCursor(Qt.PointingHandCursor)
        toggle.setFixedHeight(22)

        content.setVisible(start_open)
        cfg = self._config

        def _on_toggle():
            vis = not content.isVisible()
            content.setVisible(vis)
            toggle.setText(("▾ " if vis else "▸ ") + label)
            if config_key:
                cfg[config_key] = vis
                self.config_save_requested.emit()
        toggle.clicked.connect(_on_toggle)

        vbox.addWidget(toggle)
        vbox.addWidget(content)
        return wrapper

    def _build_ui(self) -> None:
        # from PySide6.QtWidgets import QScrollArea, QSizePolicy

        def build_help_row() -> QHBoxLayout:
            help_row = QHBoxLayout()
            help_row.setSpacing(4)
            help_row.addStretch(1)
            help_row.addWidget(make_help_btn("itembuffs", self._show_guide_fn))
            return help_row

        def build_warn_label() -> QLabel:
            warn_label = QLabel(
                "\u26A0  Buff and stat names may be inaccurate — they are community-mapped, "
                "not from official game data. Some buffs share numeric keys across different "
                "systems (stats/buffs/passives are 3 separate ID namespaces). "
                "If a name looks wrong, trust the in-game tooltip after applying."
            )
            warn_label.setWordWrap(True)
            warn_label.setStyleSheet(
                f"color: #FFB74D; padding: 6px; font-size: 10px; "
                f"border: 1px solid #5D4037; border-radius: 4px; "
                f"background-color: rgba(93,64,55,0.25);"
            )
            return warn_label

        def build_more_menu() -> QMenu:
            more_menu = QMenu(self)
            more_menu.setToolTipsVisible(True)

            act_sync = more_menu.addAction("Sync Buff Names from GitHub")
            act_sync.setToolTip(
                "Download community-verified buff/stat/passive names.")
            act_sync.triggered.connect(self._buff_sync_community_names)

            more_menu.addSeparator()
            
            act_verify = more_menu.addAction("Verify Applied Overlay...")
            act_verify.setToolTip(
                "Diagnostics: extract your current overlay and report how many "
                "items actually have each mutation applied. Use after Apply to "
                "Game to confirm the overlay matches expectations.")
            act_verify.triggered.connect(self._buff_verify_applied_overlay)

            act_restore = more_menu.addAction("Restore Original (remove overlay)")
            act_restore.setToolTip(
                "Undo 'Apply to Game': remove the ItemBuffs PAZ overlay and its "
                "PAPGT entry. Requires admin.")
            act_restore.triggered.connect(self._buff_restore_original)

            act_reset_vanilla = more_menu.addAction(
                "Reset to Vanilla PAPGT (nuclear)")
            act_reset_vanilla.setToolTip(
                "NUCLEAR RECOVERY: restore first-apply PAPGT snapshot. "
                "Disables ALL overlays. Requires admin.")
            act_reset_vanilla.triggered.connect(self._buff_reset_vanilla_papgt)
  
            return more_menu

        def build_action_row() -> QHBoxLayout:
            action_row = QHBoxLayout()
            action_row.setSpacing(4)

            extract_rust_btn = QPushButton("Extract")
            extract_rust_btn.setObjectName("accentBtn")
            extract_rust_btn.setToolTip("Extract iteminfo from game.\n"
                "Uses existing overlay if present, falls back to vanilla.")
            extract_rust_btn.clicked.connect(self._buff_extract_rust)
            action_row.addWidget(extract_rust_btn)

            extract_vanilla_btn = QPushButton("Extract Vanilla")
            extract_vanilla_btn.setToolTip("Always extract from vanilla game files (0008/).\n"
                "Use this after Apply to Game to get a clean baseline.")
            extract_vanilla_btn.clicked.connect(self._buff_extract_vanilla)
            action_row.addWidget(extract_vanilla_btn)
            
            reset_btn = QPushButton("Reset")
            reset_btn.setToolTip("Discard all in-memory changes, re-extract from disk")
            reset_btn.clicked.connect(self._buff_remove_all)
            action_row.addWidget(reset_btn)

            enable_exports_btn = QPushButton("Enable Exports")
            enable_exports_btn.setStyleSheet(
                "QPushButton { background-color: #6A1B9A; color: white; font-weight: bold; }")
            enable_exports_btn.setToolTip("Enable export features (Dev Mode required)")
            enable_exports_btn.clicked.connect(lambda: self._require_dev_mode("Enable Exports"))
            action_row.addWidget(enable_exports_btn)

            apply_game_btn = QPushButton("Apply to Game")
            apply_game_btn.setStyleSheet("QPushButton {"
                "background-color: #B71C1C; color: white; font-weight: bold; }")
            apply_game_btn.setToolTip(
                "Deploy modified iteminfo.pabgb directly to the game.\n"
                "Creates a PAZ overlay — original files are NOT modified.\n"
                "Restart the game for changes to take effect.\n"
                "Use Restore (More ▾) to undo.")
            apply_game_btn.clicked.connect(self._buff_apply_to_game)
            self._buff_apply_game_btn = apply_game_btn
            action_row.addWidget(apply_game_btn)

            import_mod_btn = QPushButton("Import")
            import_mod_btn.setStyleSheet("QPushButton {"
                "background-color: #00695C; color: white; font-weight: bold; }")
            
            # START Import Menu
            import_mod_menu = QMenu(self)
            import_mod_menu.setToolTipsVisible(True)
            import_mod_btn.setMenu(import_mod_menu)
            action_row.addWidget(import_mod_btn)
            
            act_import_config = import_mod_menu.addAction("Import ItemBuffs Config")
            act_import_config.setToolTip(
                "Load a previously saved config file.")
            act_import_config.triggered.connect(self._buff_load_config)
            
            
            def transmog_config_load():
                "STUB"
            # act_import_transmog_config = import_mod_menu.addAction("Import Transmog Config")        
            # act_import_transmog_config.triggered.connect(transmog_config_load)
             
            # act_import_custom_item_config = import_mod_menu.addAction("Import Custom Item")
            
            import_mod_menu.addSeparator()
            
            # act_import_perfect_mod = import_mod_menu.addAction("Import Perfect Loader Mod...")
            # act_import_perfect_mod.setToolTip(
            #     "Import a Perfect/Semantic-format MOD.")
            # act_import_perfect_mod.triggered.connect(self._buff_import_perfect_mod)
            
            act_import_json_mod = import_mod_menu.addAction("Import JSON Mod...")
            act_import_json_mod.setToolTip(
                "Import a Pldada/DMM-format JSON byte patch (e.g. Infinity Durability).")
            act_import_json_mod.triggered.connect(self._buff_import_community_json)
            
            act_import_cdumm_mod = import_mod_menu.addAction("Import CDUMM/PAZ Mod...")
            act_import_cdumm_mod.setToolTip(
                "Reverse-engineer any CDUMM/PAZ mod folder back into an editable "
                "config.\nPoint at a mod's files/gamedata/binary__/client/bin/"
                "iteminfo.pabgb — every modified field becomes editable here.")
            act_import_cdumm_mod.triggered.connect(self._buff_import_mod_folder)
            # END Import Menu
            
            export_mod_btn = QPushButton("Export")
            export_mod_btn.setStyleSheet("QPushButton {"
                "background-color: #00695C; color: white; font-weight: bold; }")

            export_mod_menu = QMenu(self)
            export_mod_menu.setToolTipsVisible(True)
            export_mod_btn.setMenu(export_mod_menu)
            action_row.addWidget(export_mod_btn)
            
            # START Export Menu
            # Export buttons are dev-gated (normal users never see them).
            act_export_config = export_mod_menu.addAction("Export ItemBuffs Config")
            act_export_config.setToolTip(
                "Save your current edits as a reusable config file.")
            act_export_config.triggered.connect(self._buff_save_config)
            

            act_export_field = export_mod_menu.addAction("Export as Field JSON (v3)")
            act_export_field.setToolTip(
                "Export all edits as a Format 3 field-name JSON.\n"
                "Uses field names instead of byte offsets — survives game updates.\n"
                "Compatible with Stacker Tool and future mod loaders.")
            act_export_field.triggered.connect(self._buff_export_field_json_v3)
            export_mod_menu.addAction(act_export_field)
            
            export_mod_menu.addSeparator()
            
            act_export_mod = export_mod_menu.addAction("Export as Mod Folder")
            act_export_mod.setToolTip(
                "Export as a ready-to-use mod folder (NNNN/0.paz + 0.pamt + meta/0.papgt).\n"
                "Drop the folder into your game directory or import into a mod manager.\n"
                "Same as Apply to Game, but saves to a folder you choose instead.")
            act_export_mod.triggered.connect(self._buff_export_mod_folder)
            export_mod_menu.addAction(act_export_mod)
            
            act_export_legacy = export_mod_menu.addAction("Export as Legacy JSON (v2)")
            act_export_legacy.setToolTip(
                "Opens Stacker Tool to export as Format 2 byte-diff JSON.\n"
                "Use Pull ItemBuffs Edit in Stacker, then Export Legacy JSON.")
            act_export_legacy.triggered.connect(self._goto_stacker_legacy_export)   
                     
            # END Export Menu
            
            transmog_btn = QPushButton("Transmog (Armor / Weapon Visual Swap)")
            transmog_btn.setStyleSheet("QPushButton {"
                "background-color: #6A1B9A; color: white; font-weight: bold; }")
            transmog_btn.setToolTip(
                "Visual Transmog for ANY armor or weapon you own.\n\n"
                "• Make your endgame armor look like a fancy starter set\n"
                "• Make your sword look like a legendary weapon you don't have\n"
                "• Mix and match looks per slot — boots from one set, helm from another\n\n"
                "Opens a dialog with quick-filter buttons (Helm, Chest, Sword,\n"
                "Bow, Ring, etc.) so you find the right slot in one click.\n"
                "Stats / buffs / enchants are kept — only the visual model changes.\n\n"
                "Queued swaps apply automatically on Export as Mod or Apply to Game.")
            transmog_btn.clicked.connect(self._buff_open_transmog_dialog)
            action_row.addWidget(transmog_btn)

            create_item_btn = QPushButton("⚒ Create Custom Item")
            create_item_btn.setStyleSheet(
                "QPushButton { background-color: #00695C; color: white; "
                "font-weight: bold; font-size: 13px; }")
            create_item_btn.setToolTip(
                "Design a brand-new item by cloning an existing donor.\n\n"
                "• Pick any of 6,000+ game items as a starting point\n"
                "• Edit stats per enchant level, passives, buffs, sockets, gimmicks\n"
                "• Save/Load shareable configs\n"
                "• Two deploy modes:\n"
                "    — Swap to Vendor: replace a vendor item with your stats\n"
                "    — Apply to Game (New Item): mint a brand new key 999001+\n"
                "      with custom localized name\n\n"
                "Use 'Add to Save File' from inside the creator to push the new\n"
                "item into your save without external tools.")
            create_item_btn.clicked.connect(self._open_item_creator)
            action_row.addWidget(create_item_btn)

            # Standalone entry into the Add-to-Save dialog without having to
            # re-run Create Item. Scans the current 0058/ overlay for any
            # custom keys (>= 999001) and lets the user pick one to swap
            # into a save-file vendor item. Useful for testing or adding
            # an already-deployed custom item to additional saves.
            add_save_btn = QPushButton("🎒 Add Custom Item to Save")
            add_save_btn.setStyleSheet(
                "QPushButton { background-color: #1565C0; color: white; "
                "font-weight: bold; font-size: 13px; }")
            add_save_btn.setToolTip(
                "Open the Add-to-Save dialog for an ALREADY-deployed custom item.\n"
                "Scans <game>/0058/iteminfo.pabgb for keys in the custom range\n"
                "(999001+) and lets you pick one, then swaps it into a save file\n"
                "vendor/repurchase item — same flow as the Create Item post-apply\n"
                "prompt, but reusable without re-running Create Item.")
            add_save_btn.clicked.connect(self._open_add_to_save_picker)
            action_row.addWidget(add_save_btn)

            more_btn = QPushButton("More")
            more_menu = build_more_menu()
            more_btn.setMenu(more_menu)
            action_row.addWidget(more_btn)

            action_row.addWidget(make_help_btn("itembuffs", self._show_guide_fn))
            action_row.addStretch(1)
            
            credit = QLabel("credit: Potter420 & LukeFZ")
            credit.setStyleSheet("color: #FF5252; font-style: italic; padding: 2px;")
            action_row.addWidget(credit)
            
            return action_row

        def build_search_row() -> QHBoxLayout:
            search_row = QHBoxLayout()
            search_row.setSpacing(4)
            self._buff_search = QLineEdit()
            self._buff_search.setPlaceholderText("Item name (e.g. Earring, Sword, Necklace)...")
            self._buff_search.returnPressed.connect(self._buff_search_items)

            search_btn = QPushButton("Search")
            search_btn.clicked.connect(self._buff_search_items)

            # Category filter (populated after extract — empty until then)
            self._buff_category_filter = QComboBox()
            self._buff_category_filter.setToolTip(
                "Restrict results to items in a specific category.\n"
                "Populated from live iteminfo after Extract.")
            self._buff_category_filter.setMinimumWidth(180)
            self._buff_category_filter.addItem("All categories", None)
            self._buff_category_filter.currentIndexChanged.connect(self._buff_search_items)

            my_inv_btn = QPushButton("My Inventory")
            my_inv_btn.setToolTip("Show only items from your loaded save that exist in iteminfo")
            my_inv_btn.clicked.connect(self._buff_show_my_inventory)

            self._buff_show_icons_btn = QPushButton("Icons")
            self._buff_show_icons_btn.setToolTip("Toggle item icons in the items list")
            self._buff_show_icons_btn.clicked.connect(self._buff_toggle_icons)
            self._buff_icons_enabled = False

            def toggle_fav():
                self._buff_items_table.cellChanged.disconnect(toggle_fav)
                self._showing_favorites = False
            def show_favs():
                if self._showing_favorites: toggle_fav()
                self._showing_favorites = True
                self._show_similar_items({"key": 0},"favorites")
                self._buff_items_table.cellChanged.connect(toggle_fav)
            self._show_favorite_items = show_favs

            fav_btn = QPushButton("⭐")
            fav_btn.setToolTip("Show favorited items only")
            fav_btn.clicked.connect(show_favs)
            self._showing_favorites = False

            search_row.addWidget(fav_btn)
            search_row.addWidget(QLabel("Search:"))
            search_row.addWidget(self._buff_search, 1)
            search_row.addWidget(search_btn)
            search_row.addWidget(self._buff_category_filter)
            # search_row.addWidget(desc_search_btn)
            search_row.addWidget(my_inv_btn)
            search_row.addWidget(self._buff_show_icons_btn)
            
            return search_row

        def build_items_frame() -> QFrame:
            items_frame = QFrame()
            items_vlayout = QVBoxLayout(items_frame)
            items_vlayout.setContentsMargins(0, 0, 0, 0)
            items_vlayout.setSpacing(2)
            items_vlayout.addWidget(QLabel("Matching Items:"))
            self._buff_items_table = QTableWidget()
            self._buff_items_table.setColumnCount(6)
            self._buff_items_table.setHorizontalHeaderLabels(["", "Name", "Type", "Tier", "Enchants", "Stack"])
            self._buff_items_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
            self._buff_items_table.setSelectionBehavior(QAbstractItemView.SelectRows)
            self._buff_items_table.setSelectionMode(QAbstractItemView.SingleSelection)
            hdr_items = self._buff_items_table.horizontalHeader()
            hdr_items.setSectionResizeMode(0, QHeaderView.Interactive)
            self._buff_items_table.setColumnWidth(0, 0)
            hdr_items.setSectionResizeMode(1, QHeaderView.Interactive)
            self._buff_items_table.setColumnWidth(1, 180)
            hdr_items.setSectionResizeMode(2, QHeaderView.Interactive)
            self._buff_items_table.setColumnWidth(2, 70)
            hdr_items.setSectionResizeMode(3, QHeaderView.Interactive)
            self._buff_items_table.setColumnWidth(3, 70)
            hdr_items.setSectionResizeMode(4, QHeaderView.Interactive)
            self._buff_items_table.setColumnWidth(4, 70)
            hdr_items.setSectionResizeMode(5, QHeaderView.Interactive)
            self._buff_items_table.setColumnWidth(5, 50)
            hdr_items.setStretchLastSection(False)
            self._buff_items_table.verticalHeader().setDefaultSectionSize(24)
            self._buff_items_table.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
            self._buff_items_table.setSortingEnabled(True)
            self._buff_items_table.setContextMenuPolicy(Qt.CustomContextMenu)
            self._buff_items_table.customContextMenuRequested.connect(self._buff_items_context_menu)
            self._buff_items_table.selectionModel().selectionChanged.connect(
                self._buff_item_selected
            )
            self._buff_items_table.setMinimumHeight(120)
            self._buff_items_table.setColumnHidden(2, True)
            self._buff_items_table.setColumnHidden(4, True)
            self._buff_items_table.setColumnHidden(5, True)
            items_vlayout.addWidget(self._buff_items_table, 1)
            items_frame.setMinimumWidth(120)
            # items_frame.setMaximumWidth(280)
            return items_frame

        def build_stats_table_frame() -> QFrame:
            stats_table_frame = QFrame()
            stf_layout = QVBoxLayout(stats_table_frame)
            stf_layout.setContentsMargins(0, 0, 0, 0)
            stf_layout.setSpacing(2)
            self._buff_selected_label = QLabel("No item selected — search and click an item on the left")
            self._buff_selected_label.setStyleSheet(
                f"color: {COLORS['text_dim']}; font-weight: bold; padding: 2px 4px;"
            )
            stf_layout.addWidget(self._buff_selected_label)
            stf_layout.addWidget(QLabel("Current Stats / Buffs:"))
            self._buff_stats_table = QTableWidget()
            self._buff_stats_table.setColumnCount(2)
            self._buff_stats_table.setHorizontalHeaderLabels([
                "Stat/Buff", "Value",
            ])
            self._buff_stats_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
            self._buff_stats_table.setSelectionBehavior(QAbstractItemView.SelectRows)
            self._buff_stats_table.setSelectionMode(QAbstractItemView.SingleSelection)
            self._buff_stats_table.setContextMenuPolicy(Qt.CustomContextMenu)
            self._buff_stats_table.customContextMenuRequested.connect(self._buff_stats_context_menu)
            hdr_stats = self._buff_stats_table.horizontalHeader()
            hdr_stats.setSectionResizeMode(0, QHeaderView.Interactive)
            self._buff_stats_table.setColumnWidth(0, 240)
            hdr_stats.setSectionResizeMode(1, QHeaderView.Interactive)
            self._buff_stats_table.setColumnWidth(1, 100)
            hdr_stats.setStretchLastSection(False)
            self._buff_stats_table.verticalHeader().setDefaultSectionSize(24)
            self._buff_stats_table.setMinimumHeight(100)
            stf_layout.addWidget(self._buff_stats_table, 1)
            stats_table_frame.setMinimumHeight(120)
            stats_table_frame.setMinimumWidth(120)
            return stats_table_frame
        
        def build_buff_action_tabs() -> None:
            # ══════════════════════════════════════════════════════════════════
            # Action tabs — replaces the old scrollable controls panel and the
            # third-column "right panel". All the ~35 control rows that used to
            # be crammed into one scrollarea now live in a QTabWidget with 8
            # focused sub-tabs. Every widget attribute name (_buff_*, _eb_*,
            # _stack_check, _inf_dura_check, _buff_overlay_spin, etc.) is
            # preserved so existing handlers continue to reference them.
            #
            # Layout now: two-column horizontal splitter — items list | stats/tabs.
            # The old third "_buff_right_panel" is kept as an empty widget for
            # backwards compatibility; nothing is added to it.
            # ══════════════════════════════════════════════════════════════════

            # Build the QTabWidget and its 8 sub-pages via helper methods.
            self._buff_action_tabs = QTabWidget()
            self._buff_action_tabs.setMinimumHeight(220)
            self._buff_action_tabs.setMinimumWidth(120)
            self._buff_action_tabs.addTab(
                self._build_buff_hero_presets_page(), "Presets")
            self._buff_action_tabs.addTab(
                self._build_buff_quick_edit_page(), "Quick Edit")
            self._buff_action_tabs.addTab(
                self._build_buff_drop_data_page(), "Drop Data")
            self._buff_action_tabs.addTab(
                self._build_buff_effects_page(), "Passives & Effects")
            self._buff_action_tabs.addTab(
                self._build_buff_stats_page(), "Stats & Buffs")
            self._buff_action_tabs.addTab(
                self._build_buff_imbue_page(), "Imbue")
            self._buff_action_tabs.addTab(
                self._build_buff_global_mods_page(), "Global Mods")
            self._buff_action_tabs.addTab(
                self._build_buff_bulk_page(), "Bulk Actions")
            self._buff_action_adv_idx = self._buff_action_tabs.count()
            self._buff_action_tabs.addTab(
                self._build_buff_advanced_page(), "Advanced")
            # tab_test_idx = self._buff_action_tabs.count()

            # Advanced tab hidden unless dev/experimental mode is on.
            self._buff_action_tabs.setTabVisible(
                self._buff_action_adv_idx, self._experimental_mode)
            # for i in range(self._buff_action_tabs.count()):
            #     if i >= tab_test_idx:
            #          self._buff_action_tabs.setTabVisible(i, False)

        def build_status_label() -> QLabel:
            # Status label — always visible, directly above the compact bottom bar.
            self._buff_status_label = QLabel("")
            self._buff_status_label.setWordWrap(True)
            self._buff_status_label.setStyleSheet(
                f"color: {COLORS['text_dim']}; padding: 2px;"
            )

        def build_bottom_bar_wrap() -> QWidget:
            # ── Compact bottom bar: 4 primary buttons + More ▾ menu ──
            # Old bar had ~15 widgets in a FlowLayout that silently wrapped to
            # multiple rows at smaller resolutions. New bar keeps the must-have
            # actions visible on one row at 1280px wide and moves the rest into
            # a popup menu.
            from PySide6.QtWidgets import QToolButton
            bottom_bar_wrap = QWidget()
            bottom_bar = QHBoxLayout(bottom_bar_wrap)
            bottom_bar.setContentsMargins(0, 0, 0, 0)
            bottom_bar.setSpacing(6)

            export_field_btn = QPushButton("Export as Field JSON v3")
            export_field_btn.setStyleSheet(
                "background-color: #00695C; color: white; font-weight: bold;")
            export_field_btn.setToolTip(
                "Export all edits as a Format 3 field-name JSON.\n"
                "Uses field names instead of byte offsets — survives game updates.\n"
                "Compatible with Stacker Tool and future mod loaders.")
            export_field_btn.clicked.connect(self._buff_export_field_json_v3)
            bottom_bar.addWidget(export_field_btn)

            export_mod_btn = QPushButton("Export as Mod Folder")
            export_mod_btn.setStyleSheet(
                "background-color: #7B1FA2; color: white; font-weight: bold;")
            export_mod_btn.setToolTip(
                "Export as a ready-to-use mod folder (NNNN/0.paz + 0.pamt + meta/0.papgt).\n"
                "Drop the folder into your game directory or import into a mod manager.\n"
                "Same as Apply to Game, but saves to a folder you choose instead.")
            export_mod_btn.clicked.connect(self._buff_export_mod_folder)
            bottom_bar.addWidget(export_mod_btn)

            self._dev_export_btns_buffs = []

            # Primary action 1: Create Item (green)
            create_item_btn = QPushButton("Create Item")
            create_item_btn.setStyleSheet(
                "background-color: #00695C; color: white; font-weight: bold;")
            create_item_btn.setToolTip(
                "Create a new custom item by cloning an existing one.\n"
                "Pick a donor item, customize name and stats, deploy.\n"
                "Use the save editor Repurchase tab to acquire it in-game.")
            create_item_btn.clicked.connect(self._open_item_creator)
            bottom_bar.addWidget(create_item_btn)

            # Primary action 2: Apply to Game (red)
            apply_game_btn = QPushButton("Apply to Game")
            apply_game_btn.setStyleSheet(
                "background-color: #B71C1C; color: white; font-weight: bold;")
            apply_game_btn.setToolTip(
                "Deploy modified iteminfo.pabgb directly to the game.\n"
                "Creates a PAZ overlay — original files are NOT modified.\n"
                "Restart the game for changes to take effect.\n"
                "Use Restore (More ▾) to undo.")
            apply_game_btn.clicked.connect(self._buff_apply_to_game)
            self._buff_apply_game_btn = apply_game_btn
            bottom_bar.addWidget(apply_game_btn)

            # Primary action 3: Import Mod Folder (teal, power-user friendly)
            import_mod_btn = QPushButton("Import Mod Folder")
            import_mod_btn.setStyleSheet(
                "background-color: #00695C; color: white; font-weight: bold;")
            import_mod_btn.setToolTip(
                "Reverse-engineer any CDUMM/PAZ mod folder back into an editable "
                "config.\nPoint at a mod's files/gamedata/binary__/client/bin/"
                "iteminfo.pabgb — every modified field becomes editable here.")
            import_mod_btn.clicked.connect(self._buff_import_mod_folder)
            bottom_bar.addWidget(import_mod_btn)

            # More ▾ popup menu — collapses the other 6 rarely-used actions.
            more_btn = QToolButton()
            more_btn.setText("More ▾")
            more_btn.setPopupMode(QToolButton.InstantPopup)
            more_btn.setToolButtonStyle(Qt.ToolButtonTextOnly)
            more_btn.setStyleSheet(
                "QToolButton { padding: 6px 12px; border: 1px solid #554430; "
                "border-radius: 4px; background: #3d2e1a; color: #f0e6d4; } "
                "QToolButton:hover { background: #5c4320; } "
                "QToolButton::menu-indicator { image: none; width: 0; }"
            )
            more_menu = QMenu(self)

            act_import = more_menu.addAction("Import Community JSON Patch...")
            act_import.setToolTip(
                "Import a Pldada/DMM-format JSON byte patch (e.g. Infinity Durability).")
            act_import.triggered.connect(self._buff_import_community_json)

            act_sync = more_menu.addAction("Sync Buff Names from GitHub")
            act_sync.setToolTip(
                "Download community-verified buff/stat/passive names.")
            act_sync.triggered.connect(self._buff_sync_community_names)

            more_menu.addSeparator()

            act_save = more_menu.addAction("Save Config...")
            act_save.setToolTip(
                "Save your current edits as a reusable config file.")
            act_save.triggered.connect(self._buff_save_config)

            act_load = more_menu.addAction("Load Config...")
            act_load.setToolTip(
                "Load a previously saved config file.")
            act_load.triggered.connect(self._buff_load_config)

            more_menu.addSeparator()

            act_restore = more_menu.addAction("Restore Original (remove overlay)")
            act_restore.setToolTip(
                "Undo 'Apply to Game': remove the ItemBuffs PAZ overlay and its "
                "PAPGT entry. Requires admin.")
            act_restore.triggered.connect(self._buff_restore_original)

            act_reset_vanilla = more_menu.addAction(
                "Reset to Vanilla PAPGT (nuclear)")
            act_reset_vanilla.setToolTip(
                "NUCLEAR RECOVERY: restore first-apply PAPGT snapshot. "
                "Disables ALL overlays. Requires admin.")
            act_reset_vanilla.triggered.connect(self._buff_reset_vanilla_papgt)

            more_menu.addSeparator()

            act_verify = more_menu.addAction("Verify Applied Overlay...")
            act_verify.setToolTip(
                "Diagnostics: extract your current overlay and report how many "
                "items actually have each mutation applied. Use after Apply to "
                "Game to confirm the overlay matches expectations.")
            act_verify.triggered.connect(self._buff_verify_applied_overlay)

            more_btn.setMenu(more_menu)
            bottom_bar.addWidget(more_btn)

            bottom_bar.addStretch(1)

            credit = QLabel("credit: Potter420 & LukeFZ")
            credit.setStyleSheet("color: #FF5252; font-style: italic; padding: 2px;")
            bottom_bar.addWidget(credit)
            
            return bottom_bar_wrap
        
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(6, 6, 6, 6)
        outer_layout.setSpacing(4)
        
        inner_layout = QSplitter(Qt.Horizontal)
        inner_layout.setChildrenCollapsible(False)
        
        def ui_add_line(shadow=False) -> QFrame:
            line = QFrame()
            line.setFrameShape(QFrame.Shape.HLine)
            if shadow:
                line.setFrameShadow(QFrame.Shadow.Sunken)
            return line
        
        self._ui_add_line = ui_add_line
        
        # Kept as empty widget for any legacy code that references it.
        self._buff_right_panel = QWidget()
        
        self._buff_path_widget = QWidget()
        self._buff_path_widget.setVisible(False)
        self._buff_game_path = None

        # Passive skill + equip buff name maps — populated by helper before
        # building the Passives/Stats sub-tabs, so their combos can be filled.
        self._PASSIVE_SKILL_NAMES: dict = {}
        self._EQUIP_BUFF_NAMES: dict = {}
        self._buff_skill_descs: dict = {}
        self._buff_community_ranges: dict = {}
        self._cd_patches: dict = {}
        self._dev_buff_widgets: list = getattr(self, '_dev_buff_widgets', [])
        self._buff_load_name_data()

        warn_label = build_warn_label()
        outer_layout.addWidget(
            self._make_collapsible("Notice", warn_label,
                                   start_open=True, config_key="buffs_show_notice"))

        action_row_layout = build_action_row()
        action_row_widget = QWidget()
        action_row_widget.setLayout(action_row_layout)
        outer_layout.addWidget(
            self._make_collapsible("Actions", action_row_widget,
                                   start_open=True, config_key="buffs_show_actions"))

        search_row_layout = build_search_row()
        search_row_widget = QWidget()
        search_row_widget.setLayout(search_row_layout)
        outer_layout.addWidget(
            self._make_collapsible("Search", search_row_widget,
                                   start_open=True, config_key="buffs_show_search"))

        items_frame = build_items_frame()
        inner_layout.addWidget(items_frame)

        stats_table_frame = build_stats_table_frame()
        inner_layout.addWidget(stats_table_frame)

        build_buff_action_tabs()
        inner_layout.addWidget(self._buff_action_tabs)

        outer_layout.addWidget(inner_layout, 1)

        build_status_label()
        outer_layout.addWidget(self._buff_status_label)

        self._bottom_bar_wrap = build_bottom_bar_wrap()
        outer_layout.addWidget(self._bottom_bar_wrap)
        self._bottom_bar_wrap.setVisible(False)

        #---------------------------------------------------------------------

        # Mod folder load-order spinners — kept accessible but compact.
        # The attribute names (_buff_overlay_spin, _buff_modgroup_spin) are
        # referenced by export/config handlers, so they must exist.
        self._buff_overlay_spin = QSpinBox()
        self._buff_overlay_spin.setRange(1, 9999)
        self._buff_overlay_spin.setValue(self._config.get("buff_overlay_dir", 58))
        self._buff_overlay_spin.setFixedWidth(60)
        self._buff_overlay_spin.setToolTip(
            "PAZ folder slot used by 'Export JSON Patch'.\nDefault: 0058.")
        self._buff_overlay_spin.valueChanged.connect(
            lambda v: self._config.update({"buff_overlay_dir": v}) or self.config_save_requested.emit()
        )
        self._buff_modgroup_spin = QSpinBox()
        self._buff_modgroup_spin.setRange(1, 9999)
        self._buff_modgroup_spin.setValue(self._config.get("buff_mod_group", 36))
        self._buff_modgroup_spin.setFixedWidth(60)
        self._buff_modgroup_spin.setToolTip(
            "PAZ folder slot used by 'Export as Mod'.\nDefault: 0036.")
        self._buff_modgroup_spin.valueChanged.connect(
            lambda v: self._config.update({"buff_mod_group": v}) or self.config_save_requested.emit()
        )
        # Parent the spinners to bottom_bar_wrap so they exist in the widget
        # tree, but hide them by default (they're exposed via the Advanced tab).
        self._buff_overlay_spin.setParent(self._bottom_bar_wrap)
        self._buff_overlay_spin.setVisible(False)
        self._buff_modgroup_spin.setParent(self._bottom_bar_wrap)
        self._buff_modgroup_spin.setVisible(False)

        self._item_buffs_tab_widget = self
        self._itembuffs_tab_widget = self

        self._buff_patcher: Optional[ItemBuffPatcher] = None
        self._buff_rust_items: Optional[list] = None
        self._buff_rust_lookup: dict = {}
        self._buff_use_rust: bool = False
        self._buff_data: Optional[bytearray] = None
        self._buff_items: List[ItemRecord] = []
        self._buff_current_item: Optional[ItemRecord] = None
        self._armor_catalog: list = []
        self._transmog_swaps: list = []
        self._vfx_summaries: list = []
        self._vfx_size_changes: list = []
        self._vfx_swaps: list = []
        self._vfx_anim_swaps: list = []
        self._vfx_attach_changes: list = []

        self._buff_item_limits = {}
        try:
            import json as _json
            limits_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'item_limits.json')
            if os.path.isfile(limits_path):
                with open(limits_path, 'r') as _f:
                    self._buff_item_limits = _json.load(_f).get('items', {})
        except Exception:
            pass
        self._buff_modified = False

    # ─── ItemBuffs tab — data loader + sub-tab builders ──────────────────
    # Split out from _build_ui() so the main tab builder stays readable.
    # Widget attribute names are intentionally preserved (self._buff_*,
    # self._eb_*, self._stack_check, self._inf_dura_check, etc.) so the
    # dozens of handlers elsewhere in this class continue to reference
    # them without changes.

    def _buff_load_name_data(self) -> None:
        """Populate PASSIVE_SKILL_NAMES, EQUIP_BUFF_NAMES, descriptions.

        Reads skill_english_names.json, buff_database.json, buff_english_names.json,
        buff_names_community.json, and buff_skill_descriptions.json from a
        variety of plausible locations (src dir, data subdir, _MEIPASS, cwd).
        """
        import json as _json
        _here = os.path.dirname(os.path.abspath(__file__))
        _root = os.path.dirname(os.path.dirname(_here))
        _search_dirs = [
            _here,
            os.path.join(_here, 'data'),
            _root,
            os.path.join(_root, 'data'),
            os.path.join(getattr(sys, '_MEIPASS', ''), 'data'),
            getattr(sys, '_MEIPASS', ''),
            os.path.join(os.getcwd(), 'data'),
            os.getcwd(),
        ]

        # Passive skill names from skill_english_names.json
        try:
            for base in _search_dirs:
                _sep = os.path.join(base, 'skill_english_names.json')
                if os.path.isfile(_sep):
                    with open(_sep, 'r', encoding='utf-8') as _sf:
                        _all_skills = _json.load(_sf)
                    for _sk, _sv in _all_skills.items():
                        _key = int(_sk)
                        _name = _sv.get('english_name', '')
                        _internal = _sv.get('skill_name', '')
                        if (_internal.startswith('Equip_Passive_') or
                                _internal.startswith('Equip_Socket_Passive_') or
                                _key in (70994, 9128)):
                            if _name:
                                self._PASSIVE_SKILL_NAMES[_key] = _name
                            else:
                                clean = (_internal
                                         .replace('Equip_Socket_Passive_', '')
                                         .replace('Equip_Passive_', '')
                                         .replace('_', ' '))
                                self._PASSIVE_SKILL_NAMES[_key] = clean
                    log.info("Loaded %d passive skills from skill_english_names.json",
                             len(self._PASSIVE_SKILL_NAMES))
                    break
        except Exception as _e:
            log.warning("Passive skill load failed: %s", _e)

        _fallbacks = {
            70994: "Invincible", 9128: "Great Thief",
            8037: "Fire Resistance", 8038: "Ice Resistance",
            8039: "Lightning Resistance",
            7201: "Flying Boots", 7202: "Swimming Boots",
            7204: "Equip Drop Rate",
        }
        for _fk, _fv in _fallbacks.items():
            self._PASSIVE_SKILL_NAMES.setdefault(_fk, _fv)

        # Seed equip buff names (curated list of the most common buffs).
        self._EQUIP_BUFF_NAMES.update({
            1000001: "Max HP (HP)",
            1000002: "Max Spirit (MP)",
            1000003: "Max Stamina (SP)",
            1000004: "Damage Dealt (DDD)",
            1000005: "Defense (DPV)",
            1000006: "Attack Speed (AttackSpeedRate)",
            1000008: "HP Regen",
            1000009: "Spirit Regen (MP Regen)",
            1000046: "Stamina Regen",
            1000012: "Fire Resistance (Burn/Heat Immunity)",
            1000013: "Ice Resistance (Freeze/Cold Immunity)",
            1000014: "Lightning Resistance (Shock Immunity)",
            1000090: "Dmg vs Machines",
            1000108: "Dmg vs Earthen Beings (Hexe)",
            1000109: "Dmg vs Humanoids",
            1000110: "Dmg vs Walkers (Golems)",
            1000111: "Dmg vs Mighty Foes (Bosses)",
            1000112: "Dmg vs Beasts (Animals)",
            1000113: "Dmg vs Abyssal Creatures",
            1000147: "Crit vs Plate Armor",
            1000154: "Crit vs Leather Armor",
            1000157: "Crit vs Cloth Armor",
            1000096: "Damage Reduction (Aegis)",
            1000097: "Guard Stamina Cost Reduction",
            1000066: "Disarm on Hit (Equip Drop)",
            1000116: "Arrow Save Chance (Block Ammo)",
            1000141: "Stamina Regen Rate Change",
            1000015: "Stamina Cost Reduction",
            1000091: "Craft Material Save",
            1000071: "Bonus Ore Drop",
            1000072: "Bonus Plant Drop",
            1000073: "Bonus Animal Drop",
            1000093: "Bonus Log Drop",
            1000105: "Bonus Log Drop (Tool)",
            1000176: "Bonus Mining Drop (Tool)",
            1000117: "Silver Drop Rate",
            1000100: "Climb Speed",
            1000107: "Swim Speed",
            1000089: "Food Effect Lv Up",
            1000099: "NPC Trust Gain (Affinity)",
            1000114: "Contribution EXP Gain",
            1000115: "Skill EXP Gain",
            1000119: "Great Thief (Bonus Theft)",
            1000123: "Bonus Crafting Result Chance",
            1000124: "Pet Trust Gain",
            1000132: "Solidarity - Trust Boost (SizlekSword)",
            1000133: "Equestrian - Horse EXP Boost (MasterDoo)",
            1000081: "Daze Immunity (Boss)",
            1000149: "Abyss Toxin Immunity",
            1000150: "Poison Immunity",
            1000151: "Bismuth Immunity",
            1000191: "Sleep Immunity",
            1000192: "Daze Immunity (Food)",
            1000130: "Myurdin Sword Passive",
            1000131: "Split-Horn Sword Passive",
            1000134: "Companionship - Pet Trust Boost (Crowman Sword)",
            1000136: "Reed Devil Sword Passive",
            1000148: "Soul Knight Sword Passive",
            1000152: "Deer King Helm Passive",
            1000153: "Kill Resource Recovery",
            1000161: "White Horn Passive",
            1000030: "HP DOT (Damage Over Time)",
            1000051: "HP DOT Damage",
            1000052: "Spirit DOT Damage",
            1000053: "Stamina DOT Damage",
            1000200: "Wolf Disguise",
            1000201: "Bear Disguise",
            1000202: "Deer Disguise",
            1000203: "Wildlife Disguise",
            1000087: "Civilian Disguise",
        })

        # Merge buff_database.json (all 275 internal names) into the dict.
        try:
            for base in _search_dirs:
                _bdp = os.path.join(base, 'buff_database.json')
                if os.path.isfile(_bdp):
                    with open(_bdp, 'r', encoding='utf-8') as _bf:
                        _all_buffs = _json.load(_bf)
                    for _bk, _bv in _all_buffs.items():
                        _key = int(_bk)
                        if _key not in self._EQUIP_BUFF_NAMES:
                            _n = _bv.get('name', _bv.get('internal', ''))
                            if not _n:
                                _n = _bv.get('clean', '')
                            _n = _n.replace('BuffLevel_', '').replace('_', ' ')
                            if _n:
                                self._EQUIP_BUFF_NAMES[_key] = _n
                    log.info(
                        "Loaded %d buffs from buff_database.json (total: %d)",
                        len(_all_buffs), len(self._EQUIP_BUFF_NAMES))
                    break
        except Exception as _e:
            log.warning("buff_database.json load failed: %s", _e)

        # Overwrite with English names where available.
        try:
            for base in _search_dirs:
                _bep = os.path.join(base, 'buff_english_names.json')
                if os.path.isfile(_bep):
                    with open(_bep, 'r', encoding='utf-8') as _bf:
                        _eng_buffs = _json.load(_bf)
                    _eng_count = 0
                    for _bk, _bv in _eng_buffs.items():
                        _key = int(_bk)
                        _eng = _bv.get('english_name', '')
                        if _eng:
                            self._EQUIP_BUFF_NAMES[_key] = _eng
                            _eng_count += 1
                    log.info("Applied %d English buff names", _eng_count)
                    break
        except Exception as _e:
            log.warning("buff_english_names.json load failed: %s", _e)

        # Override with community-verified names + value ranges.
        try:
            for base in _search_dirs:
                _cnp = os.path.join(base, 'buff_names_community.json')
                if os.path.isfile(_cnp):
                    with open(_cnp, 'r', encoding='utf-8') as _cf:
                        _cdata = _json.load(_cf)
                    _c_count = 0
                    for _entry in _cdata.get('buffs', []):
                        _key = _entry.get('key', 0)
                        _name = _entry.get('name', '')
                        _effect = _entry.get('effect', '')
                        if _key > 0 and _name:
                            _display = _name
                            if _effect and _effect != _name:
                                _display = f"{_name} \u2014 {_effect[:40]}"
                            self._EQUIP_BUFF_NAMES[_key] = _display
                            _c_count += 1
                        _mn = _entry.get('minValue')
                        _mx = _entry.get('maxValue')
                        _vt = _entry.get('valueType', '')
                        if _mn is not None and _mx is not None:
                            self._buff_community_ranges[_key] = (_mn, _mx, _vt)
                    log.info("Applied %d community buff names, %d with ranges from %s",
                             _c_count, len(self._buff_community_ranges), _cnp)
                    break
        except Exception as _e:
            log.warning("buff_names_community.json load failed: %s", _e)

        # Skill descriptions for combo labels and search-by-description.
        try:
            for base in _search_dirs:
                _desc_path = os.path.join(base, 'buff_skill_descriptions.json')
                if os.path.isfile(_desc_path):
                    with open(_desc_path, 'r', encoding='utf-8') as _df:
                        self._buff_skill_descs = json.load(_df)
                    break
        except Exception:
            pass

        # Enchant stat list used by the Stats & Buffs sub-tab's stat combo.
        self._ENCHANT_STAT_LIST = [
            ("DDD / Damage", 1000002, "stat_list_static", 999999),
            ("DPV / Defense", 1000003, "stat_list_static", 999999),
            ("Max HP", 1000000, "stat_list_static", 999999),
            ("Critical Damage", 1000006, "stat_list_static", 999999),
            ("Incoming Damage Rate", 1000008, "stat_list_static", 999999),
            ("Incoming Damage Reduction", 1000009, "stat_list_static", 999999),
            ("DHIT / Accuracy", 1000004, "stat_list_static", 999999),
            ("DDV / Base Attack", 1000005, "stat_list_static", 999999),
            ("Stamina Cost Reduction", 1000037, "stat_list_static", 100000000),
            ("MP Cost Reduction", 1000046, "stat_list_static", 100000000),
            ("Max Damage Rate", 1000035, "stat_list_static", 999999),
            ("DPV Rate (%)", 1000050, "stat_list_static", 999999),
            ("Pressure", 1000036, "stat_list_static", 999999),
            ("Attack Speed", 1000010, "stat_list_static_level", 15),
            ("Move Speed", 1000011, "stat_list_static_level", 15),
            ("Crit Rate", 1000007, "stat_list_static_level", 15),
            ("Climb Speed", 1000012, "stat_list_static_level", 15),
            ("Swim Speed", 1000013, "stat_list_static_level", 15),
            ("Fire Resistance", 1000016, "stat_list_static_level", 15),
            ("Ice Resistance", 1000017, "stat_list_static_level", 15),
            ("Lightning Resistance", 1000018, "stat_list_static_level", 15),
            ("Guard PV Rate", 1000043, "stat_list_static_level", 15),
            ("Hit Rate", 1000031, "stat_list_static_level", 15),
            ("Equip Drop Rate", 1000049, "stat_list_static_level", 15),
            ("Add Money Drop Rate", 1000047, "stat_list_static_level", 15),
            ("HP Regen", 1000000, "regen_stat_list", 1000000),
            ("Stamina Regen", 1000026, "regen_stat_list", 100000),
            ("MP Regen", 1000027, "regen_stat_list", 100000),
        ]

    def _build_buff_quick_edit_page(self) -> QWidget:
        """Quick Edit sub-tab — preset, custom row, edit-selected stat."""
        page = QWidget()
        pl = QVBoxLayout(page)
        pl.setContentsMargins(8, 8, 8, 8)
        pl.setSpacing(6)

        preset_row = QHBoxLayout()
        preset_row.setSpacing(4)
        preset_row.addWidget(QLabel("Preset:"))
        self._buff_preset_combo = QComboBox()
        self._buff_preset_combo.addItems([
            "Max All (max every stat value, no hash changes)",
            "Max All Flat (max value on all flat stat entries)",
            "Max DDD (max value on flat2 entries)",
            "Max DPV (max value on flat2 entries)",
            "Max HP (max value on flat1 entries)",
            "Max All Rates (max value on all rate entries)",
            "Swap to DDD (change flat2 hashes to Damage)",
            "Swap to DPV (change flat2 hashes to Defense)",
            "Custom (pick stat + value)",
        ])
        self._buff_preset_combo.currentIndexChanged.connect(self._buff_preset_changed)
        preset_row.addWidget(self._buff_preset_combo, 1)

        apply_preset_btn = QPushButton("Apply Preset")
        apply_preset_btn.setObjectName("accentBtn")
        apply_preset_btn.clicked.connect(self._buff_add_to_item)
        preset_row.addWidget(apply_preset_btn)

        suggest_btn = QPushButton("Suggest from Cluster")
        suggest_btn.setToolTip(
            "Show the stat template typical for items like the one selected "
            "(same item_type + tier). Read-only.")
        suggest_btn.clicked.connect(self._buff_show_stat_template)
        preset_row.addWidget(suggest_btn)

        reset_btn = QPushButton("Reset")
        reset_btn.setToolTip("Discard all in-memory changes, re-extract from disk")
        reset_btn.clicked.connect(self._buff_remove_all)
        preset_row.addWidget(reset_btn)
        pl.addLayout(preset_row)

        # Custom stat row (hidden unless "Custom" preset selected).
        self._buff_custom_row = QWidget()
        custom_layout = QHBoxLayout(self._buff_custom_row)
        custom_layout.setContentsMargins(0, 0, 0, 0)
        custom_layout.setSpacing(4)
        custom_layout.addWidget(QLabel("Stat:"))
        self._buff_type_combo = QComboBox()
        for name in BUFF_HASHES:
            self._buff_type_combo.addItem(name)
        custom_layout.addWidget(self._buff_type_combo, 1)
        custom_layout.addWidget(QLabel("Value:"))
        self._buff_value_spin = QSpinBox()
        self._buff_value_spin.setRange(0, 999999999)
        self._buff_value_spin.setValue(1000000)
        self._buff_value_spin.setToolTip(
            "Flat stats (HP/DDD/DPV): use large values like 1,000,000\n"
            "Rate stats: 1 byte, 0-255 (max varies per stat type)\n"
            "Invincible: 1 = on, 0 = off")
        custom_layout.addWidget(self._buff_value_spin)
        self._buff_custom_row.setVisible(False)
        pl.addWidget(self._buff_custom_row)

        # Edit-Selected-Stat + Refinement Level row.
        edit_refine_row = QHBoxLayout()
        edit_refine_row.setSpacing(4)
        edit_refine_row.addWidget(QLabel("Edit Stat:"))
        self._buff_sel_value_spin = QSpinBox()
        self._buff_sel_value_spin.setRange(0, 999999999)
        self._buff_sel_value_spin.setValue(0)
        self._buff_sel_value_spin.setToolTip(
            "Set the value for the selected base stat "
            "(Attack/Defense/DDD/DPV/HP etc.)")
        edit_refine_row.addWidget(self._buff_sel_value_spin)

        edit_sel_btn = QPushButton("Apply to Stat")
        edit_sel_btn.setToolTip("Change ONLY the clicked base stat")
        edit_sel_btn.clicked.connect(self._buff_apply_to_selected)
        edit_refine_row.addWidget(edit_sel_btn)

        self._buff_sel_label = QLabel("")
        self._buff_sel_label.setStyleSheet(f"color: {COLORS['accent']};")
        edit_refine_row.addWidget(self._buff_sel_label, 1)

        edit_refine_row.addWidget(QLabel("Refine:"))
        self._buff_array_combo = QComboBox()
        self._buff_array_combo.addItem("All Levels (apply to every array)")
        self._buff_array_combo.setToolTip(
            "Select which refinement level to apply presets to.\n"
            "'All Levels' applies to every array (default).")
        edit_refine_row.addWidget(self._buff_array_combo)
        pl.addLayout(edit_refine_row)

        # Kept as empty widgets for any legacy API references.
        self._buff_edit_selected_row = QWidget()
        self._buff_array_row = QWidget()

        # Connect stat-table selection to the edit-selected controls.
        self._buff_stats_table.selectionModel().selectionChanged.connect(
            self._buff_on_stat_selected)

        # Description label (compact, hidden by default).
        self._buff_desc_label = QLabel()
        self._buff_desc_label.setWordWrap(True)
        self._buff_desc_label.setStyleSheet(
            f"color: {COLORS['text_dim']}; padding: 2px; font-size: 10px;"
        )
        self._buff_desc_label.setVisible(False)
        self._buff_preset_combo.currentIndexChanged.connect(self._buff_update_desc)
        self._buff_type_combo.currentTextChanged.connect(self._buff_update_desc)
        self._buff_update_desc()
        pl.addWidget(self._buff_desc_label)
        
        pl.addWidget(self._ui_add_line(True))
        
        # --- Socket count ---
        self._eb_socket_row_widget = QWidget()
        socket_row = QHBoxLayout(self._eb_socket_row_widget)
        socket_row.setContentsMargins(0, 0, 0, 0)
        socket_row.setSpacing(6)
        socket_row.addWidget(QLabel("Count:"))
        self._eb_socket_count = QSpinBox()
        self._eb_socket_count.setRange(1, 8)
        self._eb_socket_count.setValue(5)
        self._eb_socket_count.setFixedWidth(60)
        self._eb_socket_count.setToolTip(
            "Target max socket count. Writes to drop_default_data."
            "add_socket_material_item_list.")
        socket_row.addWidget(self._eb_socket_count)
        socket_row.addWidget(QLabel("Pre-unlocked:"))
        self._eb_socket_valid = QSpinBox()
        self._eb_socket_valid.setRange(0, 8)
        self._eb_socket_valid.setValue(0)
        self._eb_socket_valid.setFixedWidth(60)
        self._eb_socket_valid.setToolTip(
            "How many sockets are unlocked on drop. Extra sockets need to "
            "be unlocked at the Witch NPC.")
        socket_row.addWidget(self._eb_socket_valid)
        socket_apply_btn = QPushButton("Extend Sockets (Selected)")
        socket_apply_btn.setToolTip(
            "Extend socket capacity on the SELECTED item. Only applies to "
            "items with use_socket=1 (abyss gear).")
        socket_apply_btn.clicked.connect(self._eb_extend_sockets)
        socket_row.addWidget(socket_apply_btn)
        socket_row.addStretch(1)
        pl.addWidget(self._eb_socket_row_widget)
        
        # --- Drop enchant level ---
        drop_enchant = QWidget()
        drop_enchant_row = QHBoxLayout(drop_enchant)
        drop_enchant_row.setContentsMargins(0, 0, 0, 0)
        drop_enchant_row.setSpacing(6)
        drop_enchant_row.addWidget(QLabel("Enchant Level:"))
        self._eb_drop_enchant_level = QSpinBox()
        self._eb_drop_enchant_level.setRange(0, 10)
        self._eb_drop_enchant_level.setValue(0)
        self._eb_drop_enchant_level.setFixedWidth(60)
        self._eb_drop_enchant_level.setToolTip(
            "What refinement level the item will be on drop.")
        drop_enchant_row.addWidget(self._eb_drop_enchant_level)
        drop_enchant_apply = QPushButton("Change Drop Level (Selected)")
        drop_enchant_apply.setToolTip(
            "Change the default enchantment level for the SELECTED item "
            "when it drops or is purchased.")
        drop_enchant_apply.clicked.connect(self._eb_change_drop_enchant)
        drop_enchant_row.addWidget(drop_enchant_apply)
        drop_enchant_row.addStretch(1)
        pl.addWidget(drop_enchant)

        pl.addStretch(1)
        return page

    def _build_buff_effects_page(self) -> QWidget:
        """Passives & Effects sub-tab — passive combo, effect catalog, gimmick."""
        from PySide6.QtWidgets import QFormLayout, QCompleter
        page = QWidget()
        form = QFormLayout(page)
        form.setContentsMargins(8, 8, 8, 8)
        form.setSpacing(6)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)

        # Passive row
        passive_container = QWidget()
        passive_row = QHBoxLayout(passive_container)
        passive_row.setContentsMargins(0, 0, 0, 0)
        passive_row.setSpacing(4)
        self._eb_passive_combo = QComboBox()
        self._eb_passive_combo.setToolTip(
            "Change the passive skill on this item. Green text on tooltips.")
        self._eb_passive_combo.setMinimumWidth(200)
        self._eb_passive_combo.setEditable(True)
        self._eb_passive_combo.setInsertPolicy(QComboBox.NoInsert)
        self._eb_passive_combo.lineEdit().setPlaceholderText("Type to search passives...")
        self._eb_passive_combo.completer().setCompletionMode(QCompleter.PopupCompletion)
        self._eb_passive_combo.completer().setFilterMode(Qt.MatchContains)
        for sk in sorted(self._PASSIVE_SKILL_NAMES.keys()):
            name = self._PASSIVE_SKILL_NAMES[sk]
            desc = self._buff_skill_descs.get(str(sk), {}).get("description", "")
            label = f"{name} ({sk})" + (f" \u2014 {desc}" if desc else "")
            self._eb_passive_combo.addItem(label, sk)
        passive_row.addWidget(self._eb_passive_combo, 1)

        passive_row.addWidget(QLabel("Lv:"))
        self._eb_level_spin = QSpinBox()
        self._eb_level_spin.setRange(1, 100)
        self._eb_level_spin.setValue(1)
        self._eb_level_spin.setToolTip("Passive level (shown as 'Lv X' in-game)")
        self._eb_level_spin.setMinimumWidth(60)
        passive_row.addWidget(self._eb_level_spin)

        add_pass_btn = QPushButton("Add")
        add_pass_btn.setObjectName("accentBtn")
        add_pass_btn.setToolTip(
            "ADD a passive skill to this item (stacks with existing passives).")
        add_pass_btn.clicked.connect(self._eb_apply)
        passive_row.addWidget(add_pass_btn)

        remove_pass_btn = QPushButton("Remove")
        remove_pass_btn.setToolTip("Remove selected passive from this item")
        remove_pass_btn.clicked.connect(self._eb_remove_passive)
        passive_row.addWidget(remove_pass_btn)

        god_mode_btn = QPushButton("God Mode")
        god_mode_btn.setToolTip(
            "Inject full God Mode stats: Invincible + Great Thief, max DDD/DPV, "
            "max regen, max speed/crit/resist, 8 equipment buffs.")
        god_mode_btn.setStyleSheet(
            "background-color: #cc3333; color: white; font-weight: bold;")
        god_mode_btn.clicked.connect(self._eb_god_mode)
        passive_row.addWidget(god_mode_btn)

        self._eb_status = QLabel("")
        self._eb_status.setStyleSheet(f"color: {COLORS['accent']};")
        passive_row.addWidget(self._eb_status)
        form.addRow("Passive:", passive_container)

        # Effect row: search + catalog + apply
        effect_container = QWidget()
        effect_row = QHBoxLayout(effect_container)
        effect_row.setContentsMargins(0, 0, 0, 0)
        effect_row.setSpacing(4)
        self._effect_search = QLineEdit()
        self._effect_search.setPlaceholderText("shadow, lightning, boot...")
        self._effect_search.setToolTip(
            "Filter effects by name, gimmick, skill ID, or source item.")
        self._effect_search.setMaximumWidth(200)
        self._effect_search.textChanged.connect(self._effect_filter_changed)
        effect_row.addWidget(self._effect_search)

        self._effect_catalog_combo = QComboBox()
        self._effect_catalog_combo.setToolTip(
            "Pick a gimmick effect from an existing in-game item.\n"
            "Copies gimmick_info, docking, cooltime, passive skills.")
        self._effect_catalog_combo.setMinimumWidth(220)
        self._effect_catalog_combo.addItem("(load item data first)", None)
        effect_row.addWidget(self._effect_catalog_combo, 1)

        copy_effect_btn = QPushButton("Apply Effect")
        copy_effect_btn.setObjectName("accentBtn")
        copy_effect_btn.setToolTip(
            "Apply the selected gimmick effect to the current item.\n"
            "Passives STACK; gimmick/docking REPLACES (one per item).")
        copy_effect_btn.clicked.connect(self._eb_copy_effect)
        effect_row.addWidget(copy_effect_btn)
        form.addRow("Effect:", effect_container)

        # Gimmick row: searchable combo + Apply + user-preview label
        gimmick_container = QWidget()
        gl = QVBoxLayout(gimmick_container)
        gl.setContentsMargins(0, 0, 0, 0)
        gl.setSpacing(2)
        gimmick_row = QHBoxLayout()
        gimmick_row.setSpacing(4)
        self._eb_vfx_combo = QComboBox()
        self._eb_vfx_combo.setEditable(True)
        self._eb_vfx_combo.setInsertPolicy(QComboBox.NoInsert)
        self._eb_vfx_combo.lineEdit().setPlaceholderText(
            "Search gimmicks (lantern, lightning, flame, drone, thief...)")
        self._eb_vfx_combo.setMinimumWidth(220)
        self._eb_vfx_combo.setSizeAdjustPolicy(
            QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self._eb_vfx_combo.setToolTip(
            "Attach any equip-gimmick to the current item. Clones gimmick_info, "
            "docking_child_data, cooltime, charge config from a sample item.")
        self._eb_vfx_combo.completer().setCompletionMode(QCompleter.PopupCompletion)
        self._eb_vfx_combo.completer().setFilterMode(Qt.MatchContains)
        self._load_vfx_catalog_into_combo()
        gimmick_row.addWidget(self._eb_vfx_combo, 1)

        apply_gimmick_btn = QPushButton("Apply Gimmick")
        apply_gimmick_btn.setStyleSheet(
            "background-color: #006064; color: white; font-weight: bold;")
        apply_gimmick_btn.setToolTip(
            "Apply the selected gimmick to the current item.\n"
            "Replaces any existing gimmick slot — one gimmick per item.")
        apply_gimmick_btn.clicked.connect(self._eb_apply_vfx_gimmick)
        gimmick_row.addWidget(apply_gimmick_btn)
        gl.addLayout(gimmick_row)

        # Live preview of which vanilla items already use the selected gimmick.
        self._eb_vfx_users_label = QLabel("")
        self._eb_vfx_users_label.setStyleSheet(
            f"color: {COLORS['text_dim']}; font-size: 10px; padding: 1px 4px;")
        self._eb_vfx_users_label.setWordWrap(True)
        gl.addWidget(self._eb_vfx_users_label)
        self._eb_vfx_combo.currentIndexChanged.connect(
            lambda _i: self._refresh_gimmick_user_label())
        form.addRow("Gimmick:", gimmick_container)

        return page

    def _build_buff_stats_page(self) -> QWidget:
        """Stats & Buffs sub-tab — enchant level + stat + equip buff rows."""
        from PySide6.QtWidgets import QFormLayout, QCompleter
        page = QWidget()
        form = QFormLayout(page)
        form.setContentsMargins(8, 8, 8, 8)
        form.setSpacing(6)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)

        # Enchant level row
        level_container = QWidget()
        level_row = QHBoxLayout(level_container)
        level_row.setContentsMargins(0, 0, 0, 0)
        level_row.setSpacing(4)
        self._eb_level_target = QComboBox()
        self._eb_level_target.addItem("All Levels (0-10)", -1)
        for i in range(11):
            self._eb_level_target.addItem(f"Level +{i} only", i)
        self._eb_level_target.setToolTip(
            "Which enchant level(s) to apply stat/buff changes to. Items have "
            "11 enchant levels (0-10). 'All Levels' applies to every one.")
        self._eb_level_target.setFixedWidth(180)
        self._eb_level_target.currentIndexChanged.connect(
            lambda: self._buff_refresh_stats() if self._buff_current_item else None)
        level_row.addWidget(self._eb_level_target)
        hint = QLabel("Pick level \u2192 Add stats/buffs \u2192 Apply to Game")
        hint.setStyleSheet(f"color: {COLORS['text_dim']}; font-size: 10px;")
        level_row.addWidget(hint, 1)
        form.addRow("Enchant level:", level_container)

        # Stat row
        stat_container = QWidget()
        stat_row = QHBoxLayout(stat_container)
        stat_row.setContentsMargins(0, 0, 0, 0)
        stat_row.setSpacing(4)
        self._eb_stat_combo = QComboBox()
        self._eb_stat_combo.setEditable(True)
        self._eb_stat_combo.setInsertPolicy(QComboBox.NoInsert)
        self._eb_stat_combo.lineEdit().setPlaceholderText("Type to search stats...")
        self._eb_stat_combo.completer().setCompletionMode(QCompleter.PopupCompletion)
        self._eb_stat_combo.completer().setFilterMode(Qt.MatchContains)
        for idx, (sname, skey, slist, sdefault) in enumerate(self._ENCHANT_STAT_LIST):
            label_type = slist.replace('stat_list_', '').replace('_', ' ')
            self._eb_stat_combo.addItem(f"{sname} [{label_type}] ({skey})", idx)
        stat_row.addWidget(self._eb_stat_combo, 1)

        stat_row.addWidget(QLabel("Val:"))
        self._eb_stat_value = QSpinBox()
        self._eb_stat_value.setRange(0, 999999999)
        self._eb_stat_value.setValue(999999)
        self._eb_stat_value.setToolTip(
            "Flat stats (DDD/DPV): 999,999 = strong, 1,000,000 = dev ring\n"
            "Rate stats (Speed/Crit): 0-15 where 15 = max\n"
            "Regen: 100,000 = very fast, 1,000,000 = dev ring")
        self._eb_stat_value.setMinimumWidth(100)
        stat_row.addWidget(self._eb_stat_value)

        stat_add_btn = QPushButton("Add Stat")
        stat_add_btn.setObjectName("accentBtn")
        stat_add_btn.setToolTip(
            "Add this stat to ALL enchant levels (structural edit)")
        stat_add_btn.clicked.connect(self._eb_add_stat)
        stat_row.addWidget(stat_add_btn)

        stat_remove_btn = QPushButton("Remove")
        stat_remove_btn.setToolTip("Remove this stat from ALL enchant levels")
        stat_remove_btn.clicked.connect(self._eb_remove_stat)
        stat_row.addWidget(stat_remove_btn)
        form.addRow("Stat:", stat_container)

        # Equip buff row
        buff_container = QWidget()
        eb_row = QHBoxLayout(buff_container)
        eb_row.setContentsMargins(0, 0, 0, 0)
        eb_row.setSpacing(4)
        self._eb_buff_combo = QComboBox()
        self._eb_buff_combo.setToolTip(
            "Select an equipment buff to add to ALL enchant levels.\n"
            "Colored effects on items (Fire Res, Ice Res, etc).")
        self._eb_buff_combo.setMinimumWidth(200)
        self._eb_buff_combo.setEditable(True)
        self._eb_buff_combo.setInsertPolicy(QComboBox.NoInsert)
        self._eb_buff_combo.lineEdit().setPlaceholderText("Type to search buffs...")
        self._eb_buff_combo.completer().setCompletionMode(QCompleter.PopupCompletion)
        self._eb_buff_combo.completer().setFilterMode(Qt.MatchContains)
        for bk in sorted(self._EQUIP_BUFF_NAMES.keys()):
            bname = self._EQUIP_BUFF_NAMES[bk]
            desc = self._buff_skill_descs.get(str(bk), {}).get("description", "")
            label = f"{bname} ({bk})" + (f" \u2014 {desc}" if desc else "")
            self._eb_buff_combo.addItem(label, bk)
        self._eb_buff_combo.currentIndexChanged.connect(self._buff_on_buff_selected)
        eb_row.addWidget(self._eb_buff_combo, 1)

        eb_row.addWidget(QLabel("Lv:"))
        self._eb_buff_level = QSpinBox()
        self._eb_buff_level.setRange(0, 100)
        self._eb_buff_level.setValue(15)
        self._eb_buff_level.setToolTip("Buff level (0-100, 15 = max for most buffs)")
        self._eb_buff_level.setMinimumWidth(60)
        eb_row.addWidget(self._eb_buff_level)

        self._buff_range_label = QLabel("")
        self._buff_range_label.setStyleSheet(
            f"color: {COLORS['text_dim']}; font-size: 10px;")
        eb_row.addWidget(self._buff_range_label)

        eb_add_btn = QPushButton("Add Buff")
        eb_add_btn.setObjectName("accentBtn")
        eb_add_btn.setToolTip(
            "Add this equipment buff to ALL enchant levels of the selected item")
        eb_add_btn.clicked.connect(self._eb_add_buff)
        eb_row.addWidget(eb_add_btn)

        eb_remove_btn = QPushButton("Remove Buff")
        eb_remove_btn.setToolTip(
            "Remove this buff from ALL enchant levels of the selected item")
        eb_remove_btn.clicked.connect(self._eb_remove_buff)
        eb_row.addWidget(eb_remove_btn)
        form.addRow("Equip Buff:", buff_container)

        return page

    def _build_buff_drop_data_page(self) -> QWidget:
        page = QWidget()
        pl = QVBoxLayout(page)
        pl.setContentsMargins(8, 8, 8, 8)
        pl.setSpacing(8)
        return page


    def _build_buff_hero_presets_page(self) -> QWidget:
        """Hero Presets sub-tab — 3 large colored buttons."""
        
        # TEMP styles array
        styles = [
            ("#4682B4","White"),
            ("#FFFFFF","Black"),
            ("#00FF7F","Black"),
            ("#00BFFF","Black"),
            ("#9370DB","Black"),
            ("#DC143C","White"),
            ("#778899","Black"),
            ("#FFD700","Black"),
            ("#FF69B4","Black"),
            ("#FF8C00","Black"),
            ("#00FFFF","Black"),
            ("#7FFF00","Black"),
            ("#DDA0DD","Black"),
            ("#2E8B57","White"),
        ]   
        
        def gen_styles(font_color: str, bkg_color: str):
            return f"""
            QPushButton, QToolTip {{
                font-size: 13px;
                font-weight: bold;
            }}
            
            QPushButton {{
                color: {font_color};
                background-color: {bkg_color};
                padding: 16px 24px;
            }}
            
            QToolTip {{
                color: black;
                background-color: white;
                border: 1px solid black;
            }}
            """        
        
        page = QWidget()
        pl = QVBoxLayout(page)
        pl.setContentsMargins(8, 8, 8, 8)
        pl.setSpacing(8)

        grid = QGridLayout()
        grid.setSpacing(8)
        grid_columns = 3
        grid_buttons: list[QPushButton] = []
        grid_label = QLabel(
            "One-click presets. Click an item in the list, "
            "then choose a preset below to apply.")
        
        dev_grid = QGridLayout()
        dev_grid.setSpacing(8)
        dev_grid_buttons: list[QPushButton] = []
        dev_grid_label = QLabel(
            "DEV Ring presets. Click an item in the list, "
            "then choose a preset below to apply.")

        custom_grid = QGridLayout()
        custom_grid.setSpacing(8)
        custom_grid_buttons: list[QPushButton] = []
        custom_grid_label = QLabel("")
      
        sockets_btn = QPushButton("5 Sockets")
        sockets_btn.setToolTip("Item will drop with 5 open sockets by default.")
        sockets_btn.clicked.connect(
            lambda: self._eb_apply_preset("open_sockets"))
        grid_buttons.append(sockets_btn)
        
        enchant_btn = QPushButton("Max Refine")
        enchant_btn.setToolTip("Item will drop at lvl 10 by default.")
        enchant_btn.clicked.connect(
            lambda: self._eb_apply_preset("max_enchant"))
        grid_buttons.append(enchant_btn)
        
        cooldown_btn = QPushButton("No Cooldown")
        cooldown_btn.setToolTip("Item will have 1s cooldown by default.")
        cooldown_btn.clicked.connect(
            lambda: self._eb_apply_preset("no_cooldown"))
        grid_buttons.append(cooldown_btn)
        
        charges_btn = QPushButton("Max Charges")
        charges_btn.setToolTip("Item will have 100 charges by default.")
        charges_btn.clicked.connect(
            lambda: self._eb_apply_preset("max_charges"))
        grid_buttons.append(charges_btn)
        stacks_btn = QPushButton("Max Stacks")
        stacks_btn.setToolTip("Item will have a max stack size of 999999.")
        stacks_btn.clicked.connect(
            lambda: self._eb_apply_preset("max_stacks"))
        grid_buttons.append(stacks_btn)

        abyss_socket_btn = QPushButton("Abyss + 5 Sockets")
        abyss_socket_btn.setToolTip(
            "Unlock abyss restriction (equipable_hash = 0) AND\n"
            "extend to 5 sockets on the selected item.")
        abyss_socket_btn.clicked.connect(self._eb_abyss_plus_sockets)
        grid_buttons.append(abyss_socket_btn)

        godmode_desc = textwrap.dedent("""
            - No Cooldown   
            - Max Charges
            - Max Sockets
            - Max Enchant
            - Invincible
            - Great Thief (All Crimes)
            - Max Attack/Defense
            - Max Attack/Move Speed
            - Max Regen
            - Max Crit/Resist
            - 8 Equipment Buffs at level 10
        """).strip()
        def apply_godmode():
            if not hasattr(self, '_buff_rust_items') or not self._buff_rust_items:
                QMessageBox.warning(self, "God Mode", "Extract with Rust parser first.")
                return
            if not hasattr(self, '_buff_current_item') or self._buff_current_item is None:
                QMessageBox.warning(self, "God Mode", "Select an item first.")
                return

            rust_info = self._buff_rust_lookup.get(self._buff_current_item.item_key)
            if not rust_info:
                QMessageBox.warning(self, "God Mode", "Item not found in Rust data.")
                return

            edl = rust_info.get('enchant_data_list', [])
            if not edl:
                QMessageBox.warning(self, "God Mode",
                    "This item has no enchant data.\n"
                    "Only equippable items (weapons, armor, accessories) can have buffs.")
                return

            display_name = self._name_db.get_name(self._buff_current_item.item_key)

            reply = QMessageBox.warning(
                self, "Potter's God Mode",
                f"Apply God Mode to {display_name}?\n\n"
                f"This will inject into ALL enchant levels:\n"
                f"{godmode_desc}\n\n"
                f"Click 'Export as Mod' after to write.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return
            self._eb_god_mode(True)
            self._eb_apply_preset("great_thief_all", True)
            self._eb_apply_preset("open_sockets", True)
            self._eb_apply_preset("max_charges", True)
            self._eb_apply_preset("max_enchant", True)
            self._eb_apply_preset("no_cooldown", True)
        godmode_btn = QPushButton("God Mode")
        godmode_btn.setToolTip(f"Inject full God Mode stats:\n{godmode_desc}")
        godmode_btn.clicked.connect(apply_godmode)
        grid_buttons.append(godmode_btn)
        
        shadow_boots_btn = QPushButton("Shadow Boots")
        shadow_boots_btn.setToolTip(
            "Apply Potter's Shadow Boots config to selected item:\n"
            "Skills: Shadow Dash (7201) + Breeze Step (7055) + Swimming (7202)\n"
            "Gimmick: 1004431 (boots gimmick — activates the skills)")
        shadow_boots_btn.clicked.connect(
            lambda: self._eb_apply_preset("shadow_boots"))
        grid_buttons.append(shadow_boots_btn)

        lightning_btn = QPushButton("Lightning Weapon")
        lightning_btn.setToolTip(
            "Apply lightning weapon config (Potter's Hwando recipe):\n"
            "Skills: Lightning (91101) + Fire (91105) + Ice (91104) affinity\n"
            "Gimmick: 1001961 (weapon gimmick)")
        lightning_btn.clicked.connect(
            lambda: self._eb_apply_preset("lightning_weapon"))
        grid_buttons.append(lightning_btn)
        
        great_thief_btn = QPushButton("Great Thief")
        great_thief_btn.setToolTip(
            "Apply Great Thief activated skill (works on ANY item).\n"
            "Opens a picker: Block Theft only, or Block ALL crime.\n"
            "Gimmick: 1002041, 1 charge, 30-min cooldown.")
        great_thief_btn.clicked.connect(self._eb_great_thief_pick_variant)
        grid_buttons.append(great_thief_btn)
        
        dev_immunity_btn = QPushButton("Immunity")
        dev_immunity_btn.setToolTip("Adds DEV Immune Ring buff to item.")
        dev_immunity_btn.clicked.connect(
            lambda: self._eb_apply_dev_preset("immune"))
        dev_grid_buttons.append(dev_immunity_btn)
        
        dev_str_hp_btn = QPushButton("STR/HP")
        dev_str_hp_btn.setToolTip(
            "Inject DEV STR/HP Ring stats:\n"
            "- Max DDD (Damage)\n"
            "- Max HP Regen")
        dev_str_hp_btn.clicked.connect(
            lambda: self._eb_apply_dev_preset("str_hp"))
        dev_grid_buttons.append(dev_str_hp_btn)

        dev_def_hp_btn = QPushButton("DEF/HP")
        dev_def_hp_btn.setToolTip(
            "Inject DEV DEF/HP Ring stats:\n"
            "- Max DPV (Defense)\n"
            "- Max HP Regen")
        dev_def_hp_btn.clicked.connect(
            lambda: self._eb_apply_dev_preset("def_hp"))
        dev_grid_buttons.append(dev_def_hp_btn)

        dev_mp_stam_btn = QPushButton("MP/Stamina")
        dev_mp_stam_btn.setToolTip(
            "Inject DEV MP/Stamina Ring stats:\n"
            "- Max Spirit Regen\n"
            "- Max Stamina Regen\n"
            "- Max Stamina Cost Reduction")
        dev_mp_stam_btn.clicked.connect(
            lambda: self._eb_apply_dev_preset("mp_stam"))
        dev_grid_buttons.append(dev_mp_stam_btn)

        dev_speed = QPushButton("Speed")
        dev_speed.setToolTip(
            "Inject DEV Speed Ring stats:\n"
            "- Max Attack Speed\n"
            "- Max Move Speed\n"
            "- Max Crit Rate")
        dev_speed.clicked.connect(
            lambda: self._eb_apply_dev_preset("speed"))
        dev_grid_buttons.append(dev_speed)

        dev_mode_desc = textwrap.dedent("""
            Inject ALL DEV Ring stats:
            - Immunity
            - Max DDD (Damage)
            - Max DPV (Defense)
            - Max Attack Speed
            - Max Move Speed
            - Max Crit Rate
            - Max HP Regen
            - Max Spirit Regen
            - Max Stamina Regen
            - Max Stamina Cost Reduction
        """).strip()
        dev_all = QPushButton("All")
        dev_all.setToolTip(dev_mode_desc)
        dev_all.clicked.connect(
            lambda: self._eb_apply_dev_preset("all"))
        dev_grid_buttons.append(dev_all)

        # Apply Layout and Styles to all grid buttons
        i = 0
        for btn in grid_buttons:
            bc,fc = styles[i % len(styles)]
            r,c = divmod(i,grid_columns)
            btn.setStyleSheet(gen_styles(fc,bc))
            grid.addWidget(btn,r,c)
            i += 1

        i = 0
        for btn in dev_grid_buttons:
            bc,fc = styles[~(i % len(styles))]
            r,c = divmod(i,grid_columns)
            btn.setStyleSheet(gen_styles(fc,bc))
            dev_grid.addWidget(btn,r,c)
            i += 1
            
        i = 0
        for btn in custom_grid_buttons:
            bc,fc = styles[i % len(styles)]
            r,c = divmod(i,grid_columns)
            btn.setStyleSheet(gen_styles(fc,bc))
            custom_grid.addWidget(btn,r,c)
            i += 1
        
        pl.addWidget(grid_label)
        pl.addLayout(grid)

        # ── DEV Ring Presets (collapsible) ──────────────────────────────
        dev_content = QWidget()
        dev_vbox = QVBoxLayout(dev_content)
        dev_vbox.setContentsMargins(0, 0, 0, 0)
        dev_vbox.setSpacing(4)

        dev_grid = QGridLayout()
        dev_grid.setSpacing(6)
        dev_btns: list[QPushButton] = []

        dev_defs = [
            ("Immunity",   "immune",  "Invincible passive + Max HP regen + Max DDD"),
            ("STR / HP",   "str_hp",  "Max DDD (Damage) + Max HP Regen"),
            ("DEF / HP",   "def_hp",  "Max DPV (Defense) + Max HP Regen"),
            ("MP / Stam",  "mp_stam", "Max Spirit Regen + Max Stamina Regen + Stamina Cost Reduction"),
            ("Speed",      "speed",   "Max Attack Speed + Move Speed + Crit Rate"),
            ("All DEV",    "all",     "All DEV Ring stats combined"),
            ("Elemental",  "elemental_weapon", "Lightning + Ice + Fire weapon imbue"),
            ("Jump Boots", "jump_boots", "Shadow Dash + Breeze Step + Swimming"),
        ]

        for label, key, tip in dev_defs:
            b = QPushButton(label)
            b.setToolTip(tip)
            b.clicked.connect(lambda _=False, k=key: self._eb_apply_dev_preset(k))
            dev_btns.append(b)

        dev_styles = [
            ("#4682B4","White"), ("#2E8B57","White"), ("#00695C","White"),
            ("#6A1B9A","White"), ("#00BFFF","Black"), ("#DC143C","White"),
            ("#FF8C00","Black"), ("#778899","Black"),
        ]
        for idx, btn in enumerate(dev_btns):
            bc, fc = dev_styles[idx % len(dev_styles)]
            btn.setStyleSheet(gen_styles(fc, bc))
            r, c = divmod(idx, grid_columns)
            dev_grid.addWidget(btn, r, c)

        dev_vbox.addLayout(dev_grid)

        pl.addWidget(
            self._make_collapsible("DEV Ring Presets", dev_content,
                                   start_open=False, config_key="buffs_show_dev_presets"))

        pl.addStretch(1)
        return page


    def _build_buff_imbue_page(self) -> QWidget:
        """Imbue sub-tab — searchable passive combo + add/preview/coverage."""
        from PySide6.QtWidgets import QCompleter
        page = QWidget()
        pl = QVBoxLayout(page)
        pl.setContentsMargins(8, 8, 8, 8)
        pl.setSpacing(6)

        info = QLabel(
            "Imbue weapons/items with elemental passives (Fire, Ice, Lightning, "
            "Bismuth, Poison, Shadow, etc.) and open the weapon class to use them.")
        info.setWordWrap(True)
        info.setStyleSheet(f"color: {COLORS['text_dim']}; font-size: 10px;")
        pl.addWidget(info)

        imbue_row = QHBoxLayout()
        imbue_row.setSpacing(4)
        imbue_row.addWidget(QLabel("Imbue:"))
        self._eb_imbue_combo = QComboBox()
        try:
            _imbue = __import__("imbue")
            _catalog = _imbue.get_passive_skill_catalog()
            _CLASS_RANK = {'visual': 0, 'functional': 1, 'stat_only': 2}
            def _sort_key(entry):
                sid, info_d = entry
                vrank = _CLASS_RANK.get(info_d.get('visual_class', 'stat_only'), 2)
                group = info_d.get('group', 'other')
                GROUP_ORDER = {
                    'fire': 0, 'ice': 1, 'lightning': 2, 'bismuth': 3,
                    'poison': 4, 'bleed': 5, 'shadow': 6, 'wind': 7, 'water': 8,
                }
                return (vrank, GROUP_ORDER.get(group, 99),
                        info_d.get('pretty_name') or info_d.get('display', ''))
            _sorted = sorted(_catalog.items(), key=_sort_key)
            _CLASS_ICON = {'visual': '\U0001f386', 'functional': '\u2699', 'stat_only': '\u00b7'}
            for sid, info_d in _sorted:
                pretty = info_d.get('pretty_name') or info_d.get('display') or info_d.get('name', f'skill_{sid}')
                internal = info_d.get('name', '')
                desc = info_d.get('description', '') or ''
                group = info_d.get('group', 'other')
                vclass = info_d.get('visual_class', 'stat_only')
                tag = f"[{group}]" if group != 'other' else ''
                icon = _CLASS_ICON.get(vclass, '\u00b7')
                label = f"{icon} {pretty} ({sid}) \u2014 {internal}"
                if tag:
                    label += f" {tag}"
                if desc and desc.lower() != pretty.lower():
                    label += f" \u2014 {desc}"
                self._eb_imbue_combo.addItem(label, sid)
        except Exception:
            try:
                _imbue = __import__("imbue")
                for sid, (disp, _name) in sorted(_imbue.IMBUE_SKILLS.items()):
                    self._eb_imbue_combo.addItem(f"{disp} ({sid})", sid)
            except Exception:
                pass
        self._eb_imbue_combo.setMinimumWidth(300)
        self._eb_imbue_combo.setEditable(True)
        self._eb_imbue_combo.setInsertPolicy(QComboBox.NoInsert)
        self._eb_imbue_combo.lineEdit().setPlaceholderText(
            "Type to search by name, key, or internal name...")
        _comp = self._eb_imbue_combo.completer()
        _comp.setCompletionMode(QCompleter.PopupCompletion)
        _comp.setFilterMode(Qt.MatchContains)
        _comp.setCaseSensitivity(Qt.CaseInsensitive)
        self._eb_imbue_combo.setToolTip(
            "Pick any Equip_Passive_* skill to imbue. Search matches pretty "
            "name, skill key, or internal name.\n\n"
            "Tier markers:\n"
            "  \U0001f386 Visual — real VFX (fire/ice/lightning aura, glow, footstep)\n"
            "  \u2699 Functional — gimmick exists but invisible (stealth, immunity)\n"
            "  \u00b7 Stat-only — no vanilla gimmick. Stat buff only."
        )
        imbue_row.addWidget(self._eb_imbue_combo, 1)

        imbue_btn = QPushButton("Add to Selected")
        imbue_btn.setStyleSheet(
            "background-color: #7B1FA2; color: white; font-weight: bold;")
        imbue_btn.setToolTip(
            "One-click imbue. Adds the passive to the selected item and "
            "opens the weapon class if needed.")
        imbue_btn.clicked.connect(self._eb_add_imbue_to_selected)
        imbue_row.addWidget(imbue_btn)

        preview_btn = QPushButton("\U0001f441 Preview Item")
        preview_btn.setStyleSheet(
            "background-color: #1565C0; color: white; font-weight: bold;")
        preview_btn.setToolTip(
            "Show a preview of how the selected item will look in-game.")
        preview_btn.clicked.connect(self._buff_preview_item)
        imbue_row.addWidget(preview_btn)

        imbue_coverage_btn = QPushButton("Coverage Report")
        imbue_coverage_btn.setToolTip(
            "Show skill.pabgb-aware coverage report for the selected imbue "
            "skill: weapon count currently allowed, what 'Imbue All' opens.")
        imbue_coverage_btn.clicked.connect(self._imbue_show_coverage)
        imbue_row.addWidget(imbue_coverage_btn)
        pl.addLayout(imbue_row)

        # Live coverage label (refreshed when imbue combo changes).
        self._eb_imbue_coverage_label = QLabel("")
        self._eb_imbue_coverage_label.setStyleSheet(
            f"color: {COLORS['text_dim']}; font-size: 10px; padding: 1px 4px;")
        self._eb_imbue_coverage_label.setWordWrap(True)
        pl.addWidget(self._eb_imbue_coverage_label)
        self._eb_imbue_combo.currentIndexChanged.connect(
            lambda _i: self._refresh_imbue_coverage_label())

        # ── Imbue All Weapons (bulk) — lives here so it's next to the imbue
        # combo that picks its target passive. Same handler as before.
        bulk_imbue_btn = QPushButton("Imbue All Weapons")
        bulk_imbue_btn.setStyleSheet(
            "background-color: #4A148C; color: white; font-weight: bold; "
            "padding: 10px;")
        bulk_imbue_btn.setToolTip(
            "Apply the passive picked above (Lightning, Fire, Ice, etc.) to "
            "every weapon. Adds passive + gimmick + docking + staged "
            "skill.pabgb edit that whitelists every weapon class.")
        bulk_imbue_btn.clicked.connect(self._eb_bulk_imbue_all_weapons)
        pl.addWidget(bulk_imbue_btn)

        pl.addStretch(1)
        return page

    def _build_buff_global_mods_page(self) -> QWidget:
        """Global Mods sub-tab — apply-to-all toggles, scroll-safe at small heights.

        Wrapped in QScrollArea so the content stays reachable even when the
        tab body is shorter than the natural content height (1080p, stats
        table taking half the vertical space, etc). The old layout used 5
        separate QGroupBoxes that clipped the bottom button off-screen.
        Everything now lives in one compact group with stacked rows.
        """
        # Outer QScrollArea wraps the content — the page returned to QTabWidget.
        page = QScrollArea()
        page.setWidgetResizable(True)
        page.setFrameShape(QFrame.NoFrame)
        page.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        page.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)

        inner = QWidget()
        pl = QVBoxLayout(inner)
        pl.setContentsMargins(8, 8, 8, 8)
        pl.setSpacing(8)

        # ULTRA one-click: everything bulk-applicable in one shot.
        enable_all_btn = QPushButton(
            "Enable EVERYTHING (QoL + Dye + Sockets + Abyss + Universal Prof)")
        enable_all_btn.setStyleSheet(
            "background-color: #B71C1C; color: white; font-weight: bold; "
            "padding: 12px; font-size: 13px;")
        enable_all_btn.setToolTip(
            "Runs ALL bulk apply-to-many mods in one shot:\n"
            "  \u2022 QoL bundle (stacks 999999, charges 99, durability 65535, no cooldown)\n"
            "  \u2022 Make All Equipment Dyeable\n"
            "  \u2022 All items \u2192 5 sockets\n"
            "  \u2022 Unlock All Abyss Gear (equipable_hash \u2192 0)\n"
            "  \u2022 Universal Proficiency v2 (tribe_gender + equipslotinfo)\n\n"
            "Skipped (needs a target): Imbue passive/gimmick, per-item Add Buff/Stat.\n"
            "Everything lands in a single overlay slot on Apply to Game.")
        enable_all_btn.clicked.connect(self._eb_enable_everything_oneclick)
        pl.addWidget(enable_all_btn)

        # Classic QoL-only button (narrower scope, no UP/Dye/Sockets) kept for
        # users who just want the 4 QoL flags without the full bundle.
        all_qol_btn = QPushButton("Enable All QoL only (no UP / Dye / Sockets)")
        all_qol_btn.setStyleSheet(
            "background-color: #00796B; color: white; font-weight: bold; "
            "padding: 10px; font-size: 12px;")
        all_qol_btn.setToolTip(
            "Narrower one-click bundle (QoL only):\n"
            "  \u2022 No Cooldown on every item\n"
            "  \u2022 Max Charges (99) on every charged item\n"
            "  \u2022 Max Stacks ticked at 999999\n"
            "  \u2022 Infinity Durability ticked (65535)\n\n"
            "For the full QoL + Dye + Sockets + UP bundle use the red button above.")
        all_qol_btn.clicked.connect(self._eb_enable_all_qol)
        pl.addWidget(all_qol_btn)

        # Single consolidated group — 4 rows instead of 4 separate group boxes.
        toggles_grp = QGroupBox("Apply to All Items (individual mods)")
        tl = QVBoxLayout(toggles_grp)
        tl.setSpacing(6)
        tl.setContentsMargins(10, 14, 10, 10)

        # Row 1: No Cooldown
        row1 = QHBoxLayout()
        row1.setSpacing(6)
        no_cd_btn = QPushButton("No Cooldown")
        no_cd_btn.setToolTip(
            "Queue cooldown \u2192 1s for every item that has one. Included "
            "in the next Apply / Export. Same as Pldada's No Cooldown mod.")
        no_cd_btn.clicked.connect(self._cd_patch_all_items)
        row1.addWidget(no_cd_btn)
        row1.addStretch(1)
        tl.addLayout(row1)

        # Row 2: Max Charges (spin + apply button)
        row2 = QHBoxLayout()
        row2.setSpacing(6)
        row2.addWidget(QLabel("Charges:"))
        self._max_charges_spin = QSpinBox()
        self._max_charges_spin.setRange(1, 99)
        self._max_charges_spin.setValue(30)
        self._max_charges_spin.setFixedWidth(70)
        self._max_charges_spin.setToolTip(
            "Target max charges. Vanilla highest is 30. Values above may be "
            "clamped by the game.")
        row2.addWidget(self._max_charges_spin)
        max_charges_btn = QPushButton("Apply Max Charges")
        max_charges_btn.setToolTip(
            "Set max_charged_useable_count to the chosen value on every "
            "item that uses charges. Takes effect on FRESH copies only.")
        max_charges_btn.clicked.connect(self._max_charges_all_items)
        row2.addWidget(max_charges_btn)
        row2.addStretch(1)
        tl.addLayout(row2)

        # Row 3: Max Stacks (checkbox + size spin)
        row3 = QHBoxLayout()
        row3.setSpacing(6)
        self._stack_check = QCheckBox("Max Stacks")
        self._stack_check.setStyleSheet(
            f"color: {COLORS['accent']}; font-weight: bold;")
        self._stack_check.setToolTip(
            "When checked, every export sets the custom stack size on all "
            "stackable items. Replaces FatStacks mod.")
        row3.addWidget(self._stack_check)
        row3.addWidget(QLabel("Size:"))
        self._stack_spin = QSpinBox()
        self._stack_spin.setRange(1, 2147483647)
        self._stack_spin.setValue(9999)
        self._stack_spin.setFixedWidth(100)
        self._stack_spin.setToolTip("Custom max stack size for all stackable items")
        row3.addWidget(self._stack_spin)
        row3.addStretch(1)
        tl.addLayout(row3)

        # Row 4: Infinity Durability (single checkbox with inline description)
        row4 = QHBoxLayout()
        row4.setSpacing(6)
        self._inf_dura_check = QCheckBox("Infinity Durability (max_endurance = 65535)")
        self._inf_dura_check.setStyleSheet(
            f"color: {COLORS['accent']}; font-weight: bold;")
        self._inf_dura_check.setToolTip(
            "When checked, every export sets max_endurance = 65535 and "
            "is_destroy_when_broken = 0 on all items that have durability. "
            "Replaces Pldada Infinity Durability byte-patch mod.")
        row4.addWidget(self._inf_dura_check)
        row4.addStretch(1)
        tl.addLayout(row4)

        # Row 5: Unlock All Abyss Gear
        row5 = QHBoxLayout()
        row5.setSpacing(6)
        abyss_btn = QPushButton("Unlock All Abyss Gear")
        abyss_btn.setToolTip(
            "Sets equipable_hash = 0 on every Abyss Gear item so they\n"
            "can be socketed into ANY equipment, not just matching types.\n\n"
            "Original concept: OhmesmileTH (Nexus Mods) discovered that\n"
            "zeroing _equipableHash removes abyss socket restrictions.\n"
            "Re-implemented here using field names instead of byte offsets\n"
            "so it stacks with all other mods and survives game updates.")
        abyss_btn.clicked.connect(self._eb_unlock_all_abyss_gear)
        row5.addWidget(abyss_btn)

        abyss_sockets_bulk_btn = QPushButton("All Inventory → Abyss + 5 Sockets")
        abyss_sockets_bulk_btn.setToolTip(
            "For every item in your loaded inventory:\n"
            "  1. Unlock abyss restriction (equipable_hash = 0)\n"
            "  2. Extend to 5 sockets\n\n"
            "Combines 'Unlock All Abyss Gear' + 'All → 5 Sockets'\n"
            "but only on items you actually own.")
        abyss_sockets_bulk_btn.clicked.connect(self._eb_bulk_abyss_plus_sockets)
        row5.addWidget(abyss_sockets_bulk_btn)

        row5.addStretch(1)
        tl.addLayout(row5)

        pl.addWidget(toggles_grp)
        pl.addStretch(1)

        page.setWidget(inner)
        return page

    def _build_buff_bulk_page(self) -> QWidget:
        """Bulk Actions sub-tab — apply-to-many operations, scroll-safe.

        Wrapped in QScrollArea so buttons stay reachable when the tab body
        is shorter than the content (1080p etc). Imbue All Weapons now
        lives on the Imbue tab where it belongs next to its imbue combo.
        Socket bulk/per-item controls moved here from Advanced since
        'All \u2192 5 Sockets' is literally a bulk action.
        """
        page = QScrollArea()
        page.setWidgetResizable(True)
        page.setFrameShape(QFrame.NoFrame)
        page.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        page.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)

        inner = QWidget()
        pl = QVBoxLayout(inner)
        pl.setContentsMargins(8, 8, 8, 8)
        pl.setSpacing(8)

        info = QLabel(
            "Bulk operations \u2014 click a source item first (where it matters), "
            "then a button below.")
        info.setWordWrap(True)
        info.setStyleSheet(f"color: {COLORS['text_dim']}; font-size: 11px;")
        pl.addWidget(info)

        # Weapons group
        weapons_grp = QGroupBox("Weapons")
        wgl = QVBoxLayout(weapons_grp)
        wgl.setSpacing(6)
        wgl.setContentsMargins(10, 14, 10, 10)

        bulk_buffs_btn = QPushButton("Copy Selected Item's Buffs \u2192 All Weapons")
        bulk_buffs_btn.setStyleSheet(
            "background-color: #b71c1c; color: white; font-weight: bold; "
            "padding: 10px;")
        bulk_buffs_btn.setToolTip(
            "Broadcast the equip_buffs from the CURRENTLY SELECTED item onto "
            "every weapon. Existing weapon buffs preserved; duplicates merge "
            "with higher-level wins. Then 'Apply to Game' to write.")
        bulk_buffs_btn.clicked.connect(self._eb_bulk_apply_buffs_to_weapons)
        wgl.addWidget(bulk_buffs_btn)
        pl.addWidget(weapons_grp)

        # Equipment / Character group
        equip_grp = QGroupBox("Equipment / Character")
        egl = QVBoxLayout(equip_grp)
        egl.setSpacing(6)
        egl.setContentsMargins(10, 14, 10, 10)

        bulk_dye_btn = QPushButton("Make All Equipment Dyeable")
        bulk_dye_btn.setStyleSheet(
            "background-color: #1565C0; color: white; font-weight: bold; "
            "padding: 10px;")
        bulk_dye_btn.setToolTip(
            "Flip is_dyeable + is_editable_grime to 1 on every equipment item.\n"
            "Vanilla: only 530 of 3,038 items are dyeable. After: every armor + "
            "weapon shows up in the Dye tab.")
        bulk_dye_btn.clicked.connect(self._eb_bulk_make_dyeable)
        egl.addWidget(bulk_dye_btn)

        bulk_equip_v2_btn = QPushButton("Universal Proficiency (all chars)")
        bulk_equip_v2_btn.setStyleSheet(
            "background-color: #B71C1C; color: white; font-weight: bold; "
            "padding: 10px;")
        bulk_equip_v2_btn.setToolTip(
            "Make ALL items equippable by Kliff, Damiane, and Oongka.\n"
            "Adds player tribe hashes to restricted items; expands equip slots.\n"
            "Only the 3 player characters are modified (NPCs untouched).")
        bulk_equip_v2_btn.clicked.connect(self._eb_universal_proficiency_v2)
        egl.addWidget(bulk_equip_v2_btn)

        # Dev-only v1 Universal Proficiency.
        bulk_equip_btn = QPushButton("Universal Proficiency v1 [DEV]")
        bulk_equip_btn.setStyleSheet(
            "background-color: #E65100; color: white; font-weight: bold; "
            "padding: 10px;")
        bulk_equip_btn.setToolTip(
            "[DEV] Legacy universal proficiency \u2014 blanket expansion. Kept "
            "for research. Use the non-DEV button above for production.")
        bulk_equip_btn.clicked.connect(self._eb_universal_proficiency)
        bulk_equip_btn.setVisible(self._experimental_mode)
        egl.addWidget(bulk_equip_btn)
        self._dev_buff_widgets.append(bulk_equip_btn)

        pl.addWidget(equip_grp)

        # Sockets group — per-item + bulk, moved from Advanced tab.
        sockets_grp = QGroupBox("Sockets")
        sgl = QVBoxLayout(sockets_grp)
        sgl.setSpacing(6)
        sgl.setContentsMargins(10, 14, 10, 10)

        # Per-item socket extender row
        # self._eb_socket_row_widget = QWidget()
        # socket_row = QHBoxLayout(self._eb_socket_row_widget)
        # socket_row.setContentsMargins(0, 0, 0, 0)
        # socket_row.setSpacing(6)
        # socket_row.addWidget(QLabel("Count:"))
        # self._eb_socket_count = QSpinBox()
        # self._eb_socket_count.setRange(1, 8)
        # self._eb_socket_count.setValue(5)
        # self._eb_socket_count.setFixedWidth(60)
        # self._eb_socket_count.setToolTip(
        #     "Target max socket count. Writes to drop_default_data."
        #     "add_socket_material_item_list.")
        # socket_row.addWidget(self._eb_socket_count)
        # socket_row.addWidget(QLabel("Pre-unlocked:"))
        # self._eb_socket_valid = QSpinBox()
        # self._eb_socket_valid.setRange(0, 8)
        # self._eb_socket_valid.setValue(0)
        # self._eb_socket_valid.setFixedWidth(60)
        # self._eb_socket_valid.setToolTip(
        #     "How many sockets are unlocked on drop. Extra sockets need to "
        #     "be unlocked at the Witch NPC.")
        # socket_row.addWidget(self._eb_socket_valid)
        # socket_apply_btn = QPushButton("Extend Sockets (Selected)")
        # socket_apply_btn.setToolTip(
        #     "Extend socket capacity on the SELECTED item. Only applies to "
        #     "items with use_socket=1 (abyss gear).")
        # socket_apply_btn.clicked.connect(self._eb_extend_sockets)
        # socket_row.addWidget(socket_apply_btn)
        # socket_row.addStretch(1)
        # sgl.addWidget(self._eb_socket_row_widget)

        # Bulk 'All → 5' socket button on its own row so it isn't squeezed
        # alongside the per-item row above.
        socket_bulk_btn = QPushButton("All \u2192 5 Sockets")
        socket_bulk_btn.setStyleSheet(
            "background-color: #1565C0; color: white; font-weight: bold; "
            "padding: 10px;")
        socket_bulk_btn.setToolTip(
            "Bulk-extend every item that's already socket-capable to 5 "
            "sockets.\nSkips items without drop_default_data or already at 5+.")
        socket_bulk_btn.clicked.connect(self._eb_extend_all_sockets_to_5)
        sgl.addWidget(socket_bulk_btn)

        pl.addWidget(sockets_grp)

        pl.addStretch(1)

        page.setWidget(inner)
        return page

    def _build_buff_advanced_page(self) -> QWidget:
        """Advanced sub-tab (dev-gated) — JSON Edit + mod load-order spinners."""
        from PySide6.QtWidgets import QFormLayout
        page = QWidget()
        form = QFormLayout(page)
        form.setContentsMargins(8, 8, 8, 8)
        form.setSpacing(6)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)

        info = QLabel("Advanced tools. Dev mode only. Use with care.")
        info.setWordWrap(True)
        info.setStyleSheet(f"color: {COLORS['warning']}; font-size: 10px;")
        form.addRow(info)

        # Item Tools
        item_tools_container = QWidget()
        item_tools_row = QHBoxLayout(item_tools_container)
        item_tools_row.setContentsMargins(0, 0, 0, 0)
        item_tools_row.setSpacing(4)
        
        diff_btn = QPushButton("Item Diff")
        diff_btn.setToolTip(
            "Compare two items field by field — see exactly what's different\n"
            "between e.g. a working modded item and a broken one.")
        diff_btn.clicked.connect(self._buff_open_item_diff_dialog)
        item_tools_row.addWidget(diff_btn)

        inspect_btn = QPushButton("Inspect Item")
        inspect_btn.setToolTip(
            "Deep-dive on the currently selected item — every field, type,\n"
            "and value rendered in a searchable tree. Shows crafting deps\n"
            "and any references back to this item from elsewhere in iteminfo.")
        inspect_btn.clicked.connect(self._buff_open_item_inspector)
        item_tools_row.addWidget(inspect_btn)

        # JSON Edit
        json_btn = QPushButton("Edit JSON")
        json_btn.setToolTip("Open raw enchant data as editable JSON — full control")
        json_btn.clicked.connect(self._eb_json_edit)
        item_tools_row.addWidget(json_btn)
        raw_import_btn = QPushButton("Import ITEMINFO")
        raw_import_btn.setToolTip("Import a previously dumped item JSON back into the editor")
        raw_import_btn.clicked.connect(self._import_item_info)
        item_tools_row.addWidget(raw_import_btn)
        form.addRow("Item Tools:", item_tools_container)

        # Socket controls moved to Bulk Actions tab (where 'All → 5 Sockets'
        # naturally belongs). Advanced only keeps JSON Edit + load-order spins.

        # Mod folder load order (exposed in Advanced tab instead of bottom bar).
        overlay_container = QWidget()
        overlay_row = QHBoxLayout(overlay_container)
        overlay_row.setContentsMargins(0, 0, 0, 0)
        overlay_row.setSpacing(4)
        adv_overlay_spin = QSpinBox()
        adv_overlay_spin.setRange(1, 9999)
        adv_overlay_spin.setValue(self._config.get("buff_overlay_dir", 58))
        adv_overlay_spin.setFixedWidth(80)
        adv_overlay_spin.setToolTip(
            "PAZ folder slot for 'Export JSON Patch'. Default 0058.")
        def _sync_overlay(v):
            self._buff_overlay_spin.setValue(v)
        adv_overlay_spin.valueChanged.connect(_sync_overlay)
        overlay_row.addWidget(adv_overlay_spin)
        overlay_row.addWidget(QLabel(" (0058 default)"))
        overlay_row.addStretch(1)
        form.addRow("JSON Load Order:", overlay_container)

        modgroup_container = QWidget()
        modgroup_row = QHBoxLayout(modgroup_container)
        modgroup_row.setContentsMargins(0, 0, 0, 0)
        modgroup_row.setSpacing(4)
        adv_modgroup_spin = QSpinBox()
        adv_modgroup_spin.setRange(1, 9999)
        adv_modgroup_spin.setValue(self._config.get("buff_mod_group", 36))
        adv_modgroup_spin.setFixedWidth(80)
        adv_modgroup_spin.setToolTip(
            "PAZ folder slot for 'Export as Mod'. Default 0036.")
        def _sync_modgroup(v):
            self._buff_modgroup_spin.setValue(v)
        adv_modgroup_spin.valueChanged.connect(_sync_modgroup)
        modgroup_row.addWidget(adv_modgroup_spin)
        modgroup_row.addWidget(QLabel(" (0036 default)"))
        modgroup_row.addStretch(1)
        form.addRow("Mod Load Order:", modgroup_container)

        return page


    def _effect_swap_blackberry_test(self) -> None:
        game_path = self._paz_game_path.text().strip()
        if not game_path:
            QMessageBox.warning(self, "No Game Path", "Set the game install path first.")
            return
        if not _can_write_game_dir(game_path):
            QMessageBox.warning(self, "No Write Access",
                                f"Cannot write to:\n{game_path}\n\n"
                                "Right-click → Run as administrator")
            return

        reply = QMessageBox.question(
            self, "Item Effect Swap",
            "Swap Blackberry's food effect with Narima's Horn instant Dragon CD reset?\n\n"
            "This patches iteminfo.pabgb. A backup will be created.\n"
            "Use Steam Verify Integrity to undo.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        self._effect_status.setText("Patching...")
        QApplication.processEvents()

        try:
            patcher = ItemEffectPatcher(game_path)
            ok, msg = patcher.swap_effect('Blackberry', 0xB0A8256B)
            self._effect_status.setText("OK" if ok else "FAILED")
            if ok:
                QMessageBox.information(self, "Effect Swapped", msg)
            else:
                QMessageBox.critical(self, "Failed", msg)
        except Exception as e:
            self._effect_status.setText(f"Error: {e}")
            QMessageBox.critical(self, "Error", str(e))


    def _effect_check_blackberry(self) -> None:
        game_path = self._paz_game_path.text().strip()
        if not game_path:
            QMessageBox.warning(self, "No Game Path", "Set the game install path first.")
            return
        try:
            patcher = ItemEffectPatcher(game_path)
            result = patcher.check_effect('Blackberry')
            if result:
                h, desc = result
                self._effect_status.setText(f"Blackberry effect: {desc}")
            else:
                self._effect_status.setText("Could not read Blackberry effect")
        except Exception as e:
            self._effect_status.setText(f"Error: {e}")


    def _rebuild_index(self) -> None:
        """Rebuild the IteminfoIndex from current `_buff_rust_items`.

        Called by every code path that replaces `_buff_rust_items` (extract,
        revert, reload). Safe to call when the list is empty — produces an
        empty index. Also refreshes any UI elements that depend on the index
        (category filter dropdown, etc.).
        """
        items = getattr(self, "_buff_rust_items", None) or []
        try:
            self._index = IteminfoIndex(items) if items else None
        except Exception as e:
            log.warning("IteminfoIndex build failed: %s", e)
            self._index = None
        self._refresh_category_filter_choices()
        self._refresh_gimmick_user_label()
        self._refresh_imbue_coverage_label()

    def _refresh_category_filter_choices(self) -> None:
        """Populate the category filter dropdown after the index is built."""
        combo = getattr(self, "_buff_category_filter", None)
        if combo is None:
            return
        try:
            current = combo.currentData()
        except Exception:
            current = None
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("All categories", None)
        if self._index is not None:
            for cat, label, count in self._index.category_choices():
                combo.addItem(f"{label} ({count})", cat)
        # restore prior selection if still valid
        if current is not None:
            idx = combo.findData(current)
            if idx >= 0:
                combo.setCurrentIndex(idx)
        combo.blockSignals(False)

    def _refresh_gimmick_user_label(self) -> None:
        """Update the 'items using this gimmick' preview under the gimmick combo."""
        lbl = getattr(self, "_eb_vfx_users_label", None)
        combo = getattr(self, "_eb_vfx_combo", None)
        if lbl is None or combo is None:
            return
        gk = combo.currentData()
        if not gk or self._index is None:
            lbl.setText("")
            return
        users = self._index.gimmick_users(int(gk))
        if not users:
            lbl.setText(f"No vanilla items use gimmick {gk} — applying may not work.")
            lbl.setStyleSheet(f"color: {COLORS['error']}; font-size: 10px; padding: 1px 4px;")
            return
        sample = ", ".join(
            (u.get("string_key") or f"item_{u.get('key')}") for u in users[:3]
        )
        more = f" + {len(users) - 3} more" if len(users) > 3 else ""
        lbl.setText(f"Used by {len(users)} item(s): {sample}{more}")
        lbl.setStyleSheet(f"color: {COLORS['text_dim']}; font-size: 10px; padding: 1px 4px;")

    def _refresh_imbue_coverage_label(self) -> None:
        """Update the imbue coverage preview under the imbue combo.

        Cheap version: counts weapons that have the passive vs total. The full
        skill.pabgb-aware calculation runs on demand from `_imbue_show_coverage`.
        """
        lbl = getattr(self, "_eb_imbue_coverage_label", None)
        combo = getattr(self, "_eb_imbue_combo", None)
        if lbl is None or combo is None:
            return
        sid = combo.currentData()
        if sid is None or self._index is None:
            lbl.setText("")
            return
        sid = int(sid)
        weapons = [it for it in self._buff_rust_items
                   if self._is_weapon_item(it)] if self._buff_rust_items else []
        with_passive = sum(
            1 for w in weapons
            if any(p.get("skill") == sid for p in (w.get("equip_passive_skill_list") or []))
        )
        total = len(weapons)
        lbl.setText(
            f"Currently on {with_passive}/{total} weapons. "
            f"Click 'Imbue Coverage' for skill.pabgb filter analysis."
        )
        lbl.setStyleSheet(f"color: {COLORS['text_dim']}; font-size: 10px; padding: 1px 4px;")

    def _buff_ensure_patcher(self) -> bool:
        game_path = (self._game_path or self._config.get("game_install_path", "") or "").strip()
        if not game_path:
            QMessageBox.warning(
                self, "No Game Path",
                "Set the game install path at the top of the main window first.",
            )
            return False
        if self._buff_patcher is None or self._buff_patcher.game_path != game_path:
            self._buff_patcher = ItemBuffPatcher(game_path)
        return True


    def _buff_extract_iteminfo_preferring_overlay(self) -> tuple[bytes, str]:
        """Return (iteminfo_bytes, source_label).

        Checks if our tool has previously written an overlay to
        {game_path}/{buff_overlay_dir}/ (detected via .se_itembuffs sentinel
        file). If so, extracts iteminfo from THAT overlay so previously-applied
        changes (Universal Proficiency v2, prior buff edits, etc.) persist
        across tool sessions and don't get wiped on next Apply to Game.

        Falls back to vanilla 0008/ iteminfo if no overlay exists, or if
        overlay extraction fails.

        The source_label is 'vanilla' or e.g. '0058/' so the UI can tell the
        user where the baseline came from.
        """
        # Always call vanilla extract first — it sets up patcher internal
        # state (paz_path / offset / original_size / _original_data) that
        # other code paths (apply-to-game fallback, byte diffing) rely on.
        vanilla_raw = self._buff_patcher.extract_iteminfo()

        game_path = self._buff_patcher.game_path
        buff_dir = f"{self._buff_overlay_spin.value():04d}"
        overlay_paz = os.path.join(game_path, buff_dir, "0.paz")
        sentinel = os.path.join(game_path, buff_dir, ".se_itembuffs")

        # Only read from overlay if OUR tool wrote it (sentinel file present).
        # Skip overlay if user requested vanilla extraction.
        if getattr(self, '_buff_force_vanilla', False):
            return vanilla_raw, 'vanilla'
        if not (os.path.isfile(overlay_paz) and os.path.isfile(sentinel)):
            return vanilla_raw, 'vanilla'

        try:
            import crimson_rs
            iteminfo = bytes(crimson_rs.extract_file(
                game_path, buff_dir, "gamedata/binary__/client/bin",
                "iteminfo.pabgb"))
            
            # Verify overlay is from the SAME game version — if the size
            # differs significantly from vanilla, the overlay is stale
            # (from a prior game update) and will fail to parse. Fall back
            # to vanilla so the user doesn't get 0 parsed items.
            size_diff = abs(len(iteminfo) - len(vanilla_raw))
            if size_diff > 100000:
                log.warning(
                    "Overlay %s/ iteminfo (%d bytes) differs from vanilla "
                    "(%d bytes) by %d bytes — stale overlay from old game "
                    "version. Falling back to vanilla.",
                    buff_dir, len(iteminfo), len(vanilla_raw), size_diff)
                return vanilla_raw, 'vanilla (stale overlay ignored)'

            # Try parsing to verify it's valid
            try:
                test_items = crimson_rs.parse_iteminfo_from_bytes(iteminfo)
                if not test_items:
                    log.warning("Overlay %s/ iteminfo parsed 0 items, using vanilla", buff_dir)
                    return vanilla_raw, 'vanilla (overlay parse failed)'
            except Exception as parse_err:
                log.warning("Overlay %s/ iteminfo parse failed: %s — using vanilla",
                            buff_dir, parse_err)
                return vanilla_raw, 'vanilla (overlay parse failed)'

            # Overwrite patcher original so byte-level diff logic references
            # the overlay state instead of vanilla.
            self._buff_patcher._original_data = iteminfo

            # CRITICAL: also rehydrate staged equipslotinfo + skill files from
            # the existing overlay, otherwise a subsequent Apply to Game would
            # bundle only iteminfo and DROP these companion files. The user
            # would see UP v2 slot-expansion disappear, imbue class whitelists
            # disappear, etc. — the overlay would drift to partial state.
            internal_dir = "gamedata/binary__/client/bin"
            # equipslotinfo — staged by UP v2 / UP v1
            if (not hasattr(self, '_staged_equip_files')
                    or self._staged_equip_files is None):
                self._staged_equip_files = {}
            for fname in ("equipslotinfo.pabgb", "equipslotinfo.pabgh"):
                if fname in self._staged_equip_files:
                    continue  # current-session edit already present
                try:
                    data = bytes(crimson_rs.extract_file(
                        game_path, buff_dir, internal_dir, fname))
                    if data:
                        self._staged_equip_files[fname] = data
                        log.info("Rehydrated %s from overlay %s/ (%d bytes)",
                                 fname, buff_dir, len(data))
                except Exception:
                    # File not in overlay — that's fine, it just means UP v2
                    # wasn't applied previously.
                    pass
            # skill.pabgb/pabgh — staged by imbue
            if (not hasattr(self, '_staged_skill_files')
                    or self._staged_skill_files is None):
                self._staged_skill_files = {}
            for fname in ("skill.pabgb", "skill.pabgh"):
                if fname in self._staged_skill_files:
                    continue
                try:
                    data = bytes(crimson_rs.extract_file(
                        game_path, buff_dir, internal_dir, fname))
                    if data:
                        self._staged_skill_files[fname] = data
                        log.info("Rehydrated %s from overlay %s/ (%d bytes)",
                                 fname, buff_dir, len(data))
                except Exception:
                    pass

            log.info("Extracted iteminfo from overlay %s/ (%d bytes) — preserves "
                     "prior Apply to Game state (iteminfo + staged companions)",
                     buff_dir, len(iteminfo))
            return iteminfo, f'{buff_dir}/ overlay'
        except Exception as e:
            log.warning("Overlay extraction from %s/ failed: %s — using vanilla",
                        buff_dir, e)
            return vanilla_raw, 'vanilla'

    def _buff_extract_vanilla(self) -> None:
        """Extract from vanilla (0008/) only, ignoring any overlay."""
        if not self._buff_ensure_patcher():
            return
        # Temporarily disable overlay preference
        self._buff_force_vanilla = True
        try:
            self._buff_extract_rust()
        finally:
            self._buff_force_vanilla = False

    def _buff_extract_rust(self) -> None:
        if not self._buff_ensure_patcher():
            return

        if _is_game_running():
            QMessageBox.warning(
                self, "Close Crimson Desert First",
                "Crimson Desert is running — extract cannot read the PAZ while\n"
                "the game has it locked. Close the game and try again.",
            )
            return

        self._buff_status_label.setText("Extracting iteminfo.pabgb (Rust parser)...")
        QApplication.processEvents()

        try:
            import crimson_rs
        except ImportError:
            QMessageBox.critical(self, "Rust Parser",
                "crimson_rs.pyd not found. This requires Potter's Rust parser module.")
            return

        try:
            import time
            raw, _buff_source = self._buff_extract_iteminfo_preferring_overlay()
            self._buff_data = bytearray(raw)
            self._buff_modified = False

            if len(raw) < 1000:
                QMessageBox.critical(self, "Extract Failed",
                    f"Extracted data too small ({len(raw)} bytes).\n"
                    f"Game files may be corrupted or modded.")
                return
            import struct as _st
            _magic = _st.unpack_from('<I', raw, 0)[0]
            if _magic == 0 or _magic > 0x10000000:
                QMessageBox.critical(self, "Extract Failed",
                    f"Invalid iteminfo header (0x{_magic:08X}).\n"
                    f"Game files may be corrupted by another mod.\n"
                    f"Try: Steam > Verify Integrity of Game Files")
                return

            t0 = time.perf_counter()
            try:
                rust_items = crimson_rs.parse_iteminfo_from_bytes(bytes(raw))
                self._buff_unparsed_raw = []
            except Exception:
                import struct as _pst
                game_path = self._buff_patcher.game_path
                # Use the MATCHING pabgh for this pabgb — overlay if extracting
                # from overlay, vanilla otherwise.
                pabgh = None
                _src = getattr(self, '_buff_force_vanilla', False)
                buff_dir = f"{self._buff_overlay_spin.value():04d}"
                if not _src:
                    try:
                        pabgh = bytes(crimson_rs.extract_file(
                            game_path, buff_dir, 'gamedata/binary__/client/bin',
                            'iteminfo.pabgh'))
                    except Exception:
                        pass
                if not pabgh:
                    try:
                        pabgh = bytes(crimson_rs.extract_file(
                            game_path, '0008', 'gamedata/binary__/client/bin',
                            'iteminfo.pabgh'))
                    except Exception:
                        pabgh = None
                if pabgh:
                    countA = _pst.unpack_from('<H', pabgh, 0)[0]
                    _rec_size = (len(pabgh) - 2) // countA if countA else 8
                    entries = []
                    for _i in range(countA):
                        _rec = 2 + _i * _rec_size
                        if _rec + _rec_size > len(pabgh):
                            break
                        _soff = _pst.unpack_from('<I', pabgh, _rec + (_rec_size - 4))[0]
                        if _soff + 8 > len(raw):
                            continue
                        entries.append(_soff)
                    entries.sort()
                    rust_items = []
                    _unparsed_raw = []
                    for _idx, _soff in enumerate(entries):
                        _nxt = entries[_idx + 1] if _idx + 1 < len(entries) else len(raw)
                        try:
                            _parsed = crimson_rs.parse_iteminfo_from_bytes(
                                bytes(raw[_soff:_nxt]))
                            if _parsed:
                                rust_items.append(_parsed[0])
                            else:
                                _unparsed_raw.append(bytes(raw[_soff:_nxt]))
                        except Exception:
                            _unparsed_raw.append(bytes(raw[_soff:_nxt]))
                    # If no unparsed items found (e.g. re-extracting from overlay
                    # where appended items aren't in the pabgh), always pull them
                    # from vanilla so they're never lost on re-apply.
                    if not _unparsed_raw:
                        try:
                            _van_raw = bytes(crimson_rs.extract_file(
                                game_path, '0008', 'gamedata/binary__/client/bin',
                                'iteminfo.pabgb'))
                            _van_gh = bytes(crimson_rs.extract_file(
                                game_path, '0008', 'gamedata/binary__/client/bin',
                                'iteminfo.pabgh'))
                            _van_countA = _pst.unpack_from('<H', _van_gh, 0)[0]
                            _van_rs = (len(_van_gh) - 2) // _van_countA if _van_countA else 8
                            _van_entries = []
                            for _vi in range(_van_countA):
                                _vrec = 2 + _vi * _van_rs
                                if _vrec + _van_rs > len(_van_gh): break
                                _vsoff = _pst.unpack_from('<I', _van_gh, _vrec + (_van_rs - 4))[0]
                                if _vsoff + 8 <= len(_van_raw):
                                    _van_entries.append(_vsoff)
                            _van_entries.sort()
                            _parsed_keys = {it['key'] for it in rust_items}
                            for _vi, _vsoff in enumerate(_van_entries):
                                _vnxt = _van_entries[_vi+1] if _vi+1 < len(_van_entries) else len(_van_raw)
                                _vkey = _pst.unpack_from('<I', _van_raw, _vsoff)[0]
                                if _vkey not in _parsed_keys:
                                    _unparsed_raw.append(bytes(_van_raw[_vsoff:_vnxt]))
                            if _unparsed_raw:
                                log.info("Recovered %d unparsed items from vanilla", len(_unparsed_raw))
                        except Exception as _ve:
                            log.warning("Could not recover unparsed items from vanilla: %s", _ve)
                    self._buff_unparsed_raw = _unparsed_raw
                    log.info("Per-entry parse: %d/%d items (%d kept as raw)",
                             len(rust_items), len(entries), len(_unparsed_raw))
                else:
                    rust_items = []
            t1 = time.perf_counter()

            self._buff_rust_items = rust_items
            self._buff_rust_lookup = {it['key']: it for it in rust_items}
            self._buff_use_rust = True
            import json
            # deepcopy fails on crimson_rs Rust objects in Python 3.14+
            # JSON round-trip is a safe alternative for plain dict/list data
            try:
                import copy
                self._buff_rust_items_original = copy.deepcopy(rust_items)
            except TypeError:
                self._buff_rust_items_original = json.loads(json.dumps(rust_items))
            self._rebuild_index()

            self._build_effect_catalog(rust_items)

            try:
                from armor_catalog import parse_armor_items
                self._armor_catalog = parse_armor_items(bytes(self._buff_data or b''))
                for a in self._armor_catalog:
                    pretty = self._name_db.get_name(a.item_id)
                    if pretty and not pretty.startswith('Unknown'):
                        a.display_name = pretty
                log.info("Armor catalog: %d items", len(self._armor_catalog))
            except Exception as e:
                log.warning("Armor catalog build failed: %s", e)
                self._armor_catalog = []

            self._buff_status_label.setText(f"Parsed {len(rust_items)} items in {t1-t0:.2f}s. Building offset map...")
            QApplication.processEvents()

            t2 = time.perf_counter()
            self._buff_items = self._buff_patcher.find_items(bytes(self._buff_data))
            t3 = time.perf_counter()

            self._buff_use_structural = True
            if hasattr(self, '_bottom_bar_wrap'):
                self._bottom_bar_wrap.setVisible(True)
            self._buff_status_label.setText(
                f"Extracted (Rust, source: {_buff_source}): "
                f"{len(self._buff_data):,} bytes, {len(self._buff_items)} items. "
                f"Rust parse: {t1-t0:.2f}s, offset map: {t3-t2:.2f}s. "
                f"Search to find items."
            )
        except Exception as e:
            import traceback; traceback.print_exc()
            self._buff_status_label.setText(f"Rust extraction failed: {e}")
            QMessageBox.warning(self, "Rust Parser Failed",
                f"Failed to parse iteminfo.pabgb:\n{e}\n\n"
                f"Your iteminfo has been modified by another mod or tool.\n"
                f"Restore the original game files before using this feature.\n\n"
                f"Steam > Crimson Desert > Properties > Installed Files > Verify Integrity"
            )
            QMessageBox.critical(self, "Rust Parser Failed", str(e))


    def _buff_import_mod_folder(self) -> None:
        """Load a mod folder's iteminfo.pabgb as an editable config.

        Reverse-engineers any CDUMM/PAZ mod folder back into the editor's
        rust-items state. Vanilla iteminfo is extracted as the baseline;
        the mod's iteminfo is loaded as the current edits. Every buff /
        stat / passive / enchant diff the mod author made becomes visible
        and editable in the normal ItemBuffs flow.
        """
        if not self._buff_ensure_patcher():
            return

        from PySide6.QtWidgets import QFileDialog
        mod_root = QFileDialog.getExistingDirectory(
            self, "Pick mod folder (contains files/ or gamedata/...)",
            os.path.dirname(os.path.abspath(sys.argv[0])))
        if not mod_root:
            return

        # Find the iteminfo.pabgb anywhere under the folder.
        target_pabgb = None
        for root, _dirs, files in os.walk(mod_root):
            if 'iteminfo.pabgb' in files:
                target_pabgb = os.path.join(root, 'iteminfo.pabgb')
                break
        if not target_pabgb:
            QMessageBox.warning(self, "Import Mod Folder",
                "No iteminfo.pabgb found in that folder.\n\n"
                "Expected path like:\n"
                "  <mod>/files/gamedata/binary__/client/bin/iteminfo.pabgb")
            return

        try:
            import crimson_rs, copy, time
            with open(target_pabgb, 'rb') as f:
                mod_raw = f.read()
            vanilla_raw = self._buff_patcher.extract_iteminfo()

            t0 = time.perf_counter()
            mod_items = list(self._buff_parse_to_lookup(bytes(mod_raw)).values())
            vanilla_items = list(self._buff_parse_to_lookup(bytes(vanilla_raw)).values())
            t1 = time.perf_counter()
        except Exception as e:
            QMessageBox.critical(self, "Import Mod Folder",
                f"Failed to parse iteminfo.pabgb:\n{e}")
            return

        # Count meaningful diffs to show the user what was captured.
        vanilla_by_key = {it['key']: it for it in vanilla_items}
        diff_items = 0
        diff_buffs = 0
        diff_passives = 0
        diff_stats = 0
        diff_stacks = 0
        for it in mod_items:
            v = vanilla_by_key.get(it['key'])
            if v is None:
                continue
            changed = False
            m_edl = it.get('enchant_data_list') or []
            v_edl = v.get('enchant_data_list') or []
            for mi, me in enumerate(m_edl):
                if mi >= len(v_edl):
                    changed = True
                    continue
                ve = v_edl[mi]
                if (me.get('equip_buffs') or []) != (ve.get('equip_buffs') or []):
                    diff_buffs += 1
                    changed = True
                if (me.get('enchant_stat_data') or {}) != (ve.get('enchant_stat_data') or {}):
                    diff_stats += 1
                    changed = True
            if (it.get('equip_passive_skill_list') or []) != (v.get('equip_passive_skill_list') or []):
                diff_passives += 1
                changed = True
            if it.get('max_stack_count') != v.get('max_stack_count'):
                diff_stacks += 1
                changed = True
            if changed:
                diff_items += 1

        # Load vanilla as baseline, mod as current state — same shape as Extract.
        self._buff_data = bytearray(vanilla_raw)
        self._buff_rust_items = mod_items
        self._buff_rust_items_original = copy.deepcopy(vanilla_items)
        self._buff_rust_lookup = {it['key']: it for it in mod_items}
        self._buff_use_rust = True
        self._buff_use_structural = True
        self._buff_modified = True
        self._rebuild_index()
        self._build_effect_catalog(mod_items)

        try:
            from armor_catalog import parse_armor_items
            self._armor_catalog = parse_armor_items(bytes(self._buff_data or b''))
            for a in self._armor_catalog:
                pretty = self._name_db.get_name(a.item_id)
                if pretty and not pretty.startswith('Unknown'):
                    a.display_name = pretty
        except Exception:
            self._armor_catalog = []

        try:
            self._buff_items = self._buff_patcher.find_items(bytes(self._buff_data))
        except Exception:
            self._buff_items = []

        self._detect_qol_flags_from_items()

        self._buff_status_label.setText(
            f"Imported mod: {diff_items} items changed "
            f"(buffs:{diff_buffs} passives:{diff_passives} "
            f"stats:{diff_stats} stacks:{diff_stacks}). "
            f"Edit and re-export normally.")
        QMessageBox.information(self, "Mod Imported — Reverse-Engineered",
            f"Loaded: {target_pabgb}\n\n"
            f"Changes detected vs vanilla:\n"
            f"  Items modified:       {diff_items}\n"
            f"  Enchant buffs diff:   {diff_buffs}\n"
            f"  Passive skills diff:  {diff_passives}\n"
            f"  Enchant stats diff:   {diff_stats}\n"
            f"  Stack size diff:      {diff_stacks}\n\n"
            f"Parse time: {t1 - t0:.2f}s\n\n"
            f"Every change is now visible in the items table. You can tweak\n"
            f"them and re-export as your own config / mod.")


    def _buff_extract_iteminfo(self, use_structural: bool = False) -> None:
        if not self._buff_ensure_patcher():
            return

        parser_name = "Potter's parser" if use_structural else "Original scanner"
        self._buff_status_label.setText(f"Extracting iteminfo.pabgb ({parser_name})...")
        QApplication.processEvents()

        try:
            raw = self._buff_patcher.extract_iteminfo()
            self._buff_data = bytearray(raw)
            self._buff_modified = False

            if use_structural:
                self._buff_items = self._buff_patcher.find_items(bytes(self._buff_data))
            else:
                self._buff_items = self._buff_find_items_original(bytes(self._buff_data))

            self._buff_use_structural = use_structural
            self._buff_status_label.setText(
                f"Extracted ({parser_name}): {len(self._buff_data):,} bytes, "
                f"{len(self._buff_items)} items. Use Search to find items."
            )
        except Exception as e:
            self._buff_status_label.setText(f"Extraction failed: {e}")
            QMessageBox.critical(self, "Extraction Failed", str(e))


    def _buff_show_my_inventory(self, silent: bool = False) -> None:
        if self._buff_data is None:
            if silent:
                return
            self._buff_extract_iteminfo(use_structural=False)
            if self._buff_data is None:
                return

        if not self._items:
            if silent:
                return
            if not self._buff_icons_enabled:
                try:
                    self._buff_toggle_icons()
                except Exception:
                    pass
            self.status_message.emit("Pick a save slot in the Save Browser, then click 'My Inventory' again.")
            self.open_save_browser_requested.emit()
            return

        save_keys = set(it.item_key for it in self._items if it.item_key > 0)

        iteminfo_keys = set(it.item_key for it in self._buff_items)

        matching_keys = save_keys & iteminfo_keys

        results = [it for it in self._buff_items if it.item_key in matching_keys and it.name[0:1].isalpha() and it.item_key != 1]

        if not results:
            if silent:
                self.status_message.emit("No inventory items matched iteminfo database.")
                return
            QMessageBox.information(self, "No Matches",
                                    "No inventory items found in iteminfo database.")
            return

        table = self._buff_items_table
        table.setSortingEnabled(False)
        table.setRowCount(len(results))

        for row, item in enumerate(results):
            icon_cell = QTableWidgetItem()
            if self._buff_icons_enabled:
                px = self._icon_cache.get_pixmap(item.item_key)
                if px:
                    icon_cell.setIcon(QIcon(px))
            table.setItem(row, 0, icon_cell)

            display_name = self._name_db.get_name(item.item_key)
            if display_name.startswith("Unknown"):
                display_name = item.name
            name_cell = QTableWidgetItem(display_name)
            name_cell.setToolTip(f"Internal: {item.name}\nKey: {item.item_key}")
            name_cell.setData(Qt.UserRole, item)
            table.setItem(row, 1, name_cell)

            limits = self._buff_item_limits.get(str(item.item_key), {})
            slot_type = limits.get('slotType', -1)
            if slot_type == 65535 or slot_type == -1:
                type_str = "Item"
            elif slot_type <= 3:
                type_str = "Weapon"
            elif slot_type <= 8:
                type_str = "Armor"
            elif slot_type <= 12:
                type_str = "Accessory"
            else:
                type_str = "Equip"
            table.setItem(row, 2, QTableWidgetItem(type_str))
            table.setItem(row, 3, QTableWidgetItem(str(limits.get('stackLimit', '?'))))

        table.setSortingEnabled(True)
        self._buff_status_label.setText(
            f"Showing {len(results)} items from your inventory that exist in iteminfo "
            f"(out of {len(save_keys)} save items, {len(iteminfo_keys)} iteminfo records)"
        )


    def _buff_search_items(self) -> None:
        if self._buff_data is None:
            self._buff_extract_iteminfo(use_structural=False)
            if self._buff_data is None:
                return

        query = self._buff_search.text().strip()
        # Category filter (None = no constraint). Allows browsing a category
        # without typing — useful for "show me all OneHandBow items".
        category_filter = None
        cat_combo = getattr(self, "_buff_category_filter", None)
        if cat_combo is not None:
            category_filter = cat_combo.currentData()

        if not query and category_filter is None:
            QMessageBox.information(
                self, "No Search Term",
                "Enter an item name or pick a category to filter by.",
            )
            return

        q = query.lower()
        results = []
        for item in self._buff_items:
            if not item.name[0:1].isalpha():
                continue
            # Name match (or skip the name check entirely if only filtering by category)
            name_ok = True
            if q:
                name_ok = (q in item.name.lower()
                           or q in self._name_db.get_name(item.item_key).lower())
            if not name_ok:
                continue
            # Category match (cheap dict lookup via index, or via rust_lookup fallback)
            if category_filter is not None:
                rust_info = self._buff_rust_lookup.get(item.item_key)
                cat = rust_info.get("category_info", 0) if rust_info else 0
                if cat != category_filter:
                    continue
            results.append(item)

        table = self._buff_items_table
        table.setSortingEnabled(False)
        table.setRowCount(len(results))

        for row, item in enumerate(results):
            icon_cell = QTableWidgetItem()
            if self._buff_icons_enabled:
                px = self._icon_cache.get_pixmap(item.item_key)
                if px:
                    icon_cell.setIcon(QIcon(px))
            table.setItem(row, 0, icon_cell)

            display_name = self._name_db.get_name(item.item_key)
            if display_name.startswith("Unknown"):
                display_name = item.name
            name_cell = QTableWidgetItem(display_name)
            tip = f"Internal: {item.name}\nKey: {item.item_key}"
            rust_info = self._buff_rust_lookup.get(item.item_key)
            if rust_info:
                edl = rust_info.get('enchant_data_list', [])
                tags = rust_info.get('item_tag_list', [])
                tip += f"\nEquip type: {rust_info.get('equip_type_info', '?')}"
                tip += f"\nCategory: {rust_info.get('category_info', '?')}"
                tip += f"\nTier: {rust_info.get('item_tier', '?')}"
                tip += f"\nEnchant levels: {len(edl)}"
                tip += f"\nMax endurance: {rust_info.get('max_endurance', '?')}"
                tip += f"\nMax sharpness: {rust_info.get('sharpness_data', {}).get('max_sharpness', '?')}"
                if tags:
                    tip += f"\nTags: {', '.join(f'0x{t:X}' for t in tags[:8])}"
            name_cell.setToolTip(tip)
            name_cell.setData(Qt.UserRole, item)
            table.setItem(row, 1, name_cell)

            limits = self._buff_item_limits.get(str(item.item_key), {})
            slot_type = limits.get('slotType', -1)
            if slot_type == 65535 or slot_type == -1:
                type_str = "Item"
            elif slot_type <= 3:
                type_str = "Weapon"
            elif slot_type <= 9:
                type_str = "Armor"
            else:
                type_str = "Equip"
            stack_limit = limits.get('stackLimit', -1)

            type_cell = QTableWidgetItem(type_str)
            if type_str == "Weapon":
                type_cell.setForeground(QBrush(QColor(COLORS['error'])))
            elif type_str in ("Armor", "Equip"):
                type_cell.setForeground(QBrush(QColor(COLORS['accent'])))
            table.setItem(row, 2, type_cell)

            rust_info = self._buff_rust_lookup.get(item.item_key)
            if rust_info:
                tier = rust_info.get('item_tier', 0)
                tier_names = {0: "-", 1: "Common", 2: "Uncommon", 3: "Rare", 4: "Epic", 5: "Legendary"}
                tier_cell = QTableWidgetItem(tier_names.get(tier, str(tier)))
                if tier >= 4:
                    tier_cell.setForeground(QBrush(QColor("#c678dd")))
                elif tier == 3:
                    tier_cell.setForeground(QBrush(QColor(COLORS['accent'])))
                table.setItem(row, 3, tier_cell)

                enchant_count = len(rust_info.get('enchant_data_list', []))
                enc_cell = QTableWidgetItem(f"+{enchant_count - 1}" if enchant_count > 1 else "-")
                if enchant_count > 1:
                    enc_cell.setForeground(QBrush(QColor(COLORS['success'])))
                table.setItem(row, 4, enc_cell)
            else:
                table.setItem(row, 3, QTableWidgetItem("-"))
                table.setItem(row, 4, QTableWidgetItem("-"))

            table.setItem(row, 5, QTableWidgetItem(str(stack_limit) if stack_limit > 0 else "\u2014"))

        table.setSortingEnabled(True)

        self._buff_status_label.setText(
            f"Found {len(results)} items matching '{query}'."
        )

        if len(results) == 1:
            table.selectRow(0)
            self._buff_item_selected()
        else:
            self._buff_stats_table.setRowCount(0)
            self._buff_current_item = None
            if hasattr(self, '_buff_selected_label'):
                self._buff_selected_label.setText("No item selected — search and click an item on the left")
                self._buff_selected_label.setStyleSheet(
                    f"color: {COLORS['text_dim']}; font-weight: bold; padding: 2px 4px;"
                )


    def _buff_item_selected(self) -> None:
        rows = self._buff_items_table.selectionModel().selectedRows()
        if not rows:
            return

        row = rows[0].row()
        name_cell = self._buff_items_table.item(row, 1)
        if name_cell is None:
            return

        item: ItemRecord = name_cell.data(Qt.UserRole)
        if item is None:
            return

        self._buff_current_item = item
        display_name = self._name_db.get_name(item.item_key) if hasattr(self, '_name_db') else item.name
        if display_name.startswith("Unknown"):
            display_name = item.name
        self._buff_selected_label.setText(f"Editing: {display_name}  (key {item.item_key})")
        self._buff_selected_label.setStyleSheet(
            f"color: {COLORS['accent']}; font-weight: bold; padding: 2px 4px;"
        )
        self._buff_refresh_stats()


    def _buff_toggle_icons(self) -> None:
        self._buff_icons_enabled = not self._buff_icons_enabled
        if self._buff_icons_enabled:
            self._buff_show_icons_btn.setText("Hide Icons")
            for item in self._buff_items:
                self._icon_cache.get_pixmap(item.item_key)
            row_h = max(ICON_SIZE + 2, 24)
        else:
            self._buff_show_icons_btn.setText("Show Icons")
            row_h = 24

        self._buff_items_table.setColumnWidth(0, (ICON_SIZE + 12) if self._buff_icons_enabled else 0)
        self._buff_items_table.verticalHeader().setDefaultSectionSize(row_h)

        if self._buff_search.text().strip():
            self._buff_search_items()
        elif self._buff_items_table.rowCount() > 0:
            for row in range(self._buff_items_table.rowCount()):
                name_cell = self._buff_items_table.item(row, 1)
                if name_cell:
                    item = name_cell.data(Qt.UserRole)
                    if item and self._buff_icons_enabled:
                        icon_cell = self._buff_items_table.item(row, 0)
                        if icon_cell:
                            px = self._icon_cache.get_pixmap(item.item_key)
                            if px:
                                icon_cell.setIcon(QIcon(px))


    def _buff_refresh_stats(self) -> None:
        item = self._buff_current_item
        if item is None:
            self._buff_stats_table.setRowCount(0)
            return

        if self._buff_data is not None:
            arrays = ItemBuffPatcher.find_stat_arrays(bytes(self._buff_data), item)
            self._buff_current_arrays = arrays
            all_entries = []
            for arr in arrays:
                all_entries.extend(arr.entries)
            item.stat_triplets = all_entries
        else:
            self._buff_current_arrays = []
            item.stat_triplets = []

        table = self._buff_stats_table
        row = 0

        cd_abs_off, cd_val = self._cd_detect(item.item_key)
        patch = self._cd_patches.get(item.item_key) if hasattr(self, '_cd_patches') else None
        if cd_abs_off is not None or patch:
            display_val = patch[2] if patch else cd_val
            orig_val = struct.unpack_from('<I', patch[1])[0] if patch else cd_val
            mins = display_val // 60
            secs = display_val % 60
            time_str = (f"{mins}m {secs}s" if mins else f"{secs}s")
            cd_label = f"Cooldown ({time_str})"
            if patch:
                cd_label += f"  ← was {orig_val}s"
            cd_c1 = QTableWidgetItem(f"  {cd_label}  ← click to edit")
            cd_c1.setForeground(QBrush(QColor("#FFB74D")))
            cd_c1.setFont(QFont("Consolas", 9, QFont.Bold))
            cd_c1.setFlags(cd_c1.flags() & ~Qt.ItemIsSelectable)
            cd_c2 = QTableWidgetItem(f"{display_val:,} s")
            cd_c2.setForeground(QBrush(QColor("#FFB74D")))
            cd_c2.setFont(QFont("Consolas", 10))
            cd_c2.setFlags(cd_c2.flags() & ~Qt.ItemIsSelectable)
            table.setRowCount(row + 1)
            table.setItem(row, 0, cd_c1)
            table.setItem(row, 1, cd_c2)
            row += 1

        _STAT_NAMES = {
            1000000: "HP", 1000001: "Fatal", 1000002: "DDD (Damage)",
            1000003: "DPV (Defense)", 1000004: "DHIT (Accuracy)",
            1000005: "DDV (Base Attack)", 1000006: "Crit Damage",
            1000007: "Crit Rate", 1000008: "Incoming Dmg Rate",
            1000009: "Incoming Dmg Reduction", 1000010: "Attack Speed",
            1000011: "Move Speed", 1000012: "Climb Speed",
            1000013: "Swim Speed", 1000016: "Fire Resist",
            1000017: "Ice Resist", 1000018: "Lightning Resist",
            1000026: "Stamina", 1000027: "MP",
            1000037: "Stamina Cost Reduction", 1000043: "Guard PV Rate",
            1000035: "Max Damage Rate", 1000036: "Pressure",
            1000043: "Guard PV Rate", 1000046: "MP Cost Reduction",
            1000047: "Money Drop Rate", 1000049: "Equip Drop Rate",
            1000050: "DPV Rate",
        }

        rust_info = self._buff_rust_lookup.get(item.item_key) if hasattr(self, '_buff_rust_lookup') else None

        if rust_info:
            ddd = rust_info.get('drop_default_data', {})
            self._eb_drop_enchant_level.setValue(ddd.get('drop_enchant_level', 0))

            if ddd.get('use_socket'):
                svc = ddd.get('socket_valid_count', 0)
                sml = ddd.get('add_socket_material_item_list', [])
                self._eb_socket_valid.setValue(svc)
                self._eb_socket_count.setValue(len(sml))

                s_sep = QTableWidgetItem(f"--- Sockets ({svc}/{len(sml)}) ---")
                s_sep.setForeground(QBrush(QColor("#F83B3B")))
                s_sep.setFont(QFont("Consolas", 9, QFont.Bold))
                s_sep.setFlags(s_sep.flags() & ~Qt.ItemIsSelectable)
                table.setRowCount(row + 1)
                table.setItem(row, 0, s_sep)
                table.setItem(row, 1, QTableWidgetItem(""))
                table.setSpan(row, 0, 1, 2)
                row += 1

                sil = ddd.get('socket_item_list', [])
                if sil:
                    for si in sil:
                        si_name = self._name_db.get_name(si) if hasattr(self, '_name_db') else si
                        si_c = QTableWidgetItem(f"  {si_name}")
                        si_c.setForeground(QBrush(QColor("#F83B3B")))
                        si_c.setData(Qt.UserRole + 1, ('socket', si))
                        table.setRowCount(row + 1)
                        table.setItem(row, 0, si_c)
                        table.setItem(row, 1, QTableWidgetItem(""))
                        table.setSpan(row, 0, 1, 2)
                        row += 1
                else:
                    si_c = QTableWidgetItem("  (none)")
                    si_c.setForeground(QBrush(QColor(COLORS["text_dim"])))
                    table.setRowCount(row + 1)
                    table.setItem(row, 0, si_c)
                    table.setItem(row, 1, QTableWidgetItem(""))
                    row += 1

            edl = rust_info.get('enchant_data_list', [])

            psl = rust_info.get('equip_passive_skill_list', [])
            ps_sep = QTableWidgetItem(f"--- Passive Skills ({len(psl)}) ---")
            ps_sep.setForeground(QBrush(QColor("#4FC3F7")))
            ps_sep.setFont(QFont("Consolas", 9, QFont.Bold))
            ps_sep.setFlags(ps_sep.flags() & ~Qt.ItemIsSelectable)
            table.setRowCount(row + 1)
            table.setItem(row, 0, ps_sep)
            table.setItem(row, 1, QTableWidgetItem(""))
            table.setSpan(row, 0, 1, 2)
            row += 1

            if psl:
                for ps in psl:
                    sk_name = self._PASSIVE_SKILL_NAMES.get(ps['skill'], f"Skill {ps['skill']}")
                    c1 = QTableWidgetItem(f"  {sk_name}  ← click to select for Remove")
                    c1.setForeground(QBrush(QColor("#4FC3F7")))
                    c1.setData(Qt.UserRole + 1, ('passive', ps['skill']))
                    c2 = QTableWidgetItem(f"Lv {ps['level']}")
                    c2.setFont(QFont("Consolas", 10))
                    c2.setForeground(QBrush(QColor("#4FC3F7")))
                    c2.setData(Qt.UserRole + 1, ('passive', ps['skill']))
                    table.setRowCount(row + 1)
                    table.setItem(row, 0, c1)
                    table.setItem(row, 1, c2)
                    row += 1
            else:
                c1 = QTableWidgetItem("  (none)")
                c1.setForeground(QBrush(QColor(COLORS["text_dim"])))
                table.setRowCount(row + 1)
                table.setItem(row, 0, c1)
                table.setItem(row, 1, QTableWidgetItem(""))
                row += 1

            if edl:
                display_level = 0
                if hasattr(self, '_eb_level_target'):
                    sel = self._eb_level_target.currentData()
                    if sel is not None and sel >= 0 and sel < len(edl):
                        display_level = sel

                lvl_hdr = QTableWidgetItem(f"=== Showing Enchant Level +{display_level} (of {len(edl)}) ===")
                lvl_hdr.setForeground(QBrush(QColor(COLORS["warning"])))
                lvl_hdr.setFont(QFont("Consolas", 9, QFont.Bold))
                lvl_hdr.setFlags(lvl_hdr.flags() & ~Qt.ItemIsSelectable)
                table.setRowCount(row + 1)
                table.setItem(row, 0, lvl_hdr)
                table.setItem(row, 1, QTableWidgetItem(""))
                table.setSpan(row, 0, 1, 2)
                row += 1

                ed0 = edl[display_level]
                sd = ed0.get('enchant_stat_data', {})

                for list_name, color, label in [
                    ('stat_list_static', '#FFB74D', 'Flat Stats'),
                    ('stat_list_static_level', '#81C784', 'Rate Stats'),
                    ('regen_stat_list', '#4FC3F7', 'Regen Stats'),
                    ('max_stat_list', '#CE93D8', 'Max Stats'),
                ]:
                    stats = sd.get(list_name, [])
                    sep = QTableWidgetItem(f"--- {label} ({len(stats)}) [level 0/{len(edl)-1}] ---")
                    sep.setForeground(QBrush(QColor(color)))
                    sep.setFont(QFont("Consolas", 9, QFont.Bold))
                    sep.setFlags(sep.flags() & ~Qt.ItemIsSelectable)
                    table.setRowCount(row + 1)
                    table.setItem(row, 0, sep)
                    table.setItem(row, 1, QTableWidgetItem(""))
                    table.setSpan(row, 0, 1, 2)
                    row += 1

                    if stats:
                        for s in stats:
                            sname = (getattr(self, '_STAT_NAMES_COMMUNITY', {}).get(s['stat'])
                                     or _STAT_NAMES.get(s['stat'], f"Stat {s['stat']}"))
                            val = s['change_mb']
                            c1 = QTableWidgetItem(f"  {sname}  ← click to select for Remove")
                            c1.setForeground(QBrush(QColor(color)))
                            c1.setData(Qt.UserRole + 1, ('stat', s['stat'], list_name, val))
                            if 'level' in list_name:
                                c2 = QTableWidgetItem(f"Lv {val}")
                            else:
                                c2 = QTableWidgetItem(f"{val:,}")
                            c2.setFont(QFont("Consolas", 10))
                            c2.setForeground(QBrush(QColor(color)))
                            c2.setData(Qt.UserRole + 1, ('stat', s['stat'], list_name, val))
                            table.setRowCount(row + 1)
                            table.setItem(row, 0, c1)
                            table.setItem(row, 1, c2)
                            row += 1

            all_buffs = []
            if edl:
                all_buffs = edl[0].get('equip_buffs', [])
            eb_sep = QTableWidgetItem(f"--- Equipment Buffs ({len(all_buffs)}) ---")
            eb_sep.setForeground(QBrush(QColor("#AB47BC")))
            eb_sep.setFont(QFont("Consolas", 9, QFont.Bold))
            eb_sep.setFlags(eb_sep.flags() & ~Qt.ItemIsSelectable)
            table.setRowCount(row + 1)
            table.setItem(row, 0, eb_sep)
            table.setItem(row, 1, QTableWidgetItem(""))
            table.setSpan(row, 0, 1, 2)
            row += 1

            for b in all_buffs:
                bname = self._EQUIP_BUFF_NAMES.get(b['buff'], f"Buff {b['buff']}")
                c1 = QTableWidgetItem(f"  {bname}  ← click to select for Remove")
                c1.setForeground(QBrush(QColor("#AB47BC")))
                c1.setData(Qt.UserRole + 1, ('buff', b['buff']))
                c2 = QTableWidgetItem(f"Lv {b['level']}")
                c2.setFont(QFont("Consolas", 10))
                c2.setForeground(QBrush(QColor("#AB47BC")))
                c2.setData(Qt.UserRole + 1, ('buff', b['buff']))
                table.setRowCount(row + 1)
                table.setItem(row, 0, c1)
                table.setItem(row, 1, c2)
                row += 1

            sharp = rust_info.get('sharpness_data', {})
            max_sharp = sharp.get('max_sharpness', 0)
            if max_sharp > 0:
                sep2 = QTableWidgetItem(f"--- Sharpness (max {max_sharp}) ---")
                sep2.setForeground(QBrush(QColor(COLORS["warning"])))
                sep2.setFont(QFont("Consolas", 9, QFont.Bold))
                sep2.setFlags(sep2.flags() & ~Qt.ItemIsSelectable & ~Qt.ItemIsEditable)
                table.setRowCount(row + 1)
                table.setItem(row, 0, sep2)
                table.setItem(row, 1, QTableWidgetItem(""))
                table.setSpan(row, 0, 1, 2)
                row += 1

        display_name = self._name_db.get_name(item.item_key)
        if display_name.startswith("Unknown"):
            display_name = item.name

        n_arrays = len(arrays)
        n_entries = len(all_entries)
        classes = set(e.size_class for e in all_entries)
        class_str = "/".join(sorted(classes)) if classes else "none"

        rust_extra = ""
        if rust_info:
            tier = rust_info.get('item_tier', 0)
            tier_names = {0: "-", 1: "Common", 2: "Uncommon", 3: "Rare", 4: "Epic", 5: "Legendary"}
            edl = rust_info.get('enchant_data_list', [])
            rust_extra = f"  |  Tier: {tier_names.get(tier, str(tier))}  |  Enchant levels: {len(edl)}"

        self._buff_status_label.setText(
            f"{display_name}: {n_entries} stats in {n_arrays} arrays [{class_str}]{rust_extra}"
        )

        self._buff_array_combo.blockSignals(True)
        self._buff_array_combo.clear()
        self._buff_array_combo.addItem("All Levels (apply to every array)")
        for i in range(n_arrays):
            class_label = arrays[i].entries[0].size_class if arrays[i].entries else "?"
            self._buff_array_combo.addItem(f"Level {i+1} — Array {i} [{class_label}] ({len(arrays[i].entries)} stats)")
        self._buff_array_combo.blockSignals(False)

    _BUFF_PRESETS = [
        {"name": "Max All"},
        {"name": "Max All Flat"},
        {"name": "Max DDD"},
        {"name": "Max DPV"},
        {"name": "Max HP"},
        {"name": "Max All Rates"},
        {"name": "Swap to DDD"},
        {"name": "Swap to DPV"},
        None,
    ]

    _BUFF_DESCRIPTIONS = {
        "Invincible": "Makes you unkillable. Value: 1 = on, 0 = off. [flat2 — 12B entry]",
        "Hp": "Maximum health points. Value: raw HP number (e.g. 1,000,000). [flat1 — 8B entry]",
        "DDD (Damage)": "Direct Damage Dealt. Attack power. Value: raw damage number. [flat2 — 12B entry]",
        "DPV (Defense)": "Defense Point Value. Damage reduction. Value: raw number. [flat2 — 12B entry]",
        "CriticalDamage": "Extra damage on critical hits. [flat2 — 12B entry, value = raw]",
        "AttackedDamageRate": "Extra damage taken modifier. [flat2 — 12B entry, value = raw]",
        "AttackedDamageReduction": "Damage reduction rate. [flat2 — 12B entry, value = raw]",
        "CriticalRate": "Chance to land a critical hit. Level 0-255 (varies per stat). [rate — 5B entry]",
        "AttackSpeedRate": "How fast you swing/attack. Level 0-255 (varies per stat). [rate — 5B entry]",
        "MoveSpeedRate": "How fast you run. Level 0-255 (varies per stat). [rate — 5B entry]",
        "ClimbSpeedRate": "Climbing speed. Level 0-255 (varies per stat). [rate — 5B entry]",
        "SwimSpeedRate": "Swimming speed. Level 0-255 (varies per stat). [rate — 5B entry]",
        "StaminaRegen": "Stamina recovery rate. Level 0-255 (varies per stat). [rate — 5B entry]",
        "HpRegen": "Health regeneration rate. Level 0-255 (varies per stat). [rate — 5B entry]",
        "MpRegen": "Mana regeneration rate. Level 0-255 (varies per stat). [rate — 5B entry]",
        "FireResistance": "Fire damage resistance. Level 0-255 (varies per stat). [rate — 5B entry]",
        "IceResistance": "Ice damage resistance. Level 0-255 (varies per stat). [rate — 5B entry]",
        "ElectricResistance": "Electric damage resistance. Level 0-255 (varies per stat). [rate — 5B entry]",
        "GuardPVRate": "Guard/block effectiveness. Level 0-255 (varies per stat). [rate — 5B entry]",
        "ReduceCraftMaterial": "Reduces crafting material cost. Level 0-255 (varies per stat). [rate — 5B entry]",
        "MoreOreDrop": "Bonus ore from mining. Level 0-255 (varies per stat). [rate — 5B entry]",
        "MoreLumberDrop": "Bonus lumber from chopping. Level 0-255 (varies per stat). [rate — 5B entry]",
        "EquipDropRate": "Equipment drop rate bonus. Level 0-255 (varies per stat). [rate — 5B entry]",
        "MoneyDropRate": "Silver drop rate bonus. Level 0-255 (varies per stat). [rate — 5B entry]",
        "CollectDrop_Ore": "Collection bonus: ore. Level 0-255 (varies per stat). [rate — 5B entry]",
        "CollectDrop_Plant": "Collection bonus: plants. Level 0-255 (varies per stat). [rate — 5B entry]",
        "CollectDrop_Animal": "Collection bonus: animal parts. Level 0-255 (varies per stat). [rate — 5B entry]",
        "CollectDrop_Log": "Collection bonus: logs. Level 0-255 (varies per stat). [rate — 5B entry]",
    }

    _PRESET_DESCRIPTIONS = [
        "Max every stat on the item: flat values to 999,999, rate levels to 15. No hash changes — keeps original stat types.",
        "Max all flat stats: sets all flat2/flat1 values to 999,999 at every refinement level.",
        "Max DDD to 999,999 at every refinement level. Only edits flat2 stat entries.",
        "Max DPV to 999,999 at every refinement level. Only edits flat2 stat entries.",
        "Max HP to 999,999 at every refinement level. Only edits flat1 stat entries.",
        "Set all rate stats to Lv 15 (max). Only edits rate entries.",
        "Swap existing flat2 stat to DDD (Damage). Same size class, safe in-place swap.",
        "Swap existing flat2 stat to DPV (Defense). Same size class, safe in-place swap.",
        "",
    ]


    def _buff_update_desc(self, *_args) -> None:
        if not hasattr(self, '_buff_desc_label'):
            return
        idx = self._buff_preset_combo.currentIndex()
        if idx < len(self._PRESET_DESCRIPTIONS) - 1:
            self._buff_desc_label.setText(self._PRESET_DESCRIPTIONS[idx])
        else:
            stat_name = self._buff_type_combo.currentText()
            desc = self._BUFF_DESCRIPTIONS.get(stat_name, "")
            self._buff_desc_label.setText(desc)


    def _buff_preset_changed(self, index: int) -> None:
        is_custom = (index == len(self._BUFF_PRESETS) - 1)
        self._buff_custom_row.setVisible(is_custom)


    def _eb_apply(self) -> None:
        if not hasattr(self, '_buff_rust_items') or not self._buff_rust_items:
            QMessageBox.warning(self, "Passive Editor",
                "Extract with Rust parser first (click 'Extract (Rust)').")
            return
        if not hasattr(self, '_buff_current_item') or self._buff_current_item is None:
            QMessageBox.warning(self, "Passive Editor", "Select an item first.")
            return

        rust_info = self._buff_rust_lookup.get(self._buff_current_item.item_key)
        if not rust_info:
            QMessageBox.warning(self, "Passive Editor", "Item not found in Rust data.")
            return

        psl = rust_info.get('equip_passive_skill_list', [])

        new_skill = self._eb_passive_combo.currentData()
        new_level = self._eb_level_spin.value()
        new_name = self._PASSIVE_SKILL_NAMES.get(new_skill, f"Skill {new_skill}")

        already_has = any(p['skill'] == new_skill for p in psl)

        current_list = ", ".join(
            f"{self._PASSIVE_SKILL_NAMES.get(p['skill'], p['skill'])} Lv{p['level']}"
            for p in psl) or "(none)"

        if already_has:
            msg = (f"Update passive level on this item:\n\n"
                   f"  {new_name}: update to Lv {new_level}\n"
                   f"  Current passives: {current_list}")
        else:
            msg = (f"ADD passive to this item:\n\n"
                   f"  Adding: {new_name} Lv {new_level}\n"
                   f"  Current passives: {current_list}\n\n"
                   f"This will ADD to existing passives (not replace).")

        reply = QMessageBox.question(
            self, "Add Passive Skill",
            f"{msg}\n\n"
            f"Click 'Export as Mod' after to write.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        if already_has:
            for p in psl:
                if p['skill'] == new_skill:
                    p['level'] = new_level
                    break
        else:
            psl.append({'skill': new_skill, 'level': new_level})
            rust_info['equip_passive_skill_list'] = psl

        self._buff_modified = True
        total = len(rust_info.get('equip_passive_skill_list', []))
        self._eb_status.setText(f"Added {new_name} Lv{new_level} ({total} passives) — click Export as Mod")
        self._buff_refresh_stats()


    def _eb_add_stat(self) -> None:
        if not hasattr(self, '_buff_rust_items') or not self._buff_rust_items:
            QMessageBox.warning(self, "Add Stat", "Extract with Rust parser first.")
            return
        if not hasattr(self, '_buff_current_item') or self._buff_current_item is None:
            QMessageBox.warning(self, "Add Stat", "Select an item first.")
            return

        rust_info = self._buff_rust_lookup.get(self._buff_current_item.item_key)
        if not rust_info:
            return

        edl = rust_info.get('enchant_data_list', [])
        if not edl:
            QMessageBox.warning(self, "Add Stat", "This item has no enchant data.")
            return

        combo_idx = self._eb_stat_combo.currentData()
        if combo_idx is None or combo_idx >= len(self._ENCHANT_STAT_LIST):
            return
        stat_name, stat_key, stat_list, _ = self._ENCHANT_STAT_LIST[combo_idx]
        stat_value = self._eb_stat_value.value()

        target_level = self._eb_level_target.currentData()

        added = 0
        for idx, ed in enumerate(edl):
            if target_level != -1 and idx != target_level:
                continue
            sd = ed.setdefault('enchant_stat_data', {})
            existing = sd.get(stat_list, [])
            replaced = False
            for i, s in enumerate(existing):
                if s['stat'] == stat_key:
                    existing[i] = {'stat': stat_key, 'change_mb': stat_value}
                    replaced = True
                    break
            if not replaced:
                existing.append({'stat': stat_key, 'change_mb': stat_value})
            sd[stat_list] = existing
            added += 1

        self._buff_modified = True
        self._buff_refresh_stats()
        display_name = self._name_db.get_name(self._buff_current_item.item_key)
        level_str = f"level +{target_level}" if target_level >= 0 else f"{added} levels"
        self._buff_status_label.setText(
            f"Added {stat_name}={stat_value:,} to {display_name} ({level_str}). "
            f"Click 'Export as Mod' to write.")


    def _eb_remove_stat(self) -> None:
        if not hasattr(self, '_buff_rust_items') or not self._buff_rust_items:
            return
        if not hasattr(self, '_buff_current_item') or self._buff_current_item is None:
            return

        rust_info = self._buff_rust_lookup.get(self._buff_current_item.item_key)
        if not rust_info:
            return

        edl = rust_info.get('enchant_data_list', [])
        if not edl:
            return

        combo_idx = self._eb_stat_combo.currentData()
        if combo_idx is None or combo_idx >= len(self._ENCHANT_STAT_LIST):
            return
        stat_name, stat_key, stat_list, _ = self._ENCHANT_STAT_LIST[combo_idx]

        removed = 0
        for ed in edl:
            sd = ed.get('enchant_stat_data', {})
            existing = sd.get(stat_list, [])
            new_list = [s for s in existing if s['stat'] != stat_key]
            if len(new_list) < len(existing):
                removed += 1
            sd[stat_list] = new_list

        if removed == 0:
            QMessageBox.information(self, "Remove Stat", f"{stat_name} not found on this item.")
            return

        self._buff_modified = True
        self._buff_refresh_stats()
        self._buff_status_label.setText(f"Removed {stat_name} from {removed} enchant levels.")


    def _eb_json_edit(self) -> None:
        if not hasattr(self, '_buff_rust_items') or not self._buff_rust_items:
            QMessageBox.warning(self, "JSON Edit", "Extract with Rust parser first.")
            return
        if not hasattr(self, '_buff_current_item') or self._buff_current_item is None:
            QMessageBox.warning(self, "JSON Edit", "Select an item first.")
            return

        rust_info = self._buff_rust_lookup.get(self._buff_current_item.item_key)
        if not rust_info:
            return

        display_name = self._name_db.get_name(self._buff_current_item.item_key)

        edit_data = {
            "item_key": rust_info['key'],
            "string_key": rust_info.get('string_key', ''),
            "equip_passive_skill_list": rust_info.get('equip_passive_skill_list', []),
            "gimmick_info": rust_info.get('gimmick_info', 0),
            "cooltime": rust_info.get('cooltime', 0),
            "item_charge_type": rust_info.get('item_charge_type', 0),
            "max_charged_useable_count": rust_info.get('max_charged_useable_count', 0),
            "respawn_time_seconds": rust_info.get('respawn_time_seconds', 0),
        }
        dcd = rust_info.get('docking_child_data')
        if dcd:
            edit_data["docking_child_data"] = dcd
        else:
            edit_data["docking_child_data"] = {
                "_note": "DELETE this _note field. Fill in gimmick_info_key to enable item activation.",
                "gimmick_info_key": 0,
                "character_key": 0,
                "item_key": 0,
                "attach_parent_socket_name": "Gimmick_Weapon_00_Socket",
                "attach_child_socket_name": "",
                "docking_tag_name_hash": [0, 0, 0, 0],
                "docking_equip_slot_no": 65535,
                "spawn_distance_level": 4294967295,
                "is_item_equip_docking_gimmick": 1,
                "send_damage_to_parent": 0,
                "is_body_part": 0,
                "docking_type": 0,
                "is_summoner_team": 0,
                "is_player_only": 0,
                "is_npc_only": 0,
                "is_sync_break_parent": 0,
                "hit_part": 0,
                "detected_by_npc": 0,
                "is_bag_docking": 0,
                "enable_collision": 0,
                "disable_collision_with_other_gimmick": 1,
                "docking_slot_key": "",
                "inherit_summoner": 0,
                "summon_tag_name_hash": [0, 0, 0, 0],
            }

        ddd = rust_info.get('drop_default_data')
        if ddd:
            edit_data["drop_default_data"] = {
                "drop_enchant_level": ddd.get('drop_enchant_level', 0),
                "socket_item_list": ddd.get('socket_item_list', []),
                "add_socket_material_item_list": ddd.get('add_socket_material_item_list', []),
                "socket_valid_count": ddd.get('socket_valid_count', 0),
                "use_socket": ddd.get('use_socket', 0),
            }

        edl = rust_info.get('enchant_data_list', [])
        if edl:
            ed0 = edl[0]
            edit_data["enchant_stat_data"] = ed0.get('enchant_stat_data', {})
            edit_data["equip_buffs"] = ed0.get('equip_buffs', [])
            edit_data["_note"] = f"Edits apply to ALL {len(edl)} enchant levels"

        json_text = json.dumps(edit_data, indent=2, ensure_ascii=False)

        dlg = QDialog(self)
        dlg.setWindowTitle(f"Advanced JSON Edit: {display_name}")
        dlg.resize(650, 550)
        dl = QVBoxLayout(dlg)

        info = QLabel(
            "Edit the JSON below. Changes to enchant_stat_data and equip_buffs\n"
            "will be applied to ALL enchant levels. Click Apply to save.\n\n"
            "Stat keys: 1000002=DDD, 1000003=DPV, 1000007=CritRate, 1000010=AtkSpeed,\n"
            "1000011=MoveSpeed, 1000024=FireRes, 1000025=IceRes, 1000026=LightningRes\n\n"
            "Gimmick: Set gimmick_info + docking_child_data.gimmick_info_key to same value.\n"
            "cooltime >= 1 (0 crashes). item_charge_type: 0=activated, 2=passive.\n"
            "Lightning (gimmick 1001961) = pure VFX. Works on twohand/hammer/spear/glove;\n"
            "one-handed gets visual only (skill has weapon-type filter).\n\n"
            "drop_default_data: add entries to add_socket_material_item_list to grant\n"
            "more sockets. Length of list = max socket count."
        )
        info.setStyleSheet(f"color: {COLORS['text_dim']}; font-size: 11px; padding: 4px;")
        dl.addWidget(info)

        text_edit = QTextEdit()
        text_edit.setFont(QFont("Consolas", 10))
        text_edit.setPlainText(json_text)
        dl.addWidget(text_edit, 1)

        btn_row = QHBoxLayout()
        apply_btn = QPushButton("Apply Changes")
        apply_btn.setObjectName("accentBtn")
        apply_btn.setStyleSheet("background-color: #cc3333; color: white; font-weight: bold;")
        btn_row.addWidget(apply_btn)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(dlg.reject)
        btn_row.addWidget(cancel_btn)
        btn_row.addStretch()
        dl.addLayout(btn_row)

        def _apply():
            try:
                new_data = json.loads(text_edit.toPlainText())
            except json.JSONDecodeError as e:
                QMessageBox.warning(dlg, "Invalid JSON", f"Parse error:\n{e}")
                return

            # Soft validation against vanilla iteminfo — advisory only, never blocks.
            if self._index is not None:
                warnings = self._index.validate_edit(new_data)
                if warnings:
                    msg = "Heads up — these look risky:\n\n  " + "\n  ".join(
                        f"• {w}" for w in warnings) + "\n\nApply anyway?"
                    reply = QMessageBox.question(
                        dlg, "Validation Warnings", msg,
                        QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
                    if reply != QMessageBox.Yes:
                        return

            if 'equip_passive_skill_list' in new_data:
                rust_info['equip_passive_skill_list'] = new_data['equip_passive_skill_list']

            for gf in ('gimmick_info', 'cooltime', 'item_charge_type',
                        'max_charged_useable_count', 'docking_child_data',
                        'respawn_time_seconds'):
                if gf in new_data:
                    val = new_data[gf]
                    if isinstance(val, dict):
                        val = {k: v for k, v in val.items() if not k.startswith('_note')}
                    if gf == 'docking_child_data' and isinstance(val, dict):
                        if val.get('gimmick_info_key', 0) == 0:
                            continue
                        val.setdefault('inherit_summoner', 0)
                        val.setdefault('summon_tag_name_hash', [0, 0, 0, 0])
                    rust_info[gf] = val

            if 'cooltime' in new_data:
                rust_info['unk_post_cooltime_a'] = new_data['cooltime']
                rust_info['unk_post_cooltime_b'] = new_data['cooltime']
            if 'max_charged_useable_count' in new_data:
                rust_info['unk_post_max_charged_a'] = new_data['max_charged_useable_count']
                rust_info['unk_post_max_charged_b'] = new_data['max_charged_useable_count']

            if 'drop_default_data' in new_data:
                new_ddd = new_data['drop_default_data']
                cur_ddd = rust_info.get('drop_default_data')
                if cur_ddd and isinstance(new_ddd, dict):
                    for k in ('drop_enchant_level', 'socket_item_list',
                              'add_socket_material_item_list', 'socket_valid_count',
                              'use_socket'):
                        if k in new_ddd:
                            cur_ddd[k] = new_ddd[k]

            edl = rust_info.get('enchant_data_list', [])
            if 'enchant_stat_data' in new_data:
                for ed in edl:
                    ed['enchant_stat_data'] = json.loads(json.dumps(new_data['enchant_stat_data']))
            if 'equip_buffs' in new_data:
                for ed in edl:
                    ed['equip_buffs'] = json.loads(json.dumps(new_data['equip_buffs']))

            self._buff_modified = True
            self._buff_refresh_stats()
            dlg.accept()
            self._buff_status_label.setText(f"Applied JSON edits to {display_name}. Click 'Export as Mod'.")

        apply_btn.clicked.connect(_apply)
        dlg.exec()


    def _apply_transmog_swaps(self, final_data: bytearray) -> int:
        if not getattr(self, '_transmog_swaps', None):
            return 0
        try:
            from armor_catalog import apply_swaps_to_blob
            applied = apply_swaps_to_blob(final_data, self._transmog_swaps)
            log.info("Transmog: applied %d byte patches for %d queued swap(s)",
                     applied, len(self._transmog_swaps))
            if applied == 0 and self._transmog_swaps:
                try:
                    QMessageBox.warning(
                        self, "Transmog Not Applied",
                        "Transmog swaps were queued but 0 byte patches landed.\n\n"
                        "Likely cause: heavy ItemBuffs edits (many equip_buffs or\n"
                        "passives) expanded item records so the transmog catalog\n"
                        "lost the target items during re-parse.\n\n"
                        "Check the log for 'apply_swaps_to_blob' warnings, then\n"
                        "reduce buff stacks or report with the log attached.",
                    )
                except Exception:
                    pass
            return applied
        except Exception as e:
            log.warning("Transmog apply failed: %s", e)
            return 0


    def _apply_vfx_changes(self, final_data: bytearray) -> bool:
        if not (getattr(self, '_vfx_size_changes', None) or
                getattr(self, '_vfx_swaps', None) or
                getattr(self, '_vfx_anim_swaps', None) or
                getattr(self, '_vfx_attach_changes', None)):
            return False
        if not getattr(self, '_experimental_mode', False):
            log.info("VFX Lab changes queued but skipped — experimental mode is off")
            return False
        try:
            import vfx_lab
            new_bytes = vfx_lab.apply_all_changes(
                bytes(final_data),
                self._vfx_size_changes,
                self._vfx_swaps,
                self._vfx_anim_swaps,
                self._vfx_attach_changes,
            )
            final_data.clear()
            final_data.extend(new_bytes)
            log.info("VFX Lab: applied %d size, %d vfx, %d anim, %d attach",
                     len(self._vfx_size_changes), len(self._vfx_swaps),
                     len(self._vfx_anim_swaps), len(self._vfx_attach_changes))
            return True
        except Exception as e:
            log.warning("VFX Lab apply failed: %s", e)
            return False


    def _buff_open_vfx_dialog(self) -> None:
        if self._buff_data is None:
            QMessageBox.information(self, "VFX Lab",
                "Click 'Extract' first to load iteminfo data.")
            return
        try:
            import vfx_lab
        except ImportError as e:
            QMessageBox.warning(self, "VFX Lab", f"Module load failed: {e}")
            return

        if not self._vfx_summaries:
            try:
                summaries, _raw = vfx_lab.parse_vfx_catalog(bytes(self._buff_data))
                try:
                    from armor_catalog import get_category, clean_display_name
                except Exception:
                    get_category = lambda n: None
                    clean_display_name = lambda n: n
                name_db = getattr(self, '_name_db', None)
                for s in summaries:
                    cat = get_category(s.internal_name) or "Other"
                    s.category = cat
                    disp = ''
                    if name_db:
                        try:
                            disp = name_db.get_name(s.item_key) or ''
                        except Exception:
                            disp = ''
                    s.display_name = disp or clean_display_name(s.internal_name)
                self._vfx_summaries = summaries
            except Exception as e:
                QMessageBox.warning(self, "VFX Lab", f"Parse failed: {e}")
                return

        self._vfx_show_dialog(vfx_lab)


    def _vfx_show_dialog(self, vfx_lab) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("VFX Lab — Size / VFX / Animations / Attach Points")
        dlg.resize(1100, 720)
        lay = QVBoxLayout(dlg)
        banner = QLabel(
            "Edit item visuals directly in iteminfo.pabgb. "
            "Size & VFX are safe; Animation and Attach can crash if sockets/rigs mismatch.")
        banner.setWordWrap(True)
        banner.setStyleSheet("background: #263238; color: #B0BEC5; padding: 6px; border-radius: 4px;")
        lay.addWidget(banner)

        tabs = QTabWidget()
        lay.addWidget(tabs, 1)

        summaries = self._vfx_summaries
        by_key = {s.item_key: s for s in summaries}

        owned_keys: set[int] = set()
        try:
            for rec in (self._buff_items or []):
                k = getattr(rec, 'key', None) or getattr(rec, 'item_key', None)
                if k is not None:
                    owned_keys.add(int(k))
        except Exception:
            pass

        local_size = list(self._vfx_size_changes)
        local_vfx = list(self._vfx_swaps)
        local_anim = list(self._vfx_anim_swaps)
        local_attach = list(self._vfx_attach_changes)

        tabs.addTab(self._vfx_build_size_tab(vfx_lab, summaries, by_key, owned_keys, local_size), "Size")
        tabs.addTab(self._vfx_build_vfx_tab(vfx_lab, summaries, by_key, owned_keys, local_vfx), "VFX & Trails")
        tabs.addTab(self._vfx_build_anim_tab(vfx_lab, summaries, by_key, owned_keys, local_anim), "Animations")
        tabs.addTab(self._vfx_build_attach_tab(vfx_lab, summaries, by_key, owned_keys, local_attach), "Attach Points")

        footer = QHBoxLayout()
        apply_btn = QPushButton("Apply All to Queue")
        apply_btn.setStyleSheet("background: #2E7D32; color: white; font-weight: bold; padding: 6px 14px;")
        clear_btn = QPushButton("Clear All VFX Changes")
        clear_btn.setStyleSheet("background: #6A1B1B; color: white; padding: 6px 14px;")
        import_btn = QPushButton("Import JSON…")
        export_btn = QPushButton("Export JSON…")
        close_btn = QPushButton("Close")
        for b in (apply_btn, clear_btn, import_btn, export_btn):
            footer.addWidget(b)
        footer.addStretch()
        footer.addWidget(close_btn)
        lay.addLayout(footer)

        def on_apply():
            self._vfx_size_changes = list(local_size)
            self._vfx_swaps = list(local_vfx)
            self._vfx_anim_swaps = list(local_anim)
            self._vfx_attach_changes = list(local_attach)
            total = len(local_size) + len(local_vfx) + len(local_anim) + len(local_attach)
            self._buff_modified = self._buff_modified or total > 0
            self._buff_status_label.setText(
                f"VFX Lab: {len(local_size)} size, {len(local_vfx)} vfx, "
                f"{len(local_anim)} anim, {len(local_attach)} attach queued. "
                "Click 'Export as Mod' when ready.")
            dlg.accept()

        def on_clear():
            if QMessageBox.question(dlg, "Clear VFX Lab",
                    "Remove all queued size / VFX / animation / attach changes?",
                    QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
                return
            local_size.clear(); local_vfx.clear(); local_anim.clear(); local_attach.clear()
            self._vfx_size_changes = []; self._vfx_swaps = []
            self._vfx_anim_swaps = []; self._vfx_attach_changes = []
            QMessageBox.information(dlg, "VFX Lab", "All queued changes cleared. Close and reopen to refresh lists.")

        def on_import():
            path, _ = QFileDialog.getOpenFileName(dlg, "Import VFX Lab JSON", "", "JSON (*.json)")
            if not path:
                return
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    text = f.read()
                s, v, a, at = vfx_lab.import_changes_from_json(text)
                local_size.extend(s); local_vfx.extend(v)
                local_anim.extend(a); local_attach.extend(at)
                QMessageBox.information(dlg, "Import",
                    f"Imported {len(s)} size, {len(v)} vfx, {len(a)} anim, {len(at)} attach entries.\n"
                    "Click Apply All to Queue, then Export as Mod.")
            except Exception as e:
                QMessageBox.warning(dlg, "Import failed", str(e))

        def on_export():
            if not (local_size or local_vfx or local_anim or local_attach):
                QMessageBox.information(dlg, "Export", "Nothing to export.")
                return
            path, _ = QFileDialog.getSaveFileName(dlg, "Export VFX Lab JSON", "vfx_lab.json", "JSON (*.json)")
            if not path:
                return
            try:
                text = vfx_lab.export_changes_to_json(
                    local_size, local_vfx, local_anim, local_attach, by_key)
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(text)
                QMessageBox.information(dlg, "Export", f"Wrote {path}")
            except Exception as e:
                QMessageBox.warning(dlg, "Export failed", str(e))

        apply_btn.clicked.connect(on_apply)
        clear_btn.clicked.connect(on_clear)
        import_btn.clicked.connect(on_import)
        export_btn.clicked.connect(on_export)
        close_btn.clicked.connect(dlg.reject)

        dlg.exec()


    def _vfx_build_item_filter_widgets(self, summaries, owned_keys, show_owned_toggle: bool):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(4, 4, 4, 4)
        ctl = QHBoxLayout()
        search = QLineEdit()
        search.setPlaceholderText("Search…")
        ctl.addWidget(search, 1)
        cat_combo = QComboBox()
        cats = sorted({s.category for s in summaries if s.category}) or ["Other"]
        cat_combo.addItem("(all)")
        for c in cats:
            cat_combo.addItem(c)
        ctl.addWidget(cat_combo)
        owned_cb = QCheckBox("Only items I own")
        owned_cb.setChecked(False)
        if show_owned_toggle:
            ctl.addWidget(owned_cb)
        lay.addLayout(ctl)
        lst = QListWidget()
        lay.addWidget(lst, 1)
        return w, lst, search, cat_combo, owned_cb


    def _vfx_filter_match(self, s, q: str, cat: str, owned_only: bool, owned_keys: set) -> bool:
        if owned_only and s.item_key not in owned_keys:
            return False
        if cat and cat != "(all)" and s.category != cat:
            return False
        if q:
            ql = q.lower()
            if ql not in s.internal_name.lower() and ql not in (s.display_name or '').lower():
                return False
        return True


    def _vfx_populate_list(self, lst, summaries, filter_fn, label_fn):
        lst.clear()
        for s in summaries:
            if not filter_fn(s):
                continue
            it = QListWidgetItem(label_fn(s))
            it.setData(Qt.UserRole, s.item_key)
            lst.addItem(it)


    def _vfx_build_size_tab(self, vfx_lab, summaries, by_key, owned_keys, queue):
        tab = QWidget()
        root = QHBoxLayout(tab)

        left_w, lst, search, cat_combo, owned_cb = self._vfx_build_item_filter_widgets(
            summaries, owned_keys, show_owned_toggle=True)
        root.addWidget(left_w, 2)

        right = QWidget()
        rlay = QVBoxLayout(right)
        cur_lbl = QLabel("Select an item on the left.")
        cur_lbl.setWordWrap(True)
        rlay.addWidget(cur_lbl)

        slider = QSlider(Qt.Horizontal)
        slider.setRange(10, 500)
        slider.setValue(100)
        slider_lbl = QLabel("Scale: 1.00×")
        rlay.addWidget(slider_lbl)
        rlay.addWidget(slider)

        uniform_cb = QCheckBox("Uniform (lock X/Y/Z)")
        uniform_cb.setChecked(True)
        rlay.addWidget(uniform_cb)

        queue_list = QListWidget()
        rlay.addWidget(QLabel("Queued size changes:"))
        rlay.addWidget(queue_list, 1)

        btn_row = QHBoxLayout()
        add_btn = QPushButton("Add / Update")
        add_btn.setStyleSheet("background: #2E7D32; color: white; font-weight: bold;")
        rm_btn = QPushButton("Remove Selected")
        btn_row.addWidget(add_btn); btn_row.addWidget(rm_btn)
        rlay.addLayout(btn_row)
        root.addWidget(right, 3)

        def refresh_list():
            q = search.text().strip()
            cat = cat_combo.currentText()
            owned = owned_cb.isChecked()
            self._vfx_populate_list(
                lst, summaries,
                lambda s: bool(s.scale) and self._vfx_filter_match(s, q, cat, owned, owned_keys),
                lambda s: f"{s.display_name or s.internal_name}  [{s.category}]  cur:{s.scale}")

        def refresh_queue():
            queue_list.clear()
            for ch in queue:
                s = by_key.get(ch.item_key)
                nm = (s.display_name or s.internal_name) if s else f"key={ch.item_key}"
                queue_list.addItem(f"{nm}  →  scale {ch.scale}")

        def on_select():
            it = lst.currentItem()
            if not it:
                cur_lbl.setText("Select an item on the left.")
                return
            s = by_key.get(it.data(Qt.UserRole))
            if not s:
                return
            cur_lbl.setText(f"{s.display_name or s.internal_name}\nCurrent scale: {s.scale}")
            ex = next((c for c in queue if c.item_key == s.item_key), None)
            if ex and ex.scale:
                slider.setValue(int(round(ex.scale[0] * 100)))
            else:
                slider.setValue(100)

        def on_slider(v):
            slider_lbl.setText(f"Scale: {v/100:.2f}×")

        def on_add():
            it = lst.currentItem()
            if not it:
                QMessageBox.information(tab, "Size", "Pick an item first.")
                return
            s = by_key.get(it.data(Qt.UserRole))
            if not s or not s.scale:
                return
            v = slider.value() / 100.0
            new_scale = [v] * len(s.scale)
            queue[:] = [c for c in queue if c.item_key != s.item_key]
            queue.append(vfx_lab.SizeChange(item_key=s.item_key, gv_index=0, scale=new_scale))
            refresh_queue()

        def on_remove():
            row = queue_list.currentRow()
            if 0 <= row < len(queue):
                del queue[row]
                refresh_queue()

        search.textChanged.connect(lambda _: refresh_list())
        cat_combo.currentTextChanged.connect(lambda _: refresh_list())
        owned_cb.stateChanged.connect(lambda _: refresh_list())
        lst.currentItemChanged.connect(lambda *_: on_select())
        slider.valueChanged.connect(on_slider)
        add_btn.clicked.connect(on_add)
        rm_btn.clicked.connect(on_remove)

        refresh_list()
        refresh_queue()
        return tab


    def _vfx_build_vfx_tab(self, vfx_lab, summaries, by_key, owned_keys, queue):
        tab = QWidget()
        root = QVBoxLayout(tab)
        hint = QLabel(
            "Copy a source item's VFX prefabs (trails, glows, particle systems) onto your target. "
            "Positions [1], [3], [4] are typically trails/auras; position [0] is the mesh "
            "(handled by Transmog).")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #90A4AE; padding: 4px;")
        root.addWidget(hint)

        split = QHBoxLayout()
        root.addLayout(split, 1)

        tgt_w, tgt_lst, tgt_search, tgt_cat, tgt_owned = self._vfx_build_item_filter_widgets(
            summaries, owned_keys, show_owned_toggle=True)
        tgt_owned.setChecked(True)
        ltw = QWidget(); llay = QVBoxLayout(ltw)
        llay.addWidget(QLabel("<b>YOUR EQUIPMENT (target)</b>"))
        llay.addWidget(tgt_w)
        split.addWidget(ltw, 2)

        src_w, src_lst, src_search, src_cat, src_owned = self._vfx_build_item_filter_widgets(
            summaries, owned_keys, show_owned_toggle=False)
        rtw = QWidget(); rlay = QVBoxLayout(rtw)
        rlay.addWidget(QLabel("<b>NEW VFX (source)</b>"))
        rlay.addWidget(src_w)
        split.addWidget(rtw, 2)

        pos_row = QHBoxLayout()
        pos_row.addWidget(QLabel("Prefab positions:"))
        pos_checks = []
        for i in (0, 1, 2, 3, 4, 5):
            cb = QCheckBox(f"[{i}]")
            cb.setChecked(i == 0)
            pos_checks.append((i, cb))
            pos_row.addWidget(cb)
        pos_row.addStretch()
        root.addLayout(pos_row)

        queue_list = QListWidget()
        root.addWidget(QLabel("Queued VFX swaps:"))
        root.addWidget(queue_list, 1)

        btn_row = QHBoxLayout()
        add_btn = QPushButton("Queue Swap")
        add_btn.setStyleSheet("background: #2E7D32; color: white; font-weight: bold;")
        rm_btn = QPushButton("Remove Selected")
        btn_row.addWidget(add_btn); btn_row.addWidget(rm_btn)
        btn_row.addStretch()
        root.addLayout(btn_row)

        def refresh_lists():
            for (q, cat, owned, lst) in (
                (tgt_search.text().strip(), tgt_cat.currentText(), tgt_owned.isChecked(), tgt_lst),
                (src_search.text().strip(), src_cat.currentText(), src_owned.isChecked(), src_lst)):
                self._vfx_populate_list(
                    lst, summaries,
                    lambda s, q=q, cat=cat, owned=owned: (
                        bool(s.prefab_names) and
                        self._vfx_filter_match(s, q, cat, owned, owned_keys)),
                    lambda s: f"{s.display_name or s.internal_name}  [{s.category}]  [{len(s.prefab_names)} prefab]")

        def refresh_queue():
            queue_list.clear()
            for sw in queue:
                tn = by_key.get(sw.tgt_key); sn = by_key.get(sw.src_key)
                tnm = (tn.display_name or tn.internal_name) if tn else sw.tgt_key
                snm = (sn.display_name or sn.internal_name) if sn else sw.src_key
                queue_list.addItem(f"{tnm}  ←  {snm}   pos={sw.positions}")

        def on_add():
            ti = tgt_lst.currentItem(); si = src_lst.currentItem()
            if not ti or not si:
                QMessageBox.information(tab, "VFX", "Select one item in each list.")
                return
            tgt_key = ti.data(Qt.UserRole); src_key = si.data(Qt.UserRole)
            if tgt_key == src_key:
                return
            positions = [i for (i, cb) in pos_checks if cb.isChecked()]
            if not positions:
                QMessageBox.information(tab, "VFX", "Check at least one position.")
                return
            queue[:] = [s for s in queue if s.tgt_key != tgt_key]
            queue.append(vfx_lab.VfxSwap(tgt_key=tgt_key, src_key=src_key, gv_index=0, positions=positions))
            refresh_queue()

        def on_remove():
            row = queue_list.currentRow()
            if 0 <= row < len(queue):
                del queue[row]
                refresh_queue()

        for s in (tgt_search, src_search):
            s.textChanged.connect(lambda _: refresh_lists())
        for c in (tgt_cat, src_cat):
            c.currentTextChanged.connect(lambda _: refresh_lists())
        for o in (tgt_owned, src_owned):
            o.stateChanged.connect(lambda _: refresh_lists())
        add_btn.clicked.connect(on_add)
        rm_btn.clicked.connect(on_remove)
        refresh_lists(); refresh_queue()
        return tab


    def _vfx_build_anim_tab(self, vfx_lab, summaries, by_key, owned_keys, queue):
        tab = QWidget()
        root = QVBoxLayout(tab)
        warn = QLabel(
            "⚠ EXPERIMENTAL — Animation swaps can t-pose items or crash the game "
            "if source and target aren't rig-compatible. Only 67 vanilla items have "
            "animation data (mostly recipe books). Test each swap in a throwaway save.")
        warn.setWordWrap(True)
        warn.setStyleSheet("background: #4E342E; color: #FFB74D; padding: 6px; border-radius: 4px;")
        root.addWidget(warn)

        split = QHBoxLayout(); root.addLayout(split, 1)

        tgt_w, tgt_lst, tgt_search, tgt_cat, tgt_owned = self._vfx_build_item_filter_widgets(
            summaries, owned_keys, show_owned_toggle=True)
        tgt_owned.setChecked(True)
        ltw = QWidget(); llay = QVBoxLayout(ltw)
        llay.addWidget(QLabel("<b>YOUR ITEM (target)</b>"))
        llay.addWidget(tgt_w)
        split.addWidget(ltw, 2)

        src_w, src_lst, src_search, src_cat, src_owned = self._vfx_build_item_filter_widgets(
            summaries, owned_keys, show_owned_toggle=False)
        rtw = QWidget(); rlay = QVBoxLayout(rtw)
        rlay.addWidget(QLabel("<b>SOURCE ANIMATIONS (items with anim data)</b>"))
        rlay.addWidget(src_w)
        split.addWidget(rtw, 2)

        queue_list = QListWidget()
        root.addWidget(QLabel("Queued animation swaps:"))
        root.addWidget(queue_list, 1)

        btn_row = QHBoxLayout()
        add_btn = QPushButton("Queue Animation Swap")
        add_btn.setStyleSheet("background: #F9A825; color: black; font-weight: bold;")
        rm_btn = QPushButton("Remove Selected")
        btn_row.addWidget(add_btn); btn_row.addWidget(rm_btn); btn_row.addStretch()
        root.addLayout(btn_row)

        def refresh_lists():
            self._vfx_populate_list(
                tgt_lst, summaries,
                lambda s: bool(s.prefab_names) and self._vfx_filter_match(
                    s, tgt_search.text().strip(), tgt_cat.currentText(), tgt_owned.isChecked(), owned_keys),
                lambda s: f"{s.display_name or s.internal_name}  [{s.category}]  anim:{len(s.animation_path_list)}")
            self._vfx_populate_list(
                src_lst, summaries,
                lambda s: bool(s.animation_path_list) and self._vfx_filter_match(
                    s, src_search.text().strip(), src_cat.currentText(), src_owned.isChecked(), owned_keys),
                lambda s: f"{s.display_name or s.internal_name}  [{s.category}]  anim:{s.animation_path_list}")

        def refresh_queue():
            queue_list.clear()
            for sw in queue:
                tn = by_key.get(sw.tgt_key); sn = by_key.get(sw.src_key)
                tnm = (tn.display_name or tn.internal_name) if tn else sw.tgt_key
                snm = (sn.display_name or sn.internal_name) if sn else sw.src_key
                queue_list.addItem(f"{tnm}  ←  {snm} (anim override)")

        def on_add():
            ti = tgt_lst.currentItem(); si = src_lst.currentItem()
            if not ti or not si:
                return
            tgt_key = ti.data(Qt.UserRole); src_key = si.data(Qt.UserRole)
            if tgt_key == src_key:
                return
            queue[:] = [s for s in queue if s.tgt_key != tgt_key]
            queue.append(vfx_lab.AnimSwap(tgt_key=tgt_key, src_key=src_key, gv_index=0))
            refresh_queue()

        def on_remove():
            row = queue_list.currentRow()
            if 0 <= row < len(queue):
                del queue[row]
                refresh_queue()

        for s in (tgt_search, src_search):
            s.textChanged.connect(lambda _: refresh_lists())
        for c in (tgt_cat, src_cat):
            c.currentTextChanged.connect(lambda _: refresh_lists())
        for o in (tgt_owned, src_owned):
            o.stateChanged.connect(lambda _: refresh_lists())
        add_btn.clicked.connect(on_add)
        rm_btn.clicked.connect(on_remove)
        refresh_lists(); refresh_queue()
        return tab


    def _vfx_build_attach_tab(self, vfx_lab, summaries, by_key, owned_keys, queue):
        tab = QWidget()
        root = QVBoxLayout(tab)
        warn = QLabel(
            "⚠ EXPERIMENTAL — Changes where an item attaches on your character. "
            "Only items with existing dock data can be edited here (261 of 6024). "
            "Unknown socket names render the item invisible — stick to the whitelist.")
        warn.setWordWrap(True)
        warn.setStyleSheet("background: #4E342E; color: #FFB74D; padding: 6px; border-radius: 4px;")
        root.addWidget(warn)

        split = QHBoxLayout(); root.addLayout(split, 2)

        left_w, lst, search, cat_combo, owned_cb = self._vfx_build_item_filter_widgets(
            summaries, owned_keys, show_owned_toggle=True)
        owned_cb.setChecked(True)
        split.addWidget(left_w, 2)

        right = QWidget(); rlay = QVBoxLayout(right)
        cur_lbl = QLabel("Select a dockable item on the left.")
        cur_lbl.setWordWrap(True)
        rlay.addWidget(cur_lbl)

        rlay.addWidget(QLabel("New parent socket:"))
        socket_combo = QComboBox()
        for label, name in vfx_lab.ATTACH_SOCKET_WHITELIST:
            socket_combo.addItem(f"{label}   ({name})", userData=name)
        socket_combo.addItem("— Custom… —", userData="__custom__")
        rlay.addWidget(socket_combo)

        custom_edit = QLineEdit()
        custom_edit.setPlaceholderText("Custom socket name (exact bone name, case-sensitive)")
        custom_edit.setEnabled(False)
        rlay.addWidget(custom_edit)

        queue_list = QListWidget()
        rlay.addWidget(QLabel("Queued attach changes:"))
        rlay.addWidget(queue_list, 1)

        btn_row = QHBoxLayout()
        add_btn = QPushButton("Queue Attach Change")
        add_btn.setStyleSheet("background: #F9A825; color: black; font-weight: bold;")
        rm_btn = QPushButton("Remove Selected")
        btn_row.addWidget(add_btn); btn_row.addWidget(rm_btn); btn_row.addStretch()
        rlay.addLayout(btn_row)
        split.addWidget(right, 3)

        def refresh_list():
            q = search.text().strip()
            cat = cat_combo.currentText()
            owned = owned_cb.isChecked()
            self._vfx_populate_list(
                lst, summaries,
                lambda s: s.has_dock and self._vfx_filter_match(s, q, cat, owned, owned_keys),
                lambda s: f"{s.display_name or s.internal_name}  [{s.category}]  ← {s.dock_parent_socket}")

        def refresh_queue():
            queue_list.clear()
            for ac in queue:
                s = by_key.get(ac.item_key)
                nm = (s.display_name or s.internal_name) if s else f"key={ac.item_key}"
                queue_list.addItem(f"{nm}  →  {ac.new_parent_socket}")

        def on_select():
            it = lst.currentItem()
            if not it:
                cur_lbl.setText("Select a dockable item on the left.")
                return
            s = by_key.get(it.data(Qt.UserRole))
            if not s:
                return
            cur_lbl.setText(
                f"{s.display_name or s.internal_name}\n"
                f"Current parent socket: {s.dock_parent_socket or '(none)'}\n"
                f"Child socket: {s.dock_child_socket or '(none)'}")

        def on_combo(_):
            is_custom = socket_combo.currentData() == "__custom__"
            custom_edit.setEnabled(is_custom)

        def on_add():
            it = lst.currentItem()
            if not it:
                return
            s = by_key.get(it.data(Qt.UserRole))
            if not s:
                return
            chosen = socket_combo.currentData()
            if chosen == "__custom__":
                name = custom_edit.text().strip()
                if not name:
                    QMessageBox.information(tab, "Attach", "Enter a custom socket name.")
                    return
            else:
                name = chosen
            queue[:] = [a for a in queue if a.item_key != s.item_key]
            queue.append(vfx_lab.AttachChange(item_key=s.item_key, new_parent_socket=name))
            refresh_queue()

        def on_remove():
            row = queue_list.currentRow()
            if 0 <= row < len(queue):
                del queue[row]
                refresh_queue()

        search.textChanged.connect(lambda _: refresh_list())
        cat_combo.currentTextChanged.connect(lambda _: refresh_list())
        owned_cb.stateChanged.connect(lambda _: refresh_list())
        lst.currentItemChanged.connect(lambda *_: on_select())
        socket_combo.currentIndexChanged.connect(on_combo)
        add_btn.clicked.connect(on_add)
        rm_btn.clicked.connect(on_remove)
        refresh_list(); refresh_queue()
        return tab


    def _buff_open_transmog_dialog(self) -> None:
        if not self._armor_catalog:
            if self._buff_data is None:
                QMessageBox.information(self, "Transmog",
                    "Click 'Extract' first to load iteminfo data.")
                return
            try:
                from armor_catalog import parse_armor_items
                self._armor_catalog = parse_armor_items(bytes(self._buff_data))
            except Exception as e:
                QMessageBox.critical(self, "Transmog", f"Armor catalog build failed: {e}")
                return
            if not self._armor_catalog:
                QMessageBox.warning(self, "Transmog", "No armor items found in iteminfo.")
                return

        from PySide6.QtWidgets import (
            QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
            QComboBox, QLineEdit, QListWidget, QListWidgetItem, QFileDialog,
            QSplitter,
        )

        dlg = QDialog(self)
        dlg.setWindowTitle("Transmog / Visual Swap")
        dlg.resize(1000, 680)
        dl = QVBoxLayout(dlg)

        header = QLabel(
            "Make YOUR armor look like another armor.\n"
            "Pick a piece you own on the LEFT, then pick the look you want from the RIGHT.\n"
            "Your stats/buffs/enchants are kept — only the visual model and textures change."
        )
        header.setWordWrap(True)
        header.setStyleSheet(f"color: {COLORS['text_dim']}; padding: 4px;")
        dl.addWidget(header)

        filt_row = QHBoxLayout()
        filt_row.addWidget(QLabel("Category (both lists):"))
        cat_combo = QComboBox()
        cat_combo.addItems([
            "All",
            "Chest", "Gloves", "Boots", "Helm", "Cloak", "Shoulder",
            "OneHand Sword", "TwoHand Sword", "Dual Sword",
            "Dual Daggers", "TwoHand Axe", "Dual Axe",
            "Hammer", "Spear", "Bow",
            "Shield", "Bracer", "Lantern", "Torch",
            "Necklace", "Earring", "Ring", "Belt", "Trinket",
            "Other",
        ])
        filt_row.addWidget(cat_combo)
        only_owned_cb = QCheckBox("Only show items I own (left list)")
        only_owned_cb.setChecked(True)
        only_owned_cb.setToolTip(
            "Left list: filter to equipment you currently own.\n"
            "The right list always shows all items — you can use any look you want.")
        filt_row.addWidget(only_owned_cb)
        filt_row.addStretch(1)
        dl.addLayout(filt_row)

        # ── Quick category filter buttons (matches Mesh Swap pattern) ──
        from gui.utils import FlowLayout as _TFL
        quick_w = QWidget()
        quick_row = _TFL(quick_w, margin=0, h_spacing=4, v_spacing=4)
        ql = QLabel("Quick filter:")
        ql.setStyleSheet(f"color: {COLORS['accent']}; font-weight: bold;")
        quick_row.addWidget(ql)

        QUICK_FILTERS = [
            ("⛑️ Helm", "Helm"),
            ("🛡️ Chest", "Chest"),
            ("🧤 Gloves", "Gloves"),
            ("👢 Boots", "Boots"),
            ("🧥 Cloak", "Cloak"),
            ("🗡️ 1H Sword", "OneHand Sword"),
            ("⚔️ 2H Sword", "TwoHand Sword"),
            ("🗡️🗡️ Dual Sword", "Dual Sword"),
            ("🔪 Dual Daggers", "Dual Daggers"),
            ("🪓 2H Axe", "TwoHand Axe"),
            ("🪓🪓 Dual Axe", "Dual Axe"),
            ("🔨 Hammer", "Hammer"),
            ("🔱 Spear", "Spear"),
            ("🏹 Bow", "Bow"),
            ("🛡 Shield", "Shield"),
            ("🏮 Lantern", "Lantern"),
            ("🔥 Torch", "Torch"),
            ("📿 Necklace", "Necklace"),
            ("✨ Earring", "Earring"),
            ("💍 Ring", "Ring"),
            ("🔁 All", "All"),
        ]
        for label, cat_name in QUICK_FILTERS:
            b = QPushButton(label)
            b.setToolTip(f"Filter both lists to: {cat_name}")
            b.setStyleSheet("padding: 4px 8px;")
            def _make_setter(c):
                return lambda: cat_combo.setCurrentText(c) if cat_combo.findText(c) >= 0 else None
            b.clicked.connect(_make_setter(cat_name))
            quick_row.addWidget(b)
        dl.addWidget(quick_w)

        splitter = QSplitter(Qt.Horizontal)

        tgt_panel = QWidget()
        tgt_l = QVBoxLayout(tgt_panel)
        tgt_l.setContentsMargins(2, 2, 2, 2)
        tgt_l.addWidget(QLabel("YOUR EQUIPMENT — pick the piece to re-skin:"))
        tgt_search = QLineEdit()
        tgt_search.setPlaceholderText("Search your items...")
        tgt_l.addWidget(tgt_search)
        tgt_list = QListWidget()
        tgt_list.setIconSize(QSize(32, 32))
        tgt_l.addWidget(tgt_list, 1)
        splitter.addWidget(tgt_panel)

        src_panel = QWidget()
        src_l = QVBoxLayout(src_panel)
        src_l.setContentsMargins(2, 2, 2, 2)
        src_l.addWidget(QLabel("NEW LOOK — pick the item whose look you want:"))
        src_search = QLineEdit()
        src_search.setPlaceholderText("Search all items...")
        src_l.addWidget(src_search)
        src_list = QListWidget()
        src_list.setIconSize(QSize(32, 32))
        src_l.addWidget(src_list, 1)
        splitter.addWidget(src_panel)

        splitter.setSizes([500, 500])
        dl.addWidget(splitter, 1)

        action_row = QHBoxLayout()
        add_btn = QPushButton("Add Swap")
        add_btn.setObjectName("accentBtn")
        add_btn.setToolTip("Add the selected Target+Source pair to the swap queue")
        action_row.addWidget(add_btn)
        remove_btn = QPushButton("Remove Selected")
        action_row.addWidget(remove_btn)
        clear_btn = QPushButton("Clear All")
        action_row.addWidget(clear_btn)
        action_row.addStretch(1)
        import_btn = QPushButton("Import Config")
        import_btn.setToolTip("Load queued swaps from a JSON file")
        action_row.addWidget(import_btn)
        export_btn = QPushButton("Export Config")
        export_btn.setToolTip("Save queued swaps to a JSON file for sharing")
        action_row.addWidget(export_btn)
        dl.addLayout(action_row)

        dl.addWidget(QLabel("Queued swaps (applied on Export as Mod / Apply to Game):"))
        queue_list = QListWidget()
        queue_list.setMaximumHeight(140)
        dl.addWidget(queue_list)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        ok_btn = QPushButton("Save & Close")
        ok_btn.setObjectName("accentBtn")
        btn_row.addWidget(ok_btn)
        cancel_btn = QPushButton("Cancel")
        btn_row.addWidget(cancel_btn)
        dl.addLayout(btn_row)

        local_swaps = list(self._transmog_swaps)

        owned_keys: set = set()
        try:
            for it in getattr(self, '_items', []) or []:
                if hasattr(it, 'item_key'):
                    owned_keys.add(it.item_key)
        except Exception:
            pass
        owned_count_label = QLabel(f"({len(owned_keys)} owned items detected)")
        owned_count_label.setStyleSheet(f"color: {COLORS['text_dim']}; font-size: 10px;")
        filt_row.addWidget(owned_count_label)

        def refresh_queue():
            queue_list.clear()
            for sw in local_swaps:
                src = sw['src']; tgt = sw['tgt']
                queue_list.addItem(f"{tgt.display_name} ({tgt.category})  →  now looks like  →  "
                                   f"{src.display_name} ({src.category})")

        def matches(a, cat, q):
            if cat != "All" and a.category != cat:
                return False
            if q:
                ql = q.lower()
                if ql not in a.display_name.lower() and ql not in a.internal_name.lower():
                    return False
            return True

        def _add_row(lst, a):
            label = f"[{a.category[:8]}] {a.display_name}"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, a.item_id)
            if self._icon_cache:
                px = self._icon_cache.get_pixmap(a.item_id)
                if px:
                    item.setIcon(QIcon(px))
                elif self._icon_cache.has_icon(a.item_id):
                    self._icon_cache.request_icon(a.item_id, lambda *_: None)
            lst.addItem(item)

        def populate_target():
            prev_key = tgt_list.currentItem().data(Qt.UserRole) if tgt_list.currentItem() else None
            cat = cat_combo.currentText()
            q = tgt_search.text().strip()
            only_owned = only_owned_cb.isChecked()
            tgt_list.clear()
            restored_row = -1
            for a in self._armor_catalog:
                if not matches(a, cat, q):
                    continue
                if only_owned and owned_keys and a.item_id not in owned_keys:
                    continue
                _add_row(tgt_list, a)
                if a.item_id == prev_key:
                    restored_row = tgt_list.count() - 1
            if restored_row >= 0:
                tgt_list.setCurrentRow(restored_row)

        INVISIBLE_SENTINEL_KEY = -9999
        from armor_catalog import ArmorItem as _ArmorItem
        invisible_template = _ArmorItem(
            item_id=INVISIBLE_SENTINEL_KEY,
            internal_name='__INVISIBLE_ZERO__',
            display_name='Invisible Model',
            category='Invisible',
            hashes=[],
        )

        invisible_named_items = [
            a for a in self._armor_catalog if a.item_id == 1000491
        ]

        def populate_source():
            prev_key = src_list.currentItem().data(Qt.UserRole) if src_list.currentItem() else None
            q = src_search.text().strip()
            cat = cat_combo.currentText()
            src_list.clear()
            restored_row = -1

            show_invis = (not q or 'invis' in q.lower() or 'empty' in q.lower() or 'none' in q.lower() or 'ghost' in q.lower())
            if show_invis:
                for inv in invisible_named_items:
                    lbl = f"★ Invisible (Ghost_TwohandSword) — universal invisible"
                    it = QListWidgetItem(lbl)
                    it.setData(Qt.UserRole, inv.item_id)
                    it.setForeground(QBrush(QColor("#FFD700")))
                    src_list.addItem(it)
                    if prev_key == inv.item_id:
                        restored_row = src_list.count() - 1

            pinned_ids = {inv.item_id for inv in invisible_named_items} if show_invis else set()
            for a in self._armor_catalog:
                if a.item_id in pinned_ids:
                    continue
                if not matches(a, cat, q):
                    continue
                _add_row(src_list, a)
                if a.item_id == prev_key:
                    restored_row = src_list.count() - 1
            if restored_row >= 0:
                src_list.setCurrentRow(restored_row)

        def populate_both():
            populate_target()
            populate_source()

        cat_combo.currentTextChanged.connect(lambda _: populate_both())
        tgt_search.textChanged.connect(lambda _: populate_target())
        src_search.textChanged.connect(lambda _: populate_source())
        only_owned_cb.stateChanged.connect(lambda _: populate_target())
        populate_both()
        refresh_queue()

        def on_add():
            ti = tgt_list.currentItem()
            si = src_list.currentItem()
            if not ti or not si:
                QMessageBox.information(dlg, "Transmog",
                    "Pick ONE item in each list:\n"
                    "  Left  = your armor (the piece you want to re-skin)\n"
                    "  Right = the look you want it to have")
                return
            tgt_key = ti.data(Qt.UserRole)
            src_key = si.data(Qt.UserRole)
            if tgt_key == src_key:
                QMessageBox.information(dlg, "Transmog",
                    "Your armor and the new look must be different items.")
                return
            tgt = next((a for a in self._armor_catalog if a.item_id == tgt_key), None)
            if src_key == INVISIBLE_SENTINEL_KEY:
                src = invisible_template
                if src is None:
                    QMessageBox.warning(dlg, "Transmog",
                        "No Invisible Model template available in this iteminfo.")
                    return
                if not tgt:
                    return
                local_swaps[:] = [s for s in local_swaps if s['tgt'].item_id != tgt_key]
                import copy
                fake_src = copy.copy(src)
                fake_src.display_name = "Invisible Model"
                fake_src.category = "Invisible"
                local_swaps.append({'src': fake_src, 'tgt': tgt})
                refresh_queue()
                return
            src = next((a for a in self._armor_catalog if a.item_id == src_key), None)
            if not tgt or not src:
                return
            local_swaps[:] = [s for s in local_swaps if s['tgt'].item_id != tgt_key]
            local_swaps.append({'src': src, 'tgt': tgt})
            refresh_queue()

        def on_remove():
            row = queue_list.currentRow()
            if 0 <= row < len(local_swaps):
                del local_swaps[row]
                refresh_queue()

        def on_clear():
            local_swaps.clear()
            refresh_queue()

        def on_export():
            if not local_swaps:
                QMessageBox.information(dlg, "Export", "No swaps queued.")
                return
            path, _ = QFileDialog.getSaveFileName(
                dlg, "Export Transmog Config", "transmog_config.json", "JSON (*.json)")
            if not path:
                return
            out = {
                'version': 1,
                'swaps': [
                    {
                        'target_key': s['tgt'].item_id,
                        'target_name': s['tgt'].internal_name,
                        'source_key': s['src'].item_id,
                        'source_name': s['src'].internal_name,
                    }
                    for s in local_swaps
                ],
            }
            try:
                with open(path, 'w', encoding='utf-8') as f:
                    json.dump(out, f, indent=2)
                QMessageBox.information(dlg, "Export", f"Wrote {len(local_swaps)} swap(s) to:\n{path}")
            except Exception as e:
                QMessageBox.critical(dlg, "Export Failed", str(e))

        def on_import():
            path, _ = QFileDialog.getOpenFileName(
                dlg, "Import Transmog Config", "", "JSON (*.json)")
            if not path:
                return
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    cfg = json.load(f)
                by_key = {a.item_id: a for a in self._armor_catalog}
                by_name = {a.internal_name: a for a in self._armor_catalog}
                added = 0
                missed = 0

                if isinstance(cfg.get('swaps'), list):
                    for s in cfg['swaps']:
                        tgt = by_key.get(s.get('target_key'))
                        src = by_key.get(s.get('source_key'))
                        if (not tgt or not src) and s.get('target_name') and s.get('source_name'):
                            tgt = tgt or by_name.get(s['target_name'])
                            src = src or by_name.get(s['source_name'])
                        if not tgt or not src:
                            missed += 1
                            log.warning("Transmog import: could not resolve target_key=%s source_key=%s",
                                        s.get('target_key'), s.get('source_key'))
                            continue
                        log.info("Transmog import: queued tgt=%s (key %s) <- src=%s (key %s)",
                                 tgt.internal_name, tgt.item_id, src.internal_name, src.item_id)
                        local_swaps[:] = [x for x in local_swaps if x['tgt'].item_id != tgt.item_id]
                        local_swaps.append({'src': src, 'tgt': tgt})
                        added += 1

                elif isinstance(cfg.get('patches'), list):
                    seen_pairs = set()
                    for patch in cfg['patches']:
                        for change in patch.get('changes', []):
                            label = change.get('label', '')
                            if ' -> ' not in label:
                                continue
                            tgt_name, src_name = label.split(' -> ', 1)
                            tgt_name, src_name = tgt_name.strip(), src_name.strip()
                            pair_key = (tgt_name, src_name)
                            if pair_key in seen_pairs:
                                continue
                            seen_pairs.add(pair_key)
                            src = by_name.get(src_name)
                            tgt = by_name.get(tgt_name)
                            if not src or not tgt:
                                missed += 1
                                continue
                            local_swaps[:] = [x for x in local_swaps if x['tgt'].item_id != tgt.item_id]
                            local_swaps.append({'src': src, 'tgt': tgt})
                            added += 1
                else:
                    QMessageBox.warning(dlg, "Import",
                        "Unrecognized JSON format. Expected either our 'swaps' format "
                        "or HexeMarie's 'patches' format.")
                    return

                refresh_queue()
                QMessageBox.information(dlg, "Import",
                    f"Imported {added} swap(s). {missed} skipped (items not found).")
            except Exception as e:
                QMessageBox.critical(dlg, "Import Failed", str(e))

        add_btn.clicked.connect(on_add)
        remove_btn.clicked.connect(on_remove)
        clear_btn.clicked.connect(on_clear)
        export_btn.clicked.connect(on_export)
        import_btn.clicked.connect(on_import)

        def on_ok():
            self._transmog_swaps = list(local_swaps)
            self._buff_modified = self._buff_modified or bool(local_swaps)
            count = len(local_swaps)
            self._buff_status_label.setText(
                f"Transmog queue: {count} swap(s). Applied on Export as Mod / Apply to Game.")
            dlg.accept()

        ok_btn.clicked.connect(on_ok)
        cancel_btn.clicked.connect(dlg.reject)
        dlg.exec()


    def _buff_preview_item(self) -> None:
        if not hasattr(self, '_buff_rust_items') or not self._buff_rust_items:
            QMessageBox.warning(self, "Preview", "Extract with Rust parser first.")
            return
        if not hasattr(self, '_buff_current_item') or self._buff_current_item is None:
            QMessageBox.warning(self, "Preview", "Select an item first.")
            return

        item = self._buff_current_item
        rust_info = self._buff_rust_lookup.get(item.item_key)
        if not rust_info:
            QMessageBox.warning(self, "Preview", "No Rust data for this item.")
            return

        display_name = self._name_db.get_name(item.item_key)
        if display_name.startswith("Unknown"):
            display_name = item.name

        equip_type = rust_info.get('equip_type_info', 0)
        category = rust_info.get('category_info', 0)
        tier = rust_info.get('item_tier', 0)
        tier_names = {0: "", 1: "Common", 2: "Uncommon", 3: "Rare", 4: "Epic", 5: "Legendary"}
        tier_colors = {0: "#AAAAAA", 1: "#AAAAAA", 2: "#4FC3F7", 3: "#81C784",
                       4: "#CE93D8", 5: "#FFB74D"}
        tier_name = tier_names.get(tier, "")
        tier_color = tier_colors.get(tier, "#AAAAAA")

        limits = self._buff_item_limits.get(str(item.item_key), {})
        slot_type = limits.get('slotType', -1)
        if slot_type == 65535 or slot_type == -1:
            type_str = "Item"
        elif slot_type <= 3:
            type_str = "Weapon"
        elif slot_type <= 9:
            type_str = "Armor"
        else:
            type_str = "Equipment"

        display_level = 0
        if hasattr(self, '_eb_level_target'):
            sel = self._eb_level_target.currentData()
            edl = rust_info.get('enchant_data_list', [])
            if sel is not None and sel >= 0 and sel < len(edl):
                display_level = sel

        _PREVIEW_STAT_NAMES = {
            1000000: "Max HP", 1000001: "Fatal", 1000002: "Attack",
            1000003: "Defense", 1000004: "Accuracy",
            1000005: "Base Attack", 1000006: "Critical Damage",
            1000007: "Critical Rate", 1000008: "Incoming Dmg Rate",
            1000009: "Incoming Dmg Reduction", 1000010: "Attack Speed",
            1000011: "Movement Speed", 1000012: "Climb Speed",
            1000013: "Swim Speed", 1000016: "Fire Resistance",
            1000017: "Ice Resistance", 1000018: "Lightning Resistance",
            1000024: "Fire Resist", 1000025: "Ice Resist",
            1000026: "Stamina Regen", 1000027: "MP Regen",
            1000031: "Hit Rate", 1000035: "Max Damage Rate",
            1000036: "Pressure", 1000037: "Stamina Cost Reduction",
            1000043: "Guard PV Rate", 1000046: "MP Cost Reduction",
            1000047: "Money Drop Rate", 1000049: "Equip Drop Rate",
            1000050: "DPV Rate",
        }

        html_parts = []
        html_parts.append(f"""
        <div style="
            background-color: #1a1a2e;
            border: 2px solid #3a3a5c;
            border-radius: 8px;
            padding: 16px;
            min-width: 320px;
            max-width: 420px;
            font-family: 'Segoe UI', Arial, sans-serif;
        ">
        """)

        html_parts.append(f"""
            <div style="font-size: 18px; font-weight: bold; color: {tier_color};
                        margin-bottom: 2px;">{display_name}</div>
            <div style="font-size: 12px; color: #8888aa; margin-bottom: 10px;">
                {tier_name + ' | ' if tier_name else ''}{type_str}</div>
        """)

        edl = rust_info.get('enchant_data_list', [])
        if edl and display_level < len(edl):
            ed = edl[display_level]
            sd = ed.get('enchant_stat_data', {})

            _RATE_PCT_HASHES = {
                1000008,
                1000009,
            }
            flat_stats = sd.get('stat_list_static', [])
            for s in flat_stats:
                sname = _PREVIEW_STAT_NAMES.get(s['stat'], f"Stat {s['stat']}")
                raw = s['change_mb']
                if s['stat'] in _RATE_PCT_HASHES:
                    pct = raw / 10_000_000
                    disp_str = f"{pct:.4f}".rstrip('0').rstrip('.') + '%'
                    icon = "\U0001f4ca"
                    color = "#CE93D8"
                else:
                    display_val = raw / 1000
                    disp_str = f"{display_val:,.0f}" if display_val == int(display_val) else f"{display_val:,.1f}"
                    color = "#ffffff"
                    if s['stat'] == 1000002:
                        icon = "\u2694\ufe0f"
                    elif s['stat'] == 1000003:
                        icon = "\U0001f6e1\ufe0f"
                    elif s['stat'] == 1000000:
                        icon = "\u2764\ufe0f"
                    else:
                        icon = "\u2b50"
                html_parts.append(f"""
                    <div style="font-size: 14px; color: #e0e0e0; padding: 2px 0;">
                        <span style="color: #FFB74D;">{icon}</span>
                        <span style="color: #e0e0e0;">{sname}</span>
                        <span style="float: right; color: {color}; font-weight: bold;">{disp_str}</span>
                    </div>
                """)

            rate_stats = sd.get('stat_list_static_level', [])
            for s in rate_stats:
                sname = _PREVIEW_STAT_NAMES.get(s['stat'], f"Stat {s['stat']}")
                val = s['change_mb']
                html_parts.append(f"""
                    <div style="font-size: 14px; color: #e0e0e0; padding: 2px 0;">
                        <span style="color: #81C784;">\u26a1</span>
                        <span style="color: #e0e0e0;">{sname}</span>
                        <span style="float: right; color: #81C784;">Lv {val}</span>
                    </div>
                """)

            regen_stats = sd.get('regen_stat_list', [])
            for s in regen_stats:
                sname = _PREVIEW_STAT_NAMES.get(s['stat'], f"Regen {s['stat']}")
                raw = s['change_mb']
                display_val = raw / 1000
                disp_str = f"{display_val:,.0f}" if display_val == int(display_val) else f"{display_val:,.1f}"
                html_parts.append(f"""
                    <div style="font-size: 14px; color: #e0e0e0; padding: 2px 0;">
                        <span style="color: #4FC3F7;">\u267b\ufe0f</span>
                        <span style="color: #e0e0e0;">{sname}</span>
                        <span style="float: right; color: #4FC3F7;">{disp_str}</span>
                    </div>
                """)

        sharp = rust_info.get('sharpness_data', {})
        max_sharp = sharp.get('max_sharpness', 0)
        if max_sharp > 0:
            bars = "\u2588" * max_sharp
            html_parts.append(f"""
                <div style="font-size: 14px; color: #e0e0e0; padding: 2px 0;">
                    <span style="color: #FFB74D;">\u2728</span>
                    <span style="color: #e0e0e0;">Refinement</span>
                    <span style="float: right; color: #FFB74D; letter-spacing: 1px;">{bars}</span>
                </div>
            """)

        html_parts.append('<hr style="border: 1px solid #3a3a5c; margin: 8px 0;">')

        psl = rust_info.get('equip_passive_skill_list', [])
        if psl:
            for ps in psl:
                sk_name = self._PASSIVE_SKILL_NAMES.get(ps['skill'], f"Skill {ps['skill']}")
                level_str = f"Lv {ps['level']}" if ps['level'] > 1 else ""
                html_parts.append(f"""
                    <div style="font-size: 13px; color: #66BB6A; padding: 2px 0;">
                        <span style="color: #66BB6A;">\u2618</span>
                        {sk_name} {level_str}
                    </div>
                """)
            html_parts.append('<hr style="border: 1px solid #3a3a5c; margin: 8px 0;">')

        if edl and display_level < len(edl):
            buffs = edl[display_level].get('equip_buffs', [])
            if buffs:
                for b in buffs:
                    bname = self._EQUIP_BUFF_NAMES.get(b['buff'], f"Buff {b['buff']}")
                    blvl = b['level']
                    html_parts.append(f"""
                        <div style="font-size: 13px; color: #FFD54F; padding: 2px 0;">
                            <span style="color: #FFD54F;">\u25c9</span>
                            {bname}
                            <span style="color: #FFD54F; float: right;">Lv {blvl}</span>
                        </div>
                    """)

        if edl and len(edl) > 1:
            html_parts.append(f"""
                <div style="font-size: 11px; color: #666688; margin-top: 8px; text-align: center;">
                    Showing enchant level +{display_level} of {len(edl) - 1}
                </div>
            """)

        html_parts.append("</div>")

        dlg = QDialog(self)
        dlg.setWindowTitle(f"Item Preview: {display_name}")
        dlg.setWindowFlags(dlg.windowFlags() | Qt.WindowStaysOnTopHint)
        dl = QVBoxLayout(dlg)
        dl.setContentsMargins(8, 8, 8, 8)

        content_row = QHBoxLayout()

        icon_label = QLabel()
        px = self._icon_cache.get_pixmap(item.item_key)
        if px and not px.isNull():
            icon_label.setPixmap(px.scaled(96, 96, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        else:
            icon_label.setText("No icon")
            icon_label.setStyleSheet("color: #666; font-size: 12px;")
        icon_label.setFixedSize(100, 100)
        icon_label.setAlignment(Qt.AlignCenter)
        icon_label.setStyleSheet(
            "background-color: #0d0d1a; border: 2px solid #3a3a5c; border-radius: 8px;"
        )
        content_row.addWidget(icon_label)

        tooltip_label = QLabel()
        tooltip_label.setTextFormat(Qt.RichText)
        tooltip_label.setWordWrap(True)
        tooltip_label.setText("".join(html_parts))
        tooltip_label.setStyleSheet("background: transparent;")
        content_row.addWidget(tooltip_label, 1)

        dl.addLayout(content_row)

        if edl and len(edl) > 1:
            level_switch = QHBoxLayout()
            level_switch.addWidget(QLabel("Preview enchant level:"))
            level_combo = QComboBox()
            for i in range(len(edl)):
                level_combo.addItem(f"+{i}", i)
            level_combo.setCurrentIndex(display_level)

            def _switch_level(idx):
                nonlocal display_level
                display_level = idx
                dlg.close()
                old_idx = self._eb_level_target.currentIndex()
                self._eb_level_target.setCurrentIndex(idx + 1)
                self._buff_preview_item()
                self._eb_level_target.setCurrentIndex(old_idx)

            level_combo.currentIndexChanged.connect(_switch_level)
            level_switch.addWidget(level_combo)
            level_switch.addStretch()
            dl.addLayout(level_switch)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dlg.close)
        dl.addWidget(close_btn)

        dlg.setStyleSheet(f"""
            QDialog {{
                background-color: #0d0d1a;
            }}
            QLabel {{
                color: #e0e0e0;
            }}
            QComboBox {{
                background-color: #1a1a2e;
                color: #e0e0e0;
                border: 1px solid #3a3a5c;
                padding: 4px;
            }}
            QPushButton {{
                background-color: #2a2a4e;
                color: #e0e0e0;
                border: 1px solid #3a3a5c;
                padding: 6px 16px;
                border-radius: 4px;
            }}
            QPushButton:hover {{
                background-color: #3a3a6e;
            }}
        """)

        dlg.adjustSize()
        dlg.exec()


    def _buff_stats_context_menu(self, pos) -> None:
        if not hasattr(self, '_buff_rust_items') or not self._buff_rust_items:
            return
        if not hasattr(self, '_buff_current_item') or self._buff_current_item is None:
            return

        table = self._buff_stats_table
        item = table.itemAt(pos)
        if not item:
            return

        row = item.row()
        name_cell = table.item(row, 0)
        if not name_cell:
            return

        kind_data = name_cell.data(Qt.UserRole + 1)
        if not kind_data:
            return

        from PySide6.QtWidgets import QMenu
        menu = QMenu(table)
        rust_info = self._buff_rust_lookup.get(self._buff_current_item.item_key)
        if not rust_info:
            return

        kind = kind_data[0]

        if kind == 'passive':
            skill_id = kind_data[1]
            name = self._PASSIVE_SKILL_NAMES.get(skill_id, f"Skill {skill_id}")
            if isinstance(name, dict):
                name = name.get('suffix', name.get('english_name', str(skill_id)))
            act_remove = menu.addAction(f"Remove passive: {name}")
            act_remove_all = menu.addAction("Remove ALL passives")
            act_show_similar = menu.addAction("Show items with this passive")
            psl = rust_info.get('equip_passive_skill_list', []) or []

            action = menu.exec(table.viewport().mapToGlobal(pos))
            if action == act_remove:
                rust_info['equip_passive_skill_list'] = [p for p in psl if p['skill'] != skill_id]
                self._buff_modified = True
                self._buff_refresh_stats()
                self._buff_status_label.setText(f"Removed passive {name}")
            elif action == act_remove_all:
                rust_info['equip_passive_skill_list'] = []
                self._buff_modified = True
                self._buff_refresh_stats()
                self._buff_status_label.setText("Removed all passives")
            elif action == act_show_similar:
                if psl:
                    probe = dict(rust_info)
                    probe['equip_passive_skill_list'] = [{"skill": skill_id, "level": 0}]
                    self._show_similar_items(probe, "passives")

        elif kind == 'buff':
            buff_id = kind_data[1]
            name = self._EQUIP_BUFF_NAMES.get(buff_id, f"Buff {buff_id}")
            act_remove = menu.addAction(f"Remove buff: {name}")
            act_remove_all = menu.addAction("Remove ALL buffs")
            act_show_similar = menu.addAction("Show items with this buff")

            action = menu.exec(table.viewport().mapToGlobal(pos))
            edl = rust_info.get('enchant_data_list', [])
            if action == act_remove:
                removed_levels = 0
                for ed in edl:
                    old = ed.get('equip_buffs', []) or []
                    new = [b for b in old if b['buff'] != buff_id]
                    if len(new) < len(old):
                        ed['equip_buffs'] = new
                        removed_levels += 1
                self._buff_modified = True
                self._buff_refresh_stats()
                self._buff_status_label.setText(f"Removed buff {name} ({removed_levels} levels)")
            elif action == act_show_similar:
                if edl:
                    probe = dict(rust_info)
                    probe['enchant_data_list'] = [{"equip_buffs": [{"buff": buff_id}]}]
                    self._show_similar_items(probe, "buffs")
            elif action == act_remove_all:
                for ed in edl:
                    ed['equip_buffs'] = []
                self._buff_modified = True
                self._buff_refresh_stats()
                self._buff_status_label.setText("Removed all buffs")

        elif kind == 'stat':
            stat_key = kind_data[1]
            list_name = kind_data[2]
            _STAT_NAMES = {
                1000000: "HP", 1000002: "DDD", 1000003: "DPV",
                1000007: "Crit Rate", 1000010: "Attack Speed",
                1000011: "Move Speed",
            }
            name = _STAT_NAMES.get(stat_key, f"Stat {stat_key}")
            act_remove = menu.addAction(f"Remove stat: {name}")

            action = menu.exec(table.viewport().mapToGlobal(pos))
            if action == act_remove:
                edl = rust_info.get('enchant_data_list', [])
                removed = 0
                for ed in edl:
                    sd = ed.get('enchant_stat_data', {})
                    existing = sd.get(list_name, [])
                    new = [s for s in existing if s['stat'] != stat_key]
                    if len(new) < len(existing):
                        sd[list_name] = new
                        removed += 1
                self._buff_modified = True
                self._buff_refresh_stats()
                self._buff_status_label.setText(f"Removed stat {name} ({removed} levels)")


    def _eb_remove_passive(self) -> None:
        if not hasattr(self, '_buff_rust_items') or not self._buff_rust_items:
            return
        if not hasattr(self, '_buff_current_item') or self._buff_current_item is None:
            return

        rust_info = self._buff_rust_lookup.get(self._buff_current_item.item_key)
        if not rust_info:
            return

        psl = rust_info.get('equip_passive_skill_list', [])
        if not psl:
            QMessageBox.information(self, "Remove Passive", "This item has no passives.")
            return

        target_skill = self._eb_passive_combo.currentData()
        target_name = self._PASSIVE_SKILL_NAMES.get(target_skill, f"Skill {target_skill}")

        new_psl = [p for p in psl if p['skill'] != target_skill]
        if len(new_psl) == len(psl):
            QMessageBox.information(self, "Remove Passive",
                f"{target_name} is not on this item.")
            return

        rust_info['equip_passive_skill_list'] = new_psl
        self._buff_modified = True
        self._buff_refresh_stats()
        self._eb_status.setText(f"Removed {target_name} ({len(new_psl)} passives remain)")


    def _eb_god_mode(self, skip: bool = False) -> None:
        if not hasattr(self, '_buff_rust_items') or not self._buff_rust_items:
            QMessageBox.warning(self, "God Mode", "Extract with Rust parser first.")
            return
        if not hasattr(self, '_buff_current_item') or self._buff_current_item is None:
            QMessageBox.warning(self, "God Mode", "Select an item first.")
            return

        rust_info = self._buff_rust_lookup.get(self._buff_current_item.item_key)
        if not rust_info:
            QMessageBox.warning(self, "God Mode", "Item not found in Rust data.")
            return

        edl = rust_info.get('enchant_data_list', [])
        if not edl:
            QMessageBox.warning(self, "God Mode",
                "This item has no enchant data.\n"
                "Only equippable items (weapons, armor, accessories) can have buffs.")
            return

        if not skip:
            display_name = self._name_db.get_name(self._buff_current_item.item_key)

            reply = QMessageBox.warning(
                self, "Potter's God Mode",
                f"Apply God Mode to {display_name}?\n\n"
                f"This will inject into ALL {len(edl)} enchant levels:\n"
                f"  - Passive: Invincible + Great Thief\n"
                f"  - Regen: Stamina 100K, MP 100K\n"
                f"  - Static: DDD 999999, DPV 999999, Stamina Reduction 100M\n"
                f"  - Levels: AtkSpd 10, MoveSpd 10, CritRate 10, Resistances 10\n"
                f"  - Buffs: 8 equipment buffs at level 10\n\n"
                f"Click 'Export as Mod' after to write.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return

        rust_info['equip_passive_skill_list'] = [
            {"skill": 70994, "level": 1},
            {"skill": 9128, "level": 1},
        ]

        for ed in edl:
            sd = ed.setdefault('enchant_stat_data', {})

            sd['regen_stat_list'] = [
                {"stat": 1000026, "change_mb": 100000},
                {"stat": 1000027, "change_mb": 100000},
            ]

            sd['stat_list_static'] = [
                {"stat": 1000002, "change_mb": 999999},
                {"stat": 1000003, "change_mb": 999999},
                {"stat": 1000037, "change_mb": 100000000},
            ]

            sd['stat_list_static_level'] = [
                {"stat": 1000010, "change_mb": 10},
                {"stat": 1000011, "change_mb": 10},
                {"stat": 1000007, "change_mb": 10},
                {"stat": 1000024, "change_mb": 10},
                {"stat": 1000025, "change_mb": 10},
                {"stat": 1000026, "change_mb": 10},
            ]

            ed['equip_buffs'] = [
                {"buff": 1000072, "level": 10},
                {"buff": 1000071, "level": 10},
                {"buff": 1000073, "level": 10},
                {"buff": 1000093, "level": 10},
                {"buff": 1000091, "level": 10},
                {"buff": 1000124, "level": 10},
                {"buff": 1000123, "level": 10},
                {"buff": 1000114, "level": 10},
            ]

        self._buff_modified = True
        self._buff_refresh_stats()
        self._buff_status_label.setText(
            f"God Mode applied to {display_name} — "
            f"passives + stats + buffs injected into {len(edl)} enchant levels. "
            f"Click 'Export as Mod' to write."
        )


    def _build_effect_catalog(self, items):
        self._effect_catalog_combo.clear()
        self._effect_catalog_data = {}
        self._effect_catalog_all = []

        gimmick_names = {}
        try:
            import crimson_rs
            game_path = self._config.get("game_install_path", "")
            if game_path:
                dp = 'gamedata/binary__/client/bin'
                gi_body = bytes(crimson_rs.extract_file(game_path, '0008', dp, 'gimmickinfo.pabgb'))
                gi_gh = bytes(crimson_rs.extract_file(game_path, '0008', dp, 'gimmickinfo.pabgh'))
                from gimmickinfo_parser import parse_all_gimmicks
                full, partial, _ = parse_all_gimmicks(gi_body, gi_gh)
                for e in full + partial:
                    k = e.get('key', 0)
                    n = e.get('name', '')
                    if k and n:
                        display = n.replace('gimmick_equip_', '').replace('gimmick_', '')
                        gimmick_names[k] = display
                log.info("Loaded %d gimmick names for effect catalog", len(gimmick_names))
        except Exception as ge:
            log.warning("Could not load gimmick names: %s", ge)

        seen_gimmicks = {}
        for item in items:
            psl = item.get('equip_passive_skill_list', [])
            gi = item.get('gimmick_info', 0)
            if not psl or not gi:
                continue
            if gi >= 18000000:
                continue

            if gi in seen_gimmicks:
                continue
            seen_gimmicks[gi] = True

            skill_parts = []
            for s in psl:
                sid = s['skill']
                sname = self._PASSIVE_SKILL_NAMES.get(sid, {})
                if isinstance(sname, dict):
                    display = sname.get('suffix', sname.get('english_name', str(sid)))
                else:
                    display = str(sname) if sname else str(sid)
                skill_parts.append(display)

            src = item.get('string_key', '')
            gi_name = gimmick_names.get(gi, '')
            if gi_name:
                label = f"{gi_name}  ({' + '.join(skill_parts)})  [{src}]"
            else:
                label = f"{' + '.join(skill_parts)}  [{src}]  gi={gi}"

            dcd = item.get('docking_child_data')

            effect_data = {
                'equip_passive_skill_list': psl,
                'gimmick_info': gi,
                'cooltime': max(item.get('cooltime', 0), 1),
                'item_charge_type': item.get('item_charge_type', 0),
                'max_charged_useable_count': max(item.get('max_charged_useable_count', 0), 1),
                'respawn_time_seconds': item.get('respawn_time_seconds', 0),
                'docking_child_data': dcd,
                'source_key': item.get('key'),
                'source_name': src,
                'gimmick_name': gi_name,
            }

            idx = len(self._effect_catalog_data)
            self._effect_catalog_data[idx] = effect_data
            self._effect_catalog_all.append((label, idx))

        self._effect_catalog_all.sort(key=lambda x: x[0].lower())

        self._effect_populate_combo("")


    def _effect_populate_combo(self, filter_text: str) -> None:
        if not hasattr(self, '_effect_catalog_all'):
            return
        self._effect_catalog_combo.blockSignals(True)
        self._effect_catalog_combo.clear()

        ft = filter_text.strip().lower()
        shown = 0
        for label, idx in self._effect_catalog_all:
            data = self._effect_catalog_data.get(idx, {})
            haystack = label.lower()
            haystack += ' ' + str(data.get('source_name', '')).lower()
            haystack += ' ' + str(data.get('gimmick_info', ''))
            haystack += ' ' + str(data.get('gimmick_name', '')).lower()
            for s in data.get('equip_passive_skill_list', []):
                haystack += ' ' + str(s.get('skill', ''))
            if not ft or ft in haystack:
                self._effect_catalog_combo.addItem(label, idx)
                shown += 1

        header = f"-- {shown} effect(s) --" if ft else f"-- {len(self._effect_catalog_all)} effects available --"
        self._effect_catalog_combo.insertItem(0, header, None)
        self._effect_catalog_combo.setCurrentIndex(0)
        self._effect_catalog_combo.blockSignals(False)


    def _effect_filter_changed(self, text: str) -> None:
        self._effect_populate_combo(text)


    def _eb_copy_effect(self):
        if not hasattr(self, '_buff_rust_items') or not self._buff_rust_items:
            QMessageBox.warning(self, "Copy Effect", "Extract with Rust parser first.")
            return
        if not hasattr(self, '_buff_current_item') or self._buff_current_item is None:
            QMessageBox.warning(self, "Copy Effect", "Select an item first.")
            return

        idx = self._effect_catalog_combo.currentData()
        if idx is None:
            QMessageBox.warning(self, "Copy Effect", "Select an effect from the dropdown.")
            return

        effect = self._effect_catalog_data.get(idx)
        if not effect:
            return

        rust_info = self._buff_rust_lookup.get(self._buff_current_item.item_key)
        if not rust_info:
            QMessageBox.warning(self, "Copy Effect", "Item not found in Rust data.")
            return

        display_name = self._name_db.get_name(self._buff_current_item.item_key)
        src_name = effect.get('source_name', '?')
        skills = effect.get('equip_passive_skill_list', [])
        skill_str = ', '.join(str(s['skill']) for s in skills)

        cur_passives = rust_info.get('equip_passive_skill_list', []) or []
        cur_skill_str = ', '.join(str(p['skill']) for p in cur_passives) or '(none)'

        reply = QMessageBox.question(
            self, "Apply Effect",
            f"Apply effect from {src_name} to {display_name}?\n\n"
            f"Current passives: {cur_skill_str}\n"
            f"Adding skills: {skill_str}\n"
            f"Gimmick: {effect.get('gimmick_info', 0)}\n"
            f"Cooltime: {effect.get('cooltime', 0)}s\n"
            f"Charges: {effect.get('max_charged_useable_count', 0)}\n"
            f"Has docking: {'Yes' if effect.get('docking_child_data') else 'No'}\n\n"
            f"Passives will STACK (existing + new, deduped by skill ID).\n"
            f"Gimmick/docking/cooltime will REPLACE (one gimmick slot per item).\n\n"
            f"Click 'Export as Mod' after to write.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        existing_passives = list(rust_info.get('equip_passive_skill_list', []) or [])
        existing_keys = {p['skill'] for p in existing_passives}
        merged = existing_passives[:]
        added_count = 0
        for p in effect.get('equip_passive_skill_list', []):
            if p['skill'] not in existing_keys:
                merged.append(p)
                existing_keys.add(p['skill'])
                added_count += 1
        rust_info['equip_passive_skill_list'] = merged

        for gf in ('gimmick_info', 'cooltime', 'item_charge_type',
                    'max_charged_useable_count', 'docking_child_data',
                    'respawn_time_seconds'):
            if gf in effect:
                rust_info[gf] = effect[gf]

        if 'cooltime' in effect:
            rust_info['unk_post_cooltime_a'] = effect['cooltime']
            rust_info['unk_post_cooltime_b'] = effect['cooltime']
        if 'max_charged_useable_count' in effect:
            rust_info['unk_post_max_charged_a'] = effect['max_charged_useable_count']
            rust_info['unk_post_max_charged_b'] = effect['max_charged_useable_count']

        self._buff_modified = True
        self._buff_refresh_stats()
        self._buff_status_label.setText(
            f"Effect from {src_name} applied: +{added_count} new passive(s), "
            f"{len(merged)} total. Gimmick replaced."
        )

    _ITEM_PRESETS = {
        "open_sockets": {
            "name": "5 Sockets",
            "description": "Adds 5 open sockets to all newly obtained versions of this item.",
            "warning": "Embedding abyss gears in-game on items that do not normally have socket slots can cause crashing.",
            "drop_default_data": {
                "add_socket_material_item_list": [
                    {"item": 1,"value": 500},
                    {"item": 1,"value": 1000},
                    {"item": 1,"value": 2000},
                    {"item": 1,"value": 3000},
                    {"item": 1,"value": 4000}
                ],
                "socket_valid_count": 5,
                "use_socket": 1
            }
        },
        "max_enchant": {
            "name": "Max Refine",
            "description": "All newly obtained copies of this item will be lvl 10 be default.",
            "drop_default_data": {
                "drop_enchant_level": 10
            }
        },
        "no_cooldown": {
            "name":"No Cooldown",
            "description": "Set cooldown of item ability to 1s and remove recharge restrictions.",
            "cooltime": 1,
            "item_charge_type": 0,
            "respawn_time_seconds": 0
        },
        "max_charges": {
            "name":"Max Charges",
            "description": "Set charges of item ability to 100.",
            "max_charged_useable_count": 100
        },
        "max_stacks": {
            "name": "Max Stacks",
            "description": "Set the max stack size of an item to 999999.",
            "max_stack_count": 999999  
        },
        "shadow_boots": {
            "name": "Shadow Boots",
            "passives": [
                {"skill": 7201, "level": 1},
                {"skill": 7055, "level": 1},
                {"skill": 7202, "level": 1},
            ],
            "gimmick_info": 1004431,
            "cooltime": 1,
            "item_charge_type": 0,
            "max_charged_useable_count": 100,
            "respawn_time_seconds": 0,
            "docking_child_data": {
                "gimmick_info_key": 1004431,
                "character_key": 0,
                "item_key": 0,
                "attach_parent_socket_name": "Bip01 Footsteps",
                "attach_child_socket_name": "",
                "docking_tag_name_hash": [247236102, 0, 0, 0],
                "docking_equip_slot_no": 65535,
                "spawn_distance_level": 4294967295,
                "is_item_equip_docking_gimmick": 0,
                "send_damage_to_parent": 0,
                "is_body_part": 0,
                "docking_type": 0,
                "is_summoner_team": 0,
                "is_player_only": 0,
                "is_npc_only": 0,
                "is_sync_break_parent": 0,
                "hit_part": 0,
                "detected_by_npc": 0,
                "is_bag_docking": 0,
                "enable_collision": 0,
                "disable_collision_with_other_gimmick": 1,
                "docking_slot_key": "",
                "inherit_summoner": 0,
                "summon_tag_name_hash": [0, 0, 0, 0],
            },
        },
        "lightning_weapon": {
            "name": "Lightning Weapon",
            "passives": [
                {"skill": 91101, "level": 3},
                {"skill": 91104, "level": 3},
                {"skill": 91105, "level": 3},
            ],
            "gimmick_info": 1001961,
            "cooltime": 1,
            "item_charge_type": 0,
            "max_charged_useable_count": 100,
            "respawn_time_seconds": 0,
            "docking_child_data": {
                "gimmick_info_key": 1001961,
                "character_key": 0,
                "item_key": 0,
                "attach_parent_socket_name": "Gimmick_Weapon_00_Socket",
                "attach_child_socket_name": "",
                "docking_tag_name_hash": [3365725887, 0, 0, 0],
                "docking_equip_slot_no": 65535,
                "spawn_distance_level": 4294967295,
                "is_item_equip_docking_gimmick": 1,
                "send_damage_to_parent": 0,
                "is_body_part": 0,
                "docking_type": 0,
                "is_summoner_team": 0,
                "is_player_only": 0,
                "is_npc_only": 0,
                "is_sync_break_parent": 0,
                "hit_part": 0,
                "detected_by_npc": 0,
                "is_bag_docking": 0,
                "enable_collision": 0,
                "disable_collision_with_other_gimmick": 1,
                "docking_slot_key": "",
                "inherit_summoner": 0,
                "summon_tag_name_hash": [0, 0, 0, 0],
            },
        },
        "great_thief": {
            "name": "Great Thief (Block Theft only)",
            "passives": [
                {"skill": 9128, "level": 1},
                {"skill": 76009, "level": 1},
            ],
            "gimmick_info": 1002041,
            "cooltime": 1800,
            "item_charge_type": 0,
            "max_charged_useable_count": 1,
            "respawn_time_seconds": 0,
            "docking_child_data": {
                "gimmick_info_key": 1002041,
                "character_key": 0,
                "item_key": 0,
                "attach_parent_socket_name": "Gimmick_Hand_L_00_Socket",
                "attach_child_socket_name": "",
                "docking_tag_name_hash": [0, 0, 0, 0],
                "docking_equip_slot_no": 65535,
                "spawn_distance_level": 4294967295,
                "is_item_equip_docking_gimmick": 0,
                "send_damage_to_parent": 0,
                "is_body_part": 0,
                "docking_type": 0,
                "is_summoner_team": 0,
                "is_player_only": 0,
                "is_npc_only": 0,
                "is_sync_break_parent": 0,
                "hit_part": 0,
                "detected_by_npc": 0,
                "is_bag_docking": 0,
                "enable_collision": 0,
                "disable_collision_with_other_gimmick": 1,
                "docking_slot_key": "",
                "inherit_summoner": 0,
                "summon_tag_name_hash": [0, 0, 0, 0],
            },
        },
        "great_thief_all": {
            "name": "Great Thief (Block ALL crime)",
            "passives": [
                {"skill": 9128, "level": 1},
                {"skill": 76009, "level": 1},
                {"skill": 76011, "level": 1},
                {"skill": 76012, "level": 1},
            ],
            "gimmick_info": 1002041,
            "cooltime": 1800,
            "item_charge_type": 0,
            "max_charged_useable_count": 1,
            "respawn_time_seconds": 0,
            "docking_child_data": {
                "gimmick_info_key": 1002041,
                "character_key": 0,
                "item_key": 0,
                "attach_parent_socket_name": "Gimmick_Hand_L_00_Socket",
                "attach_child_socket_name": "",
                "docking_tag_name_hash": [0, 0, 0, 0],
                "docking_equip_slot_no": 65535,
                "spawn_distance_level": 4294967295,
                "is_item_equip_docking_gimmick": 0,
                "send_damage_to_parent": 0,
                "is_body_part": 0,
                "docking_type": 0,
                "is_summoner_team": 0,
                "is_player_only": 0,
                "is_npc_only": 0,
                "is_sync_break_parent": 0,
                "hit_part": 0,
                "detected_by_npc": 0,
                "is_bag_docking": 0,
                "enable_collision": 0,
                "disable_collision_with_other_gimmick": 1,
                "docking_slot_key": "",
                "inherit_summoner": 0,
                "summon_tag_name_hash": [0, 0, 0, 0],
            },
        },
        "crime_mask": {
            "name": "Crime Mask (Steal / Threaten)",
            "passives": [
                {"skill": 709, "level": 1},
            ],
        },
    }


    def _eb_great_thief_pick_variant(self) -> None:
        from PySide6.QtWidgets import QDialog, QVBoxLayout, QLabel, QPushButton, QHBoxLayout

        dlg = QDialog(self)
        dlg.setWindowTitle("Great Thief — Pick Variant")
        dlg.resize(480, 220)
        dl = QVBoxLayout(dlg)

        info = QLabel(
            "Pick which variant of Great Thief to apply.\n\n"
            "Block Theft only: skills 9128 + 76009. Suppresses pickpocket crime detection.\n"
            "Other crimes (vandalism, assault) will still flag you.\n\n"
            "Block ALL crime: also adds 76011 + 76012. Full crime immunity —\n"
            "theft, vandalism, and all other crime types.\n\n"
            "Passives stack with existing; gimmick replaces."
        )
        info.setWordWrap(True)
        info.setStyleSheet(f"color: {COLORS['text_dim']}; padding: 4px;")
        dl.addWidget(info)

        btn_row = QHBoxLayout()
        b1 = QPushButton("Block Theft only")
        b1.clicked.connect(lambda: (dlg.accept(), self._eb_apply_preset("great_thief")))
        btn_row.addWidget(b1)

        b2 = QPushButton("Block ALL crime")
        b2.setObjectName("accentBtn")
        b2.clicked.connect(lambda: (dlg.accept(), self._eb_apply_preset("great_thief_all")))
        btn_row.addWidget(b2)

        cancel = QPushButton("Cancel")
        cancel.clicked.connect(dlg.reject)
        btn_row.addWidget(cancel)
        dl.addLayout(btn_row)

        dlg.exec()


    def _eb_apply_preset(self, preset_key: str, skip: bool = False) -> None:
        if not hasattr(self, '_buff_rust_items') or not self._buff_rust_items:
            QMessageBox.warning(self, "Apply Preset", "Extract with Rust parser first.")
            return
        if not hasattr(self, '_buff_current_item') or self._buff_current_item is None:
            QMessageBox.warning(self, "Apply Preset", "Select an item first.")
            return

        preset = self._ITEM_PRESETS.get(preset_key)
        if not preset:
            return

        log.info("Applying preset: %s", preset)

        rust_info = self._buff_rust_lookup.get(self._buff_current_item.item_key)
        if not rust_info:
            return

        display_name = self._name_db.get_name(self._buff_current_item.item_key)
        skill_str = ', '.join(str(p['skill']) for p in preset.get('passives', []))

        if not skip:
            cur_charge = rust_info.get('item_charge_type', 0)
            new_charge = preset.get('item_charge_type', cur_charge)
            charge_change_warn = ""
            if cur_charge != new_charge and new_charge == 0:
                charge_change_warn = (
                    f"WARNING: Switching item from passive -> activated.\n"
                    f"Existing copies in your save have NO charge-tracking data and\n"
                    f"will show '0 uses' in-game. Get a FRESH copy (store/craft/drop)\n"
                    f"AFTER applying the mod for the activation to work.\n\n"
                )
                
            cur_stack_size = rust_info.get('max_stack_count', 1)
            new_stack_size = preset.get('max_stack_count', cur_stack_size)
            max_stack_warn = ""
            if cur_stack_size != new_stack_size and cur_stack_size == 1:
                max_stack_warn = (
                    "WARNING: Changing the stack size for items that do "
                    "not stack by default can cause ui glitches and lost items.\n"
                    "Proceed with caution.\n\n"                    
                )

        if not skip:
            warning = preset.get('warning', '')
            if warning:
                warning = f"WARNING: {warning}\n\n"

            default_desc = (
                f"Skills (stack with existing): {skill_str}\n"
                f"Gimmick: {preset.get('gimmick_info', 'unchanged')}\n"
                f"Replaces existing gimmick."
                if preset.get('passives') else ""
            )

            reply = QMessageBox.question(
                self, f"Apply Preset: {preset['name']}",
                f"Apply {preset['name']} preset to {display_name}?\n\n"
                f"{preset.get('description', default_desc)}\n\n"
                f"{warning}{charge_change_warn}{max_stack_warn}"
                f"Click 'Apply to Game' after to write.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return

        existing = list(rust_info.get('equip_passive_skill_list', []) or [])
        existing_keys = {p['skill'] for p in existing}
        added = 0
        for p in preset.get('passives', []):
            if p['skill'] not in existing_keys:
                existing.append({'skill': p['skill'], 'level': p['level']})
                existing_keys.add(p['skill'])
                added += 1
        rust_info['equip_passive_skill_list'] = existing

        for gf in ('gimmick_info', 'cooltime', 'item_charge_type',
                    'max_charged_useable_count', 'respawn_time_seconds',
                    'docking_child_data', 'max_stack_count'):
            if preset.get(gf) is not None:
                rust_info[gf] = preset[gf]
        
        
        if preset.get('drop_default_data') is not None:
            ddd = preset['drop_default_data']
            existing = rust_info.get('drop_default_data')
            for dd in ('drop_enchant_level','add_socket_material_item_list',
                       'socket_item_list','socket_valid_count','use_socket'):
                if ddd.get(dd) is not None:
                    existing[dd] = ddd[dd]
            # rust_info['drop_default_data'] = existing

        # Sync mirrored fields — game expects these to match their parent values
        if 'cooltime' in preset:
            rust_info['unk_post_cooltime_a'] = preset['cooltime']
            rust_info['unk_post_cooltime_b'] = preset['cooltime']
        if 'max_charged_useable_count' in preset:
            rust_info['unk_post_max_charged_a'] = preset['max_charged_useable_count']
            rust_info['unk_post_max_charged_b'] = preset['max_charged_useable_count']

        if preset.get('drop_default_data') is not None:
            ddd = preset['drop_default_data']
            existing_ddd = rust_info.get('drop_default_data')
            if existing_ddd:
                for dd in ('drop_enchant_level', 'add_socket_material_item_list',
                           'socket_item_list', 'socket_valid_count', 'use_socket'):
                    if ddd.get(dd) is not None:
                        existing_ddd[dd] = ddd[dd]

        # Sync mirrored fields — game expects these to match their parent values
        if 'cooltime' in preset:
            rust_info['unk_post_cooltime_a'] = preset['cooltime']
            rust_info['unk_post_cooltime_b'] = preset['cooltime']
        if 'max_charged_useable_count' in preset:
            rust_info['unk_post_max_charged_a'] = preset['max_charged_useable_count']
            rust_info['unk_post_max_charged_b'] = preset['max_charged_useable_count']

        self._buff_modified = True
        self._buff_refresh_stats()
        self._buff_status_label.setText(
            f"{preset['name']} applied to {display_name}: +{added} passive(s), "
            f"gimmick {preset.get('gimmick_info', 'unchanged')}. Export as Mod to write."
        )


    def _load_vfx_catalog_into_combo(self) -> None:
        self._eb_vfx_combo.clear()
        self._eb_vfx_combo.addItem("(no gimmick selected)", None)
        self._vfx_catalog_entries: list = []
        for base in [os.path.dirname(os.path.abspath(__file__)),
                     getattr(sys, '_MEIPASS', ''), os.getcwd()]:
            path = os.path.join(base, 'vfx_equip_attachments.json')
            if os.path.isfile(path):
                try:
                    with open(path, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    self._vfx_catalog_entries = data.get('gimmicks', []) or []
                    break
                except Exception:
                    continue
        for e in sorted(self._vfx_catalog_entries,
                        key=lambda x: (x.get('gimmick_name') or '').lower()):
            gk = e.get('gimmick_key')
            nm = e.get('gimmick_name') or f"gimmick {gk}"
            n_items = e.get('item_count', 0)
            pp = e.get('prefab_path') or ''
            leaf = pp.rsplit('/', 1)[-1].replace('.prefab', '') if pp else ''
            label = f"{nm}  ({gk}, {n_items} item)" + (f"  — {leaf}" if leaf else "")
            self._eb_vfx_combo.addItem(label, gk)


    def _eb_apply_vfx_gimmick(self) -> None:
        if not hasattr(self, '_buff_rust_items') or not self._buff_rust_items:
            QMessageBox.warning(self, "Apply Gimmick", "Extract with Rust parser first.")
            return
        if not hasattr(self, '_buff_current_item') or self._buff_current_item is None:
            QMessageBox.warning(self, "Apply Gimmick", "Select an item first.")
            return
        gk = self._eb_vfx_combo.currentData()
        if not gk:
            QMessageBox.information(self, "Apply Gimmick",
                                    "Pick a gimmick from the dropdown first.")
            return
        rust_info = self._buff_rust_lookup.get(self._buff_current_item.item_key)
        if not rust_info:
            return
        sample = None
        for it in self._buff_rust_items:
            if it.get('gimmick_info') == gk:
                sample = it
                break
        if sample is None:
            QMessageBox.warning(
                self, "Apply Gimmick",
                f"No sample item uses gimmick {gk}. Cannot clone config —\n"
                f"this gimmick may be referenced only indirectly.")
            return
        display = self._name_db.get_name(self._buff_current_item.item_key)
        entry = next((e for e in getattr(self, '_vfx_catalog_entries', [])
                      if e.get('gimmick_key') == gk), None)
        nm = (entry or {}).get('gimmick_name') or f"gimmick {gk}"
        cur_charge = rust_info.get('item_charge_type', 0)
        new_charge = sample.get('item_charge_type', cur_charge)
        warn = ""
        if cur_charge != new_charge and new_charge == 0:
            warn = ("\n\nNOTE: switching item to activated use. Existing save\n"
                    "copies have no charge-tracking data; get a FRESH drop.")
        reply = QMessageBox.question(
            self, "Apply Gimmick",
            f"Attach gimmick '{nm}' ({gk}) to {display}?\n\n"
            f"Cloning from sample item {sample.get('key')} "
            f"({sample.get('string_key', '?')}).{warn}\n\n"
            f"Click 'Export as Mod' after to write.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        import copy as _copy
        for gf in ('gimmick_info', 'cooltime', 'item_charge_type',
                   'max_charged_useable_count', 'respawn_time_seconds',
                   'docking_child_data'):
            if sample.get(gf) is not None:
                val = _copy.deepcopy(sample[gf])
                if gf == 'docking_child_data' and isinstance(val, dict):
                    val.setdefault('inherit_summoner', 0)
                    val.setdefault('summon_tag_name_hash', [0, 0, 0, 0])
                rust_info[gf] = val
        if sample.get('cooltime') is not None:
            rust_info['unk_post_cooltime_a'] = sample['cooltime']
            rust_info['unk_post_cooltime_b'] = sample['cooltime']
        if sample.get('max_charged_useable_count') is not None:
            rust_info['unk_post_max_charged_a'] = sample['max_charged_useable_count']
            rust_info['unk_post_max_charged_b'] = sample['max_charged_useable_count']
        self._buff_modified = True
        self._buff_refresh_stats()
        self._buff_status_label.setText(
            f"Gimmick '{nm}' applied to {display} ({gk}, cloned from item "
            f"{sample.get('key')}). Export as Mod to write."
        )


    # Accessory + cloak categories where the game RENDERS sockets when
    # present, even though these items ship with use_socket=0 and no
    # add_socket_material_item_list by default. Confirmed via DennyBro's
    # "Accessories 5 Socket OP" JSON mod (Nexus): 169 items across these
    # categories are the full set that the game treats as socketable when
    # use_socket=1 + a 5-entry material list is populated. Weapons/armor
    # already ship with sockets, so they're handled by the existing
    # extend-existing path and don't need force-enable.
    _FORCE_SOCKET_CATEGORIES = {"Ring", "Necklace", "Earring", "Cloak", "Lantern", "Bracer"}
    _FORCE_SOCKET_STRING_KEYS = {"Daeil_Band", "OOngka_Daeil_Band", "Damian_Daeil_Band"}
    # DennyBro also enables sockets on nobility-degree insignia. Those
    # don't match the standard category map, so we pattern-match them
    # separately on the string_key.
    _NOBILITY_DEGREE_PATTERN = "_Nobility_Degree_"

    def _socketable_force_target(self, it: dict) -> bool:
        """Can this equippable item receive force-enabled sockets?

        Returns True for ANY item with drop_default_data + equip_type_info
        that currently has no sockets (use_socket=0 or empty socket list).
        This covers all equipment: weapons, armor, accessories, lanterns,
        bracelets, daggers, etc.
        """
        if not it.get("drop_default_data"):
            return False
        if not it.get("equip_type_info"):
            return False
        return True

    def _eb_change_drop_enchant(self) -> None:
        if not hasattr(self, '_buff_rust_items') or not self._buff_rust_items:
            QMessageBox.warning(self, "Drop Enchant Level", "Extract with Rust parser first.")
            return
        if not hasattr(self, '_buff_current_item') or self._buff_current_item is None:
            QMessageBox.warning(self, "Drop Enchant Level", "Select an item first.")
            return

        rust_info = self._buff_rust_lookup.get(self._buff_current_item.item_key)
        if not rust_info:
            return

        ddd = rust_info.get('drop_default_data')
        drop_level = self._eb_drop_enchant_level.value()
        if not ddd:
            QMessageBox.warning(self, "Drop Enchant Level",
                "This item has no drop_default_data — enhance not applicable.")
            return
        if not ddd.get('drop_enchant_level'):
            reply = QMessageBox.question(
                self, "Drop Enhance Level",
                "This item does not support enhancements.\n"
                "Enable enhance level and apply anyway?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply != QMessageBox.Yes:
                return

        ddd['drop_enchant_level'] = drop_level

        self._buff_modified = True
        display_name = self._name_db.get_name(self._buff_current_item.item_key)
        self._buff_status_label.setText(
            f"Refinement level of {display_name} set to {drop_level} on drop. "
            f"Export as Mod to write.")

    def _eb_extend_sockets(self) -> None:
        if not hasattr(self, '_buff_rust_items') or not self._buff_rust_items:
            QMessageBox.warning(self, "Extend Sockets", "Extract with Rust parser first.")
            return
        if not hasattr(self, '_buff_current_item') or self._buff_current_item is None:
            QMessageBox.warning(self, "Extend Sockets", "Select an item first.")
            return

        rust_info = self._buff_rust_lookup.get(self._buff_current_item.item_key)
        if not rust_info:
            return

        ddd = rust_info.get('drop_default_data')
        if not ddd:
            QMessageBox.warning(self, "Extend Sockets",
                "This item has no drop_default_data — sockets not applicable.")
            return
        if not ddd.get('use_socket', 0):
            reply = QMessageBox.question(
                self, "Extend Sockets",
                "This item has use_socket=0 (sockets disabled).\n"
                "Enable socket support and extend anyway?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply != QMessageBox.Yes:
                return
            ddd['use_socket'] = 1

        target_count = self._eb_socket_count.value()
        target_valid = self._eb_socket_valid.value()
        if target_valid > target_count:
            target_valid = target_count

        cur_list = ddd.get('add_socket_material_item_list', [])
        DEFAULT_COSTS = [500, 1000, 2000, 3000, 4000, 5000, 6000, 7000]

        new_list = list(cur_list)
        while len(new_list) < target_count:
            cost = DEFAULT_COSTS[len(new_list)] if len(new_list) < len(DEFAULT_COSTS) else 5000
            new_list.append({'item': 1, 'value': cost})
        new_list = new_list[:target_count]

        ddd['add_socket_material_item_list'] = new_list
        ddd['socket_valid_count'] = target_valid

        self._buff_modified = True
        self._buff_refresh_stats()
        display_name = self._name_db.get_name(self._buff_current_item.item_key)
        self._buff_status_label.setText(
            f"Sockets on {display_name}: {target_count} max, {target_valid} pre-unlocked. "
            f"Export as Mod to write.")

    def _eb_bulk_abyss_plus_sockets(self) -> None:
        if not hasattr(self, '_buff_rust_items') or not self._buff_rust_items:
            QMessageBox.warning(self, "Bulk Abyss + Sockets", "Extract first.")
            return
        owned_keys = set()
        for it in getattr(self, '_items', []) or []:
            if hasattr(it, 'item_key'):
                owned_keys.add(it.item_key)
        if not owned_keys:
            QMessageBox.warning(self, "Bulk Abyss + Sockets",
                                "Load a save file first so we know which items you own.")
            return
        abyss_count = 0
        socket_count = 0
        for it in self._buff_rust_items:
            if it.get('key') not in owned_keys:
                continue
            if it.get('equipable_hash', 0) != 0:
                it['equipable_hash'] = 0
                abyss_count += 1
            if self._socketable_force_target(it):
                self._force_enable_sockets(it, 5, 0)
                socket_count += 1
        if abyss_count or socket_count:
            self._buff_modified = True
            self._buff_refresh_stats()
        self._buff_status_label.setText(
            f"Bulk: {abyss_count} abyss unlocked, {socket_count} items → 5 sockets")
        QMessageBox.information(self, "Bulk Abyss + Sockets",
            f"Inventory items processed:\n\n"
            f"  Abyss unlocked: {abyss_count}\n"
            f"  Sockets → 5: {socket_count}\n\n"
            f"Export as Mod or Apply to Game to write.")

    def _eb_abyss_plus_sockets(self) -> None:
        if not hasattr(self, '_buff_rust_items') or not self._buff_rust_items:
            QMessageBox.warning(self, "Abyss + Sockets", "Extract first.")
            return
        if self._buff_current_item is None:
            QMessageBox.warning(self, "Abyss + Sockets", "Select an item first.")
            return
        key = self._buff_current_item.item_key
        rust_info = None
        for it in self._buff_rust_items:
            if it.get('key') == key:
                rust_info = it
                break
        if rust_info is None:
            QMessageBox.warning(self, "Abyss + Sockets", "Item not found in parsed data.")
            return
        rust_info['equipable_hash'] = 0
        self._eb_apply_preset("open_sockets", skip=True)
        self._buff_modified = True
        self._buff_refresh_stats()
        display_name = self._name_db.get_name(key)
        self._buff_status_label.setText(
            f"{display_name}: abyss unlocked + 5 sockets. Export to write.")

    def _eb_extend_all_sockets_to_5(self) -> None:
        if not hasattr(self, '_buff_rust_items') or not self._buff_rust_items:
            QMessageBox.warning(self, "All -> 5 Sockets",
                "Extract with Rust parser first (click 'Extract (Rust)').")
            return

        DEFAULT_COSTS = [500, 1000, 2000, 3000, 4000, 5000, 6000, 7000]
        TARGET = 5

        # Preflight count: how many force-enable candidates (rings, cloaks,
        # earrings, necklaces, nobility insignia) currently have no
        # sockets? Used so the user decides up-front whether to include
        # that set or run the extend-only legacy behavior.
        force_candidates = [
            it for it in self._buff_rust_items
            if self._socketable_force_target(it)
            and (not (it.get("drop_default_data") or {}).get("use_socket", 0)
                 or not ((it.get("drop_default_data") or {})
                         .get("add_socket_material_item_list") or []))
        ]

        include_force = True
        if force_candidates:
            reply = QMessageBox.question(
                self, "All -> 5 Sockets",
                f"Also FORCE-ENABLE sockets on {len(force_candidates)} "
                f"accessories/cloaks that currently have NONE?\n\n"
                f"  · {sum(1 for it in force_candidates if 'Ring' in (it.get('string_key') or '') and 'Earring' not in (it.get('string_key') or ''))} rings\n"
                f"  · {sum(1 for it in force_candidates if '_Necklace' in (it.get('string_key') or '') or 'Necklace' in (it.get('string_key') or ''))} necklaces\n"
                f"  · {sum(1 for it in force_candidates if '_Earring' in (it.get('string_key') or '') or 'Earring' in (it.get('string_key') or ''))} earrings\n"
                f"  · {sum(1 for it in force_candidates if '_Cloak' in (it.get('string_key') or '') or '_Cape' in (it.get('string_key') or ''))} cloaks/capes\n"
                f"  · {sum(1 for it in force_candidates if self._NOBILITY_DEGREE_PATTERN in (it.get('string_key') or ''))} nobility insignia\n\n"
                "This matches DennyBro's 'Accessories 5 Socket OP' mod but runs\n"
                "through the dict merge so it stacks cleanly with other mods.\n\n"
                "Yes = also enable sockets on these.\n"
                "No  = only extend items that already have sockets.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
            include_force = (reply == QMessageBox.Yes)

        changed = 0          # items whose existing socket list was extended
        force_enabled = 0    # items that went from 0 sockets → 5 sockets
        skipped_no_ddd = 0
        skipped_not_socketable = 0
        already_at_target = 0
        examples = []

        def _build_list(existing: list) -> list:
            new_list = list(existing)
            while len(new_list) < TARGET:
                cost = DEFAULT_COSTS[len(new_list)] if len(new_list) < len(DEFAULT_COSTS) else 5000
                new_list.append({'item': 1, 'value': cost})
            return new_list

        for it in self._buff_rust_items:
            ddd = it.get('drop_default_data')
            if not ddd:
                skipped_no_ddd += 1
                continue

            cur_list = ddd.get('add_socket_material_item_list') or []
            use_socket = ddd.get('use_socket', 0)

            if use_socket and cur_list:
                # Existing behavior — extend items that already have sockets.
                if len(cur_list) >= TARGET:
                    already_at_target += 1
                    continue
                ddd['add_socket_material_item_list'] = _build_list(cur_list)
                ddd['socket_valid_count'] = TARGET
                changed += 1
                if len(examples) < 6:
                    examples.append(f"EXT  {it.get('key')} "
                                    f"({it.get('string_key', '?')}): "
                                    f"{len(cur_list)} -> {TARGET}")
                continue

            # Force-enable branch — only runs for categories the game
            # actually accepts sockets on (accessories, cloaks, nobility).
            if include_force and self._socketable_force_target(it):
                ddd['use_socket'] = 1
                ddd['add_socket_material_item_list'] = _build_list([])
                ddd['socket_valid_count'] = TARGET
                force_enabled += 1
                if len(examples) < 6:
                    examples.append(f"NEW  {it.get('key')} "
                                    f"({it.get('string_key', '?')}): "
                                    f"0 -> {TARGET} (force-enabled)")
                continue

            skipped_not_socketable += 1

        QMessageBox.information(
            self, "All -> 5 Sockets",
            f"Bulk socket update complete.\n\n"
            f"  EXTENDED existing:    {changed:>5}  items (grew socket list to {TARGET})\n"
            f"  FORCE-ENABLED:        {force_enabled:>5}  items (0 -> {TARGET}, rings/cloaks/etc.)\n"
            f"  already at target:    {already_at_target:>5}\n"
            f"  not socket-capable:   {skipped_not_socketable:>5}  (weapons that game won't accept, etc.)\n"
            f"  no drop_default_data: {skipped_no_ddd:>5}  (materials, quest items, etc.)\n\n"
            f"Examples:\n  " + "\n  ".join(examples)
            + "\n\nExport as Mod / Apply to Game to write changes.",
        )

        if changed or force_enabled:
            self._buff_modified = True
            self._buff_status_label.setText(
                f"Bulk sockets: +{changed} extended, +{force_enabled} force-enabled "
                f"({TARGET} slots). Export / Apply to write.")


    def _eb_add_imbue_to_selected(self) -> None:
        try:
            import imbue
            import crimson_rs
        except Exception as e:
            QMessageBox.critical(self, "Imbue", f"Import failed: {e}")
            return

        if not hasattr(self, "_buff_rust_items") or not self._buff_rust_items:
            QMessageBox.warning(self, "Imbue",
                "Extract with Rust parser first (click 'Extract' at the top).")
            return
        item = getattr(self, "_buff_current_item", None)
        if item is None:
            QMessageBox.warning(self, "Imbue",
                "Click an item in the Matching Items table first.")
            return

        skill_id = self._eb_imbue_combo.currentData()
        catalog = imbue.get_passive_skill_catalog()
        skill_info = catalog.get(int(skill_id)) or {}
        disp_name = (skill_info.get('pretty_name')
                     or skill_info.get('display')
                     or skill_info.get('name')
                     or f'skill_{skill_id}')
        skill_rec_name = skill_info.get('name') or disp_name
        target_key = item.item_key
        rust_item = self._buff_rust_lookup.get(target_key)
        if rust_item is None:
            QMessageBox.warning(self, "Imbue", f"Item {target_key} not in Rust data.")
            return

        psl = rust_item.get("equip_passive_skill_list") or []
        already_has_passive = any(p.get("skill") == skill_id for p in psl)
        if not already_has_passive:
            psl.append({"skill": skill_id, "level": 3})
            rust_item["equip_passive_skill_list"] = psl
        self._buff_modified = True

        plan = imbue.get_imbue_plan(int(skill_id), rust_item)
        patches = plan.get('patches') or {}
        for field, value in patches.items():
            rust_item[field] = value
        self._buff_modified = True

        gimmick_note_parts: list[str] = [f"reason={plan['reason']}"]
        if patches.get('gimmick_info'):
            gimmick_note_parts.append(f"gimmick_info={patches['gimmick_info']}")
        dcd = patches.get('docking_child_data')
        if dcd:
            h = (dcd.get('docking_tag_name_hash') or [0])[0]
            sock = dcd.get('attach_parent_socket_name') or ''
            gimmick_note_parts.append(f"socket={sock}")
            gimmick_note_parts.append(f"hash=0x{h:08X}")
        if plan.get('warnings'):
            gimmick_note_parts.append(f"warn={len(plan['warnings'])}")
        gimmick_note = " | " + " ".join(gimmick_note_parts)

        for w in plan.get('warnings') or []:
            log.warning("Imbue %s on %s: %s", disp_name, item.name, w)
        log.info(
            "Imbue %s on %s: reason=%s ok=%s gimmick=%s hash=0x%x",
            disp_name, item.name, plan['reason'], plan['ok'],
            patches.get('gimmick_info', 0),
            (dcd.get('docking_tag_name_hash') or [0])[0] if dcd else 0)

        class_hash = rust_item.get("equip_type_info") or 0
        if not class_hash:
            self._eb_status.setText(
                f"Added {disp_name} passive to {item.name}; item has no equip_type_info "
                f"so skill filter unchanged.{gimmick_note}"
            )
            return

        try:
            gp_widget = getattr(self, "_buff_game_path", None)
            gp_text = ""
            if gp_widget is not None:
                try:
                    gp_text = (gp_widget.text() or "").strip()
                except Exception:
                    gp_text = ""
            if not gp_text:
                gp_text = getattr(self, "_game_path", "") or \
                          r"C:\Program Files (x86)\Steam\steamapps\common\Crimson Desert"
            gp = gp_text
            staged = getattr(self, "_staged_skill_files", {}) or {}
            pabgh = staged.get("skill.pabgh") or crimson_rs.extract_file(
                gp, "0008", "gamedata/binary__/client/bin", "skill.pabgh")
            pabgb = staged.get("skill.pabgb") or crimson_rs.extract_file(
                gp, "0008", "gamedata/binary__/client/bin", "skill.pabgb")
        except Exception as e:
            QMessageBox.critical(self, "Imbue",
                f"Could not read skill.pabgb/pabgh:\n{e}")
            return

        entries = imbue.parse_skill_pabgh(pabgh, len(pabgb))
        by_key = {k: (o, l) for (k, o, l) in entries}
        if skill_id not in by_key:
            QMessageBox.critical(self, "Imbue",
                f"Skill {skill_id} ({skill_rec_name}) not in skill.pabgh. "
                "Game version mismatch?")
            return
        off, length = by_key[skill_id]
        rec = pabgb[off:off + length]

        if imbue.skill_allows_class(rec, class_hash):
            self._eb_status.setText(
                f"Added {disp_name} passive to {item.name}. Class already allowed — "
                f"iteminfo edit only.{gimmick_note} Click 'Apply to Game'."
            )
            return

        def _edit(r):
            return imbue.add_class_to_skill_record(r, class_hash)
        try:
            new_pabgh, new_pabgb = imbue.rebuild_skill_pair(
                pabgh, pabgb, {skill_id: _edit}
            )
        except Exception as e:
            import traceback; traceback.print_exc()
            QMessageBox.critical(self, "Imbue",
                f"Failed to rebuild skill pair:\n{e}")
            return

        # Post-edit verify — non-fatal. If the scanner can't find the class
        # in the new record it usually means the skill's class-list block
        # hit the game's count cap. The edit is still in the blob; stage and
        # let the user test in-game rather than losing their work.
        verify_ok = True
        verify_msg = ""
        try:
            _verify_entries = imbue.parse_skill_pabgh(new_pabgh, len(new_pabgb))
            by2 = {k: (o, l) for (k, o, l) in _verify_entries}
            v_off, v_len = by2[skill_id]
            v_rec = new_pabgb[v_off:v_off + v_len]
            verify_ok = imbue.skill_allows_class(v_rec, class_hash)
            if not verify_ok:
                verify_msg = (" (post-edit verify couldn't see the class in the "
                              "rebuilt record — may still work in-game, test it)")
                log.warning(
                    "Imbue %s on %s: class 0x%08X not visible in rebuilt record; "
                    "staged anyway.", disp_name, item.name, class_hash)
        except Exception as e:
            verify_msg = f" (post-edit verify failed: {e}; staged anyway)"
            log.warning("Imbue %s verify exception: %s", disp_name, e)

        if not hasattr(self, "_staged_skill_files") or self._staged_skill_files is None:
            self._staged_skill_files = {}
        self._staged_skill_files["skill.pabgh"] = new_pabgh
        self._staged_skill_files["skill.pabgb"] = new_pabgb

        self._eb_status.setText(
            f"Added {disp_name} passive to {item.name}. Class was not allowed — "
            f"staged skill.pabgb (+{len(new_pabgb) - len(pabgb):+d}) + "
            f"skill.pabgh updates.{verify_msg}{gimmick_note} Click 'Apply to Game'."
        )


    @staticmethod
    def _is_weapon_item(rust_item: dict) -> bool:
        """True when the item is an equippable weapon eligible for bulk buff/imbue.

        Mirrors the set DMM's 'all weapon buffs' mod targeted: any string_key
        containing OneHand/TwoHand plus a few one-off weapon suffixes. We also
        require enchant_data_list to be non-empty so we have a slot to write into.
        """
        sk = (rust_item.get('string_key') or '')
        if not rust_item.get('enchant_data_list'):
            return False
        if 'OneHand' in sk or 'TwoHand' in sk:
            return True
        for suf in ('_Hammer', '_Sickle', '_MarniMusket', '_SuperWeapon'):
            if sk.endswith(suf):
                return True
        return False

    def _eb_bulk_apply_buffs_to_weapons(self) -> None:
        """Copy equip_buffs from the selected item onto every weapon — DMM-clone.

        Per-weapon, per-enchant-level: union the source buff list with the
        existing buff list, de-duplicating by buff ID and keeping the higher
        level. Existing buffs are preserved.
        """
        if not hasattr(self, '_buff_rust_items') or not self._buff_rust_items:
            QMessageBox.warning(self, "Copy Buffs to All Weapons",
                "Extract with Rust parser first (click 'Extract').")
            return
        if not hasattr(self, '_buff_current_item') or self._buff_current_item is None:
            QMessageBox.warning(self, "Copy Buffs to All Weapons",
                "Pick a SOURCE item first.\n\n"
                "This button copies the buffs from the currently-selected item\n"
                "onto every weapon. There's no built-in 'universal buff pack' —\n"
                "the selected item IS the template.\n\n"
                "Click an item in the table, give it the buffs you want, then\n"
                "click this button again.")
            return

        src_info = self._buff_rust_lookup.get(self._buff_current_item.item_key)
        if not src_info:
            QMessageBox.warning(self, "Copy Buffs to All Weapons",
                "Selected source item not found in Rust data.")
            return

        src_edl = src_info.get('enchant_data_list') or []
        src_buffs: list[dict] = []
        for ed in src_edl:
            for b in (ed.get('equip_buffs') or []):
                src_buffs.append(b)
        if not src_buffs:
            QMessageBox.information(self, "Copy Buffs to All Weapons",
                "The selected source item has no equip_buffs — nothing to copy.\n\n"
                "Use 'Add Buff' to give this item some buffs first, or pick\n"
                "a different item that already has the buff loadout you want.")
            return

        # De-dup the source list itself: highest level per buff ID
        src_max: dict[int, int] = {}
        for b in src_buffs:
            bid = int(b.get('buff', 0))
            lvl = int(b.get('level', 0))
            if bid <= 0:
                continue
            if lvl > src_max.get(bid, -1):
                src_max[bid] = lvl

        if not src_max:
            QMessageBox.information(self, "Copy Buffs to All Weapons",
                "Source item's buffs all have buff_id=0 — nothing to copy.")
            return

        weapons = [it for it in self._buff_rust_items if self._is_weapon_item(it)]
        if not weapons:
            QMessageBox.warning(self, "Copy Buffs to All Weapons",
                "No weapons found in iteminfo. Did extraction succeed?")
            return

        src_name = self._name_db.get_name(self._buff_current_item.item_key)
        if src_name.startswith('Unknown'):
            src_name = src_info.get('string_key') or f"item {src_info.get('key')}"

        buff_preview = ', '.join(
            f"{bid}(L{lvl})" for bid, lvl in list(src_max.items())[:6])
        if len(src_max) > 6:
            buff_preview += f", +{len(src_max) - 6} more"

        reply = QMessageBox.question(
            self, "Copy Buffs to All Weapons",
            f"Copy {len(src_max)} buff(s) from source item\n"
            f"  '{src_name}'\n"
            f"onto {len(weapons)} weapon(s)?\n\n"
            f"Buffs to copy: {buff_preview}\n\n"
            f"Existing buffs on weapons are preserved. Duplicates are merged —\n"
            f"the higher level wins.\n\n"
            f"Click 'Export as Mod' or 'Apply to Game' afterwards to write.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        items_changed = 0
        levels_touched = 0
        buffs_added_total = 0
        for it in weapons:
            edl = it.get('enchant_data_list') or []
            item_changed = False
            for ed in edl:
                existing = ed.get('equip_buffs') or []
                cur: dict[int, int] = {int(b.get('buff', 0)): int(b.get('level', 0))
                                       for b in existing if int(b.get('buff', 0)) > 0}
                before = dict(cur)
                for bid, lvl in src_max.items():
                    if cur.get(bid, -1) < lvl:
                        cur[bid] = lvl
                if cur != before:
                    ed['equip_buffs'] = [{'buff': bid, 'level': lvl}
                                         for bid, lvl in cur.items()]
                    levels_touched += 1
                    buffs_added_total += sum(1 for bid in cur
                                             if before.get(bid, -1) < cur[bid])
                    item_changed = True
            if item_changed:
                items_changed += 1

        if items_changed == 0:
            QMessageBox.information(self, "Universal Buffs — No Change",
                "All target weapons already had these buffs at this level or higher.")
            return

        self._buff_modified = True
        self._buff_refresh_stats()
        self._buff_status_label.setText(
            f"Universal Buffs: {len(src_max)} buff(s) broadcast to "
            f"{items_changed} weapon(s) ({levels_touched} enchant levels, "
            f"{buffs_added_total} additions). Click 'Apply to Game'."
        )
        QMessageBox.information(self, "Universal Buffs — Done",
            f"Broadcast {len(src_max)} buff(s) to {items_changed} weapon(s).\n\n"
            f"  Enchant levels touched: {levels_touched}\n"
            f"  Buff additions:         {buffs_added_total}\n\n"
            f"Click 'Apply to Game' (or 'Export as Mod') to write.")

    def _eb_bulk_imbue_all_weapons(self) -> None:
        """Apply the selected Imbue passive (Lightning, Bismuth, etc.) to every weapon.

        Per-weapon: get_imbue_plan + append passive + apply patches. Skill filter
        edits are aggregated and rebuilt ONCE so skill.pabgb only grows as needed.
        Skipped: weapons whose imbue plan returns ok=False (no compatible gimmick).
        """
        try:
            import imbue
            import crimson_rs
        except Exception as e:
            QMessageBox.critical(self, "Imbue All", f"Import failed: {e}")
            return

        if not hasattr(self, '_buff_rust_items') or not self._buff_rust_items:
            QMessageBox.warning(self, "Imbue All",
                "Extract with Rust parser first (click 'Extract').")
            return

        skill_id = self._eb_imbue_combo.currentData()
        if skill_id is None:
            QMessageBox.warning(self, "Imbue All",
                "Pick a skill from the Imbue dropdown first.")
            return
        skill_id = int(skill_id)
        catalog = imbue.get_passive_skill_catalog()
        sinfo = catalog.get(skill_id) or {}
        disp_name = (sinfo.get('pretty_name')
                     or sinfo.get('display')
                     or sinfo.get('name')
                     or f'skill_{skill_id}')
        skill_rec_name = sinfo.get('name') or disp_name

        weapons = [it for it in self._buff_rust_items if self._is_weapon_item(it)]
        if not weapons:
            QMessageBox.warning(self, "Imbue All",
                "No weapons found in iteminfo. Did extraction succeed?")
            return

        reply = QMessageBox.question(
            self, "Imbue All Weapons",
            f"Apply '{disp_name}' (skill {skill_id}) to {len(weapons)} weapon(s)?\n\n"
            f"For each weapon: appends the passive, applies the matching\n"
            f"gimmick + docking + cooltime/charge config. A single skill.pabgb\n"
            f"edit will whitelist every weapon class that needs it.\n\n"
            f"Weapons that already have the passive are skipped (no double-add).\n"
            f"Click 'Apply to Game' afterwards to write.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        # Pass 1 — per-weapon iteminfo edits + collect needed class hashes.
        applied = 0
        skipped_already = 0
        skipped_no_plan = 0
        needed_class_hashes: set[int] = set()
        warnings: list[str] = []
        for rust_item in weapons:
            psl = rust_item.get('equip_passive_skill_list') or []
            if any(p.get('skill') == skill_id for p in psl):
                skipped_already += 1
                continue
            try:
                plan = imbue.get_imbue_plan(skill_id, rust_item)
            except Exception as e:
                warnings.append(f"{rust_item.get('string_key')}: plan error {e}")
                skipped_no_plan += 1
                continue
            if not plan.get('ok'):
                skipped_no_plan += 1
                continue
            psl.append({'skill': skill_id, 'level': 3})
            rust_item['equip_passive_skill_list'] = psl
            for field, value in (plan.get('patches') or {}).items():
                rust_item[field] = value
            ch = rust_item.get('equip_type_info') or 0
            if ch:
                needed_class_hashes.add(int(ch))
            applied += 1

        if applied == 0:
            QMessageBox.information(self, "Imbue All — No Change",
                f"No weapons needed the change.\n"
                f"  Already had passive: {skipped_already}\n"
                f"  No imbue plan:       {skipped_no_plan}")
            return
        self._buff_modified = True

        # Pass 2 — single skill.pabgb rebuild covering every needed class hash.
        skill_msg = ""
        try:
            gp_widget = getattr(self, '_buff_game_path', None)
            gp_text = (gp_widget.text() or '').strip() if gp_widget is not None else ''
            if not gp_text:
                gp_text = getattr(self, '_game_path', '') or \
                    r'C:\Program Files (x86)\Steam\steamapps\common\Crimson Desert'
            staged = getattr(self, '_staged_skill_files', {}) or {}
            pabgh = staged.get('skill.pabgh') or crimson_rs.extract_file(
                gp_text, '0008', 'gamedata/binary__/client/bin', 'skill.pabgh')
            pabgb = staged.get('skill.pabgb') or crimson_rs.extract_file(
                gp_text, '0008', 'gamedata/binary__/client/bin', 'skill.pabgb')
            entries = imbue.parse_skill_pabgh(pabgh, len(pabgb))
            by_key = {k: (o, l) for (k, o, l) in entries}
            if skill_id not in by_key:
                skill_msg = (f"\n(Warning: skill {skill_id} not found in skill.pabgh "
                             f"— iteminfo edits stand, no skill filter edit applied.)")
            else:
                off, length = by_key[skill_id]
                rec = pabgb[off:off + length]
                missing = [ch for ch in needed_class_hashes
                           if not imbue.skill_allows_class(rec, ch)]
                if not missing:
                    skill_msg = "\nSkill filter already allowed every weapon class — no skill edit needed."
                else:
                    # Single-pass batched edit — avoids the iterative re-scan
                    # drift that produced "13 class hashes missing" failures.
                    def _edit(r):
                        return imbue.add_classes_to_skill_record(r, list(missing))
                    new_pabgh, new_pabgb = imbue.rebuild_skill_pair(
                        pabgh, pabgb, {skill_id: _edit})
                    # post-edit verification — non-fatal, staged either way.
                    # If a class hash somehow didn't land in every non-empty
                    # block, that usually means the block's class-list was
                    # count>64 (game-enforced cap) and got skipped; the edit
                    # is still the best we can do.
                    v_entries = imbue.parse_skill_pabgh(new_pabgh, len(new_pabgb))
                    v_by = {k: (o, l) for (k, o, l) in v_entries}
                    v_off, v_len = v_by[skill_id]
                    v_rec = new_pabgb[v_off:v_off + v_len]
                    bad = [ch for ch in missing if not imbue.skill_allows_class(v_rec, ch)]
                    if not hasattr(self, '_staged_skill_files') or self._staged_skill_files is None:
                        self._staged_skill_files = {}
                    self._staged_skill_files['skill.pabgh'] = new_pabgh
                    self._staged_skill_files['skill.pabgb'] = new_pabgb
                    delta = len(new_pabgb) - len(pabgb)
                    if bad:
                        bad_preview = ', '.join(f'0x{ch:08X}' for ch in bad[:5])
                        more = f' (+{len(bad) - 5} more)' if len(bad) > 5 else ''
                        log.warning(
                            "Imbue All: %d of %d class hashes not in every block "
                            "after rebuild (skill=%s). Staged anyway. Missing: %s%s",
                            len(bad), len(missing), skill_rec_name, bad_preview, more)
                        skill_msg = (
                            f"\nStaged skill.pabgb {delta:+d} bytes "
                            f"(added {len(missing) - len(bad)} of {len(missing)} class hashes to {skill_rec_name}).\n"
                            f"  Note: {len(bad)} hash(es) may not activate in all blocks "
                            f"(likely a game-cap limit). Test in-game.")
                    else:
                        skill_msg = (
                            f"\nStaged skill.pabgb {delta:+d} bytes "
                            f"(added {len(missing)} class hashes to {skill_rec_name}).")
        except Exception as e:
            log.exception("Imbue All: skill rebuild failed")
            skill_msg = (f"\n(Warning: iteminfo edits applied but skill.pabgb edit failed:\n  {e}\n"
                         f"You can still try Apply to Game — passives may not activate without this.)")

        self._buff_refresh_stats()
        self._buff_status_label.setText(
            f"Imbue All '{disp_name}': applied to {applied} weapon(s) "
            f"({skipped_already} already had it, {skipped_no_plan} no plan). "
            f"Click 'Apply to Game'."
        )
        warn_str = ('\n\nWarnings (first 5):\n  ' + '\n  '.join(warnings[:5])) if warnings else ''
        QMessageBox.information(self, "Imbue All — Done",
            f"Applied '{disp_name}' to {applied} weapon(s).\n\n"
            f"  Already had passive: {skipped_already}\n"
            f"  No imbue plan:       {skipped_no_plan}"
            f"{skill_msg}{warn_str}\n\n"
            f"Click 'Apply to Game' (or 'Export as Mod') to write.")

    def _imbue_show_coverage(self) -> None:
        """Show full coverage report for the currently selected imbue skill.

        Reads skill.pabgb to find which weapon classes are currently allowed,
        then computes the imbue.IteminfoIndex coverage diff. No file mutation —
        purely informational.
        """
        if self._index is None or not self._buff_rust_items:
            QMessageBox.warning(self, "Coverage Report",
                "Extract iteminfo first.")
            return
        sid_data = self._eb_imbue_combo.currentData()
        if sid_data is None:
            QMessageBox.warning(self, "Coverage Report",
                "Pick a skill from the Imbue dropdown first.")
            return
        sid = int(sid_data)

        try:
            import imbue, crimson_rs
            gp_widget = getattr(self, "_buff_game_path", None)
            gp_text = (gp_widget.text() or "").strip() if gp_widget is not None else ""
            if not gp_text:
                gp_text = getattr(self, "_game_path", "") or \
                    r"C:\Program Files (x86)\Steam\steamapps\common\Crimson Desert"
            staged = getattr(self, "_staged_skill_files", {}) or {}
            pabgh = staged.get("skill.pabgh") or crimson_rs.extract_file(
                gp_text, "0008", "gamedata/binary__/client/bin", "skill.pabgh")
            pabgb = staged.get("skill.pabgb") or crimson_rs.extract_file(
                gp_text, "0008", "gamedata/binary__/client/bin", "skill.pabgb")
            entries = imbue.parse_skill_pabgh(pabgh, len(pabgb))
            by = {k: (o, l) for k, o, l in entries}
            if sid not in by:
                QMessageBox.warning(self, "Coverage Report",
                    f"Skill {sid} not found in skill.pabgh.")
                return
            o, l = by[sid]
            rec = pabgb[o:o+l]
            # Probe every observed weapon class hash to see which the skill allows
            weapons = [it for it in self._buff_rust_items if self._is_weapon_item(it)]
            class_hashes = {int(w.get("equip_type_info") or 0)
                            for w in weapons if w.get("equip_type_info")}
            allowed = {ch for ch in class_hashes if imbue.skill_allows_class(rec, ch)}
        except Exception as e:
            QMessageBox.critical(self, "Coverage Report", f"Failed to read skill.pabgb:\n{e}")
            return

        cov = self._index.imbue_coverage(sid, allowed, weapons)
        catalog = imbue.get_passive_skill_catalog()
        sinfo = catalog.get(sid) or {}
        disp = (sinfo.get("pretty_name")
                or sinfo.get("display")
                or sinfo.get("name")
                or f"skill_{sid}")

        # Per-class table sorted by count desc.
        per_class_lines = []
        for ch, info in sorted(cov["per_class"].items(),
                               key=lambda kv: -kv[1]["count"]):
            mark = "ALLOWED" if info["currently_allowed"] else "BLOCKED"
            per_class_lines.append(
                f"  {mark:<8}  {info['count']:>3} weapons  "
                f"0x{ch:08X}  e.g. {info['sample_string_key']}"
            )

        body = (
            f"Skill: {disp} (id {sid})\n"
            f"Weapon population: {cov['weapon_count']}\n\n"
            f"Currently has passive on item:    {cov['weapons_with_passive']:>4}\n"
            f"Currently allowed by skill filter:{cov['weapons_in_filter_now']:>4}\n"
            f"After 'Imbue All Weapons':        {cov['weapons_in_filter_after']:>4}\n"
            f"Class hashes to add to filter:    {len(cov['missing_class_hashes'])}\n\n"
            f"Per weapon class:\n"
            + "\n".join(per_class_lines)
        )
        dlg = QDialog(self)
        dlg.setWindowTitle(f"Imbue Coverage — {disp}")
        dlg.resize(640, 480)
        layout = QVBoxLayout(dlg)
        text = QTextEdit()
        text.setFont(QFont("Consolas", 10))
        text.setReadOnly(True)
        text.setPlainText(body)
        layout.addWidget(text, 1)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dlg.accept)
        layout.addWidget(close_btn)
        dlg.exec()

    def _eb_universal_proficiency(self) -> None:
        """Remove character-weapon restrictions via equipslotinfo.pabgb.

        For each character record, expands every equip-type array to include
        all hashes that canonically belong in that slot type (determined by
        majority vote across all characters). Stages the result so Apply to
        Game / Export as Mod picks it up.
        """
        try:
            import crimson_rs
            import equipslotinfo_parser as esp
        except Exception as e:
            QMessageBox.critical(self, "Universal Proficiency",
                f"Import failed: {e}")
            return

        gp_widget = getattr(self, '_buff_game_path', None)
        gp_text = (gp_widget.text() or '').strip() if gp_widget is not None else ''
        if not gp_text:
            gp_text = getattr(self, '_game_path', '') or \
                r'C:\Program Files (x86)\Steam\steamapps\common\Crimson Desert'

        try:
            pabgh = crimson_rs.extract_file(
                gp_text, '0008', 'gamedata/binary__/client/bin', 'equipslotinfo.pabgh')
            pabgb = crimson_rs.extract_file(
                gp_text, '0008', 'gamedata/binary__/client/bin', 'equipslotinfo.pabgb')
        except Exception as e:
            QMessageBox.critical(self, "Universal Proficiency",
                f"Could not extract equipslotinfo:\n{e}")
            return

        records = esp.parse_all(pabgh, pabgb)

        from collections import Counter
        hash_votes: dict[int, Counter] = {}
        for rec in records:
            for e in rec.entries:
                key = (e.category_a, e.category_b)
                for h in e.etl_hashes:
                    if h not in hash_votes:
                        hash_votes[h] = Counter()
                    hash_votes[h][key] += 1

        hash_canonical = {h: v.most_common(1)[0][0] for h, v in hash_votes.items()}
        slot_hashes: dict[tuple[int, int], set[int]] = {}
        for h, slot in hash_canonical.items():
            slot_hashes.setdefault(slot, set()).add(h)

        total_added = 0
        for rec in records:
            for e in rec.entries:
                key = (e.category_a, e.category_b)
                candidates = slot_hashes.get(key, set())
                to_add = sorted(candidates - set(e.etl_hashes))
                if to_add:
                    e.etl_hashes.extend(to_add)
                    total_added += len(to_add)

        new_pabgh, new_pabgb = esp.serialize_all(records)

        if not hasattr(self, '_staged_equip_files') or self._staged_equip_files is None:
            self._staged_equip_files = {}
        self._staged_equip_files['equipslotinfo.pabgb'] = new_pabgb
        self._staged_equip_files['equipslotinfo.pabgh'] = new_pabgh

        # Find the top player-tribe hashes by frequency across all items.
        # These are the tribe_gender IDs that cover the main player characters.
        # We UNION these into every item — preserves original restrictions AND
        # adds player-character access. Clearing the list breaks items because
        # empty doesn't mean "any character" (some items use empty to mean "no
        # equipable" and rely on their non-empty prefab entries to gate).
        from collections import Counter
        tg_counter: Counter = Counter()
        if hasattr(self, '_buff_rust_items') and self._buff_rust_items:
            for it in self._buff_rust_items:
                if not it.get('equip_type_info'):
                    continue
                for pd in (it.get('prefab_data_list') or []):
                    for h in (pd.get('tribe_gender_list') or []):
                        tg_counter[h] += 1

        # Take the hashes that appear on >= 5% of equippable items.
        # That filters out per-NPC one-offs and keeps the shared player tribes.
        total_items = sum(1 for it in (self._buff_rust_items or [])
                          if it.get('equip_type_info'))
        threshold = max(1, total_items // 20)
        player_tribes = {h for h, c in tg_counter.items() if c >= threshold}

        # Game semantics (verified empirically):
        #   tribe_gender_list == []      → no restriction, ANY character can equip
        #   tribe_gender_list == [a,b,c] → only those tribes can equip
        # So: only union into lists that are ALREADY non-empty. Leaving an
        # already-empty list alone preserves the "open to all" state.
        # Converting [] → [12 player tribes] was actually RESTRICTING items
        # that had no restriction (breaking NPC default equips like Batz dagger).
        tg_unioned = 0
        if player_tribes and hasattr(self, '_buff_rust_items') and self._buff_rust_items:
            for it in self._buff_rust_items:
                pdl = it.get('prefab_data_list') or []
                for pd in pdl:
                    tg = pd.get('tribe_gender_list')
                    if not tg:
                        continue  # empty = already open, don't touch
                    existing = set(tg)
                    to_add = [h for h in player_tribes if h not in existing]
                    if to_add:
                        pd['tribe_gender_list'] = list(tg) + to_add
                        tg_unioned += 1
            if tg_unioned:
                self._buff_modified = True

        self._buff_status_label.setText(
            f"Universal Proficiency: +{total_added} slot hashes, "
            f"{tg_unioned} items unlocked. Click 'Apply to Game' or 'Export'.")
        QMessageBox.information(self, "Universal Proficiency — Staged",
            f"Equip slot filter: +{total_added} hashes across {len(records)} characters.\n"
            f"Item tribe/gender filter: unioned {len(player_tribes)} player-tribe hashes\n"
            f"into {tg_unioned} prefab entries (originals kept, player tribes added).\n\n"
            f"equipslotinfo: {len(pabgb):,} -> {len(new_pabgb):,} bytes\n\n"
            f"WARNING: CDUMM export is not supported with Universal Proficiency.\n"
            f"The expanded equipslotinfo causes the game to reject the overlay.\n"
            f"Use 'Apply to Game' instead.\n\n"
            f"Note: weapons may lack animations on non-native characters\n"
            f"(e.g. muskets on Kliff won't have fire/reload anims).\n\n"
            f"Click 'Apply to Game' to write.")

    # Per-character tribe_gender hashes (confirmed via exclusive-item analysis 2026-04-17).
    # Kliff has 11 (superset of both), Damiane has 4, Oongka has 6.
    _CHAR_TRIBE_HASHES = {
        1: {0x13FB2B6E, 0x26BE971F, 0x87D08287, 0x8BF46446,  # Kliff
            0xABFCD791, 0xBFA1F64B, 0xD0A2E1EF, 0xF96C1DD4,
            0xFC66D914, 0xFE7169E2, 0xFF16A579},
        4: {0x26BE971F, 0x8BF46446, 0xABFCD791, 0xF96C1DD4},  # Damiane
        6: {0x13FB2B6E, 0x87D08287, 0xBFA1F64B, 0xD0A2E1EF,  # Oongka
            0xFC66D914, 0xFE7169E2},
    }
    # Union of all 3 = the 12 player hashes (plus 0xF21FE2D6 unknown/NPC)
    _PLAYER_TRIBE_HASHES = _CHAR_TRIBE_HASHES[1] | _CHAR_TRIBE_HASHES[4] | _CHAR_TRIBE_HASHES[6] | {0xF21FE2D6}
    _PLAYER_CHAR_KEYS = {1, 4, 6}  # Only expand these characters

    def _eb_universal_proficiency_v2(self) -> None:
        """Make ALL items equippable by ALL 3 player characters (Kliff/Damiane/Oongka).

        Two targeted changes:
        1. iteminfo tribe_gender: union the 12 known player tribe hashes into
           every item with a non-empty tribe_gender_list. Empty lists (already
           open to all) are untouched. Never clears or removes hashes.
        2. equipslotinfo slot expansion: for each of the 3 player characters,
           collect equip_type hashes from the other 2 players' MATCHING slot
           categories and add them. Weapons stay in weapon slots, armor in
           armor slots. NPC characters (201, 701, etc.) are NOT modified.

        Architecture:
        - iteminfo → staged for Apply to Game (group 0058)
        - equipslotinfo pair → deployed immediately to group 0059
        """
        if not hasattr(self, '_buff_rust_items') or not self._buff_rust_items:
            QMessageBox.warning(self, "Universal Prof v2",
                "Extract with Rust parser first (click 'Extract (Rust)').")
            return

        reply = QMessageBox.question(
            self, "Universal Proficiency v2",
            "Make ALL items equippable by Kliff, Damiane, and Oongka.\n\n"
            "Changes:\n"
            "1. Adds player tribe hashes to every restricted item\n"
            "   (items already open to all are untouched)\n"
            "2. Expands equip slots on the 3 player characters ONLY\n"
            "   (weapons \u2192 weapon slots, armor \u2192 armor slots)\n"
            "   NPCs/mercenaries are NOT modified.\n\n"
            "Stacks with buff mods, dye mods, and other ItemBuffs edits:\n"
            "the tool now re-extracts from your existing 0058/ overlay on\n"
            "each Extract, so prior edits (UP v2, Make Dyeable, etc.) are\n"
            "preserved — you can pile edits on top across sessions.\n\n"
            "Note: weapons may lack animations on non-native characters.\n"
            "Deploy via Apply to Game.\n\n"
            "Continue?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply != QMessageBox.Yes:
            return

        # ── Step 1: tribe_gender union (iteminfo) ──
        player_tribes = self._PLAYER_TRIBE_HASHES

        tg_unioned = 0
        tg_added_total = 0
        for it in self._buff_rust_items:
            if not it.get('equip_type_info'):
                continue
            for pd in (it.get('prefab_data_list') or []):
                tg = pd.get('tribe_gender_list')
                if not tg:
                    continue  # empty = already open, don't touch
                existing = set(tg)
                to_add = sorted(player_tribes - existing)
                if to_add:
                    pd['tribe_gender_list'] = list(tg) + to_add
                    tg_unioned += 1
                    tg_added_total += len(to_add)

        if tg_unioned:
            self._buff_modified = True

        # ── Step 2: targeted equipslotinfo expansion (slot-aware, players only) ──
        equip_msg = ""
        try:
            import crimson_rs
            import equipslotinfo_parser as esp

            gp_widget = getattr(self, '_buff_game_path', None)
            gp_text = (gp_widget.text() or '').strip() if gp_widget is not None else ''
            if not gp_text:
                gp_text = getattr(self, '_game_path', '') or \
                    r'C:\Program Files (x86)\Steam\steamapps\common\Crimson Desert'

            es_pabgh = crimson_rs.extract_file(
                gp_text, '0008', 'gamedata/binary__/client/bin', 'equipslotinfo.pabgh')
            es_pabgb = crimson_rs.extract_file(
                gp_text, '0008', 'gamedata/binary__/client/bin', 'equipslotinfo.pabgb')
            es_records = esp.parse_all(es_pabgh, es_pabgb)

            # Build per-category hash pool from PLAYER characters only
            player_keys = self._PLAYER_CHAR_KEYS
            player_records = [r for r in es_records if r.key in player_keys]

            # For each slot category, collect ALL hashes across all 3 players
            category_hashes: dict[tuple[int, int], set[int]] = {}
            for rec in player_records:
                for e in rec.entries:
                    key = (e.category_a, e.category_b)
                    category_hashes.setdefault(key, set()).update(e.etl_hashes)

            # Expand: for each player character's slot, add hashes from the
            # SAME category that other players have. NPC records untouched.
            total_slot_added = 0
            for rec in es_records:
                if rec.key not in player_keys:
                    continue  # Skip NPCs/mercenaries
                for e in rec.entries:
                    key = (e.category_a, e.category_b)
                    pool = category_hashes.get(key, set())
                    to_add = sorted(pool - set(e.etl_hashes))
                    if to_add:
                        e.etl_hashes.extend(to_add)
                        total_slot_added += len(to_add)

            new_es_pabgh, new_es_pabgb = esp.serialize_all(es_records)

            # DEPLOY equipslotinfo directly to 0059/ as a separate overlay.
            # User-confirmed empirically (2026-04-21) that single-overlay
            # bundling of iteminfo + equipslotinfo breaks UP v2 for guns on
            # Kliff. v1.0.3 used split deployment (0058/ + 0059/) and it
            # worked. Reverting to that.
            self._buff_deploy_equipslotinfo_0059(gp_text, new_es_pabgb, new_es_pabgh)
            # Clear any previously-staged equipslotinfo so _buff_apply_to_game
            # does NOT bundle it into the iteminfo overlay alongside.
            if hasattr(self, '_staged_equip_files') and self._staged_equip_files:
                for k in ('equipslotinfo.pabgb', 'equipslotinfo.pabgh'):
                    self._staged_equip_files.pop(k, None)
            self._buff_modified = True

            log.info("UP v2: staged equipslotinfo (pabgb=%d pabgh=%d, "
                     "+%d hashes, players only: %s) — will deploy via Apply to Game",
                     len(new_es_pabgb), len(new_es_pabgh),
                     total_slot_added, sorted(player_keys))

            equip_msg = (f"\nEquipslotinfo: +{total_slot_added} hashes across "
                         f"{len(player_records)} player characters \u2192 staged "
                         f"for the next Apply to Game\n"
                         f"(NPCs untouched, slot categories preserved)")
        except Exception as e:
            log.exception("UP v2: equipslotinfo expansion failed")
            equip_msg = f"\nEquipslotinfo expansion failed: {e}"

        # ── Step 3: Kliff gun fix (characterinfo.pabgb) ──
        charinfo_msg = ""
        gun_reply = QMessageBox.question(
            self, "Kliff Gun Fix",
            "Universal Proficiency needs one more patch to fully work:\n\n"
            "Kliff can't use muskets/pistols without a characterinfo fix\n"
            "(copies Damiane's upper action chart + Oongka's gameplay data\n"
            "to Kliff so muskets attach and fire correctly).\n\n"
            "Apply Kliff Gun Fix now?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if gun_reply == QMessageBox.Yes:
            charinfo_msg = self._stage_kliff_gun_fix(gp_text)

        buff_slot = f"{self._buff_overlay_spin.value():04d}"
        self._buff_status_label.setText(
            f"Prof v2 staged: {tg_unioned} items + {total_slot_added} slot hashes. "
            f"Click 'Apply to Game' to deploy everything to {buff_slot}/.")
        QMessageBox.information(self, "Universal Proficiency v2 — Staged",
            f"Tribe restriction: added {len(player_tribes)} player tribe hashes\n"
            f"to {tg_unioned} restricted items (+{tg_added_total} total).\n"
            f"{equip_msg}{charinfo_msg}\n\n"
            f"Click 'Apply to Game' to deploy everything to {buff_slot}/.\n"
            f"Apply will also include any buff/stat/dye edits you've made\n"
            f"this session.\n\n"
            f"Note: weapons may lack animations on non-native characters.")

    def _stage_kliff_gun_fix(self, game_path: str) -> str:
        """Load characterinfo, apply the Kliff gun 2-field copy, stage for deploy."""
        try:
            import crimson_rs
            from characterinfo_full_parser import parse_all_entries as ci_parse_all
            dp = 'gamedata/binary__/client/bin'
            ci_body = bytes(crimson_rs.extract_file(game_path, '0008', dp, 'characterinfo.pabgb'))
            ci_gh = bytes(crimson_rs.extract_file(game_path, '0008', dp, 'characterinfo.pabgh'))
            all_ci = ci_parse_all(ci_body, ci_gh)

            by_name = {e.get('name'): e for e in all_ci}
            if not all(n in by_name for n in ('Kliff', 'Damian', 'Oongka')):
                return "\nKliff Gun Fix: could not find all 3 player chars."

            kliff = by_name['Kliff']
            damian = by_name['Damian']
            oongka = by_name['Oongka']

            ci_data = bytearray(ci_body)

            upper_off = kliff['_upperActionChartPackageGroupName_offset']
            damian_upper_off = damian['_upperActionChartPackageGroupName_offset']
            damian_upper = struct.unpack_from('<I', ci_body, damian_upper_off)[0]
            struct.pack_into('<I', ci_data, upper_off, damian_upper)

            gp_off = kliff['_characterGamePlayDataName_offset']
            oongka_gp_off = oongka['_characterGamePlayDataName_offset']
            oongka_gp = struct.unpack_from('<I', ci_body, oongka_gp_off)[0]
            struct.pack_into('<I', ci_data, gp_off, oongka_gp)

            if not hasattr(self, '_staged_charinfo_files') or self._staged_charinfo_files is None:
                self._staged_charinfo_files = {}
            self._staged_charinfo_files['characterinfo.pabgb'] = bytes(ci_data)
            self._staged_charinfo_files['characterinfo.pabgh'] = ci_gh
            self._buff_modified = True

            log.info("Kliff gun fix staged: upperAC=0x%08x (from Damian), "
                     "gamePlay=0x%08x (from Oongka)", damian_upper, oongka_gp)
            return (f"\nKliff Gun Fix: staged \u2014 upperAC \u2190 Damian "
                    f"(0x{damian_upper:08X}), gamePlay \u2190 Oongka (0x{oongka_gp:08X})")
        except Exception as e:
            log.exception("Kliff gun fix failed")
            return f"\nKliff Gun Fix failed: {e}"

    def _buff_deploy_equipslotinfo_0059(self, game_path: str,
                                         new_pabgb: bytes,
                                         new_pabgh: bytes) -> None:
        """Write equipslotinfo.pabgb/.pabgh to 0059/ as a separate
        overlay + register in PAPGT. Mirrors v1.0.3 behavior.

        Empirically proven (2026-04-21): single-overlay bundling of
        iteminfo + equipslotinfo in 0058/ breaks UP v2 -- muskets and
        blasters become unequippable on Kliff even though both files
        individually contain correct data. Splitting into 0058/ (item)
        + 0059/ (slot) restores function.
        """
        import crimson_rs, shutil, tempfile
        INTERNAL_DIR = "gamedata/binary__/client/bin"
        GROUP = "0059"
        with tempfile.TemporaryDirectory() as tmp:
            build_dir = os.path.join(tmp, GROUP)
            b = crimson_rs.PackGroupBuilder(
                build_dir, crimson_rs.Compression.NONE, crimson_rs.Crypto.NONE)
            b.add_file(INTERNAL_DIR, "equipslotinfo.pabgb", new_pabgb)
            b.add_file(INTERNAL_DIR, "equipslotinfo.pabgh", new_pabgh)
            pamt_bytes = bytes(b.finish())
            pamt_checksum = crimson_rs.parse_pamt_bytes(pamt_bytes)["checksum"]
            dst = os.path.join(game_path, GROUP)
            if os.path.isdir(dst):
                shutil.rmtree(dst)
            os.makedirs(dst, exist_ok=True)
            for fname in os.listdir(build_dir):
                shutil.copy2(os.path.join(build_dir, fname),
                             os.path.join(dst, fname))
        papgt_path = os.path.join(game_path, "meta", "0.papgt")
        papgt = crimson_rs.parse_papgt_file(papgt_path)
        papgt["entries"] = [e for e in papgt["entries"]
                            if e.get("group_name") != GROUP]
        papgt = crimson_rs.add_papgt_entry(
            papgt, GROUP, pamt_checksum, 0, 16383)
        crimson_rs.write_papgt_file(papgt, papgt_path)
        log.info("equipslotinfo deployed to %s/ (pabgb=%d, pabgh=%d, checksum=0x%08X)",
                 GROUP, len(new_pabgb), len(new_pabgh), pamt_checksum)

    def _eb_bulk_make_dyeable(self) -> None:
        """Flip is_dyeable + is_editable_grime on every equipment item.

        Equipment = anything with equip_type_info != 0 (weapons, armor,
        accessories, mount gear). Items already dyeable are left alone so
        re-running is a no-op.
        """
        if not self._buff_rust_items:
            QMessageBox.warning(self, "Make Dyeable",
                "Extract iteminfo first.")
            return

        equipment = [it for it in self._buff_rust_items
                     if it.get("equip_type_info")]
        candidates = [it for it in equipment
                      if not it.get("is_dyeable")]
        if not candidates:
            QMessageBox.information(self, "Make Dyeable — Nothing to do",
                "Every equipment item is already marked dyeable.")
            return

        # Bucket the changes by category so the user sees exactly what they're
        # touching before clicking Yes.
        from collections import Counter
        by_cat = Counter()
        for it in candidates:
            cat = it.get("category_info") or 0
            by_cat[cat] += 1
        cat_lines = []
        for cat, n in by_cat.most_common(10):
            label = self._index.category_label(cat) if self._index else f"category_{cat}"
            cat_lines.append(f"  {n:>4}  {label}")
        more = (f"\n  + {len(by_cat) - 10} more categories"
                if len(by_cat) > 10 else "")

        reply = QMessageBox.question(
            self, "Make All Equipment Dyeable",
            f"Flip is_dyeable + is_editable_grime → 1 on "
            f"{len(candidates)} of {len(equipment)} equipment items?\n\n"
            f"By category:\n" + "\n".join(cat_lines) + more + "\n\n"
            f"Vanilla 530 dyeable → after this {len(candidates) + 530}.\n"
            f"Items without a dye palette in their prefab will simply not\n"
            f"render dye changes — the flag never crashes the game.\n\n"
            f"Click 'Apply to Game' afterwards to write.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        flipped = 0
        for it in candidates:
            it["is_dyeable"] = 1
            it["is_editable_grime"] = 1
            flipped += 1
        self._buff_modified = True
        self._buff_refresh_stats()
        self._buff_status_label.setText(
            f"Marked {flipped} equipment item(s) as dyeable. "
            f"Click 'Apply to Game' to write."
        )
        QMessageBox.information(self, "Make Dyeable — Done",
            f"Set is_dyeable + is_editable_grime = 1 on {flipped} item(s).\n\n"
            f"Open the Dye tab after Apply to Game to dye them — items without\n"
            f"prefab palettes will appear in the list but show no color change\n"
            f"in-game (this is normal; the engine silently skips them).")

    def _buff_show_stat_template(self) -> None:
        """Show the modal stat template for the currently selected item's cluster.

        Read-only advisory dialog — tells the user which stats and buffs the
        game typically applies to items with the same (item_type, item_tier).
        Useful for staying within in-spec value ranges and discovering which
        stats are even valid for a given equipment slot.
        """
        if self._index is None:
            QMessageBox.warning(self, "Suggest Stats", "Extract iteminfo first.")
            return
        if not getattr(self, "_buff_current_item", None):
            QMessageBox.warning(self, "Suggest Stats",
                "Select an item first — the suggestion is based on its "
                "item_type + tier cluster.")
            return
        cur = self._buff_rust_lookup.get(self._buff_current_item.item_key)
        if not cur:
            return
        it_type = cur.get("item_type") or 0
        tier = cur.get("item_tier") or 0
        tpl = self._index.stat_template_for(it_type, tier)
        if not tpl:
            QMessageBox.information(self, "Suggest Stats",
                f"No items found with item_type={it_type}, tier={tier}.")
            return

        # Resolve buff IDs to friendly names if we have them
        buff_names = getattr(self, "_EQUIP_BUFF_NAMES", {}) or {}

        def _stat_label(sid: int) -> str:
            n = BUFF_NAMES.get(sid) if sid in BUFF_NAMES else None
            return n if n else f"stat_{sid}"

        lines = [
            f"Stat template for item_type={it_type}, tier={tier}",
            f"Cluster size: {tpl['cluster_size']} item(s) "
            f"({tpl['with_enchants']} with enchant data)",
            "─" * 70,
            "",
            "stat_list_static (i64 flat values):",
        ]
        for sid, n in tpl["common_stats_static"]:
            lines.append(f"  {n:>4} items use  {sid:>10}  {_stat_label(sid)}")
        lines.append("\nstat_list_static_level (i8 rate values):")
        for sid, n in tpl["common_stats_level"]:
            lines.append(f"  {n:>4} items use  {sid:>10}  {_stat_label(sid)}")
        lines.append("\nregen_stat_list:")
        for sid, n in tpl["common_regen_stats"]:
            lines.append(f"  {n:>4} items use  {sid:>10}  {_stat_label(sid)}")
        lines.append("\nmax_stat_list:")
        for sid, n in tpl["common_max_stats"]:
            lines.append(f"  {n:>4} items use  {sid:>10}  {_stat_label(sid)}")
        lines.append("\nequip_buffs (top 20):")
        for bid, n in tpl["common_buffs"]:
            bn = buff_names.get(bid, f"buff_{bid}")
            lines.append(f"  {n:>4} items use  {bid:>10}  {bn}")
        lines.append("\n(Read-only advisory. Use Add Stat / Add Buff to apply.)")

        dlg = QDialog(self)
        dlg.setWindowTitle(
            f"Stat Template — item_type={it_type}, tier={tier}")
        dlg.resize(640, 560)
        v = QVBoxLayout(dlg)
        text = QTextEdit()
        text.setFont(QFont("Consolas", 10))
        text.setReadOnly(True)
        text.setPlainText("\n".join(lines))
        v.addWidget(text, 1)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dlg.accept)
        v.addWidget(close_btn)
        dlg.exec()

    def _buff_open_item_inspector(self, *_args, item_key: int = 0) -> None:
        """Deep-dive read-only inspector for any single item.

        Renders all 105 top-level fields as a searchable QTreeWidget. Nested
        dicts/lists become expandable nodes. Includes a cross-reference panel
        showing other items in iteminfo that reference this one (e.g. as a
        crafting transmutation material).
        """
        if self._index is None or not self._buff_rust_items:
            QMessageBox.warning(self, "Inspect Item", "Extract iteminfo first.")
            return

        # Default to the currently selected item; allow caller to override.
        if not item_key:
            cur = getattr(self, "_buff_current_item", None)
            if cur is None:
                QMessageBox.warning(self, "Inspect Item",
                    "Select an item first (or right-click an item → Inspect).")
                return
            item_key = int(cur.item_key)

        item = self._buff_rust_lookup.get(int(item_key))
        if not item:
            QMessageBox.warning(self, "Inspect Item",
                f"Item key {item_key} not found in extracted iteminfo.")
            return

        from PySide6.QtWidgets import (QTreeWidget, QTreeWidgetItem,
                                        QHeaderView as _HV)

        try:
            disp_name = self._name_db.get_name(item_key)
            if disp_name.startswith("Unknown"):
                disp_name = ""
        except Exception:
            disp_name = ""
        title = f"Inspect — {item.get('string_key') or 'item'} (key {item_key})"
        if disp_name:
            title += f" — {disp_name}"

        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg.resize(900, 720)
        v = QVBoxLayout(dlg)

        # Header strip — quick stats
        header = QLabel(
            f"<b>key:</b> {item_key} &nbsp;&nbsp; "
            f"<b>string_key:</b> {item.get('string_key', '')} &nbsp;&nbsp; "
            f"<b>category:</b> {self._index.category_label(item.get('category_info', 0))} &nbsp;&nbsp; "
            f"<b>item_type:</b> {item.get('item_type', '?')} &nbsp;&nbsp; "
            f"<b>tier:</b> {item.get('item_tier', '?')}"
        )
        header.setStyleSheet(f"padding: 6px; color: {COLORS['text_dim']};")
        header.setTextFormat(Qt.RichText)
        header.setWordWrap(True)
        v.addWidget(header)

        # Field-search box
        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Filter fields:"))
        filter_input = QLineEdit()
        filter_input.setPlaceholderText("Type to hide non-matching fields (case-insensitive substring)…")
        filter_row.addWidget(filter_input, 1)
        only_set_chk = QCheckBox("Only show set / non-default")
        only_set_chk.setChecked(False)
        filter_row.addWidget(only_set_chk)
        v.addLayout(filter_row)

        tree = QTreeWidget()
        tree.setColumnCount(3)
        tree.setHeaderLabels(["Field", "Type", "Value"])
        tree.setAlternatingRowColors(True)
        tree.header().setSectionResizeMode(0, _HV.Interactive)
        tree.header().setSectionResizeMode(1, _HV.Interactive)
        tree.header().setSectionResizeMode(2, _HV.Stretch)
        tree.setColumnWidth(0, 280)
        tree.setColumnWidth(1, 110)
        v.addWidget(tree, 1)

        def _value_repr(v) -> str:
            if isinstance(v, (dict, list)):
                try:
                    s = json.dumps(v, ensure_ascii=False, default=str)
                except Exception:
                    s = repr(v)
            else:
                s = repr(v)
            if len(s) > 200:
                s = s[:200] + f"… ({len(s)} chars)"
            return s

        def _is_default(value) -> bool:
            return value in (None, 0, 0.0, "", [], {})

        def _add_node(parent: QTreeWidgetItem | None, label: str, value):
            type_str = type(value).__name__
            if isinstance(value, list):
                type_str = f"list[{len(value)}]"
            elif isinstance(value, dict):
                type_str = f"dict({len(value)})"
            row = [label, type_str, _value_repr(value) if not isinstance(value, (dict, list)) else ""]
            node = QTreeWidgetItem(row)
            if parent is None:
                tree.addTopLevelItem(node)
            else:
                parent.addChild(node)
            # Recurse into nested structures
            if isinstance(value, dict):
                for k in sorted(value.keys()):
                    _add_node(node, str(k), value[k])
            elif isinstance(value, list):
                for i, elem in enumerate(value):
                    _add_node(node, f"[{i}]", elem)
            return node

        # Top-level fields, sorted
        for field in sorted(item.keys()):
            _add_node(None, field, item[field])

        # Cross-reference scan — what other items reference this key as a
        # crafting input or convert target. O(n*k) scan; ~6024 items × small
        # field set ≈ 30 ms. Run once when dialog opens.
        target_key = int(item_key)
        cross_refs: list[tuple[str, str]] = []
        for other in self._buff_rust_items:
            ok = other.get("key")
            if ok == target_key:
                continue
            sk = other.get("string_key", "")
            if target_key in (other.get("transmutation_material_item_list") or []):
                cross_refs.append(("transmutation_material_item_list", f"{ok}  {sk}"))
            if target_key in (other.get("sealable_money_info_list") or []):
                cross_refs.append(("sealable_money_info_list", f"{ok}  {sk}"))
            if other.get("packed_item_info") == target_key:
                cross_refs.append(("packed_item_info", f"{ok}  {sk}"))
            if other.get("unpacked_item_info") == target_key:
                cross_refs.append(("unpacked_item_info", f"{ok}  {sk}"))
            if other.get("convert_item_info_by_drop_npc") == target_key:
                cross_refs.append(("convert_item_info_by_drop_npc", f"{ok}  {sk}"))

        if cross_refs:
            xref_node = QTreeWidgetItem(
                [f"⤴ Referenced by {len(cross_refs)} other item(s)", "xref", ""])
            xref_node.setExpanded(True)
            tree.addTopLevelItem(xref_node)
            for field, who in cross_refs[:200]:
                QTreeWidgetItem(xref_node, [field, "ref", who])
            if len(cross_refs) > 200:
                QTreeWidgetItem(xref_node, ["…", "trim", f"+{len(cross_refs) - 200} more refs"])

        def _apply_filter():
            q = filter_input.text().strip().lower()
            only_set = only_set_chk.isChecked()
            for i in range(tree.topLevelItemCount()):
                top = tree.topLevelItem(i)
                field = top.text(0).lower()
                value_str = top.text(2).lower()
                # Determine "is default" by checking if value column is one of the empties
                vstr = top.text(2)
                empty_repr = vstr in ("None", "0", "0.0", "''", '""', "[]", "{}", "")
                show = True
                if q and (q not in field) and (q not in value_str):
                    show = False
                if only_set and empty_repr and top.text(1).startswith(("list[0", "dict(0")):
                    show = False
                top.setHidden(not show)

        filter_input.textChanged.connect(_apply_filter)
        only_set_chk.toggled.connect(_apply_filter)

        btn_row = QHBoxLayout()
        copy_btn = QPushButton("Copy as JSON")
        copy_btn.setToolTip("Copy the full item dict to the clipboard as pretty JSON.")
        def _copy():
            QApplication.clipboard().setText(
                json.dumps(item, indent=2, ensure_ascii=False, default=str))
            copy_btn.setText("Copied!")
            QTimer.singleShot(1500, lambda: copy_btn.setText("Copy as JSON"))
        copy_btn.clicked.connect(_copy)
        btn_row.addWidget(copy_btn)

        diff_btn = QPushButton("Diff Against...")
        diff_btn.setToolTip("Open the Item Diff dialog with this item pre-loaded as A.")
        diff_btn.clicked.connect(lambda: (dlg.accept(),
            self._buff_open_item_diff_dialog(initial_a=int(item_key))))
        btn_row.addWidget(diff_btn)

        btn_row.addStretch(1)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dlg.accept)
        btn_row.addWidget(close_btn)
        v.addLayout(btn_row)

        dlg.exec()

    def _buff_open_item_diff_dialog(self, *_args, initial_a: int = 0) -> None:
        """Pick two items by name → show field-by-field diff between them.

        Useful for reverse-engineering Potter's mods: compare a working modded
        item against the vanilla one, or compare two items where only one of
        them activates a gimmick correctly.
        """
        if self._index is None or not self._buff_rust_items:
            QMessageBox.warning(self, "Item Diff",
                "Extract iteminfo first.")
            return

        # Item picker list (label = "key — string_key — display_name")
        choices: list[tuple[int, str]] = []
        for it in self._buff_rust_items:
            k = it["key"]
            sk = it.get("string_key", "")
            try:
                disp = self._name_db.get_name(k)
                if disp.startswith("Unknown"):
                    disp = ""
            except Exception:
                disp = ""
            label = f"{k} — {sk}" + (f" — {disp}" if disp else "")
            choices.append((k, label))
        choices.sort(key=lambda kv: kv[1])

        dlg = QDialog(self)
        dlg.setWindowTitle("Item Diff")
        dlg.resize(900, 700)
        v = QVBoxLayout(dlg)

        picker_row = QHBoxLayout()
        picker_row.addWidget(QLabel("A:"))
        a_combo = QComboBox()
        a_combo.setEditable(True)
        a_combo.setInsertPolicy(QComboBox.NoInsert)
        from PySide6.QtWidgets import QCompleter
        a_combo.setMinimumWidth(280)
        for k, lbl in choices:
            a_combo.addItem(lbl, k)
        a_combo.completer().setCompletionMode(QCompleter.PopupCompletion)
        a_combo.completer().setFilterMode(Qt.MatchContains)
        picker_row.addWidget(a_combo, 1)

        picker_row.addWidget(QLabel("B:"))
        b_combo = QComboBox()
        b_combo.setEditable(True)
        b_combo.setInsertPolicy(QComboBox.NoInsert)
        b_combo.setMinimumWidth(280)
        for k, lbl in choices:
            b_combo.addItem(lbl, k)
        b_combo.completer().setCompletionMode(QCompleter.PopupCompletion)
        b_combo.completer().setFilterMode(Qt.MatchContains)
        picker_row.addWidget(b_combo, 1)

        diff_btn = QPushButton("Diff")
        diff_btn.setObjectName("accentBtn")
        picker_row.addWidget(diff_btn)
        v.addLayout(picker_row)

        # Pre-fill A from caller (e.g. context menu) and B from current selection.
        if initial_a:
            i = a_combo.findData(int(initial_a))
            if i >= 0:
                a_combo.setCurrentIndex(i)
        cur = getattr(self, "_buff_current_item", None)
        if cur:
            i = b_combo.findData(int(cur.item_key))
            if i >= 0:
                b_combo.setCurrentIndex(i)

        text = QTextEdit()
        text.setFont(QFont("Consolas", 10))
        text.setReadOnly(True)
        text.setPlaceholderText("Pick two items and click Diff.")
        v.addWidget(text, 1)

        def _short_repr(value, limit: int = 200) -> str:
            try:
                s = json.dumps(value, ensure_ascii=False, default=str)
            except Exception:
                s = repr(value)
            if len(s) > limit:
                s = s[:limit] + f"… ({len(s)} chars)"
            return s

        def _do_diff():
            ka = a_combo.currentData()
            kb = b_combo.currentData()
            if ka is None or kb is None or ka == kb:
                text.setPlainText("Pick two distinct items.")
                return
            diffs = self._index.diff_items(int(ka), int(kb))
            if not diffs:
                text.setPlainText("Items are identical across all 105 fields.")
                return
            lines = [
                f"{len(diffs)} field(s) differ between {ka} and {kb}.",
                "─" * 80,
            ]
            for d in diffs:
                lines.append(f"\n• {d['field']}")
                lines.append(f"    A = {_short_repr(d['value_a'])}")
                lines.append(f"    B = {_short_repr(d['value_b'])}")
            text.setPlainText("\n".join(lines))

        diff_btn.clicked.connect(_do_diff)
        if initial_a and cur:
            _do_diff()

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dlg.accept)
        v.addWidget(close_btn)
        dlg.exec()


    def _buff_on_buff_selected(self, index: int) -> None:
        buff_key = self._eb_buff_combo.currentData()
        if buff_key and buff_key in self._buff_community_ranges:
            mn, mx, vtype = self._buff_community_ranges[buff_key]
            self._eb_buff_level.setRange(mn, mx)
            if self._eb_buff_level.value() > mx:
                self._eb_buff_level.setValue(mx)
            if self._eb_buff_level.value() < mn:
                self._eb_buff_level.setValue(mn)
            type_label = vtype if vtype else "?"
            self._buff_range_label.setText(f"[{mn}-{mx}] {type_label}")
            self._eb_buff_level.setToolTip(
                f"Value range: {mn} - {mx} ({type_label})\n"
                f"Community-verified range from buff_names_community.json")
        else:
            self._eb_buff_level.setRange(0, 100)
            self._buff_range_label.setText("[0-100] unverified")
            self._eb_buff_level.setToolTip(
                "Buff level (0-100, range unknown)\n"
                "Help us: verify in-game and contribute to buff_names_community.json")


    def _eb_add_buff(self) -> None:
        if not hasattr(self, '_buff_rust_items') or not self._buff_rust_items:
            QMessageBox.warning(self, "Add Buff", "Extract with Rust parser first.")
            return
        if not hasattr(self, '_buff_current_item') or self._buff_current_item is None:
            QMessageBox.warning(self, "Add Buff", "Select an item first.")
            return

        rust_info = self._buff_rust_lookup.get(self._buff_current_item.item_key)
        if not rust_info:
            QMessageBox.warning(self, "Add Buff", "Item not found in Rust data.")
            return

        edl = rust_info.get('enchant_data_list', [])
        if not edl:
            QMessageBox.warning(self, "Add Buff",
                "This item has no enchant data.\n"
                "Only equippable items (weapons, armor, accessories) can have buffs.")
            return

        buff_key = self._eb_buff_combo.currentData()
        buff_level = self._eb_buff_level.value()
        buff_name = self._EQUIP_BUFF_NAMES.get(buff_key, f"Buff {buff_key}")

        added = 0
        target_level = self._eb_level_target.currentData()

        for idx, ed in enumerate(edl):
            if target_level != -1 and idx != target_level:
                continue
            existing = ed.get('equip_buffs', [])
            already = any(b['buff'] == buff_key for b in existing)
            if already:
                for b in existing:
                    if b['buff'] == buff_key:
                        b['level'] = buff_level
                added += 1
            else:
                existing.append({'buff': buff_key, 'level': buff_level})
                ed['equip_buffs'] = existing
                added += 1

        self._buff_modified = True
        self._buff_refresh_stats()
        display_name = self._name_db.get_name(self._buff_current_item.item_key)
        level_str = f"level +{target_level}" if target_level >= 0 else f"{added} enchant levels"
        self._buff_status_label.setText(
            f"Added {buff_name} Lv{buff_level} to {display_name} ({level_str}). "
            f"Click 'Export as Mod' to write."
        )


    def _eb_remove_buff(self) -> None:
        if not hasattr(self, '_buff_rust_items') or not self._buff_rust_items:
            QMessageBox.warning(self, "Remove Buff", "Extract with Rust parser first.")
            return
        if not hasattr(self, '_buff_current_item') or self._buff_current_item is None:
            QMessageBox.warning(self, "Remove Buff", "Select an item first.")
            return

        rust_info = self._buff_rust_lookup.get(self._buff_current_item.item_key)
        if not rust_info:
            QMessageBox.warning(self, "Remove Buff", "Item not found in Rust data.")
            return

        edl = rust_info.get('enchant_data_list', [])
        if not edl:
            return

        buff_key = self._eb_buff_combo.currentData()
        buff_name = self._EQUIP_BUFF_NAMES.get(buff_key, f"Buff {buff_key}")

        removed = 0
        for ed in edl:
            existing = ed.get('equip_buffs', [])
            new_list = [b for b in existing if b['buff'] != buff_key]
            if len(new_list) < len(existing):
                removed += 1
            ed['equip_buffs'] = new_list

        if removed == 0:
            QMessageBox.information(self, "Remove Buff",
                f"{buff_name} not found on this item.")
            return

        self._buff_modified = True
        self._buff_refresh_stats()
        display_name = self._name_db.get_name(self._buff_current_item.item_key)
        self._buff_status_label.setText(
            f"Removed {buff_name} from {display_name} ({removed} enchant levels). "
            f"Click 'Export as Mod' to write."
        )

    _DEV_PRESETS = {
        "immune": {
            "label": "Immune Ring",
            "passives": [{"skill": 70994, "level": 1}],
            "regen_stat_list": [{"stat": 1000000, "change_mb": 1000000}],
            "stat_list_static": [{"stat": 1000002, "change_mb": 1000000}],
        },
        "str_hp": {
            "label": "Str+HP Ring",
            "passives": [],
            "regen_stat_list": [{"stat": 1000000, "change_mb": 1000000}],
            "stat_list_static": [{"stat": 1000002, "change_mb": 1000000}],
        },
        "def_hp": {
            "label": "Def+HP Ring",
            "passives": [],
            "regen_stat_list": [{"stat": 1000000, "change_mb": 1000000}],
            "stat_list_static": [{"stat": 1000003, "change_mb": 1000000}],
        },
        "mp_stam": {
            "label": "MP+Stamina Ring",
            "passives": [],
            "regen_stat_list": [
                {"stat": 1000026, "change_mb": 100000},
                {"stat": 1000027, "change_mb": 100000},
            ],
            "stat_list_static": [
                {"stat": 1000037, "change_mb": 100000000},
            ],
        },
        "speed": {
            "label": "Speed Ring",
            "passives": [],
            "regen_stat_list": [],
            "stat_list_static": [],
            "stat_list_static_level": [
                {"stat": 1000010, "change_mb": 15},
                {"stat": 1000011, "change_mb": 15},
                {"stat": 1000007, "change_mb": 15},
            ],
        },
        "all": {
            "label": "All Dev Rings",
            "passives": [{"skill": 70994, "level": 1}],
            "regen_stat_list": [
                {"stat": 1000000, "change_mb": 1000000},
                {"stat": 1000026, "change_mb": 100000},
                {"stat": 1000027, "change_mb": 100000},
            ],
            "stat_list_static": [
                {"stat": 1000002, "change_mb": 1000000},
                {"stat": 1000003, "change_mb": 1000000},
                {"stat": 1000037, "change_mb": 100000000},
            ],
            "stat_list_static_level": [
                {"stat": 1000010, "change_mb": 15},
                {"stat": 1000011, "change_mb": 15},
                {"stat": 1000007, "change_mb": 15},
            ],
        },
        "elemental_weapon": {
            "label": "Elemental Weapon (Lightning+Ice+Fire)",
            "passives": [
                {"skill": 91101, "level": 3},
                {"skill": 91104, "level": 3},
                {"skill": 91105, "level": 3},
            ],
            "gimmick_info": 1001961,
            "cooltime": 1,
            "item_charge_type": 0,
            "max_charged_useable_count": 100,
            "respawn_time_seconds": 0,
            "docking_child_data": {
                "gimmick_info_key": 1001961,
                "character_key": 0,
                "item_key": 0,
                "attach_parent_socket_name": "Gimmick_Weapon_00_Socket",
                "attach_child_socket_name": "",
                "docking_tag_name_hash": [3365725887, 0, 0, 0],
                "docking_equip_slot_no": 65535,
                "spawn_distance_level": 4294967295,
                "is_item_equip_docking_gimmick": 1,
                "send_damage_to_parent": 0,
                "is_body_part": 0,
                "docking_type": 0,
                "is_summoner_team": 0,
                "is_player_only": 0,
                "is_npc_only": 0,
                "is_sync_break_parent": 0,
                "hit_part": 0,
                "detected_by_npc": 0,
                "is_bag_docking": 0,
                "enable_collision": 0,
                "disable_collision_with_other_gimmick": 1,
                "docking_slot_key": "",
                "inherit_summoner": 0,
                "summon_tag_name_hash": [0, 0, 0, 0],
            },
        },
        "jump_boots": {
            "label": "Jump Boots (Dash+Breeze+Swimming)",
            "passives": [
                {"skill": 7201, "level": 1},
                {"skill": 7055, "level": 1},
                {"skill": 7202, "level": 1},
            ],
            "gimmick_info": 1004431,
            "cooltime": 1,
            "item_charge_type": 0,
            "max_charged_useable_count": 100,
            "respawn_time_seconds": 0,
            "docking_child_data": {
                "gimmick_info_key": 1004431,
                "character_key": 0,
                "item_key": 0,
                "attach_parent_socket_name": "Bip01 Footsteps",
                "attach_child_socket_name": "",
                "docking_tag_name_hash": [247236102, 0, 0, 0],
                "docking_equip_slot_no": 65535,
                "spawn_distance_level": 4294967295,
                "is_item_equip_docking_gimmick": 0,
                "send_damage_to_parent": 0,
                "is_body_part": 0,
                "docking_type": 0,
                "is_summoner_team": 0,
                "is_player_only": 0,
                "is_npc_only": 0,
                "is_sync_break_parent": 0,
                "hit_part": 0,
                "detected_by_npc": 0,
                "is_bag_docking": 0,
                "enable_collision": 0,
                "disable_collision_with_other_gimmick": 1,
                "docking_slot_key": "",
                "inherit_summoner": 0,
                "summon_tag_name_hash": [0, 0, 0, 0],
            },
        },
    }


    def _eb_apply_dev_preset(self, preset_key: str = None) -> None:
        if not hasattr(self, '_buff_rust_items') or not self._buff_rust_items:
            QMessageBox.warning(self, "Dev Preset", "Extract with Rust parser first.")
            return
        if not hasattr(self, '_buff_current_item') or self._buff_current_item is None:
            QMessageBox.warning(self, "Dev Preset", "Select an item first.")
            return

        rust_info = self._buff_rust_lookup.get(self._buff_current_item.item_key)
        if not rust_info:
            QMessageBox.warning(self, "Dev Preset", "Item not found in Rust data.")
            return

        edl = rust_info.get('enchant_data_list', [])
        if not edl:
            QMessageBox.warning(self, "Dev Preset",
                "This item has no enchant data.\n"
                "Only equippable items can receive dev presets.")
            return

        if preset_key is None:
            preset_key = getattr(self, '_dev_preset_combo', None)
            if preset_key is not None:
                preset_key = preset_key.currentData()
        preset = self._DEV_PRESETS.get(preset_key)
        if not preset:
            return

        display_name = self._name_db.get_name(self._buff_current_item.item_key)
        reply = QMessageBox.question(
            self, f"Apply {preset['label']}",
            f"Apply {preset['label']} to {display_name}?\n\n"
            f"This injects into ALL {len(edl)} enchant levels:\n"
            f"  Passives: {len(preset.get('passives', []))}\n"
            f"  Regen stats: {len(preset.get('regen_stat_list', []))}\n"
            f"  Flat stats: {len(preset.get('stat_list_static', []))}\n"
            f"  Level stats: {len(preset.get('stat_list_static_level', []))}\n\n"
            f"Click 'Export as Mod' after to write.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        if preset.get('passives'):
            existing = rust_info.get('equip_passive_skill_list', [])
            for p in preset['passives']:
                if not any(e['skill'] == p['skill'] for e in existing):
                    existing.append(p)
            rust_info['equip_passive_skill_list'] = existing

        for gf in ('gimmick_info', 'cooltime', 'item_charge_type',
                    'max_charged_useable_count', 'docking_child_data'):
            if gf in preset:
                rust_info[gf] = preset[gf]

        if 'cooltime' in preset:
            rust_info['unk_post_cooltime_a'] = preset['cooltime']
            rust_info['unk_post_cooltime_b'] = preset['cooltime']
        if 'max_charged_useable_count' in preset:
            rust_info['unk_post_max_charged_a'] = preset['max_charged_useable_count']
            rust_info['unk_post_max_charged_b'] = preset['max_charged_useable_count']

        for ed in edl:
            sd = ed.setdefault('enchant_stat_data', {})

            for field in ['regen_stat_list', 'stat_list_static', 'stat_list_static_level']:
                new_stats = preset.get(field, [])
                if not new_stats:
                    continue
                existing = sd.get(field, [])
                for ns in new_stats:
                    replaced = False
                    for i, es in enumerate(existing):
                        if es['stat'] == ns['stat']:
                            existing[i] = ns
                            replaced = True
                            break
                    if not replaced:
                        existing.append(ns)
                sd[field] = existing

        self._buff_modified = True
        self._buff_refresh_stats()
        self._buff_status_label.setText(
            f"Applied {preset['label']} to {display_name} ({len(edl)} levels). "
            f"Click 'Export as Mod' to write."
        )


    @staticmethod
    def _diff_to_json_patches(orig: bytes, new: bytes, label: str) -> list:
        """Produce Oongka/DMM-style JSON patch ops for a file rewrite.

        Emits minimal ops covering the size change: one or more 'replace' ops
        for same-size regions that differ, plus 'insert' ops for any net
        growth. Handles the typical case where equipslotinfo grows by having
        more hashes appended to arrays.
        """
        ops: list = []
        # Walk prefix of identical bytes
        i = 0
        while i < min(len(orig), len(new)) and orig[i] == new[i]:
            i += 1
        # Find trailing identical region
        t_orig = len(orig); t_new = len(new)
        while t_orig > i and t_new > i and orig[t_orig - 1] == new[t_new - 1]:
            t_orig -= 1; t_new -= 1

        diff_orig = orig[i:t_orig]
        diff_new  = new[i:t_new]

        if len(diff_orig) == 0 and len(diff_new) == 0:
            return ops
        if len(diff_orig) == len(diff_new):
            # Pure replace — but may still have interior matches; emit one big
            # replace op covering the whole diff region for simplicity.
            ops.append({
                "type": "replace",
                "offset": f"{i:X}",
                "original": diff_orig.hex(),
                "patched": diff_new.hex(),
                "label": f"[{label}] replace {len(diff_orig)}B at 0x{i:X}",
            })
        elif len(diff_new) > len(diff_orig):
            # File grew. Emit a replace for the overlapping region then an
            # insert for the extra bytes.
            same = len(diff_orig)
            if same > 0:
                ops.append({
                    "type": "replace",
                    "offset": f"{i:X}",
                    "original": diff_orig.hex(),
                    "patched": diff_new[:same].hex(),
                    "label": f"[{label}] replace {same}B at 0x{i:X}",
                })
            ops.append({
                "type": "insert",
                "offset": f"{i + same:X}",
                "bytes": diff_new[same:].hex(),
                "label": f"[{label}] insert {len(diff_new) - same}B at 0x{i + same:X}",
            })
        else:
            # File shrank — uncommon for our edits. Emit replace + delete.
            same = len(diff_new)
            if same > 0:
                ops.append({
                    "type": "replace",
                    "offset": f"{i:X}",
                    "original": diff_orig[:same].hex(),
                    "patched": diff_new.hex(),
                    "label": f"[{label}] replace {same}B at 0x{i:X}",
                })
            ops.append({
                "type": "delete",
                "offset": f"{i + same:X}",
                "original": diff_orig[same:].hex(),
                "label": f"[{label}] delete {len(diff_orig) - same}B at 0x{i + same:X}",
            })
        return ops

    def _buff_export_field_json_v3(self) -> None:
        """Export edits as Format 3 field-name JSON (survives game updates)."""
        if not hasattr(self, '_buff_rust_items') or not self._buff_rust_items:
            QMessageBox.warning(self, "Export Field JSON v3",
                "Extract iteminfo first (click 'Extract').")
            return
        orig = getattr(self, '_buff_rust_items_original', None)
        if not orig:
            QMessageBox.warning(self, "Export Field JSON v3",
                "No vanilla baseline found. Re-extract iteminfo.")
            return

        orig_by_key = {it['key']: it for it in orig}
        intents = []
        for item in self._buff_rust_items:
            ikey = item.get('key', 0)
            skey = item.get('string_key', '')
            vanilla = orig_by_key.get(ikey)
            if not vanilla:
                intents.append({
                    'entry': skey, 'key': ikey,
                    'op': 'add_entry',
                    'data': {k: v for k, v in item.items()
                             if k not in ('key', 'string_key')},
                })
                continue
            diffs = self._field_diff(skey, ikey, vanilla, item)
            intents.extend(diffs)

        if not intents:
            QMessageBox.information(self, "Export Field JSON v3",
                "No field-level changes detected. Nothing to export.")
            return

        from PySide6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(self, "Export Field JSON v3",
            "Mod name:", text="My ItemBuffs Mod")
        if not ok or not name.strip():
            return
        name = name.strip()

        default_name = name.replace(' ', '_') + ".field.json"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Field JSON v3", default_name,
            "Field JSON (*.field.json *.json);;All Files (*)")
        if not path:
            return

        doc = {
            'modinfo': {
                'title': name,
                'version': '1.0',
                'author': 'CrimsonGameMods ItemBuffs',
                'description': f'{len(intents)} field-level intent(s)',
                'note': 'Format 3 — uses field names, survives game updates',
            },
            'format': 3,
            'target': 'iteminfo.pabgb',
            'intents': intents,
        }

        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(doc, f, indent=2, ensure_ascii=False, default=str)
            self._buff_status_label.setText(
                f"Exported {len(intents)} field intents to {os.path.basename(path)}")
            QMessageBox.information(self, "Export Field JSON v3",
                f"Exported {len(intents)} field-level intents.\n\n"
                f"This file uses field names — it survives game updates.\n"
                f"Compatible with Stacker Tool and future mod loaders.\n\n"
                f"File: {path}")
        except Exception as e:
            QMessageBox.critical(self, "Export Failed", str(e))

    @staticmethod
    def _field_diff(entry: str, key: int, a: dict, b: dict,
                    prefix: str = '') -> list[dict]:
        intents = []
        all_keys = set(list(a.keys()) + list(b.keys()))
        for k in sorted(all_keys):
            if k in ('key', 'string_key'):
                continue
            path = f'{prefix}.{k}' if prefix else k
            va, vb = a.get(k), b.get(k)
            if va == vb:
                continue
            if isinstance(va, dict) and isinstance(vb, dict):
                intents.extend(
                    ItemBuffsTab._field_diff(entry, key, va, vb, path))
            elif isinstance(va, list) and isinstance(vb, list):
                if va != vb:
                    intents.append({
                        'entry': entry, 'key': key,
                        'field': path, 'op': 'set', 'new': vb,
                    })
            else:
                intents.append({
                    'entry': entry, 'key': key,
                    'field': path, 'op': 'set', 'new': vb,
                })
        return intents

    def _buff_export_all_formats(self) -> None:
        if not self._require_dev_mode("Export All Formats"):
            return
        """Dev-only: prompt once, export in all three formats.

        Bundles the JSON patch, raw Mod folder, and CDUMM packed mod into a
        single packs/<name>/ directory so downstream users can pick whichever
        loader they already have without chasing down three separate builds.
        """
        if not self._buff_ensure_patcher():
            return
        if not hasattr(self, '_buff_rust_items') or not self._buff_rust_items:
            QMessageBox.warning(self, "Export All",
                "Extract with Rust parser first.")
            return

        from PySide6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(self, "Export All Formats",
            "Mod name (used as folder name + JSON filename):",
            text="My ItemBuffs Mod")
        if not ok or not name.strip():
            return
        name = name.strip()

        exe_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
        folder = "".join(c if (c.isalnum() or c in "-_ ") else "_" for c in name)
        out_root = os.path.join(exe_dir, "packs", folder)
        os.makedirs(out_root, exist_ok=True)

        # One subdir per format so artifacts don't overwrite each other
        # (modinfo.json from Raw Mod and CDUMM would otherwise clash).
        json_dir = os.path.join(out_root, "1_JsonPatch")
        raw_dir  = os.path.join(out_root, "2_RawMod")
        cdumm_dir = os.path.join(out_root, "3_CDUMM")
        for d in (json_dir, raw_dir, cdumm_dir):
            os.makedirs(d, exist_ok=True)

        self._buff_batch_mod_name = name
        steps: list[tuple[str, str]] = []

        for label, fn, sub in (
            ("JSON Patch", self._buff_export_json, json_dir),
            ("Raw Mod Folder", self._buff_export_mod, raw_dir),
            ("CDUMM Packed Mod", self._buff_export_cdumm_mod, cdumm_dir),
        ):
            self._buff_batch_dir = sub
            try:
                fn()
                steps.append((label, 'ok'))
            except Exception as e:
                log.exception("Export All: %s failed", label)
                steps.append((label, f'FAILED: {e}'))

        self._buff_batch_mod_name = None
        self._buff_batch_dir = None

        # Write a README at the root so downloaders know what's what.
        try:
            with open(os.path.join(out_root, "README.txt"), 'w', encoding='utf-8') as f:
                f.write(
                    f"{name}\n"
                    f"{'=' * len(name)}\n\n"
                    f"This folder contains the mod in THREE loader formats.\n"
                    f"Pick ONE based on the mod manager you use:\n\n"
                    f"  1_JsonPatch/{name}.json\n"
                    f"      For JSON Mod Manager / Pldada / DMM users.\n"
                    f"      Drop the .json into your mod manager.\n\n"
                    f"  2_RawMod/\n"
                    f"      Raw mod folder. Contains files/gamedata/...\n"
                    f"      For anyone who manually copies mod files into\n"
                    f"      the game directory, or uses Crimson Browser.\n\n"
                    f"  3_CDUMM/\n"
                    f"      CDUMM-packed mod. Contains 0036/, meta/, modinfo.json.\n"
                    f"      Drop this whole subfolder into CDUMM's Import.\n\n"
                    f"You only need ONE of the three. They all do the same thing.\n")
        except Exception:
            pass

        summary = '\n'.join(f"  {s[0]}: {s[1]}" for s in steps)
        QMessageBox.information(self, "Export All — Done",
            f"Exported '{name}' in three formats:\n"
            f"  {out_root}\n\n"
            f"  • 1_JsonPatch/  (JSON loader)\n"
            f"  • 2_RawMod/     (drop-in + Crimson Browser)\n"
            f"  • 3_CDUMM/      (CDUMM manager)\n"
            f"  • README.txt    (explains which is which)\n\n"
            f"{summary}\n\n"
            f"Zip the whole {folder}/ folder and upload — users pick\n"
            f"whichever subfolder matches their loader.")


    def _buff_export_mod(self) -> None:
        if not self._require_dev_mode("Export as Mod"):
            return
        if not self._buff_ensure_patcher():
            return

        if not hasattr(self, '_buff_rust_items') or not self._buff_rust_items:
            QMessageBox.warning(self, "Export Mod",
                "Extract with Rust parser first (click 'Extract (Rust)').")
            return

        has_cd = bool(getattr(self, '_cd_patches', {}))
        if not self._buff_modified and not has_cd:
            apply_stacks = hasattr(self, '_stack_check') and self._stack_check.isChecked()
            if not apply_stacks:
                QMessageBox.information(self, "No Changes",
                    "No modifications have been made.\n"
                    "Add buffs, apply God Mode, check 'Max Stacks', or use 'No Cooldown (All Items)' first.")
                return

        apply_stacks = hasattr(self, '_stack_check') and self._stack_check.isChecked()
        if apply_stacks:
            target_val = self._stack_spin.value()
            for it in self._buff_rust_items:
                if it.get('max_stack_count', 1) > 1:
                    it['max_stack_count'] = target_val

        _mod_grp = f"{self._buff_modgroup_spin.value():04d}"
        reply = QMessageBox.question(
            self, "Export as Mod — Full PAZ Pack",
            f"This exports a full PAZ mod folder (like community mods).\n\n"
            f"WHAT THIS SUPPORTS:\n"
            f"  - Everything 'Export JSON Patch' can do, PLUS:\n"
            f"  - Add NEW equipment buffs (Fire Res, Ice Res, etc)\n"
            f"  - Add NEW stats that don't exist on the item\n"
            f"  - Add passive skills (Invincible, Great Thief, etc)\n"
            f"  - God Mode injection\n"
            f"  - Any edit that changes the file size\n\n"
            f"OUTPUT:\n"
            f"  A mod folder with {_mod_grp}/, meta/, and modinfo.json.\n"
            f"  Import into CDUMM or copy to your game directory.\n\n"
            f"NOTE: Only ONE mod can use the {_mod_grp}/ slot at a time.\n"
            f"If you already have a {_mod_grp}/ mod, it will be replaced.\n"
            f"Use CDUMM to manage multiple mods, or change the 'Mod:' number.\n\n"
            f"Continue?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
        )
        if reply != QMessageBox.Yes:
            return

        batch_name = getattr(self, '_buff_batch_mod_name', None)
        batch_dir = getattr(self, '_buff_batch_dir', None)
        if batch_name:
            name = batch_name
        else:
            from PySide6.QtWidgets import QInputDialog
            name, ok = QInputDialog.getText(self, "Export as Mod",
                                            "Mod name (used as folder name):",
                                            text="My ItemBuffs Mod")
            if not ok or not name.strip():
                return
        name = name.strip()

        exe_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
        packs_dir = os.path.join(exe_dir, "packs")
        folder_name = "".join(c if (c.isalnum() or c in "-_") else "_" for c in name)
        # In batch mode, all three exports write to the same root folder so
        # the output is one self-contained zip-ready directory.
        if batch_name and batch_dir:
            out_path = batch_dir
        else:
            out_path = os.path.join(packs_dir, folder_name)

        apply_stacks = hasattr(self, '_stack_check') and self._stack_check.isChecked()
        if apply_stacks:
            target_val = self._stack_spin.value()
            for it in self._buff_rust_items:
                if it.get('max_stack_count', 1) > 1:
                    it['max_stack_count'] = target_val

        apply_inf_dura = hasattr(self, '_inf_dura_check') and self._inf_dura_check.isChecked()
        if apply_inf_dura:
            dura_count = 0
            for it in self._buff_rust_items:
                endurance = it.get('max_endurance', 0)
                if endurance > 0 and endurance != 65535:
                    it['max_endurance'] = 65535
                    it['is_destroy_when_broken'] = 0
                    dura_count += 1
            log.info("Infinity Durability: patched %d items", dura_count)

        self._buff_status_label.setText("Serializing with crimson_rs...")
        QApplication.processEvents()

        try:
            import crimson_rs
            import crimson_rs.pack_mod

            _mod_count = 0
            for _it in self._buff_rust_items:
                _psl = _it.get('equip_passive_skill_list', [])
                _edl = _it.get('enchant_data_list', [])
                if _psl:
                    _mod_count += 1
                    log.info("Export: %s has %d passives", _it.get('string_key', '?'), len(_psl))
                for _ed in _edl:
                    _buffs = _ed.get('equip_buffs', [])
                    if len(_buffs) > 1:
                        _mod_count += 1
                        log.info("Export: %s level %d has %d equip_buffs",
                                 _it.get('string_key', '?'), _ed.get('level', 0), len(_buffs))
                        break
            if _mod_count == 0:
                log.warning("Export: NO structural edits found in Rust dicts!")

            final_data = self._rebuild_full_iteminfo()
            log.info("Rebuilt iteminfo: %d bytes", len(final_data))

            self._apply_vfx_changes(final_data)

            cd_patches = getattr(self, '_cd_patches', {})
            if cd_patches:
                cd_hit = 0
                for item_key, (_, _, new_val) in cd_patches.items():
                    cd_off, _ = self._cd_detect(item_key, bytes(final_data))
                    if cd_off is not None:
                        final_data[cd_off:cd_off + 4] = struct.pack('<I', new_val)
                        cd_hit += 1
                log.info("Applied %d/%d cooldown patches to serialized data", cd_hit, len(cd_patches))

            self._apply_transmog_swaps(final_data)

            final_data = bytes(final_data)

        except Exception as e:
            import traceback; traceback.print_exc()
            QMessageBox.critical(self, "Serialize Failed",
                f"crimson_rs.serialize_iteminfo() failed:\n{e}")
            return

        self._buff_status_label.setText("Packing with pack_mod...")
        QApplication.processEvents()

        try:
            import tempfile
            import shutil

            game_path = self._buff_patcher.game_path
            mod_group = f"{self._buff_modgroup_spin.value():04d}"

            if os.path.isdir(out_path):
                shutil.rmtree(out_path)
            os.makedirs(out_path, exist_ok=True)

            files_dir = os.path.join(out_path, "files",
                                     "gamedata", "binary__", "client", "bin")
            os.makedirs(files_dir, exist_ok=True)
            with open(os.path.join(files_dir, "iteminfo.pabgb"), "wb") as f:
                f.write(final_data)
            # Matching pabgh: vanilla index points into vanilla pabgb layout;
            # shipping only pabgb makes items after the first mutation
            # unreachable (rings / cloaks lose sockets in-game).
            try:
                from item_creator import build_iteminfo_pabgh
                _extra = getattr(self, '_buff_appended_entries', [])
                _pabgh = build_iteminfo_pabgh(final_data, extra_entries=_extra)
                with open(os.path.join(files_dir, "iteminfo.pabgh"), "wb") as f:
                    f.write(_pabgh)
                log.info("Export mod: wrote iteminfo.pabgh (%d bytes)", len(_pabgh))
            except Exception as _e:
                log.warning("Export mod: pabgh regen failed (%s) -- mod may be partially broken in-game", _e)

            staged_equip = getattr(self, "_staged_equip_files", None) or {}
            for fname, fdata in staged_equip.items():
                with open(os.path.join(files_dir, fname), "wb") as f:
                    f.write(fdata)
                log.info("Export mod: included staged %s (%d bytes)", fname, len(fdata))

            modinfo = {
                "id": name.lower().replace(" ", "_"),
                "name": name,
                "version": "1.0.0",
                "game_version": "1.00.03",
                "author": "CrimsonSaveEditor",
                "description": f"ItemBuffs mod: {name}",
            }
            with open(os.path.join(out_path, "modinfo.json"), "w", encoding="utf-8") as f:
                json.dump(modinfo, f, indent=2)

            data_size = len(final_data)
            self._buff_status_label.setText(
                f"Exported mod to packs/{folder_name}/ ({data_size:,} bytes)")
            QMessageBox.information(self, "Mod Exported",
                f"Mod exported to:\n{out_path}\n\n"
                f"Contents:\n"
                f"  files/gamedata/binary__/client/bin/iteminfo.pabgb ({data_size:,} bytes)\n"
                f"  modinfo.json\n\n"
                f"To install:\n"
                f"  Copy '{folder_name}' into your mod loader's mods/ directory\n"
                f"  (CD JSON Mod Manager, DMM, or CDUMM)")

        except Exception as e:
            import traceback; traceback.print_exc()
            self._buff_status_label.setText(f"Export failed: {e}")
            QMessageBox.critical(self, "Export Failed", str(e))


    def _buff_export_cdumm_mod(self) -> None:
        if not self._require_dev_mode("Export CDUMM Mod"):
            return
        if not self._buff_ensure_patcher():
            return

        if not hasattr(self, '_buff_rust_items') or not self._buff_rust_items:
            QMessageBox.warning(self, "Export CDUMM Mod",
                "Extract with Rust parser first (click 'Extract (Rust)').")
            return

        staged_equip = getattr(self, '_staged_equip_files', None) or {}
        if staged_equip:
            QMessageBox.warning(self, "Export CDUMM Mod \u2014 Use Apply to Game",
                "Universal Proficiency (v1 or v2) stages equipslotinfo files\n"
                "that CDUMM's LZ4 packer inflates and the game then rejects.\n\n"
                "Use 'Apply to Game' instead \u2014 it bundles iteminfo, equipslotinfo,\n"
                "and all your buff/stat/dye edits into a single overlay slot\n"
                f"({self._buff_overlay_spin.value():04d}/), uncompressed, which the game\n"
                "accepts cleanly.\n\n"
                "Tip: no more 0058 vs 0059 mismatch \u2014 everything's in one place,\n"
                "so nothing can disable anything else.")
            return

        has_cd = bool(getattr(self, '_cd_patches', {}))
        if not self._buff_modified and not has_cd:
            apply_stacks = hasattr(self, '_stack_check') and self._stack_check.isChecked()
            if not apply_stacks:
                QMessageBox.information(self, "No Changes",
                    "No modifications have been made.\n"
                    "Add buffs, apply God Mode, check 'Max Stacks', or use 'No Cooldown (All Items)' first.")
                return

        _mod_grp = f"{self._buff_modgroup_spin.value():04d}"
        reply = QMessageBox.question(
            self, "Export as CDUMM Mod — PAZ Packed",
            f"This exports a fully packed CDUMM mod folder.\n\n"
            f"WHAT THIS SUPPORTS:\n"
            f"  - Everything 'Export as Mod' can do, PLUS:\n"
            f"  - Proper PAZ archives (0.paz + 0.pamt)\n"
            f"  - PAPGT metadata for game loading\n"
            f"  - Direct import into CDUMM mod manager\n\n"
            f"OUTPUT:\n"
            f"  {_mod_grp}/0.paz + {_mod_grp}/0.pamt\n"
            f"  meta/0.papgt\n"
            f"  modinfo.json\n\n"
            f"REQUIRES: Game path set (needed for pack_mod).\n\n"
            f"Continue?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
        )
        if reply != QMessageBox.Yes:
            return

        from PySide6.QtWidgets import QInputDialog, QFileDialog
        batch_name = getattr(self, '_buff_batch_mod_name', None)
        batch_dir = getattr(self, '_buff_batch_dir', None)
        if batch_name:
            name = batch_name
        else:
            name, ok = QInputDialog.getText(self, "Export as CDUMM Mod",
                                            "Mod name (used as folder name):",
                                            text="My ItemBuffs Mod")
            if not ok or not name.strip():
                return
            name = name.strip()

        exe_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
        default_dir = os.path.join(exe_dir, "packs")
        os.makedirs(default_dir, exist_ok=True)
        folder_name = "".join(c if (c.isalnum() or c in "-_ ") else "_" for c in name)
        # Batch mode: all three exports share the batch_dir as their root so
        # everything lands in ONE ready-to-zip folder.
        if batch_name and batch_dir:
            out_path = batch_dir
        else:
            save_dir = QFileDialog.getExistingDirectory(
                self, f"Choose folder to create '{folder_name}' CDUMM mod in", default_dir)
            if not save_dir:
                return
            out_path = os.path.join(save_dir, folder_name)

        apply_stacks = hasattr(self, '_stack_check') and self._stack_check.isChecked()
        if apply_stacks:
            target_val = self._stack_spin.value()
            for it in self._buff_rust_items:
                if it.get('max_stack_count', 1) > 1:
                    it['max_stack_count'] = target_val

        apply_inf_dura = hasattr(self, '_inf_dura_check') and self._inf_dura_check.isChecked()
        if apply_inf_dura:
            dura_count = 0
            for it in self._buff_rust_items:
                endurance = it.get('max_endurance', 0)
                if endurance > 0 and endurance != 65535:
                    it['max_endurance'] = 65535
                    it['is_destroy_when_broken'] = 0
                    dura_count += 1
            log.info("CDUMM Infinity Durability: patched %d items", dura_count)

        self._buff_status_label.setText("Serializing with crimson_rs...")
        QApplication.processEvents()

        try:
            import crimson_rs
            import crimson_rs.pack_mod

            final_data = bytearray(crimson_rs.serialize_iteminfo(self._buff_rust_items))
            log.info("CDUMM export: serialized iteminfo: %d bytes", len(final_data))

            cd_patches = getattr(self, '_cd_patches', {})
            self._apply_vfx_changes(final_data)

            if cd_patches:
                cd_hit = 0
                for item_key, (_, _, new_val) in cd_patches.items():
                    cd_off, _ = self._cd_detect(item_key, bytes(final_data))
                    if cd_off is not None:
                        final_data[cd_off:cd_off + 4] = struct.pack('<I', new_val)
                        cd_hit += 1
                log.info("Applied %d/%d cooldown patches", cd_hit, len(cd_patches))

            self._apply_transmog_swaps(final_data)

            final_data = bytes(final_data)

        except Exception as e:
            import traceback; traceback.print_exc()
            QMessageBox.critical(self, "Serialize Failed",
                f"crimson_rs.serialize_iteminfo() failed:\n{e}")
            return

        self._buff_status_label.setText("Packing with pack_mod...")
        QApplication.processEvents()

        try:
            import tempfile
            import shutil

            game_path = self._buff_patcher.game_path
            mod_group = f"{self._buff_modgroup_spin.value():04d}"

            if os.path.isdir(out_path):
                shutil.rmtree(out_path)
            os.makedirs(out_path, exist_ok=True)

            # Use PackGroupBuilder(NONE) instead of pack_mod() — the latter
            # LZ4-compresses small index files (equipslotinfo.pabgh, skill.pabgh)
            # and the resulting PAMT checksum doesn't match what the game
            # expects, so the game rejects the overlay and ALL buffs stop
            # functioning. Mirrors the Apply to Game flow.
            INTERNAL_DIR = "gamedata/binary__/client/bin"
            with tempfile.TemporaryDirectory() as tmp_dir:
                group_build_dir = os.path.join(tmp_dir, mod_group)
                builder = crimson_rs.PackGroupBuilder(
                    group_build_dir,
                    crimson_rs.Compression.NONE,
                    crimson_rs.Crypto.NONE,
                )
                builder.add_file(INTERNAL_DIR, "iteminfo.pabgb", final_data)
                try:
                    from item_creator import build_iteminfo_pabgh
                    _extra = getattr(self, '_buff_appended_entries', [])
                    _pabgh = build_iteminfo_pabgh(final_data, extra_entries=_extra)
                    builder.add_file(INTERNAL_DIR, "iteminfo.pabgh", _pabgh)
                    log.info("CDUMM export: bundled iteminfo.pabgh (%d bytes)", len(_pabgh))
                except Exception as _e:
                    log.warning("CDUMM export: pabgh regen failed (%s)", _e)

                staged_equip = getattr(self, "_staged_equip_files", None) or {}
                for fname, fdata in staged_equip.items():
                    builder.add_file(INTERNAL_DIR, fname, fdata)
                    log.info("CDUMM export: bundled staged %s (%d bytes)", fname, len(fdata))

                staged_skill = getattr(self, "_staged_skill_files", None) or {}
                for fname in ("skill.pabgb", "skill.pabgh"):
                    if fname in staged_skill:
                        builder.add_file(INTERNAL_DIR, fname, staged_skill[fname])
                        log.info("CDUMM export: bundled staged %s (%d bytes)",
                                 fname, len(staged_skill[fname]))

                staged_charinfo = getattr(self, "_staged_charinfo_files", None) or {}
                for fname in ("characterinfo.pabgb", "characterinfo.pabgh"):
                    if fname in staged_charinfo:
                        builder.add_file(INTERNAL_DIR, fname, staged_charinfo[fname])
                        log.info("CDUMM export: bundled staged %s (%d bytes)",
                                 fname, len(staged_charinfo[fname]))

                pamt_bytes = bytes(builder.finish())
                pamt_checksum = crimson_rs.parse_pamt_bytes(pamt_bytes)["checksum"]

                paz_dst = os.path.join(out_path, mod_group)
                os.makedirs(paz_dst, exist_ok=True)
                # Copy .paz files and .pamt from the build dir
                for f in os.listdir(group_build_dir):
                    shutil.copy2(os.path.join(group_build_dir, f),
                                 os.path.join(paz_dst, f))

                # Build PAPGT: copy vanilla, add/update our group entry
                vanilla_papgt = os.path.join(game_path, "meta", "0.papgt")
                if os.path.isfile(vanilla_papgt):
                    cur = crimson_rs.parse_papgt_file(vanilla_papgt)
                else:
                    cur = {"unknown0": 1610, "checksum": 0, "unknown1": 0, "unknown2": 0, "entries": []}
                    cur["entries"] = [e for e in cur["entries"]
                                  if e.get("group_name") != mod_group]
                cur = crimson_rs.add_papgt_entry(
                    cur, mod_group, pamt_checksum, is_optional=0, language=0x3FFF)

                meta_dst = os.path.join(out_path, "meta")
                os.makedirs(meta_dst, exist_ok=True)
                crimson_rs.write_papgt_file(cur, os.path.join(meta_dst, "0.papgt"))

            modinfo = {
                "id": name.lower().replace(" ", "_"),
                "name": name,
                "version": "1.0.0",
                "game_version": "1.00.03",
                "author": "CrimsonSaveEditor",
                "description": f"ItemBuffs mod: {name}",
            }
            with open(os.path.join(out_path, "modinfo.json"), "w", encoding="utf-8") as f:
                json.dump(modinfo, f, indent=2)

            paz_size = os.path.getsize(os.path.join(paz_dst, "0.paz"))
            self._buff_status_label.setText(
                f"Exported CDUMM mod to {folder_name}/ ({paz_size:,} bytes PAZ)")
            QMessageBox.information(self, "CDUMM Mod Exported",
                f"Mod exported to:\n{out_path}\n\n"
                f"Contents:\n"
                f"  {mod_group}/0.paz ({paz_size:,} bytes)\n"
                f"  {mod_group}/0.pamt\n"
                f"  meta/0.papgt\n"
                f"  modinfo.json\n\n"
                f"To install:\n"
                f"  Import the '{folder_name}' folder into CDUMM,\n"
                f"  or copy the contents to your game directory.")

        except Exception as e:
            import traceback; traceback.print_exc()
            self._buff_status_label.setText(f"CDUMM export failed: {e}")
            QMessageBox.critical(self, "Export Failed",
                f"pack_mod failed:\n{e}\n\n"
                f"Make sure the game path is set correctly.")


    def _buff_save_config(self) -> None:
        if not hasattr(self, '_buff_rust_items') or not self._buff_rust_items:
            QMessageBox.warning(self, "Save Config",
                "Extract with Rust parser first (click 'Extract (Rust)').")
            return

        if not self._buff_modified:
            apply_stacks = hasattr(self, '_stack_check') and self._stack_check.isChecked()
            if not apply_stacks:
                QMessageBox.information(self, "Save Config",
                    "No modifications to save.")
                return

        try:
            import crimson_rs
            vanilla_lookup = self._buff_parse_to_lookup(
                self._buff_patcher._original_data)
        except Exception as e:
            QMessageBox.critical(self, "Save Config",
                f"Failed to parse vanilla data for diff:\n{e}")
            return

        items_config = {}
        for item in self._buff_rust_items:
            key = item['key']
            vanilla = vanilla_lookup.get(key)
            if not vanilla:
                continue

            item_changes = {}

            if item.get('equip_passive_skill_list') != vanilla.get('equip_passive_skill_list'):
                item_changes['equip_passive_skill_list'] = item.get('equip_passive_skill_list', [])

            for gf in ('gimmick_info', 'cooltime', 'item_charge_type',
                        'max_charged_useable_count', 'docking_child_data',
                        'respawn_time_seconds', 'drop_default_data'):
                if item.get(gf) != vanilla.get(gf):
                    item_changes[gf] = item.get(gf)

            if item.get('max_stack_count') != vanilla.get('max_stack_count'):
                item_changes['max_stack_count'] = item['max_stack_count']

            v_edl = vanilla.get('enchant_data_list', [])
            m_edl = item.get('enchant_data_list', [])
            if v_edl and m_edl and len(v_edl) == len(m_edl):
                enchant_changes = {}
                for i, (v_ed, m_ed) in enumerate(zip(v_edl, m_edl)):
                    level_changes = {}

                    if m_ed.get('equip_buffs') != v_ed.get('equip_buffs'):
                        level_changes['equip_buffs'] = m_ed.get('equip_buffs', [])

                    v_sd = v_ed.get('enchant_stat_data', {})
                    m_sd = m_ed.get('enchant_stat_data', {})
                    for stat_field in ['stat_list_static', 'stat_list_static_level',
                                       'regen_stat_list', 'max_stat_list']:
                        if m_sd.get(stat_field) != v_sd.get(stat_field):
                            level_changes.setdefault('enchant_stat_data', {})[stat_field] = \
                                m_sd.get(stat_field, [])

                    if level_changes:
                        enchant_changes[str(i)] = level_changes

                if enchant_changes:
                    item_changes['enchant_levels'] = enchant_changes

            if item_changes:
                item_changes['string_key'] = item.get('string_key', '')
                items_config[str(key)] = item_changes

        if not items_config:
            QMessageBox.information(self, "Save Config",
                "No differences found between current edits and vanilla.")
            return

        config = {
            "format": "crimson_itembuffs_config",
            "version": 1,
            "name": "",
            "description": f"{len(items_config)} item(s) modified",
            "items": items_config,
        }

        from PySide6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(self, "Save Config",
                                        "Config name:", text="My ItemBuffs Config")
        if not ok or not name.strip():
            return
        config["name"] = name.strip()

        path, _ = QFileDialog.getSaveFileName(
            self, "Save Config", f"{name.strip()}.json",
            "Config Files (*.json)")
        if not path:
            return

        with open(path, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2)

        summary_parts = []
        for key, changes in items_config.items():
            skey = changes.get('string_key', key)
            parts = []
            if 'equip_passive_skill_list' in changes:
                parts.append(f"{len(changes['equip_passive_skill_list'])} passives")
            if 'enchant_levels' in changes:
                n_buffs = 0
                n_stats = 0
                for lv_changes in changes['enchant_levels'].values():
                    n_buffs += len(lv_changes.get('equip_buffs', []))
                    for sd in lv_changes.get('enchant_stat_data', {}).values():
                        n_stats += len(sd)
                if n_buffs:
                    parts.append(f"{n_buffs} buffs")
                if n_stats:
                    parts.append(f"{n_stats} stats")
            if 'max_stack_count' in changes:
                parts.append(f"stack={changes['max_stack_count']}")
            summary_parts.append(f"  {skey}: {', '.join(parts)}")

        self._buff_status_label.setText(f"Config saved: {os.path.basename(path)}")
        if len(summary_parts) > 6:
            shown = "\n".join(summary_parts[:6])
            shown += f"\n  ... and {len(summary_parts) - 6} more items"
        else:
            shown = "\n".join(summary_parts)
        QMessageBox.information(self, "Config Saved",
            f"Saved to:\n{path}\n\n"
            f"Changes ({len(summary_parts)} items):\n{shown}\n\n"
            f"Share this file or load it later to tweak and re-export.")


    def _buff_load_config(self) -> None:
        if not hasattr(self, '_buff_rust_items') or not self._buff_rust_items:
            QMessageBox.warning(self, "Load Config",
                "Extract with Rust parser first (click 'Extract (Rust)').\n"
                "The config will be applied on top of fresh game data.")
            return

        path, _ = QFileDialog.getOpenFileName(
            self, "Load Config", "",
            "Config Files (*.json);;All Files (*)")
        if not path:
            return

        try:
            with open(path, 'r', encoding='utf-8') as f:
                config = json.load(f)
        except Exception as e:
            QMessageBox.critical(self, "Load Config", f"Failed to read file:\n{e}")
            return

        if config.get('format') != 'crimson_itembuffs_config':
            QMessageBox.warning(self, "Load Config",
                "This doesn't look like an ItemBuffs config file.\n"
                f"Expected format 'crimson_itembuffs_config', got '{config.get('format', '?')}'.")
            return

        items_config = config.get('items', {})
        if not items_config:
            QMessageBox.information(self, "Load Config", "Config has no item changes.")
            return

        reply = QMessageBox.question(
            self, "Load Config",
            f"Loading config: {config.get('name', 'unnamed')}\n"
            f"Contains {len(items_config)} item edit(s).\n\n"
            f"This will RESET current edits and apply the config\n"
            f"on top of fresh vanilla data.\n\n"
            f"Continue?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
        )
        if reply != QMessageBox.Yes:
            return

        try:
            import crimson_rs
            vanilla_data = bytes(self._buff_patcher._original_data)
            try:
                fresh = crimson_rs.parse_iteminfo_from_bytes(vanilla_data)
                self._buff_unparsed_raw = []
            except Exception:
                fresh = list(self._buff_parse_to_lookup(vanilla_data).values())
            self._buff_rust_items = fresh
            self._buff_rust_lookup = {it['key']: it for it in fresh}
            self._rebuild_index()
        except Exception as e:
            QMessageBox.critical(self, "Load Config",
                f"Failed to re-parse vanilla data:\n{e}")
            return

        applied = 0
        skipped = []
        for key_str, changes in items_config.items():
            key = int(key_str)
            rust_info = self._buff_rust_lookup.get(key)
            if not rust_info:
                skipped.append(changes.get('string_key', key_str))
                continue

            if 'equip_passive_skill_list' in changes:
                rust_info['equip_passive_skill_list'] = changes['equip_passive_skill_list']

            for gf in ('gimmick_info', 'cooltime', 'item_charge_type',
                        'max_charged_useable_count', 'docking_child_data',
                        'respawn_time_seconds', 'drop_default_data'):
                if gf in changes:
                    val = changes[gf]
                    if gf == 'docking_child_data' and isinstance(val, dict):
                        val.setdefault('inherit_summoner', 0)
                        val.setdefault('summon_tag_name_hash', [0, 0, 0, 0])
                    rust_info[gf] = val

            if 'cooltime' in changes:
                rust_info['unk_post_cooltime_a'] = changes['cooltime']
                rust_info['unk_post_cooltime_b'] = changes['cooltime']
            if 'max_charged_useable_count' in changes:
                rust_info['unk_post_max_charged_a'] = changes['max_charged_useable_count']
                rust_info['unk_post_max_charged_b'] = changes['max_charged_useable_count']

            if 'max_stack_count' in changes:
                rust_info['max_stack_count'] = changes['max_stack_count']

            if 'enchant_levels' in changes:
                edl = rust_info.get('enchant_data_list', [])
                for lvl_str, lv_changes in changes['enchant_levels'].items():
                    lvl_idx = int(lvl_str)
                    if lvl_idx >= len(edl):
                        continue

                    ed = edl[lvl_idx]

                    if 'equip_buffs' in lv_changes:
                        ed['equip_buffs'] = lv_changes['equip_buffs']

                    if 'enchant_stat_data' in lv_changes:
                        sd = ed.setdefault('enchant_stat_data', {})
                        for stat_field, stat_list in lv_changes['enchant_stat_data'].items():
                            sd[stat_field] = stat_list

            applied += 1

        try:
            import crimson_rs
            new_data = crimson_rs.serialize_iteminfo(self._buff_rust_items)
            self._buff_data = bytearray(new_data)
            self._buff_rust_items = crimson_rs.parse_iteminfo_from_bytes(new_data)
            self._buff_rust_lookup = {it['key']: it for it in self._buff_rust_items}
            self._rebuild_index()
            self._buff_items = self._buff_patcher.find_items(bytes(self._buff_data))
            log.info("Load Config: synced byte buffer (%d bytes)", len(new_data))
        except Exception as e:
            log.warning("Load Config: byte buffer sync failed: %s", e)

        self._buff_modified = True
        self._detect_qol_flags_from_items()
        self._buff_refresh_stats()

        msg = f"Applied config to {applied} item(s)."
        if skipped:
            if len(skipped) > 6:
                shown = ', '.join(skipped[:6]) + f' ... +{len(skipped)-6} more'
            else:
                shown = ', '.join(skipped)
            msg += f"\nSkipped {len(skipped)} item(s) not found in game data: {shown}"

        self._buff_status_label.setText(
            f"Loaded config: {config.get('name', '')} ({applied} items). "
            f"Click 'Export as Mod' to write.")
        QMessageBox.information(self, "Config Loaded", msg)


    def _buff_on_stat_selected(self, *_args) -> None:
        rows = self._buff_stats_table.selectionModel().selectedRows()
        if not rows:
            self._buff_sel_label.setText("")
            return
        row = rows[0].row()
        item = self._buff_stats_table.item(row, 0)
        if not item:
            return

        kind_data = item.data(Qt.UserRole + 1)
        if kind_data:
            kind = kind_data[0]
            if kind == 'passive':
                key = kind_data[1]
                for i in range(self._eb_passive_combo.count()):
                    if self._eb_passive_combo.itemData(i) == key:
                        self._eb_passive_combo.setCurrentIndex(i)
                        break
                name = self._PASSIVE_SKILL_NAMES.get(key, f"Skill {key}")
                self._eb_status.setText(f"Selected: {name} — click Remove to delete")
                self._buff_sel_label.setText("(passive row — use Remove button above)")
                return
            elif kind == 'buff':
                key = kind_data[1]
                for i in range(self._eb_buff_combo.count()):
                    if self._eb_buff_combo.itemData(i) == key:
                        self._eb_buff_combo.setCurrentIndex(i)
                        break
                name = self._EQUIP_BUFF_NAMES.get(key, f"Buff {key}")
                self._eb_status.setText(f"Selected: {name} — click Remove Buff to delete")
                self._buff_sel_label.setText("(buff row — use Remove Buff button above)")
                return
            elif kind == 'stat':
                stat_key_val = kind_data[1]
                stat_list_name = kind_data[2]
                raw_val = kind_data[3] if len(kind_data) > 3 else None
                if raw_val is not None:
                    self._eb_stat_value.setValue(raw_val)
                for i in range(self._eb_stat_combo.count()):
                    idx = self._eb_stat_combo.itemData(i)
                    if idx is not None and idx < len(self._ENCHANT_STAT_LIST):
                        _, skey, slist, _ = self._ENCHANT_STAT_LIST[idx]
                        if skey == stat_key_val and slist == stat_list_name:
                            self._eb_stat_combo.setCurrentIndex(i)
                            sname = self._ENCHANT_STAT_LIST[idx][0]
                            self._eb_status.setText(f"Selected: {sname} (current value: {raw_val:,}) — click Remove to delete")
                            self._buff_sel_label.setText("(stat row — use Stat Remove button above)")
                            return
                self._buff_sel_label.setText(f"(stat {stat_key_val} in {stat_list_name} — not in Remove list)")
                return

        entry = item.data(Qt.UserRole)
        if not entry:
            self._buff_sel_label.setText("(header row — select a stat)")
            return
        self._buff_sel_label.setText(f"{entry.name} [{entry.size_class}]")
        self._buff_sel_value_spin.setValue(entry.value)


    _CD_MARKER = b'\x00\x00\x00\x00\x00\x00\x00\x0e'


    def _cd_detect(self, item_key: int, raw: bytes = None):
        if raw is None:
            if not hasattr(self, '_buff_patcher') or self._buff_patcher is None:
                return None, None
            raw = self._buff_patcher._original_data
        if raw is None:
            return None, None

        item_name = None
        if hasattr(self, '_buff_items') and self._buff_items:
            for it in self._buff_items:
                if it.item_key == item_key:
                    item_name = it.name
                    break
        if not item_name:
            return None, None

        nb = item_name.encode('utf-8')
        pos = raw.find(nb)
        while pos != -1:
            check = pos - 8
            if check >= 0:
                nlen_check = struct.unpack_from('<I', raw, check + 4)[0]
                if nlen_check == len(nb) and (pos + len(nb) < len(raw)) and raw[pos + len(nb)] == 0:
                    entry_start = check
                    break
            pos = raw.find(nb, pos + 1)
        else:
            return None, None

        scan_end = min(entry_start + 2000, len(raw) - 16)
        i = entry_start
        while i < scan_end:
            idx = raw.find(self._CD_MARKER, i, scan_end)
            if idx == -1:
                break
            cd_off = idx + 8
            cd_val = struct.unpack_from('<I', raw, cd_off)[0]
            after_val = struct.unpack_from('<I', raw, cd_off + 4)[0]
            if 1 <= cd_val <= 86400 and after_val == 0:
                return cd_off, cd_val
            i = idx + 1
        return None, None


    def _buff_open_desc_search(self):
        dlg = DescriptionSearchDialog(parent=self)
        if dlg.exec() == QDialog.Accepted and dlg.selected_key:
            QApplication.clipboard().setText(str(dlg.selected_key))
            self._buff_status_label.setText(
                f"Selected: {dlg.selected_name} (key {dlg.selected_key}, type: {dlg.selected_type}) — copied to clipboard")


    def _detect_qol_flags_from_items(self) -> None:
        """Inspect current _buff_rust_items and tick the QoL checkboxes
        to match what's already there. Called after Import / Load Config.

        Heuristics:
          - If any item has max_stack_count >= 999 AND original vanilla had
            max_stack_count < 999, tick Max Stacks and set spin to match.
          - If any item has max_endurance == 65535 AND original was not,
            tick Infinity Durability.
        """
        if not hasattr(self, '_buff_rust_items') or not self._buff_rust_items:
            return
        orig = getattr(self, '_buff_rust_items_original', None) or []
        orig_by = {it['key']: it for it in orig}

        stack_candidates: list[int] = []
        dura_hits = 0
        for it in self._buff_rust_items:
            v = orig_by.get(it['key'])
            if not v:
                continue
            cur_stack = it.get('max_stack_count', 0) or 0
            van_stack = v.get('max_stack_count', 0) or 0
            if cur_stack >= 999 and cur_stack > van_stack:
                stack_candidates.append(cur_stack)
            cur_dura = it.get('max_endurance', 0) or 0
            van_dura = v.get('max_endurance', 0) or 0
            if cur_dura == 65535 and van_dura != 65535 and van_dura > 0:
                dura_hits += 1

        if stack_candidates and hasattr(self, '_stack_check'):
            self._stack_check.setChecked(True)
            from collections import Counter
            target = Counter(stack_candidates).most_common(1)[0][0]
            self._stack_spin.setValue(min(target, self._stack_spin.maximum()))
        if dura_hits and hasattr(self, '_inf_dura_check'):
            self._inf_dura_check.setChecked(True)


    def _eb_unlock_all_abyss_gear(self, silent: bool = False) -> int:
        """Set equipable_hash=0 on every AbyssGear item (field-name based).

        Returns the number of items unlocked. When silent=True, skips the
        confirmation dialog (used by Enable Everything).
        """
        if not hasattr(self, '_buff_rust_items') or not self._buff_rust_items:
            if not silent:
                QMessageBox.warning(self, "Abyss Gear Unlock",
                    "Extract iteminfo first (click 'Extract').")
            return 0

        abyss = [it for it in self._buff_rust_items
                 if 'AbyssGear' in (it.get('string_key') or '')
                 and it.get('equipable_hash', 0) != 0]

        if not abyss:
            if not silent:
                QMessageBox.information(self, "Abyss Gear Unlock",
                    "All abyss gear items already have equipable_hash = 0.\n"
                    "Nothing to unlock.")
            return 0

        if not silent:
            reply = QMessageBox.question(
                self, "Unlock All Abyss Gear",
                f"Set equipable_hash = 0 on {len(abyss)} abyss gear items?\n\n"
                f"This removes the socket-type restriction so every abyss gem\n"
                f"can be socketed into ANY equipment slot.\n\n"
                f"Original concept by OhmesmileTH (Nexus Mods) who discovered\n"
                f"that zeroing _equipableHash unlocks abyss socket restrictions.\n"
                f"Re-implemented here as a field-name based mod so it stacks\n"
                f"with sockets, buffs, and all other ItemBuffs edits — and\n"
                f"survives game updates without offset fixes.\n\n"
                f"Click 'Apply to Game' after to deploy.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply != QMessageBox.Yes:
                return 0

        for it in abyss:
            it['equipable_hash'] = 0

        self._buff_modified = True
        if not silent:
            self._buff_refresh_stats()
            self._buff_status_label.setText(
                f"Abyss Gear Unlock: {len(abyss)} items set equipable_hash=0. "
                f"Click 'Apply to Game' to deploy.")
            QMessageBox.information(self, "Abyss Gear Unlock",
                f"Unlocked {len(abyss)} abyss gear items.\n\n"
                f"equipable_hash set to 0 (unrestricted) on all of them.\n"
                f"Click 'Apply to Game' to deploy.")
        return len(abyss)

    def _eb_enable_all_qol(self) -> None:
        """One-click QoL bundle: no cooldown + max charges + max stacks + infinity durability."""
        if not hasattr(self, '_buff_rust_items') or not self._buff_rust_items:
            QMessageBox.warning(self, "Enable All QoL",
                "Extract iteminfo first.")
            return

        STACK_TARGET = 999999
        CHARGES_TARGET = 99
        DURA_TARGET = 65535

        stacks = charges = dura = cd = 0
        for it in self._buff_rust_items:
            # Max stacks
            cur_stack = it.get('max_stack_count', 0) or 0
            if cur_stack > 1 and cur_stack != STACK_TARGET:
                it['max_stack_count'] = STACK_TARGET
                stacks += 1
            # Max charges (only on active/charged items: item_charge_type == 0 means charged)
            cur_charge = it.get('max_charged_useable_count', 0) or 0
            if it.get('item_charge_type', 0) == 0 and cur_charge > 0 and cur_charge != CHARGES_TARGET:
                it['max_charged_useable_count'] = CHARGES_TARGET
                it['unk_post_max_charged_a'] = CHARGES_TARGET
                it['unk_post_max_charged_b'] = CHARGES_TARGET
                charges += 1
            # Infinity durability
            cur_dura = it.get('max_endurance', 0) or 0
            if cur_dura > 0 and cur_dura != DURA_TARGET:
                it['max_endurance'] = DURA_TARGET
                it['is_destroy_when_broken'] = 0
                dura += 1
            # No cooldown
            cur_cd = it.get('cooltime', 0) or 0
            if cur_cd > 1:
                it['cooltime'] = 1
                it['unk_post_cooltime_a'] = 1
                it['unk_post_cooltime_b'] = 1
                cd += 1

        if hasattr(self, '_stack_check'):
            self._stack_check.setChecked(True)
            self._stack_spin.setValue(min(STACK_TARGET, self._stack_spin.maximum()))
        if hasattr(self, '_inf_dura_check'):
            self._inf_dura_check.setChecked(True)

        self._buff_modified = True
        self._buff_refresh_stats()
        self._buff_status_label.setText(
            f"QoL bundle: stacks={stacks} charges={charges} durability={dura} cooldown={cd}. "
            f"Click 'Apply to Game' or 'Export as Mod'.")
        QMessageBox.information(self, "All QoL Enabled",
            f"Applied in-memory:\n"
            f"  Max Stack (999999):   {stacks} items\n"
            f"  Max Charges (99):     {charges} items\n"
            f"  Infinity Durability:  {dura} items\n"
            f"  No Cooldown (\u21921s):    {cd} items\n\n"
            f"Checkboxes (Max Stacks / Infinity Durability) are now ticked\n"
            f"so the next export picks them up.\n\n"
            f"Click 'Apply to Game' or 'Export as Mod' to write.")


    def _buff_verify_applied_overlay(self) -> None:
        """Diagnostic: compare current overlay vs vanilla, report per-mutation counts.

        Answers "what actually made it into 0058/0.paz?" vs "what should have
        been there?". Useful when a user reports features aren't working in-
        game and we need to know whether it's a serialize/overlay issue or a
        game-side issue.
        """
        if not self._buff_ensure_patcher():
            return

        game_path = self._buff_patcher.game_path
        buff_dir = f"{self._buff_overlay_spin.value():04d}"
        overlay_paz = os.path.join(game_path, buff_dir, "0.paz")
        overlay_pamt = os.path.join(game_path, buff_dir, "0.pamt")
        if not (os.path.isfile(overlay_paz) and os.path.isfile(overlay_pamt)):
            QMessageBox.warning(self, "Verify Overlay",
                f"No overlay found at {buff_dir}/.\n"
                "Click 'Apply to Game' first, then re-run Verify.")
            return

        self._buff_status_label.setText("Verifying overlay...")
        QApplication.processEvents()

        try:
            import crimson_rs
        except ImportError:
            QMessageBox.critical(self, "Verify Overlay",
                "crimson_rs not available.")
            return

        INTERNAL = "gamedata/binary__/client/bin"
        PLAYER_TRIBES = self._PLAYER_TRIBE_HASHES
        STACK_TARGET, CHARGES_TARGET, DURA_TARGET = 999999, 99, 65535

        # Pull vanilla + overlay iteminfo.
        try:
            van_bytes = bytes(crimson_rs.extract_file(
                game_path, '0008', INTERNAL, 'iteminfo.pabgb'))
            mod_bytes = bytes(crimson_rs.extract_file(
                game_path, buff_dir, INTERNAL, 'iteminfo.pabgb'))
        except Exception as e:
            QMessageBox.critical(self, "Verify Overlay",
                f"Failed to extract iteminfo:\n{e}")
            return

        van_items = list(self._buff_parse_to_lookup(van_bytes).values())
        mod_items = list(self._buff_parse_to_lookup(mod_bytes).values())
        mod_by_key = {it['key']: it for it in mod_items}

        # Initialise counters for each mutation type.
        stacks_hit = stacks_expected = 0
        charges_hit = charges_expected = 0
        dura_hit = dura_expected = 0
        cd_hit = cd_expected = 0
        dye_hit = dye_expected = 0
        sock_hit = sock_expected = 0
        up_hit = up_expected = 0

        for v in van_items:
            m = mod_by_key.get(v['key'])
            if not m:
                continue

            # QoL
            if (v.get('max_stack_count', 0) or 0) > 1 and v['max_stack_count'] != STACK_TARGET:
                stacks_expected += 1
                if m.get('max_stack_count') == STACK_TARGET:
                    stacks_hit += 1
            if (v.get('item_charge_type', 0) == 0
                    and (v.get('max_charged_useable_count', 0) or 0) > 0
                    and v['max_charged_useable_count'] != CHARGES_TARGET):
                charges_expected += 1
                if m.get('max_charged_useable_count') == CHARGES_TARGET:
                    charges_hit += 1
            if (v.get('max_endurance', 0) or 0) > 0 and v['max_endurance'] != DURA_TARGET:
                dura_expected += 1
                if m.get('max_endurance') == DURA_TARGET:
                    dura_hit += 1
            if (v.get('cooltime', 0) or 0) > 1:
                cd_expected += 1
                if m.get('cooltime') == 1:
                    cd_hit += 1

            # Dyeable
            if v.get('equip_type_info') and not v.get('is_dyeable'):
                dye_expected += 1
                if m.get('is_dyeable'):
                    dye_hit += 1

            # Sockets
            vddd = v.get('drop_default_data')
            if vddd and vddd.get('use_socket') and vddd.get('add_socket_material_item_list'):
                vlen = len(vddd['add_socket_material_item_list'])
                if 1 <= vlen < 5:
                    sock_expected += 1
                    mddd = m.get('drop_default_data') or {}
                    mlist = mddd.get('add_socket_material_item_list') or []
                    if len(mlist) == 5 and mddd.get('socket_valid_count') == 5:
                        sock_hit += 1

            # UP v2 tribe_gender (per-PD)
            if v.get('equip_type_info'):
                v_pdl = v.get('prefab_data_list') or []
                m_pdl = m.get('prefab_data_list') or []
                for i, vpd in enumerate(v_pdl):
                    vtg = vpd.get('tribe_gender_list') or []
                    if not vtg:
                        continue
                    to_add = sorted(PLAYER_TRIBES - set(vtg))
                    if not to_add:
                        continue
                    up_expected += 1
                    if i < len(m_pdl):
                        mtg = m_pdl[i].get('tribe_gender_list') or []
                        if all(t in mtg for t in to_add):
                            up_hit += 1

        # Check companion files presence in the overlay.
        has_equipslot_pabgb = False
        has_equipslot_pabgh = False
        has_skill_pabgb = False
        has_skill_pabgh = False
        try:
            has_equipslot_pabgb = bool(crimson_rs.extract_file(
                game_path, buff_dir, INTERNAL, 'equipslotinfo.pabgb'))
        except Exception:
            pass
        try:
            has_equipslot_pabgh = bool(crimson_rs.extract_file(
                game_path, buff_dir, INTERNAL, 'equipslotinfo.pabgh'))
        except Exception:
            pass
        try:
            has_skill_pabgb = bool(crimson_rs.extract_file(
                game_path, buff_dir, INTERNAL, 'skill.pabgb'))
        except Exception:
            pass
        try:
            has_skill_pabgh = bool(crimson_rs.extract_file(
                game_path, buff_dir, INTERNAL, 'skill.pabgh'))
        except Exception:
            pass

        self._buff_status_label.setText(
            f"Verify done: overlay {buff_dir}/ inspected.")

        def row(name, hit, exp):
            pct = (100 * hit / exp) if exp else 0
            status = "OK" if (exp == 0 or hit == exp) else (
                "PART" if hit > 0 else "MISS")
            return f"  [{status}]  {name:<28}  {hit:>5}/{exp:<5}  ({pct:>5.1f}%)"

        msg = (
            f"Overlay {buff_dir}/0.paz \u2014 verification vs vanilla\n\n"
            f"Iteminfo mutations (how many items had each applied):\n"
            f"{row('QoL Max Stack (999999)', stacks_hit, stacks_expected)}\n"
            f"{row('QoL Max Charges (99)', charges_hit, charges_expected)}\n"
            f"{row('QoL Infinity Durability', dura_hit, dura_expected)}\n"
            f"{row('QoL No Cooldown (==1)', cd_hit, cd_expected)}\n"
            f"{row('Make Dyeable', dye_hit, dye_expected)}\n"
            f"{row('Sockets (all \u2192 5)', sock_hit, sock_expected)}\n"
            f"{row('UP v2 tribe_gender PDs', up_hit, up_expected)}\n\n"
            f"Companion files bundled in overlay:\n"
            f"  equipslotinfo.pabgb:  {'yes' if has_equipslot_pabgb else 'NO'}\n"
            f"  equipslotinfo.pabgh:  {'yes' if has_equipslot_pabgh else 'NO'}\n"
            f"  skill.pabgb:          {'yes' if has_skill_pabgb else 'NO (no imbue used)'}\n"
            f"  skill.pabgh:          {'yes' if has_skill_pabgh else 'NO (no imbue used)'}\n\n"
            f"Legend:\n"
            f"  [OK]   \u2014 every expected item got the mutation\n"
            f"  [PART] \u2014 some items got it but others didn't (investigate)\n"
            f"  [MISS] \u2014 no items got this mutation (the handler didn't "
            f"run or its changes were dropped)\n\n"
            f"Expected=0 rows are also OK \u2014 it means nothing in vanilla "
            f"needed that mutation.\n\n"
            f"UP v2 note: items with empty tribe_gender_list are intentionally "
            f"skipped (empty == 'any character can equip' per game semantics; "
            f"filling empty lists broke Batz dagger in past tests)."
        )

        log.info("Verify overlay: %s", msg.replace('\n', ' | '))
        QMessageBox.information(self, "Verify Applied Overlay", msg)


    def _eb_enable_everything_oneclick(self) -> None:
        """One-click: QoL + Make Dyeable + Sockets (all\u21925) + Universal Proficiency v2.

        Skipped on purpose: imbue (needs a selected target passive/item) and
        any per-item operations. Everything here is bulk apply-to-many.
        Runs every mutation on the in-memory rust items, stages the UP v2
        equipslotinfo into _staged_equip_files, shows ONE summary dialog,
        and leaves everything ready for a single 'Apply to Game' click that
        bundles it all into {buff_overlay_spin:04d}/.
        """
        if not hasattr(self, '_buff_rust_items') or not self._buff_rust_items:
            QMessageBox.warning(self, "Enable Everything",
                "Extract iteminfo first (click 'Extract').")
            return

        # Single confirm — we skip the per-operation dialogs so this is actually
        # one click, not five.
        reply = QMessageBox.question(
            self, "Enable Everything (bulk)",
            "This runs in one shot:\n"
            "  \u2022 All QoL: Max Stacks (999999), Max Charges (99), "
            "Infinity Durability (65535), No Cooldown (\u21921s)\n"
            "  \u2022 Make All Equipment Dyeable\n"
            "  \u2022 All Items \u2192 5 Sockets (extends existing + force-enables\n"
            "     rings, cloaks, earrings, necklaces, nobility insignia)\n"
            "  \u2022 Unlock All Abyss Gear (equipable_hash \u2192 0)\n"
            "  \u2022 Universal Proficiency v2 (tribe_gender + equipslotinfo)\n\n"
            "Skipped (needs a selected item/passive):\n"
            "  \u2022 Imbue (use the Imbue tab after this for specific weapons)\n"
            "  \u2022 Per-item Add Passive / Add Buff / Add Stat\n\n"
            "Everything lands in a single overlay slot "
            f"({self._buff_overlay_spin.value():04d}/) on Apply to Game.\n\n"
            "Continue?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if reply != QMessageBox.Yes:
            return

        # ── 1) QoL bundle ──
        STACK_TARGET, CHARGES_TARGET, DURA_TARGET = 999999, 99, 65535
        stacks = charges = dura = cd = 0
        for it in self._buff_rust_items:
            cur_stack = it.get('max_stack_count', 0) or 0
            if cur_stack > 1 and cur_stack != STACK_TARGET:
                it['max_stack_count'] = STACK_TARGET
                stacks += 1
            cur_charge = it.get('max_charged_useable_count', 0) or 0
            if (it.get('item_charge_type', 0) == 0 and cur_charge > 0
                    and cur_charge != CHARGES_TARGET):
                it['max_charged_useable_count'] = CHARGES_TARGET
                it['unk_post_max_charged_a'] = CHARGES_TARGET
                it['unk_post_max_charged_b'] = CHARGES_TARGET
                charges += 1
            cur_dura = it.get('max_endurance', 0) or 0
            if cur_dura > 0 and cur_dura != DURA_TARGET:
                it['max_endurance'] = DURA_TARGET
                it['is_destroy_when_broken'] = 0
                dura += 1
            cur_cd = it.get('cooltime', 0) or 0
            if cur_cd > 1:
                it['cooltime'] = 1
                it['unk_post_cooltime_a'] = 1
                it['unk_post_cooltime_b'] = 1
                cd += 1

        # Tick the export checkboxes so Apply to Game's byte-level paths also
        # pick these up (Max Stacks, Infinity Durability use them).
        if hasattr(self, '_stack_check'):
            self._stack_check.setChecked(True)
            self._stack_spin.setValue(min(STACK_TARGET, self._stack_spin.maximum()))
        if hasattr(self, '_inf_dura_check'):
            self._inf_dura_check.setChecked(True)

        # ── 2) Make All Equipment Dyeable ──
        dye_flipped = 0
        for it in self._buff_rust_items:
            if it.get('equip_type_info') and not it.get('is_dyeable'):
                it['is_dyeable'] = 1
                it['is_editable_grime'] = 1
                dye_flipped += 1

        # ── 3) All \u2192 5 Sockets (extend existing + force-enable accessories) ──
        # Mirrors the two-branch logic in _eb_extend_all_sockets_to_5 so the
        # oneclick matches what the dedicated button does. Without the
        # force-enable branch, rings / cloaks / earrings / necklaces /
        # nobility insignia (all vanilla-socketless) silently skip and the
        # user sees no sockets on accessories in-game.
        DEFAULT_COSTS = [500, 1000, 2000, 3000, 4000, 5000, 6000, 7000]
        SOCKET_TARGET = 5

        def _build_socket_list(existing: list) -> list:
            new_list = list(existing)
            while len(new_list) < SOCKET_TARGET:
                cost = (DEFAULT_COSTS[len(new_list)]
                        if len(new_list) < len(DEFAULT_COSTS) else 5000)
                new_list.append({'item': 1, 'value': cost})
            return new_list

        sock_changed = 0         # items that already had sockets, extended to 5
        sock_force_enabled = 0   # rings/cloaks/etc: 0 -> 5
        for it in self._buff_rust_items:
            ddd = it.get('drop_default_data')
            if not ddd:
                continue
            cur_list = ddd.get('add_socket_material_item_list') or []
            use_socket = ddd.get('use_socket', 0)

            if use_socket and cur_list:
                if len(cur_list) >= SOCKET_TARGET:
                    continue
                ddd['add_socket_material_item_list'] = _build_socket_list(cur_list)
                ddd['socket_valid_count'] = SOCKET_TARGET
                sock_changed += 1
                continue

            if self._socketable_force_target(it):
                ddd['use_socket'] = 1
                ddd['add_socket_material_item_list'] = _build_socket_list([])
                ddd['socket_valid_count'] = SOCKET_TARGET
                sock_force_enabled += 1

        # ── 3b) Unlock All Abyss Gear ──
        abyss_unlocked = self._eb_unlock_all_abyss_gear(silent=True)

        # ── 4) Universal Proficiency v2 (tribe_gender + equipslotinfo) ──
        player_tribes = self._PLAYER_TRIBE_HASHES
        tg_unioned = tg_added_total = 0
        for it in self._buff_rust_items:
            if not it.get('equip_type_info'):
                continue
            for pd in (it.get('prefab_data_list') or []):
                tg = pd.get('tribe_gender_list')
                if not tg:
                    continue
                existing = set(tg)
                to_add = sorted(player_tribes - existing)
                if to_add:
                    pd['tribe_gender_list'] = list(tg) + to_add
                    tg_unioned += 1
                    tg_added_total += len(to_add)

        # equipslotinfo expansion + stage. Wrapped in try/except so a parser
        # hiccup doesn't lose the other mutations above.
        total_slot_added = 0
        equip_msg = ""
        try:
            import crimson_rs
            import equipslotinfo_parser as esp

            gp_widget = getattr(self, '_buff_game_path', None)
            gp_text = (gp_widget.text() or '').strip() if gp_widget is not None else ''
            if not gp_text:
                gp_text = getattr(self, '_game_path', '') or \
                    r'C:\Program Files (x86)\Steam\steamapps\common\Crimson Desert'

            es_pabgh = crimson_rs.extract_file(
                gp_text, '0008', 'gamedata/binary__/client/bin', 'equipslotinfo.pabgh')
            es_pabgb = crimson_rs.extract_file(
                gp_text, '0008', 'gamedata/binary__/client/bin', 'equipslotinfo.pabgb')
            es_records = esp.parse_all(es_pabgh, es_pabgb)

            player_keys = self._PLAYER_CHAR_KEYS
            player_records = [r for r in es_records if r.key in player_keys]
            category_hashes: dict[tuple[int, int], set[int]] = {}
            for rec in player_records:
                for e in rec.entries:
                    key = (e.category_a, e.category_b)
                    category_hashes.setdefault(key, set()).update(e.etl_hashes)
            for rec in es_records:
                if rec.key not in player_keys:
                    continue
                for e in rec.entries:
                    key = (e.category_a, e.category_b)
                    pool = category_hashes.get(key, set())
                    to_add = sorted(pool - set(e.etl_hashes))
                    if to_add:
                        e.etl_hashes.extend(to_add)
                        total_slot_added += len(to_add)

            new_es_pabgh, new_es_pabgb = esp.serialize_all(es_records)
            # Deploy to 0059/ as a separate overlay -- see _eb_universal_proficiency_v2
            # for empirical rationale.
            self._buff_deploy_equipslotinfo_0059(gp_text, new_es_pabgb, new_es_pabgh)
            if hasattr(self, '_staged_equip_files') and self._staged_equip_files:
                for _k in ('equipslotinfo.pabgb', 'equipslotinfo.pabgh'):
                    self._staged_equip_files.pop(_k, None)

            # (legacy 0059/ cleanup removed 2026-04-21 -- 0059/ is now the
            # canonical equipslotinfo overlay again; cleanup would delete it)

            equip_msg = (f"Equipslotinfo: +{total_slot_added} hashes "
                         f"staged for Apply to Game")
        except Exception as e:
            log.exception("Enable Everything: equipslotinfo expansion failed")
            equip_msg = f"Equipslotinfo expansion failed: {e}"

        # ── 5) Kliff Gun Fix (characterinfo.pabgb) ──
        charinfo_msg = ""
        _gp_for_kliff = gp_text if 'gp_text' in dir() else (
            getattr(self, '_game_path', '') or
            r'C:\Program Files (x86)\Steam\steamapps\common\Crimson Desert')
        gun_reply = QMessageBox.question(
            self, "Kliff Gun Fix",
            "Universal Proficiency needs one more patch to fully work:\n\n"
            "Kliff can't use muskets/pistols without a characterinfo fix\n"
            "(copies Damiane's upper action chart + Oongka's gameplay data\n"
            "to Kliff so muskets attach and fire correctly).\n\n"
            "Apply Kliff Gun Fix now?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if gun_reply == QMessageBox.Yes:
            charinfo_msg = self._stage_kliff_gun_fix(_gp_for_kliff)

        # ── Finalise ──
        self._buff_modified = True
        self._buff_refresh_stats()

        buff_slot = f"{self._buff_overlay_spin.value():04d}"
        self._buff_status_label.setText(
            f"Enable Everything: stacks={stacks} charges={charges} "
            f"dura={dura} cd={cd} | dye={dye_flipped} | "
            f"sockets ext={sock_changed} force={sock_force_enabled} "
            f"| abyss={abyss_unlocked} "
            f"| tribe={tg_unioned} | slots={total_slot_added}. "
            f"Click 'Apply to Game' to write \u2192 {buff_slot}/.")

        QMessageBox.information(self, "Enable Everything \u2014 Done",
            f"In-memory mutations applied:\n\n"
            f"QoL bundle\n"
            f"  Max Stack (999999):      {stacks:>5} items\n"
            f"  Max Charges (99):        {charges:>5} items\n"
            f"  Infinity Durability:     {dura:>5} items\n"
            f"  No Cooldown (\u21921s):       {cd:>5} items\n\n"
            f"Make Dyeable\n"
            f"  is_dyeable + grime flag: {dye_flipped:>5} equipment items\n\n"
            f"Sockets\n"
            f"  Extended to 5 slots:     {sock_changed:>5} items already socketed\n"
            f"  Force-enabled 0 -> 5:    {sock_force_enabled:>5} rings/cloaks/earrings/necklaces/nobility\n\n"
            f"Abyss Gear Unlock\n"
            f"  equipable_hash \u2192 0:     {abyss_unlocked:>5} abyss gems unrestricted\n\n"
            f"Universal Proficiency v2\n"
            f"  Tribe hashes added:      {tg_unioned:>5} items (+{tg_added_total} total)\n"
            f"  {equip_msg}"
            f"{charinfo_msg}\n\n"
            f"Click 'Apply to Game' now \u2014 everything lands in a single\n"
            f"{buff_slot}/ overlay (iteminfo + equipslotinfo) plus any\n"
            f"previously-staged skill files for imbue.\n\n"
            f"Want elemental imbue too? Use the Imbue sub-tab after this\n"
            f"\u2014 it needs a passive selection + target item(s).")


    def _max_charges_all_items(self) -> None:
        if not hasattr(self, '_buff_rust_items') or not self._buff_rust_items:
            QMessageBox.warning(self, "Max Charges", "Extract with Rust parser first.")
            return

        target = self._max_charges_spin.value()
        patched = 0
        skipped_passive = 0
        skipped_unchanged = 0
        for it in self._buff_rust_items:
            if it.get('item_charge_type', 0) != 0:
                skipped_passive += 1
                continue
            cur = it.get('max_charged_useable_count', 0) or 0
            if cur == target:
                skipped_unchanged += 1
                continue
            if cur == 0:
                continue
            it['max_charged_useable_count'] = target
            it['unk_post_max_charged_a'] = target
            it['unk_post_max_charged_b'] = target
            patched += 1

        self._buff_modified = True
        QMessageBox.information(
            self, "Max Charges — Done",
            f"Set max_charged_useable_count = {target} on {patched} item(s).\n"
            f"Skipped: {skipped_passive} passive items, {skipped_unchanged} already at target.\n\n"
            f"Note: Only FRESH copies (new drops/crafts) will actually have the new\n"
            f"charge count. Items already in your save keep their current value.\n\n"
            f"Use Export as Mod to write."
        )


    def _cd_patch_all_items(self) -> None:
        if not hasattr(self, '_buff_rust_items') or not self._buff_rust_items:
            QMessageBox.warning(self, "No Cooldown", "Extract iteminfo first.")
            return

        patched = 0
        already = 0
        for it in self._buff_rust_items:
            cur_cd = it.get('cooltime', 0) or 0
            if cur_cd <= 1:
                if cur_cd == 1:
                    already += 1
                continue
            it['cooltime'] = 1
            it['unk_post_cooltime_a'] = 1
            it['unk_post_cooltime_b'] = 1
            patched += 1

        self._buff_modified = True
        self._buff_refresh_stats()

        skip_note = f"\n{already} item(s) already at 1s." if already else ""
        QMessageBox.information(
            self, "No Cooldown — Done",
            f"Set cooltime → 1s on {patched} item(s).{skip_note}\n\n"
            f"Use Export as Mod or Apply to Game to write."
        )


    def _buff_apply_to_selected(self) -> None:
        if not hasattr(self, '_buff_rust_items') or not self._buff_rust_items:
            QMessageBox.warning(self, "No Data", "Extract with Rust parser first.")
            return
        if not hasattr(self, '_buff_current_item') or self._buff_current_item is None:
            return

        rows = self._buff_stats_table.selectionModel().selectedRows()
        if not rows:
            QMessageBox.information(self, "No Selection", "Click a stat row in the table first.")
            return
        row = rows[0].row()
        item_widget = self._buff_stats_table.item(row, 0)
        if not item_widget:
            return

        kind_data = item_widget.data(Qt.UserRole + 1)
        if not kind_data or kind_data[0] != 'stat':
            QMessageBox.information(self, "No Stat",
                "Select a stat entry (not a header, passive, or buff row).")
            return

        stat_key = kind_data[1]
        stat_list_name = kind_data[2]
        old_value = kind_data[3] if len(kind_data) > 3 else 0

        new_value = self._buff_sel_value_spin.value()
        if new_value == old_value:
            return

        stat_name = self._buff_sel_label.text() if hasattr(self, '_buff_sel_label') else f"Stat {stat_key}"

        reply = QMessageBox.question(
            self, "Edit Single Stat",
            f"Change {stat_name} from {old_value:,} to {new_value:,}?\n\n"
            f"Only THIS stat entry will be modified.\n"
            f"All other stats remain untouched.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        rust_info = self._buff_rust_lookup.get(self._buff_current_item.item_key)
        if not rust_info:
            return

        edl = rust_info.get('enchant_data_list', [])
        target_level = 0
        if hasattr(self, '_eb_level_target'):
            sel = self._eb_level_target.currentData()
            if sel is not None and sel >= 0:
                target_level = sel

        if target_level < len(edl):
            sd = edl[target_level].setdefault('enchant_stat_data', {})
            existing = sd.get(stat_list_name, [])
            for i, s in enumerate(existing):
                if s['stat'] == stat_key:
                    existing[i] = {'stat': stat_key, 'change_mb': new_value}
                    break
            sd[stat_list_name] = existing

        self._buff_modified = True
        self._buff_refresh_stats()
        self._buff_status_label.setText(
            f"Changed {stat_name}: {old_value:,} -> {new_value:,}. Click 'Export as Mod' to write."
        )


    def _buff_add_to_item(self) -> None:
        if not hasattr(self, '_buff_rust_items') or not self._buff_rust_items:
            QMessageBox.warning(self, "No Data", "Extract with Rust parser first.")
            return

        item = self._buff_current_item
        if item is None:
            QMessageBox.information(self, "No Item Selected",
                                    "Select an item from the search results first.")
            return

        rust_info = self._buff_rust_lookup.get(item.item_key)
        if not rust_info:
            return

        edl = rust_info.get('enchant_data_list', [])
        if not edl:
            QMessageBox.warning(self, "No Enchant Data",
                                "This item has no enchant data to modify.")
            return

        target_level = self._eb_level_target.currentData() if hasattr(self, '_eb_level_target') else -1

        preset_idx = self._buff_preset_combo.currentIndex()
        preset_name = ""
        modified = 0

        for idx, ed in enumerate(edl):
            if target_level != -1 and idx != target_level:
                continue

            sd = ed.setdefault('enchant_stat_data', {})

            if preset_idx == 0:
                preset_name = "Max All"
                for list_name in ['stat_list_static', 'stat_list_static_level', 'regen_stat_list', 'max_stat_list']:
                    for s in sd.get(list_name, []):
                        if list_name == 'stat_list_static_level':
                            s['change_mb'] = 15
                        else:
                            s['change_mb'] = 999_999
                modified += 1

            elif preset_idx == 1:
                preset_name = "Max All Flat"
                for s in sd.get('stat_list_static', []):
                    s['change_mb'] = 999_999
                modified += 1

            elif preset_idx == 2:
                preset_name = "Max DDD"
                self._set_stat(sd, 'stat_list_static', 1000002, 999_999)
                modified += 1

            elif preset_idx == 3:
                preset_name = "Max DPV"
                self._set_stat(sd, 'stat_list_static', 1000003, 999_999)
                modified += 1

            elif preset_idx == 4:
                preset_name = "Max HP"
                self._set_stat(sd, 'stat_list_static', 1000000, 999_999)
                modified += 1

            elif preset_idx == 5:
                preset_name = "Max All Rates"
                for s in sd.get('stat_list_static_level', []):
                    s['change_mb'] = 15
                modified += 1

            elif preset_idx == 6:
                preset_name = "Swap to DDD"
                for s in sd.get('stat_list_static', []):
                    s['stat'] = 1000002
                modified += 1

            elif preset_idx == 7:
                preset_name = "Swap to DPV"
                for s in sd.get('stat_list_static', []):
                    s['stat'] = 1000003
                modified += 1

            else:
                buff_name = self._buff_type_combo.currentText()
                buff_hash = BUFF_HASHES.get(buff_name)
                if buff_hash is None:
                    continue
                value = self._buff_value_spin.value()
                preset_name = f"Custom: {buff_name}={value}"

                from paz_patcher import _stat_size_class
                size_class = _stat_size_class(buff_hash)
                if size_class == 'flat2':
                    self._set_stat(sd, 'stat_list_static', buff_hash, value)
                elif size_class == 'rate':
                    self._set_stat(sd, 'stat_list_static_level', buff_hash, value)
                else:
                    self._set_stat(sd, 'stat_list_static', buff_hash, value)
                modified += 1

        display_name = self._name_db.get_name(item.item_key)
        if display_name.startswith("Unknown"):
            display_name = item.name

        self._buff_modified = True
        self._buff_refresh_stats()
        level_str = f"level +{target_level}" if target_level >= 0 else f"{modified} levels"
        self._buff_status_label.setText(
            f"Applied '{preset_name}' to {display_name} ({level_str}). "
            f"Click 'Export as Mod' to write."
        )


    def _buff_sync_to_rust(self, item_key: int = None) -> None:
        if not hasattr(self, '_buff_rust_items') or self._buff_data is None:
            return
        try:
            import crimson_rs

            saved_structural = {}
            for it in self._buff_rust_items:
                key = it['key']
                structural = {}

                psl = it.get('equip_passive_skill_list', [])
                if psl:
                    structural['equip_passive_skill_list'] = psl

                for gf in ('gimmick_info', 'cooltime', 'item_charge_type',
                            'max_charged_useable_count', 'docking_child_data'):
                    val = it.get(gf)
                    if val is not None:
                        structural[gf] = val

                edl = it.get('enchant_data_list', [])
                if edl:
                    structural['enchant_data_list'] = [
                        {
                            'equip_buffs': ed.get('equip_buffs'),
                            'enchant_stat_data': ed.get('enchant_stat_data'),
                        }
                        for ed in edl
                    ]

                if structural:
                    saved_structural[key] = structural

            fresh = list(self._buff_parse_to_lookup(bytes(self._buff_data)).values())

            fresh_lookup = {it['key']: it for it in fresh}
            for key, structural in saved_structural.items():
                fi = fresh_lookup.get(key)
                if not fi:
                    continue

                if 'equip_passive_skill_list' in structural:
                    fi['equip_passive_skill_list'] = structural['equip_passive_skill_list']

                for gf in ('gimmick_info', 'cooltime', 'item_charge_type',
                            'max_charged_useable_count', 'docking_child_data'):
                    if gf in structural:
                        fi[gf] = structural[gf]

                if 'enchant_data_list' in structural:
                    fi_edl = fi.get('enchant_data_list', [])
                    for i, saved_ed in enumerate(structural['enchant_data_list']):
                        if i >= len(fi_edl):
                            break
                        if saved_ed.get('equip_buffs') is not None:
                            fi_edl[i]['equip_buffs'] = saved_ed['equip_buffs']
                        if saved_ed.get('enchant_stat_data') is not None:
                            fi_edl[i]['enchant_stat_data'] = saved_ed['enchant_stat_data']

            self._buff_rust_items = fresh
            self._buff_rust_lookup = {it['key']: it for it in fresh}
            self._rebuild_index()
        except Exception as e:
            log.warning("Rust re-parse failed: %s", e)


    def _buff_remove_all(self) -> None:
        if self._buff_patcher is None:
            return

        reply = QMessageBox.question(
            self, "Reset All Changes",
            "Discard all in-memory modifications?\n\n"
            "This returns everything to the extracted vanilla state.\n"
            "No files on disk are modified.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        try:
            if hasattr(self, '_buff_rust_items_original') and self._buff_rust_items_original:
                import copy, json
                try:
                    self._buff_rust_items = copy.deepcopy(self._buff_rust_items_original)
                except TypeError:
                    self._buff_rust_items = json.loads(json.dumps(self._buff_rust_items_original))
                self._buff_rust_lookup = {it['key']: it for it in self._buff_rust_items}
                original = self._buff_patcher._original_data
                if original:
                    self._buff_data = bytearray(original)
                    self._buff_items = self._buff_patcher.find_items(bytes(original))
            else:
                original = self._buff_patcher._original_data
                if original:
                    self._buff_data = bytearray(original)
                    self._buff_items = self._buff_patcher.find_items(bytes(original))
                else:
                    raw = self._buff_patcher.extract_iteminfo()
                    self._buff_data = bytearray(raw)
                    self._buff_items = self._buff_patcher.find_items(bytes(raw))
                try:
                    import crimson_rs
                    self._buff_rust_items = list(self._buff_parse_to_lookup(bytes(self._buff_data)).values())
                    self._buff_rust_lookup = {it['key']: it for it in self._buff_rust_items}
                except Exception:
                    pass

            self._buff_modified = False
            self._buff_current_item = None
            try:
                self._buff_stats_table.setRowCount(0)
            except RuntimeError:
                pass
            if hasattr(self, '_buff_selected_label'):
                try:
                    self._buff_selected_label.setText("No item selected — search and click an item on the left")
                    self._buff_selected_label.setStyleSheet(
                        f"color: {COLORS['text_dim']}; font-weight: bold; padding: 2px 4px;"
                    )
                except RuntimeError:
                    pass
            try:
                self._buff_status_label.setText("Reset complete — vanilla state restored.")
            except RuntimeError:
                pass
        except Exception as e:
            try:
                QMessageBox.critical(self, "Reset Failed", str(e))
            except RuntimeError:
                pass


    def _buff_remove_selected(self) -> None:
        if not hasattr(self, '_buff_rust_items') or not self._buff_rust_items:
            QMessageBox.warning(self, "No Data", "Extract with Rust parser first.")
            return
        if not hasattr(self, '_buff_current_item') or self._buff_current_item is None:
            return

        rows = self._buff_stats_table.selectionModel().selectedRows()
        if not rows:
            QMessageBox.information(
                self, "No Selection",
                "Select a stat entry from the table to remove.",
            )
            return

        row = rows[0].row()
        name_cell = self._buff_stats_table.item(row, 0)
        if name_cell is None:
            return

        kind_data = name_cell.data(Qt.UserRole + 1)
        if not kind_data or kind_data[0] != 'stat':
            QMessageBox.information(self, "Not a Stat",
                "Select a stat entry (not a header, passive, or buff row).\n"
                "Use the Remove buttons above for passives and buffs.")
            return

        stat_key = kind_data[1]
        stat_list_name = kind_data[2]
        stat_value = kind_data[3] if len(kind_data) > 3 else 0

        _STAT_NAMES = {
            1000000: "HP", 1000002: "DDD", 1000003: "DPV",
            1000006: "Crit Damage", 1000007: "Crit Rate",
            1000010: "Attack Speed", 1000011: "Move Speed",
        }
        stat_name = (getattr(self, '_STAT_NAMES_COMMUNITY', {}).get(stat_key)
                     or _STAT_NAMES.get(stat_key, f"Stat {stat_key}"))

        reply = QMessageBox.question(
            self, "Remove Stat",
            f"Remove '{stat_name}' (value={stat_value:,}) from this item?\n\n"
            f"Removes from ALL enchant levels.\n"
            f"The change is held in memory until you click 'Export as Mod'.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        rust_info = self._buff_rust_lookup.get(self._buff_current_item.item_key)
        if not rust_info:
            return

        edl = rust_info.get('enchant_data_list', [])
        removed = 0
        for ed in edl:
            sd = ed.get('enchant_stat_data', {})
            existing = sd.get(stat_list_name, [])
            new_list = [s for s in existing if s['stat'] != stat_key]
            if len(new_list) < len(existing):
                removed += 1
                sd[stat_list_name] = new_list

        self._buff_modified = True
        self._buff_refresh_stats()
        display_name = self._name_db.get_name(self._buff_current_item.item_key)
        self._buff_status_label.setText(
            f"Removed {stat_name} from '{display_name}' ({removed} levels). "
            f"Click 'Export as Mod' to write."
        )


    def _buff_max_stacks(self, target: int = 9999) -> None:
        game_path = self._pabgb_get_game_path() if hasattr(self, '_pabgb_get_game_path') else self._config.get("game_install_path", "")
        if not game_path:
            QMessageBox.warning(self, "No Game Path", "Set the game install path using the Browse button at the top.")
            return

        if not _can_write_game_dir(game_path):
            QMessageBox.warning(
                self, "No Write Access",
                f"Cannot write to:\n{game_path}\n\n"
                "Right-click → Run as administrator",
            )
            return

        reply = QMessageBox.question(
            self, f"Max Stacks ({target})",
            f"Set all stackable item max stacks to {target}?\n\n"
            "This modifies iteminfo.pabgb in the game files.\n"
            "Equipment and non-stackable items are NOT affected.\n"
            "Replaces the FatStacks mod — no external mod needed.\n\n"
            "A backup will be created automatically.\n"
            "Survives game updates (structural parsing, no hardcoded offsets).",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        self._buff_status_label.setText("Preparing iteminfo...")
        QApplication.processEvents()

        try:
            if not self._buff_ensure_patcher():
                return

            if self._buff_data is not None:
                data = self._buff_data
            else:
                raw = self._buff_patcher.extract_iteminfo()
                data = bytearray(raw)
                self._buff_data = data
                self._buff_items = self._buff_patcher.find_items(bytes(data))

            self._buff_status_label.setText("Patching stack sizes...")
            QApplication.processEvents()

            count, descriptions = self._buff_patcher.patch_stack_sizes(data, target_stack=target)

            if count == 0:
                QMessageBox.information(self, "No Changes", f"All items already at {target} or are non-stackable.")
                self._buff_status_label.setText("No stack changes needed.")
                return

            self._buff_modified = True
            self._buff_status_label.setText(
                f"Stack sizes set to {target} for {count} items. "
                f"Click 'Export JSON Patch' to write."
            )
            QMessageBox.information(
                self, "Stacks Patched",
                f"Patched {count} items to {target} max stack (in memory).\n\n"
                f"Click 'Export JSON Patch' to write all changes to disk.",
            )

        except Exception as e:
            self._buff_status_label.setText(f"Error: {e}")
            QMessageBox.critical(self, "Error", str(e))


    def _buff_sync_community_names(self) -> None:
        BUFF_NAMES_URL = (
            "https://raw.githubusercontent.com/"
            "NattKh/CrimsonDesertCommunityItemMapping/main/buff_names_community.json"
        )
        self._buff_status_label.setText("Syncing buff names from GitHub...")
        QApplication.processEvents()

        try:
            from urllib.request import urlopen, Request
            req = Request(BUFF_NAMES_URL, headers={"User-Agent": "CrimsonSaveEditor/3.0"})
            with urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            self._buff_status_label.setText(f"Sync failed: {e}")
            QMessageBox.warning(self, "Sync Failed", f"Could not download buff names:\n{e}")
            return

        updated_buffs = 0
        updated_stats = 0
        updated_passives = 0

        def build_display(name: str, effect: str) -> str:
            if effect and effect != name:
                return f"{name} — {effect[:40]}"
            return name

        for entry in data.get("buffs", []):
            key = entry.get("key", 0)
            name = entry.get("name", "")
            effect = entry.get("effect", "")
            if key <= 0 or not name:
                continue
            display = build_display(name, effect)
            if key not in self._EQUIP_BUFF_NAMES or self._EQUIP_BUFF_NAMES[key] != display:
                self._EQUIP_BUFF_NAMES[key] = display
                updated_buffs += 1
            mn = entry.get("minValue")
            mx = entry.get("maxValue")
            vt = entry.get("valueType", "")
            if mn is not None and mx is not None:
                self._buff_community_ranges[key] = (mn, mx, vt)

        if hasattr(self, '_PASSIVE_SKILL_NAMES'):
            for entry in data.get("passives", []):
                key = entry.get("key", 0)
                name = entry.get("name", "")
                effect = entry.get("effect", "")
                if key <= 0 or not name:
                    continue
                display = build_display(name, effect)
                cur = self._PASSIVE_SKILL_NAMES.get(key)
                cur_str = cur if isinstance(cur, str) else (
                    cur.get('suffix') or cur.get('english_name') if isinstance(cur, dict) else None)
                if cur_str != display:
                    self._PASSIVE_SKILL_NAMES[key] = display
                    updated_passives += 1

        if not hasattr(self, '_STAT_NAMES_COMMUNITY'):
            self._STAT_NAMES_COMMUNITY = {}
        for entry in data.get("stats", []):
            key = entry.get("key", 0)
            name = entry.get("name", "")
            effect = entry.get("effect", "")
            if key <= 0 or not name:
                continue
            display = build_display(name, effect)
            if self._STAT_NAMES_COMMUNITY.get(key) != display:
                self._STAT_NAMES_COMMUNITY[key] = display
                updated_stats += 1
            mn = entry.get("minValue")
            mx = entry.get("maxValue")
            vt = entry.get("valueType", "")
            if mn is not None and mx is not None:
                self._buff_community_ranges[key] = (mn, mx, vt)

        updated = updated_buffs + updated_stats + updated_passives

        try:
            exe_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
            local_path = os.path.join(exe_dir, "buff_names_community.json")
            with open(local_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

        if hasattr(self, '_eb_buff_combo'):
            self._eb_buff_combo.clear()
            for bk in sorted(self._EQUIP_BUFF_NAMES.keys()):
                bname = self._EQUIP_BUFF_NAMES[bk]
                desc = self._buff_skill_descs.get(str(bk), {}).get("description", "")
                label = f"{bname} ({bk})" + (f" — {desc}" if desc else "")
                self._eb_buff_combo.addItem(label, bk)

        if hasattr(self, '_eb_passive_combo'):
            self._eb_passive_combo.clear()
            for pk in sorted(self._PASSIVE_SKILL_NAMES.keys()):
                pname = self._PASSIVE_SKILL_NAMES[pk]
                if isinstance(pname, dict):
                    pname = pname.get('suffix') or pname.get('english_name') or str(pk)
                self._eb_passive_combo.addItem(f"{pname} ({pk})", pk)

        try:
            self._buff_refresh_stats()
        except Exception:
            pass

        v = data.get("version", "?")
        stats_count = len(data.get("stats", []))
        buffs_count = len(data.get("buffs", []))
        passives_count = len(data.get("passives", []))
        self._buff_status_label.setText(
            f"Synced v{v}: +{updated_stats} stats, +{updated_buffs} buffs, "
            f"+{updated_passives} passives")

        if updated > 0:
            QMessageBox.information(self, "Community Names Synced",
                f"Updated display names from community database.\n\n"
                f"Version: {v}\n"
                f"  Stats updated:    {updated_stats} / {stats_count}\n"
                f"  Buffs updated:    {updated_buffs} / {buffs_count}\n"
                f"  Passives updated: {updated_passives} / {passives_count}\n\n"
                f"Changes reflect in the stats table, Add Buff/Passive dropdowns,\n"
                f"and description search immediately.\n\n"
                f"Contribute corrections:\n"
                f"github.com/NattKh/CrimsonDesertCommunityItemMapping")
        else:
            QMessageBox.information(self, "Already Up to Date",
                "All names match the latest community database.")


    def _buff_import_community_json(self) -> None:
        if not hasattr(self, '_buff_data') or self._buff_data is None:
            QMessageBox.warning(self, "No Data",
                "Extract iteminfo first (click Extract Rust).")
            return

        from PySide6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getOpenFileName(
            self, "Import Community JSON Patch",
            os.path.dirname(__file__),
            "JSON Files (*.json)")
        if not path:
            return

        try:
            with open(path, "r", encoding="utf-8") as f:
                mod_data = json.load(f)

            patches = mod_data.get("patches", [])
            if not patches:
                QMessageBox.warning(self, "Invalid Format",
                    "No 'patches' array found in JSON file.\n"
                    "Expected Pldada/DMM format with patches[].changes[].")
                return

            changes = []
            for patch_block in patches:
                game_file = patch_block.get("game_file", "")
                if "iteminfo" in game_file.lower():
                    changes = patch_block.get("changes", [])
                    break

            if not changes:
                QMessageBox.warning(self, "No iteminfo Patches",
                    "This JSON doesn't contain iteminfo.pabgb patches.\n"
                    f"Found patches for: {', '.join(p.get('game_file','?') for p in patches)}")
                return

            data = bytearray(self._buff_data)
            applied = 0
            skipped = 0
            for change in changes:
                offset = change.get("offset", 0)
                orig_hex = change.get("original", "")
                patch_hex = change.get("patched", "")
                label = change.get("label", "")

                if not orig_hex or not patch_hex:
                    skipped += 1
                    continue

                orig_bytes = bytes.fromhex(orig_hex)
                patch_bytes = bytes.fromhex(patch_hex)

                if len(orig_bytes) != len(patch_bytes):
                    skipped += 1
                    continue

                if offset + len(orig_bytes) > len(data):
                    skipped += 1
                    continue

                actual = bytes(data[offset:offset + len(orig_bytes)])
                if actual == orig_bytes:
                    data[offset:offset + len(patch_bytes)] = patch_bytes
                    applied += 1
                else:
                    skipped += 1

            if applied == 0:
                QMessageBox.warning(self, "No Patches Applied",
                    f"0/{len(changes)} patches matched.\n\n"
                    "This usually means iteminfo.pabgb has been modified by another mod.\n"
                    "Re-extract from vanilla first (click Extract Rust).")
                return

            self._buff_data = data
            self._buff_modified = True

            try:
                import crimson_rs
                rust_items = crimson_rs.parse_iteminfo_from_bytes(bytes(data))
                self._buff_rust_items = rust_items
                self._buff_rust_lookup = {it['key']: it for it in rust_items}
                self._buff_use_rust = True

                self._buff_status_label.setText(
                    f"Imported: {applied}/{len(changes)} patches applied "
                    f"({skipped} skipped). {len(rust_items)} items re-parsed.")

                if hasattr(self, '_buff_table') and self._buff_table.rowCount() > 0:
                    self._buff_search_items()

            except Exception as e:
                self._buff_status_label.setText(
                    f"Patches applied ({applied}) but re-parse failed: {e}")

            mod_name = mod_data.get("name", os.path.basename(path))
            QMessageBox.information(self, "Community Patch Imported",
                f"Imported: {mod_name}\n\n"
                f"Applied: {applied}/{len(changes)} patches\n"
                f"Skipped: {skipped} (offset mismatch or invalid)\n\n"
                f"The changes are now baked into your iteminfo data.\n"
                f"Make any additional ItemBuffs edits, then 'Export as Mod'\n"
                f"to create a combined mod with both changes.")

        except Exception as e:
            import traceback; traceback.print_exc()
            QMessageBox.critical(self, "Import Failed", str(e))

    SE_ITEMBUFFS_DIR = "0058"
    SE_STORES_DIR = "0060"


    def _buff_export_json(self) -> None:
        if not self._require_dev_mode("Export JSON Patch"):
            return
        if not self._buff_ensure_patcher():
            return

        apply_stacks = hasattr(self, '_stack_check') and self._stack_check.isChecked()
        apply_inf_dura = hasattr(self, '_inf_dura_check') and self._inf_dura_check.isChecked()

        if self._buff_patcher._original_data is None:
            QMessageBox.warning(self, "Export", "Extract iteminfo first.")
            return

        has_cd_patches = bool(getattr(self, '_cd_patches', {}))
        if not self._buff_modified and not apply_stacks and not has_cd_patches and not apply_inf_dura:
            QMessageBox.information(
                self, "No Changes",
                "No modifications have been made. Apply buffs or check 'Max Stacks' first.",
            )
            return

        if self._buff_data is None:
            try:
                raw = self._buff_patcher.extract_iteminfo()
                self._buff_data = bytearray(raw)
                self._buff_items = self._buff_patcher.find_items(bytes(self._buff_data))
            except Exception as e:
                QMessageBox.critical(self, "Extract Failed", str(e))
                return

        if apply_stacks:
            target = self._stack_spin.value()
            if hasattr(self, '_buff_rust_items') and self._buff_rust_items:
                for it in self._buff_rust_items:
                    if it.get('max_stack_count', 1) > 1:
                        it['max_stack_count'] = target

        if apply_inf_dura:
            if hasattr(self, '_buff_rust_items') and self._buff_rust_items:
                dura_count = 0
                for it in self._buff_rust_items:
                    endurance = it.get('max_endurance', 0)
                    if endurance > 0 and endurance != 65535:
                        it['max_endurance'] = 65535
                        it['is_destroy_when_broken'] = 0
                        dura_count += 1
                log.info("JSON Infinity Durability: patched %d items", dura_count)

        original = self._buff_patcher._original_data
        if not self._buff_modified and not apply_stacks and not apply_inf_dura and has_cd_patches:
            final_data = original
        else:
            try:
                import crimson_rs
                if hasattr(self, '_buff_rust_items') and self._buff_rust_items:
                    final_data = crimson_rs.serialize_iteminfo(self._buff_rust_items)
                else:
                    final_data = bytes(self._buff_data) if self._buff_data else original
            except Exception as e:
                QMessageBox.warning(self, "Serialize Failed", str(e))
                return

        if getattr(self, '_vfx_size_changes', None) or getattr(self, '_vfx_swaps', None) \
                or getattr(self, '_vfx_anim_swaps', None) or getattr(self, '_vfx_attach_changes', None) \
                or getattr(self, '_transmog_swaps', None):
            fa = bytearray(final_data)
            self._apply_vfx_changes(fa)
            self._apply_transmog_swaps(fa)
            final_data = bytes(fa)

        # Same-size: emit classic per-difference byte ops (compatible with
        # legacy JSON loaders). Different size (Universal Proficiency's
        # tribe_gender union grows iteminfo): fall back to the generic
        # replace+insert emitter so the change still exports.
        changes = []
        if len(final_data) == len(original):
            i = 0
            while i < len(final_data):
                if final_data[i] != original[i]:
                    start = i
                    while i < len(final_data) and final_data[i] != original[i]:
                        i += 1
                    changes.append({
                        "offset": start,
                        "label": f"iteminfo +0x{start:X}",
                        "original": original[start:i].hex(),
                        "patched": final_data[start:i].hex(),
                    })
                else:
                    i += 1
        else:
            log.info("iteminfo size changed (%d -> %d) — emitting growth diff",
                     len(original), len(final_data))
            changes = self._diff_to_json_patches(
                bytes(original), bytes(final_data), "iteminfo")

        for item_key, (abs_off, orig_bytes, new_val) in getattr(self, '_cd_patches', {}).items():
            new_bytes = struct.pack('<I', new_val)
            already = any(c['offset'] == abs_off for c in changes)
            if not already:
                changes.append({
                    "offset": abs_off,
                    "label": f"cooldown item_key={item_key} ({orig_bytes.hex()}->{new_bytes.hex()})",
                    "original": orig_bytes.hex(),
                    "patched": new_bytes.hex(),
                })

        has_staged = bool(getattr(self, '_staged_equip_files', None)) or \
                     bool(getattr(self, '_staged_skill_files', None))
        if not changes and not has_staged:
            QMessageBox.information(self, "Export", "No byte-level changes detected.")
            return

        reply = QMessageBox.question(
            self, "Export JSON Patch — Limitations",
            f"JSON Patch exports {len(changes)} byte-level change(s).\n\n"
            f"WHAT THIS SUPPORTS:\n"
            f"  - Change stat values (e.g. DDD 5000 -> 999999)\n"
            f"  - Swap stat hashes within same size class\n"
            f"  - Max stack size changes\n"
            f"  - Cooldown changes (seconds)\n\n"
            f"WHAT THIS DOES NOT SUPPORT:\n"
            f"  - Adding NEW buffs/effects (Fire Res, Ice Res, etc)\n"
            f"  - Adding NEW stats that don't exist on the item\n"
            f"  - Adding passive skills (Invincible, etc)\n"
            f"  - God Mode injection\n"
            f"  - Any edit that changes the file size\n\n"
            f"For those, use 'Export as Mod' instead.\n\n"
            f"Continue with JSON Patch export?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
        )
        if reply != QMessageBox.Yes:
            return

        from PySide6.QtWidgets import QInputDialog
        batch_name = getattr(self, '_buff_batch_mod_name', None)
        batch_dir = getattr(self, '_buff_batch_dir', None)
        if batch_name:
            name = batch_name
        else:
            name, ok = QInputDialog.getText(self, "Export JSON Patch",
                                            "Patch name:", text="My ItemBuffs Mod")
            if not ok or not name.strip():
                return
            name = name.strip()

        if batch_dir:
            path = os.path.join(batch_dir, f"{name}.json")
        else:
            path, _ = QFileDialog.getSaveFileName(
                self, "Export JSON Patch", f"{name}.json", "JSON Files (*.json)")
        if not path:
            return

        patches_list = [{
            "game_file": "gamedata/iteminfo.pabgb",
            "changes": changes,
        }]

        # If Universal Proficiency (or manual imbue skill edit) staged
        # equipslotinfo / skill files, emit byte-diff patches for those too
        # so the JSON format carries the full Universal-Proficiency mod
        # instead of silently dropping it.
        staged_equip = getattr(self, '_staged_equip_files', None) or {}
        staged_skill = getattr(self, '_staged_skill_files', None) or {}
        import crimson_rs as _crs
        for fname, new_bytes in list(staged_equip.items()) + list(staged_skill.items()):
            try:
                orig_bytes = _crs.extract_file(self._buff_patcher.game_path,
                    '0008', 'gamedata/binary__/client/bin', fname)
            except Exception as e:
                log.warning("JSON export: couldn't read vanilla %s: %s", fname, e)
                continue
            file_changes = self._diff_to_json_patches(orig_bytes, new_bytes, fname)
            if file_changes:
                patches_list.append({
                    "game_file": f"gamedata/binary__/client/bin/{fname}",
                    "changes": file_changes,
                })
                log.info("JSON export: emitted %d byte-diff(s) for %s",
                         len(file_changes), fname)

        patch_json = {
            "name": name,
            "version": "1.0",
            "description": f"{len(changes)} iteminfo changes" +
                (f" + {len(patches_list)-1} additional file(s)"
                 if len(patches_list) > 1 else ""),
            "author": "CrimsonSaveEditor",
            "format": 2,
            "patches": patches_list,
        }

        with open(path, 'w', encoding='utf-8') as f:
            json.dump(patch_json, f, indent=2)

        self._buff_status_label.setText(f"Exported {len(changes)} patches to {os.path.basename(path)}")
        QMessageBox.information(self, "Exported",
            f"Saved {len(changes)} patches to:\n{path}\n\n"
            f"Use CD JSON Mod Manager to apply this patch.\n"
            f"Drop the JSON file into the mod manager and click Apply.")


    def _sync_buff_state_after_creator(
        self,
        mode: str,
        item_bytes: bytes,
        new_key: int = 0,
        donor_key: int = 0,
    ) -> None:
        """Keep _buff_rust_items in sync with what Create Custom Item just
        wrote to disk. Without this, a later Enable All + Apply to Game
        serializes a stale dict and clobbers the custom item on 0058/.

        mode='new'  → append the new item dict (keyed new_key).
        mode='swap' → replace the donor's dict entry (keyed donor_key).
        """
        if not hasattr(self, '_buff_rust_items') or not self._buff_rust_items:
            return
        if not item_bytes:
            return
        try:
            import crimson_rs
            parsed = crimson_rs.parse_iteminfo_from_bytes(bytes(item_bytes))
        except Exception:
            return
        if not parsed:
            return
        new_dict = parsed[0]

        if mode == 'new':
            new_dict['key'] = int(new_key)
            # Avoid duplicates if user creates the same key twice
            self._buff_rust_items[:] = [
                it for it in self._buff_rust_items
                if it.get('key') != int(new_key)
            ]
            self._buff_rust_items.append(new_dict)
            if hasattr(self, '_buff_rust_lookup'):
                self._buff_rust_lookup[int(new_key)] = new_dict
            log.info("Synced new item key=%d into _buff_rust_items", new_key)
        elif mode == 'swap':
            new_dict['key'] = int(donor_key)
            replaced = False
            for i, it in enumerate(self._buff_rust_items):
                if it.get('key') == int(donor_key):
                    self._buff_rust_items[i] = new_dict
                    replaced = True
                    break
            if hasattr(self, '_buff_rust_lookup'):
                self._buff_rust_lookup[int(donor_key)] = new_dict
            log.info("Synced donor key=%d in _buff_rust_items (%s)",
                     donor_key, "replaced" if replaced else "not found")

        # Rebuild any derived caches so UI reflects the change.
        if hasattr(self, '_rebuild_index'):
            try:
                self._rebuild_index()
            except Exception:
                pass

    def _open_add_to_save_picker(self) -> None:
        """Scan live 0058/iteminfo for already-deployed custom items,
        let the user pick one, then open the Add-to-Save dialog for it.

        Independent of Create Custom Item -- useful when the user created
        the item earlier but didn't complete the save-file swap, or wants
        to add the same custom item to another save slot.
        """
        gp = getattr(self, '_game_path', '') or \
            self._config.get('game_install_path', '')
        if not gp or not os.path.isdir(gp):
            QMessageBox.warning(
                self, "Add Custom Item to Save",
                "Game install path not set.")
            return

        # Scan 0058/iteminfo for custom-range keys. Fall back to vanilla
        # comparison to be safe.
        try:
            import crimson_rs
            dp = 'gamedata/binary__/client/bin'
            try:
                body_058 = bytes(crimson_rs.extract_file(
                    gp, '0058', dp, 'iteminfo.pabgb'))
            except Exception:
                QMessageBox.warning(
                    self, "Add Custom Item to Save",
                    "No 0058/ overlay found. Create a custom item first "
                    "(Apply to Game (New Item) mode).")
                return
            body_vanilla = bytes(crimson_rs.extract_file(
                gp, '0008', dp, 'iteminfo.pabgb'))
            items_058 = crimson_rs.parse_iteminfo_from_bytes(body_058)
            vanilla_keys = {it['key']
                            for it in self._buff_parse_to_lookup(body_vanilla).values()}
            custom_items = [it for it in items_058
                            if it['key'] not in vanilla_keys]
        except Exception as e:
            log.exception("Add-to-save picker: scan failed")
            QMessageBox.critical(
                self, "Add Custom Item to Save",
                f"Failed to scan 0058/ for custom items:\n{e}")
            return

        if not custom_items:
            QMessageBox.information(
                self, "Add Custom Item to Save",
                "No custom items found in 0058/iteminfo.pabgb.\n\n"
                "Create one first via the Create Custom Item button.")
            return

        # Resolve display names via paloc if present, else fall back to string_key.
        try:
            from item_creator import compute_paloc_ids
            paloc_bytes = None
            try:
                paloc_bytes = bytes(crimson_rs.extract_file(
                    gp, '0064', 'gamedata/stringtable/binary__',
                    'localizationstring_eng.paloc'))
            except Exception:
                pass

            def resolve_name(it):
                sk = it.get('string_key', '') or f"Custom_{it['key']}"
                if paloc_bytes is None:
                    return sk
                try:
                    name_id, _ = compute_paloc_ids(it['key'])
                    needle = str(name_id).encode('utf-8')
                    if needle in paloc_bytes:
                        idx = paloc_bytes.index(needle)
                        v_off = idx + len(needle)
                        v_len = struct.unpack_from('<I', paloc_bytes, v_off)[0]
                        if 0 < v_len < 500:
                            return paloc_bytes[v_off+4:v_off+4+v_len].decode(
                                'utf-8', errors='replace')
                except Exception:
                    pass
                return sk
        except Exception:
            resolve_name = lambda it: it.get('string_key', f"Custom_{it['key']}")

        # Pick which custom item (if more than one)
        if len(custom_items) == 1:
            chosen = custom_items[0]
        else:
            from PySide6.QtWidgets import QInputDialog
            labels = [f"{resolve_name(it)}  (key {it['key']})"
                      for it in custom_items]
            label, ok = QInputDialog.getItem(
                self, "Pick Custom Item",
                f"Found {len(custom_items)} custom items in 0058/. "
                f"Pick one to swap into a save file:",
                labels, 0, False)
            if not ok:
                return
            chosen = custom_items[labels.index(label)]

        # Open the Add-to-Save dialog with the chosen item
        try:
            from gui.add_to_save_dialog import AddCustomItemToSaveDialog
            dlg = AddCustomItemToSaveDialog(
                custom_key=chosen['key'],
                custom_name=resolve_name(chosen),
                name_db=self._name_db,
                icon_cache=self._icon_cache,
                parent=self,
            )
            dlg.exec()
        except Exception as e:
            log.exception("AddCustomItemToSaveDialog failed to open")
            QMessageBox.critical(
                self, "Add Custom Item to Save",
                f"Couldn't open the dialog:\n{e}")

    def _open_item_creator(self) -> None:
        """Open the visual item creator dialog."""
        if not hasattr(self, '_buff_rust_items') or not self._buff_rust_items:
            QMessageBox.warning(self, "Create Item",
                                "Extract with Rust parser first (click 'Extract (Rust)').")
            return

        from gui.item_creator_dialog import ItemCreatorDialog
        dlg = ItemCreatorDialog(
            rust_items=self._buff_rust_items,
            name_db=self._name_db,
            icon_cache=self._icon_cache,
            game_path=getattr(self, '_game_path', ''),
            passive_skill_names=getattr(self, '_PASSIVE_SKILL_NAMES', {}),
            equip_buff_names=getattr(self, '_EQUIP_BUFF_NAMES', {}),
            parent=self,
        )
        if dlg.exec() != dlg.DialogCode.Accepted:
            return

        try:
            import crimson_rs
            import tempfile, shutil, os
            from item_creator import append_items_to_iteminfo

            gp = getattr(self, '_game_path', '') or \
                self._config.get('game_install_path', '')
            dp = 'gamedata/binary__/client/bin'

            # Read iteminfo from 0058/ overlay if the ItemBuffs tab has
            # written there (sentinel file present), otherwise vanilla 0008.
            # Without this, "Create Custom Item" clobbers the All Sockets /
            # UP v2 / other edits the user already applied.
            iteminfo_sentinel = os.path.join(gp, '0058', '.se_itembuffs')
            iteminfo_source = '0058' if os.path.isfile(iteminfo_sentinel) else '0008'

            if dlg.finish_mode == 'stage':
                # ── STAGE ITEM ──
                # Push the edited donor into _buff_rust_items so it gets
                # bundled into the next Apply to Game alongside everything
                # else (sockets, buffs, abyss, UP, etc). No deployment here.
                edited = crimson_rs.parse_iteminfo_from_bytes(dlg.created_item_bytes)
                if edited:
                    self._safely_replace_buff_item(dlg.created_donor_key, edited[0])
                    self._buff_modified = True
                    self._buff_refresh_stats()
                    QMessageBox.information(
                        self, "Item Staged",
                        f"Item staged into current edit session:\n\n"
                        f"  Name: {dlg.created_name}\n"
                        f"  Donor key: {dlg.created_donor_key}\n\n"
                        f"The item's stats are now in memory. Click\n"
                        f"'Apply to Game' to deploy alongside all your\n"
                        f"other edits (sockets, buffs, UP, etc).")
                    self._buff_status_label.setText(
                        f"Staged: {dlg.created_name} (key {dlg.created_donor_key}) "
                        f"— click Apply to Game to deploy")

            elif dlg.finish_mode == 'swap':
                # ── SWAP TO VENDOR ──
                # Modify the donor item's stats in iteminfo, then swap
                # the store entry to point at the donor key
                body = bytes(crimson_rs.extract_file(gp, iteminfo_source, dp, 'iteminfo.pabgb'))
                items = crimson_rs.parse_iteminfo_from_bytes(body)

                # Find and replace the donor item with our edited version
                for i, it in enumerate(items):
                    if it.get('key') == dlg.created_donor_key:
                        # Replace with edited data parsed back
                        edited = crimson_rs.parse_iteminfo_from_bytes(
                            dlg.created_item_bytes)
                        if edited:
                            items[i] = edited[0]
                            items[i]['key'] = dlg.created_donor_key
                        break

                new_iteminfo = crimson_rs.serialize_iteminfo(items)

                # Deploy iteminfo overlay
                files_58 = [(dp, 'iteminfo.pabgb', new_iteminfo)]

                # Also deploy storeinfo with the swap if store item differs
                files_60 = []
                if dlg.swap_replace_item_key != dlg.created_donor_key:
                    from storeinfo_parser import StoreinfoParser
                    sgb = bytes(crimson_rs.extract_file(gp, '0008', dp, 'storeinfo.pabgb'))
                    sgh = bytes(crimson_rs.extract_file(gp, '0008', dp, 'storeinfo.pabgh'))
                    sp = StoreinfoParser()
                    sp.load_from_bytes(sgh, sgb)
                    sp.swap_item(dlg.swap_store_key,
                                 dlg.swap_replace_item_key, dlg.created_donor_key)
                    files_60 = [
                        (dp, 'storeinfo.pabgb', bytes(sp._body_data)),
                        (dp, 'storeinfo.pabgh', sp._header_data),
                    ]

                # Deploy
                groups = {}
                with tempfile.TemporaryDirectory() as tmp:
                    for gid, files in [('0058', files_58), ('0060', files_60)]:
                        if not files:
                            continue
                        gdir = os.path.join(tmp, gid)
                        os.makedirs(gdir)
                        b = crimson_rs.PackGroupBuilder(
                            gdir, crimson_rs.Compression.NONE,
                            crimson_rs.Crypto.NONE)
                        for path, name, data in files:
                            b.add_file(path, name, data)
                        pamt = bytes(b.finish())
                        ck = crimson_rs.parse_pamt_bytes(pamt)['checksum']
                        dest = os.path.join(gp, gid)
                        if os.path.isdir(dest):
                            shutil.rmtree(dest)
                        os.makedirs(dest)
                        shutil.copy2(os.path.join(gdir, '0.paz'),
                                     os.path.join(dest, '0.paz'))
                        shutil.copy2(os.path.join(gdir, '0.pamt'),
                                     os.path.join(dest, '0.pamt'))
                        # Sentinel on 0058 so ItemBuffs' Extract recognizes
                        # this overlay and preserves it on next Apply.
                        if gid == '0058':
                            with open(os.path.join(dest, '.se_itembuffs'),
                                      'w') as _sf:
                                _sf.write("Created by CrimsonSaveEditor Custom Item Creator (Swap to Vendor)\n")
                        groups[gid] = ck

                papgt_path = os.path.join(gp, 'meta', '0.papgt')
                papgt = crimson_rs.parse_papgt_file(papgt_path)
                for gid in groups:
                    papgt['entries'] = [
                        e for e in papgt['entries']
                        if e.get('group_name') != gid]
                    papgt = crimson_rs.add_papgt_entry(
                        papgt, gid, groups[gid], 0, 16383)
                crimson_rs.write_papgt_file(papgt, papgt_path)

                # Sync the in-memory dict with the donor's new stats so a
                # subsequent Enable All → Apply to Game doesn't clobber the
                # changes we just wrote. Without this, _buff_rust_items
                # carries stale donor data.
                try:
                    self._sync_buff_state_after_creator(
                        mode='swap',
                        donor_key=dlg.created_donor_key,
                        item_bytes=dlg.created_item_bytes,
                    )
                except Exception as _sync_e:
                    log.warning("Post-swap in-memory sync failed: %s", _sync_e)

                QMessageBox.information(
                    self, "Swapped to Vendor",
                    f"Item deployed to vendor!\n\n"
                    f"Item: {dlg.created_name}\n"
                    f"Stats modified on: {dlg.created_donor_key}\n"
                    f"Overlays: {', '.join(groups.keys())}\n\n"
                    f"Restart game and visit the vendor to buy it."
                )
                self._buff_status_label.setText(
                    f"Vendor swap: {dlg.created_name} — restart game")

            elif dlg.finish_mode == 'dropset':
                # ── DELIVER VIA MONEY BAG ──
                # 1. Stage the donor item's stats into _buff_rust_items
                #    (deployed with Apply to Game alongside other edits)
                # 2. Deploy dropsetinfo overlay immediately (separate group)
                DROPSET_KEY = 400002
                DROPSET_GROUP = f"{self._config.get('dropset_overlay_dir', 36):04d}"

                edited = crimson_rs.parse_iteminfo_from_bytes(dlg.created_item_bytes)
                if edited:
                    self._safely_replace_buff_item(dlg.created_donor_key, edited[0])
                    self._buff_modified = True

                from dropset_editor import DropsetEditor
                ds_pabgh = bytes(crimson_rs.extract_file(gp, '0008', dp, 'dropsetinfo.pabgh'))
                ds_pabgb = bytes(crimson_rs.extract_file(gp, '0008', dp, 'dropsetinfo.pabgb'))
                with tempfile.TemporaryDirectory() as _ds_tmp:
                    _gh_p = os.path.join(_ds_tmp, 'dropsetinfo.pabgh')
                    _gb_p = os.path.join(_ds_tmp, 'dropsetinfo.pabgb')
                    with open(_gh_p, 'wb') as f: f.write(ds_pabgh)
                    with open(_gb_p, 'wb') as f: f.write(ds_pabgb)
                    ds_editor = DropsetEditor()
                    ds_editor.load(_gh_p, _gb_p)

                    ds = ds_editor.parse_dropset(DROPSET_KEY)
                    if not ds:
                        QMessageBox.critical(self, "Dropset Error",
                            f"Dropset {DROPSET_KEY} not found in game data.")
                        return

                    ds.drops.clear()
                    ds_editor.add_item(ds, dlg.created_donor_key,
                                       rate=1000000, min_qty=1, max_qty=1)
                    ds_editor.apply_modifications([ds])

                    new_ds_pabgb = bytes(ds_editor.body_bytes)
                    new_ds_pabgh = ds_editor.header_bytes

                if not _can_write_game_dir(gp):
                    QMessageBox.warning(self, "No Write Access",
                        f"Cannot write to:\n{gp}\n\n"
                        "Item stats staged in memory. Dropset NOT deployed.\n"
                        "Run as administrator to deploy dropset overlay.")
                else:
                    with tempfile.TemporaryDirectory() as tmp:
                        gdir = os.path.join(tmp, DROPSET_GROUP)
                        os.makedirs(gdir)
                        b = crimson_rs.PackGroupBuilder(
                            gdir, crimson_rs.Compression.NONE,
                            crimson_rs.Crypto.NONE)
                        b.add_file(dp, 'dropsetinfo.pabgb', new_ds_pabgb)
                        b.add_file(dp, 'dropsetinfo.pabgh', new_ds_pabgh)
                        pamt = bytes(b.finish())
                        ck = crimson_rs.parse_pamt_bytes(pamt)['checksum']
                        dest = os.path.join(gp, DROPSET_GROUP)
                        if os.path.isdir(dest):
                            shutil.rmtree(dest)
                        os.makedirs(dest)
                        shutil.copy2(os.path.join(gdir, '0.paz'),
                                     os.path.join(dest, '0.paz'))
                        shutil.copy2(os.path.join(gdir, '0.pamt'),
                                     os.path.join(dest, '0.pamt'))

                    papgt_path = os.path.join(gp, 'meta', '0.papgt')
                    papgt = crimson_rs.parse_papgt_file(papgt_path)
                    papgt['entries'] = [
                        e for e in papgt['entries']
                        if e.get('group_name') != DROPSET_GROUP]
                    papgt = crimson_rs.add_papgt_entry(
                        papgt, DROPSET_GROUP, ck, 0, 16383)
                    crimson_rs.write_papgt_file(papgt, papgt_path)

                self._buff_refresh_stats()
                QMessageBox.information(
                    self, "Delivered via Money Bag",
                    f"Item staged + dropset deployed!\n\n"
                    f"Item: {dlg.created_name}\n"
                    f"Donor key: {dlg.created_donor_key}\n"
                    f"Dropset: {DROPSET_KEY} (Copper Money Bag) → {DROPSET_GROUP}/\n\n"
                    f"Item stats are staged in memory — click 'Apply to Game'\n"
                    f"to deploy iteminfo alongside your other edits.\n"
                    f"Then restart game and open a Copper Money Pouch.")
                self._buff_status_label.setText(
                    f"Money Bag: {dlg.created_name} staged + dropset → {DROPSET_GROUP}/ "
                    f"— click Apply to Game, then open a pouch")

            elif dlg.finish_mode == 'export_single':
                # ── EXPORT AS SINGLE-ITEM MOD ──
                # Build a standalone folder mod: vanilla iteminfo + just
                # this one item, plus paloc with the name. Does NOT touch
                # the live game. Shareable + re-importable.
                from PySide6.QtWidgets import QFileDialog, QInputDialog
                default_name = f"CustomItem_{dlg.created_key}_" + "".join(
                    c if c.isalnum() else '_' for c in dlg.created_name)[:40]
                parent_dir = QFileDialog.getExistingDirectory(
                    self, "Pick export location",
                    self._config.get("custom_item_export_dir",
                                     os.path.expanduser("~/Desktop")))
                if not parent_dir:
                    return
                self._config["custom_item_export_dir"] = parent_dir
                mod_name, ok = QInputDialog.getText(
                    self, "Mod name",
                    "Folder name for this single-item mod:",
                    text=default_name)
                if not ok or not mod_name.strip():
                    return
                mod_name = "".join(c if (c.isalnum() or c in "-_ .") else "_"
                                   for c in mod_name.strip())
                out_dir = os.path.join(parent_dir, mod_name)
                if os.path.isdir(out_dir) and os.listdir(out_dir):
                    QMessageBox.warning(self, "Export",
                        f"Output folder exists and is not empty:\n{out_dir}")
                    return
                os.makedirs(out_dir, exist_ok=True)

                # Build vanilla+item iteminfo
                v_body = bytes(crimson_rs.extract_file(gp, '0008', dp, 'iteminfo.pabgb'))
                v_head = bytes(crimson_rs.extract_file(gp, '0008', dp, 'iteminfo.pabgh'))
                new_body, new_head = append_items_to_iteminfo(
                    v_body, v_head, [(dlg.created_key, dlg.created_item_bytes)])

                # Build vanilla+name paloc
                paloc_bytes = None
                try:
                    from item_creator import compute_paloc_ids
                    paloc_raw = bytes(crimson_rs.extract_file(
                        gp, '0020', 'gamedata/stringtable/binary__',
                        'localizationstring_eng.paloc'))
                    entries = []
                    off = 0
                    while off < len(paloc_raw) - 16:
                        marker = paloc_raw[off:off+8]
                        if off + 12 > len(paloc_raw):
                            break
                        k_len = struct.unpack_from('<I', paloc_raw, off+8)[0]
                        if k_len == 0 or k_len > 500:
                            break
                        if off + 12 + k_len + 4 > len(paloc_raw):
                            break
                        key_s = paloc_raw[off+12:off+12+k_len].decode('utf-8', errors='replace')
                        v_off = off + 12 + k_len
                        v_len = struct.unpack_from('<I', paloc_raw, v_off)[0]
                        if v_len > 500000:
                            break
                        val = paloc_raw[v_off+4:v_off+4+v_len].decode('utf-8', errors='replace')
                        entries.append({'marker': marker, 'key': key_s, 'value': val})
                        off = v_off + 4 + v_len
                    paloc_tail = paloc_raw[off:]

                    name_id, desc_id = compute_paloc_ids(dlg.created_key)
                    ITEM_MARKER = bytes.fromhex('0700000000000000')
                    entries.append({'marker': ITEM_MARKER, 'key': str(name_id),
                                    'value': dlg.created_name})
                    if dlg.created_desc:
                        entries.append({'marker': ITEM_MARKER, 'key': str(desc_id),
                                        'value': dlg.created_desc})

                    def _sort_key(e):
                        try: return (0, int(e['key']), '')
                        except ValueError: return (1, 0, e['key'])
                    entries.sort(key=_sort_key)

                    paloc_out = bytearray()
                    for e in entries:
                        paloc_out += e['marker']
                        kb = e['key'].encode('utf-8')
                        paloc_out += struct.pack('<I', len(kb)) + kb
                        vb = e['value'].encode('utf-8')
                        paloc_out += struct.pack('<I', len(vb)) + vb
                    paloc_out += paloc_tail
                    paloc_bytes = bytes(paloc_out)
                except Exception as _pe:
                    log.warning("Export: paloc build failed: %s", _pe)

                # Write the two groups as shareable folders (0036 + 0064,
                # matching the standard single-item mod convention).
                import tempfile
                with tempfile.TemporaryDirectory() as tmp:
                    # 0036/ = iteminfo
                    gdir36 = os.path.join(tmp, '0036')
                    os.makedirs(gdir36)
                    b = crimson_rs.PackGroupBuilder(
                        gdir36, crimson_rs.Compression.NONE,
                        crimson_rs.Crypto.NONE)
                    b.add_file(dp, 'iteminfo.pabgb', new_body)
                    b.add_file(dp, 'iteminfo.pabgh', new_head)
                    b.finish()
                    dest36 = os.path.join(out_dir, '0036')
                    os.makedirs(dest36)
                    shutil.copy2(os.path.join(gdir36, '0.paz'),
                                 os.path.join(dest36, '0.paz'))
                    shutil.copy2(os.path.join(gdir36, '0.pamt'),
                                 os.path.join(dest36, '0.pamt'))

                    # 0064/ = paloc (if built)
                    if paloc_bytes:
                        gdir64 = os.path.join(tmp, '0064')
                        os.makedirs(gdir64)
                        b2 = crimson_rs.PackGroupBuilder(
                            gdir64, crimson_rs.Compression.NONE,
                            crimson_rs.Crypto.NONE)
                        b2.add_file('gamedata/stringtable/binary__',
                                    'localizationstring_eng.paloc', paloc_bytes)
                        b2.finish()
                        dest64 = os.path.join(out_dir, '0064')
                        os.makedirs(dest64)
                        shutil.copy2(os.path.join(gdir64, '0.paz'),
                                     os.path.join(dest64, '0.paz'))
                        shutil.copy2(os.path.join(gdir64, '0.pamt'),
                                     os.path.join(dest64, '0.pamt'))

                # modinfo.json + README for mod-loader compatibility
                import json as _json
                modinfo = {
                    "id": mod_name.lower().replace(" ", "_"),
                    "name": mod_name,
                    "version": "1.0.0",
                    "author": "CrimsonGameMods Item Creator",
                    "description": (
                        f"Single-item mod: {dlg.created_name} "
                        f"(key {dlg.created_key}). Adds one new item to iteminfo; "
                        f"does not modify any other items."),
                    "custom_item_key": dlg.created_key,
                    "custom_item_name": dlg.created_name,
                    "bundled_files": (
                        ["iteminfo.pabgb", "iteminfo.pabgh"] +
                        (["localizationstring_eng.paloc"] if paloc_bytes else [])),
                }
                with open(os.path.join(out_dir, "modinfo.json"), "w", encoding="utf-8") as f:
                    _json.dump(modinfo, f, indent=2, ensure_ascii=False)

                readme = (
                    f"{mod_name}\n"
                    f"{'=' * len(mod_name)}\n\n"
                    f"Single-item mod built from CrimsonGameMods Item Creator.\n\n"
                    f"Item:\n"
                    f"  Name : {dlg.created_name}\n"
                    f"  Key  : {dlg.created_key}\n\n"
                    f"Contents:\n"
                    f"  0036/  iteminfo.pabgb + iteminfo.pabgh (vanilla + this item)\n"
                    f"  0064/  localizationstring_eng.paloc (with custom name)\n\n"
                    f"HOW TO INSTALL\n"
                    f"--------------\n"
                    f"Via mod loader (recommended):\n"
                    f"  JMM: drag this folder into mods/_enabled/, click Apply.\n"
                    f"  CDUMM: import this folder, enable, Apply.\n\n"
                    f"Via CrimsonGameMods:\n"
                    f"  ItemBuffs tab -> Import menu -> Import CDUMM/PAZ Mod,\n"
                    f"  then pick this folder. The item gets added to your\n"
                    f"  in-memory dict and ships with your next Apply to Game.\n\n"
                    f"This mod was built against vanilla iteminfo and contains\n"
                    f"only the one new item. It will not overwrite or conflict\n"
                    f"with other item-data changes you have installed.\n"
                )
                with open(os.path.join(out_dir, "README.txt"), "w", encoding="utf-8") as f:
                    f.write(readme)

                QMessageBox.information(
                    self, "Exported",
                    f"Single-item mod exported:\n\n{out_dir}\n\n"
                    f"Item: {dlg.created_name} (key {dlg.created_key})\n"
                    f"Contents: 0036/ + {'0064/ + ' if paloc_bytes else ''}modinfo.json + README.txt\n\n"
                    f"Share this folder, or re-import via ItemBuffs -> Import menu.")
                self._buff_status_label.setText(
                    f"Exported: {dlg.created_name} -> {mod_name}/")
            else:
                # ── APPLY TO GAME (NEW ITEM) ──
                # Read from 0058/ if ItemBuffs has already modded it; else
                # vanilla. The pabgh MUST match the pabgb we read -- if the
                # 0058/ overlay lacks pabgh (pre-v1.0.5 write), regenerate
                # from the pabgb so append_items_to_iteminfo's offsets line up.
                body = bytes(crimson_rs.extract_file(gp, iteminfo_source, dp, 'iteminfo.pabgb'))
                try:
                    head = bytes(crimson_rs.extract_file(gp, iteminfo_source, dp, 'iteminfo.pabgh'))
                except Exception:
                    # Older overlay without pabgh -- rebuild from current body
                    from item_creator import build_iteminfo_pabgh
                    _extra = getattr(self, '_buff_appended_entries', [])
                    head = build_iteminfo_pabgh(body, extra_entries=_extra)
                new_body, new_head = append_items_to_iteminfo(
                    body, head, [(dlg.created_key, dlg.created_item_bytes)])

                # ── Patch paloc for custom name ──
                paloc_bytes = None
                paloc_msg = ""
                try:
                    from item_creator import compute_paloc_ids
                    paloc_raw = bytes(crimson_rs.extract_file(
                        gp, '0020', 'gamedata/stringtable/binary__',
                        'localizationstring_eng.paloc'))

                    # Parse paloc entries
                    entries = []
                    off = 0
                    while off < len(paloc_raw) - 16:
                        marker = paloc_raw[off:off+8]
                        if off + 12 > len(paloc_raw):
                            break
                        k_len = struct.unpack_from('<I', paloc_raw, off+8)[0]
                        if k_len == 0 or k_len > 500:
                            break
                        if off + 12 + k_len + 4 > len(paloc_raw):
                            break
                        key_s = paloc_raw[off+12:off+12+k_len].decode('utf-8', errors='replace')
                        v_off = off + 12 + k_len
                        v_len = struct.unpack_from('<I', paloc_raw, v_off)[0]
                        if v_len > 500000:
                            break
                        val = paloc_raw[v_off+4:v_off+4+v_len].decode('utf-8', errors='replace')
                        entries.append({'marker': marker, 'key': key_s, 'value': val})
                        off = v_off + 4 + v_len
                    paloc_tail = paloc_raw[off:]

                    # Add name + description
                    name_id, desc_id = compute_paloc_ids(dlg.created_key)
                    ITEM_MARKER = bytes.fromhex('0700000000000000')
                    entries = [e for e in entries
                               if e['key'] not in (str(name_id), str(desc_id))]
                    entries.append({'marker': ITEM_MARKER, 'key': str(name_id),
                                    'value': dlg.created_name})
                    if dlg.created_desc:
                        entries.append({'marker': ITEM_MARKER, 'key': str(desc_id),
                                        'value': dlg.created_desc})

                    def _sort_key(e):
                        try: return (0, int(e['key']), '')
                        except ValueError: return (1, 0, e['key'])
                    entries.sort(key=_sort_key)

                    # Rebuild paloc binary
                    paloc_out = bytearray()
                    for e in entries:
                        paloc_out += e['marker']
                        kb = e['key'].encode('utf-8')
                        paloc_out += struct.pack('<I', len(kb)) + kb
                        vb = e['value'].encode('utf-8')
                        paloc_out += struct.pack('<I', len(vb)) + vb
                    paloc_out += paloc_tail
                    paloc_bytes = bytes(paloc_out)
                    paloc_msg = f" + 0064/ (localization: \"{dlg.created_name}\")"
                    log.info("Paloc patched: added name for key %d", dlg.created_key)
                except Exception as e:
                    log.warning("Paloc patching failed (name may show blank): %s", e)
                    paloc_msg = " (name may show blank — paloc patch failed)"

                # Deploy iteminfo (0058) + paloc (0064)
                groups_to_deploy = {}
                with tempfile.TemporaryDirectory() as tmp:
                    # 0058: iteminfo
                    gdir58 = os.path.join(tmp, '0058')
                    os.makedirs(gdir58)
                    b = crimson_rs.PackGroupBuilder(
                        gdir58, crimson_rs.Compression.NONE,
                        crimson_rs.Crypto.NONE)
                    b.add_file(dp, 'iteminfo.pabgb', new_body)
                    b.add_file(dp, 'iteminfo.pabgh', new_head)
                    pamt = bytes(b.finish())
                    groups_to_deploy['0058'] = \
                        crimson_rs.parse_pamt_bytes(pamt)['checksum']
                    dest = os.path.join(gp, '0058')
                    if os.path.isdir(dest):
                        shutil.rmtree(dest)
                    os.makedirs(dest)
                    shutil.copy2(os.path.join(gdir58, '0.paz'),
                                 os.path.join(dest, '0.paz'))
                    shutil.copy2(os.path.join(gdir58, '0.pamt'),
                                 os.path.join(dest, '0.pamt'))
                    # Sentinel so ItemBuffs' Extract-prefer-overlay recognizes
                    # this 0058/ as safe-to-read and layers its edits on top
                    # instead of clobbering us with a vanilla-based rewrite.
                    with open(os.path.join(dest, '.se_itembuffs'), 'w') as _sf:
                        _sf.write("Created by CrimsonSaveEditor Custom Item Creator\n")

                    # 0064: paloc (if patched)
                    if paloc_bytes:
                        gdir64 = os.path.join(tmp, '0064')
                        os.makedirs(gdir64)
                        b2 = crimson_rs.PackGroupBuilder(
                            gdir64, crimson_rs.Compression.NONE,
                            crimson_rs.Crypto.NONE)
                        b2.add_file('gamedata/stringtable/binary__',
                                    'localizationstring_eng.paloc', paloc_bytes)
                        pamt2 = bytes(b2.finish())
                        groups_to_deploy['0064'] = \
                            crimson_rs.parse_pamt_bytes(pamt2)['checksum']
                        dest64 = os.path.join(gp, '0064')
                        if os.path.isdir(dest64):
                            shutil.rmtree(dest64)
                        os.makedirs(dest64)
                        shutil.copy2(os.path.join(gdir64, '0.paz'),
                                     os.path.join(dest64, '0.paz'))
                        shutil.copy2(os.path.join(gdir64, '0.pamt'),
                                     os.path.join(dest64, '0.pamt'))

                papgt_path = os.path.join(gp, 'meta', '0.papgt')
                papgt = crimson_rs.parse_papgt_file(papgt_path)
                for gid in groups_to_deploy:
                    papgt['entries'] = [
                        e for e in papgt['entries']
                        if e.get('group_name') != gid]
                    papgt = crimson_rs.add_papgt_entry(
                        papgt, gid, groups_to_deploy[gid], 0, 16383)
                crimson_rs.write_papgt_file(papgt, papgt_path)

                # Sync in-memory state — append the new item so subsequent
                # ItemBuffs actions (Enable All, Apply to Game) see it and
                # don't clobber it on the next serialize.
                try:
                    self._sync_buff_state_after_creator(
                        mode='new',
                        new_key=dlg.created_key,
                        item_bytes=dlg.created_item_bytes,
                    )
                except Exception as _sync_e:
                    log.warning("Post-create in-memory sync failed: %s", _sync_e)

                overlays = ', '.join(sorted(groups_to_deploy.keys()))
                reply = QMessageBox.question(
                    self, "Item Created",
                    f"New item deployed!\n\n"
                    f"Name: {dlg.created_name}\n"
                    f"Key: {dlg.created_key}\n"
                    f"Overlays: {overlays}{paloc_msg}\n\n"
                    f"Add it to your save file now?\n"
                    f"(Picks a save + swaps an existing item's key to "
                    f"{dlg.created_key} so it shows up in-game.)",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
                )
                self._buff_status_label.setText(
                    f"Created: {dlg.created_name} (key={dlg.created_key})")
                if reply == QMessageBox.Yes:
                    try:
                        from gui.add_to_save_dialog import AddCustomItemToSaveDialog
                        add_dlg = AddCustomItemToSaveDialog(
                            custom_key=dlg.created_key,
                            custom_name=dlg.created_name,
                            name_db=self._name_db,
                            icon_cache=self._icon_cache,
                            parent=self,
                        )
                        add_dlg.exec()
                    except Exception as _e:
                        log.exception("AddCustomItemToSaveDialog failed to open")
                        QMessageBox.warning(
                            self, "Add to Save",
                            f"Couldn't open the Add-to-Save dialog:\n{_e}\n\n"
                            f"You can still use the standalone Save Editor's "
                            f"Repurchase tab to swap an item to key "
                            f"{dlg.created_key}.")

        except Exception as e:
            log.exception("Item creator deploy failed")
            QMessageBox.critical(self, "Deploy Failed", str(e))

    def _goto_stacker_legacy_export(self) -> None:
        """Switch to Stacker Tool tab for legacy JSON export."""
        self.navigate_requested.emit("stacker")
        QMessageBox.information(self, "Legacy JSON Export",
            "Switched to Stacker Tool.\n\n"
            "1. Click 'Pull ItemBuffs Edit' to pull your edits.\n"
            "2. Click 'PREVIEW' to build the merge.\n"
            "3. Click 'EXPORT LEGACY JSON' to save.")

    def _buff_export_mod_folder(self) -> None:
        """Export as a standard folder mod (Stacker style).

        Output: <dir>/<name>/0036/0.paz + 0.pamt + modinfo.json
        Uses group 0036 so any loader (JMM, CDUMM) can remap at install time.
        """
        if not self._buff_ensure_patcher():
            return
        if not hasattr(self, '_buff_rust_items') or not self._buff_rust_items:
            QMessageBox.warning(self, "Export Mod", "Extract iteminfo first.")
            return

        from PySide6.QtWidgets import QInputDialog
        parent_dir = QFileDialog.getExistingDirectory(
            self, "Pick where to save the folder mod",
            self._config.get("buffs_export_dir",
                             os.path.dirname(os.path.abspath(sys.argv[0]))))
        if not parent_dir:
            return
        self._config["buffs_export_dir"] = parent_dir
        self.config_save_requested.emit()

        name, ok = QInputDialog.getText(
            self, "Mod name",
            "Folder mod name (a folder with this name will be created):",
            text="My ItemBuffs Mod")
        if not ok or not name.strip():
            return
        safe_name = "".join(c if (c.isalnum() or c in "-_ .") else "_"
                            for c in name.strip())
        out_dir = os.path.join(parent_dir, safe_name)
        if os.path.isdir(out_dir) and os.listdir(out_dir):
            QMessageBox.warning(self, "Export Mod",
                f"Folder already exists and is not empty:\n{out_dir}\n\n"
                "Pick a new name or delete it first.")
            return

        self._buff_status_label.setText("Serializing...")
        QApplication.processEvents()

        try:
            import crimson_rs
            import tempfile

            final_data = self._rebuild_full_iteminfo()
            fa = bytearray(final_data)
            self._apply_vfx_changes(fa)
            self._apply_transmog_swaps(fa)
            final_data = bytes(fa)

            game_path = self._buff_patcher.game_path
            INTERNAL_DIR = "gamedata/binary__/client/bin"
            group = "0036"

            os.makedirs(out_dir, exist_ok=True)

            with tempfile.TemporaryDirectory() as tmp:
                group_dir = os.path.join(tmp, group)
                builder = crimson_rs.PackGroupBuilder(
                    group_dir,
                    crimson_rs.Compression.NONE,
                    crimson_rs.Crypto.NONE,
                )
                builder.add_file(INTERNAL_DIR, "iteminfo.pabgb", final_data)
                _pabgh = getattr(self, '_buff_rebuilt_pabgh', None)
                if not _pabgh:
                    _pabgh = bytes(crimson_rs.extract_file(
                        game_path, '0008', INTERNAL_DIR, 'iteminfo.pabgh'))
                builder.add_file(INTERNAL_DIR, "iteminfo.pabgh", _pabgh)

                staged_skill = getattr(self, "_staged_skill_files", None) or {}
                for fname in ("skill.pabgb", "skill.pabgh"):
                    if fname in staged_skill:
                        builder.add_file(INTERNAL_DIR, fname, staged_skill[fname])

                staged_charinfo = getattr(self, "_staged_charinfo_files", None) or {}
                for fname in ("characterinfo.pabgb", "characterinfo.pabgh"):
                    if fname in staged_charinfo:
                        builder.add_file(INTERNAL_DIR, fname, staged_charinfo[fname])

                pamt_bytes = bytes(builder.finish())

                out_group = os.path.join(out_dir, group)
                os.makedirs(out_group, exist_ok=True)
                for f in os.listdir(group_dir):
                    shutil.copy2(os.path.join(group_dir, f),
                                 os.path.join(out_group, f))
                if not os.path.isfile(os.path.join(out_group, "0.pamt")):
                    with open(os.path.join(out_group, "0.pamt"), "wb") as f:
                        f.write(pamt_bytes)

            with open(os.path.join(out_dir, "modinfo.json"), "w",
                      encoding="utf-8") as f:
                json.dump({
                    "id": safe_name.lower().replace(" ", "_"),
                    "name": name.strip(),
                    "version": "1.0.0",
                    "author": "CrimsonGameMods",
                    "description": f"ItemBuffs mod: {name.strip()}",
                }, f, indent=2)

            paz_size = os.path.getsize(os.path.join(out_group, "0.paz"))
            self._buff_status_label.setText(f"Exported to {safe_name}/")
            QMessageBox.information(self, "Mod Exported",
                f"Folder mod exported to:\n{out_dir}\n\n"
                f"Contents:\n"
                f"  {group}/0.paz ({paz_size:,} bytes)\n"
                f"  {group}/0.pamt\n"
                f"  modinfo.json\n\n"
                f"Install with JMM / CDUMM, or drop {group}/ into game dir.")

        except Exception as e:
            import traceback; traceback.print_exc()
            self._buff_status_label.setText(f"Export failed: {e}")
            QMessageBox.critical(self, "Export Failed",
                f"Failed to export mod:\n{e}")
            
    def _buff_apply_to_game(self) -> None:
        if not self._buff_ensure_patcher():
            return

        game_path = self._buff_patcher.game_path
        if not _can_write_game_dir(game_path):
            QMessageBox.warning(
                self, "No Write Access",
                f"Cannot write to:\n{game_path}\n\n"
                "Try running the editor as Administrator:\n"
                "Right-click → Run as administrator",
            )
            return

        # Check for iteminfo conflicts from other tools before applying
        try:
            from overlay_coordinator import check_iteminfo_conflicts_before_apply
            buff_dir = f"{self._buff_overlay_spin.value():04d}"
            warning = check_iteminfo_conflicts_before_apply(
                self._buff_patcher.game_path, buff_dir, self._config)
            if warning:
                reply = QMessageBox.warning(
                    self, "ItemInfo Conflict Detected", warning,
                    QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
                if reply != QMessageBox.Yes:
                    return
        except Exception:
            pass

        if self._buff_modified:
            save_first = QMessageBox.question(
                self, "Save Config?",
                "Save your current edits as a config file before applying?\n\n"
                "This lets you re-apply the same edits later without redoing them.",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
                QMessageBox.No,
            )
            if save_first == QMessageBox.Cancel:
                return
            if save_first == QMessageBox.Yes:
                self._buff_save_config()

        apply_stacks = hasattr(self, '_stack_check') and self._stack_check.isChecked()
        apply_inf_dura = hasattr(self, '_inf_dura_check') and self._inf_dura_check.isChecked()

        if self._buff_data is None:
            if not apply_stacks and not apply_inf_dura:
                QMessageBox.warning(self, "No Data", "Extract iteminfo first.")
                return
            try:
                raw = self._buff_patcher.extract_iteminfo()
                self._buff_data = bytearray(raw)
                self._buff_items = self._buff_patcher.find_items(bytes(self._buff_data))
            except Exception as e:
                QMessageBox.critical(self, "Extract Failed", str(e))
                return

        has_transmog = bool(getattr(self, '_transmog_swaps', None))
        has_vfx = bool(getattr(self, '_vfx_size_changes', None)
                       or getattr(self, '_vfx_swaps', None)
                       or getattr(self, '_vfx_anim_swaps', None)
                       or getattr(self, '_vfx_attach_changes', None))
        has_cd = bool(getattr(self, '_cd_patches', None))
        if (not self._buff_modified and not apply_stacks and not apply_inf_dura
                and not has_transmog and not has_vfx and not has_cd):
            QMessageBox.information(
                self, "No Changes",
                "No modifications have been made.\n\n"
                "Apply buffs, set up transmog swaps, or check\n"
                "'Also apply Max Stacks' first.",
            )
            return

        stack_msg = ""
        if apply_stacks:
            target = self._stack_spin.value()
            count, _ = self._buff_patcher.patch_stack_sizes(self._buff_data, target_stack=target)
            stack_msg = f"\nMax Stacks: {count} items set to {target}"

        from pipeline_report import PipelineReport
        rpt = PipelineReport()
        rpt.stage("input", f"flags: stacks={apply_stacks} inf_dura={apply_inf_dura} "
                           f"buff_modified={self._buff_modified} "
                           f"cd_patches={len(getattr(self, '_cd_patches', {}))} "
                           f"transmog_swaps={len(getattr(self, '_transmog_swaps', []) or [])}")

        if hasattr(self, '_buff_rust_items') and self._buff_rust_items:
            try:
                import crimson_rs
                if apply_stacks:
                    target_val = self._stack_spin.value()
                    stack_keys: list[int] = []
                    for it in self._buff_rust_items:
                        if it.get('max_stack_count', 1) > 1:
                            it['max_stack_count'] = target_val
                            stack_keys.append(it.get('key'))
                    rpt.stage("max_stacks", f"set max_stack_count={target_val} on {len(stack_keys)} items")
                    rpt.expect('max_stacks', {'target': target_val, 'items': stack_keys})
                if apply_inf_dura:
                    dura_count = 0
                    dura_keys: list[int] = []
                    for it in self._buff_rust_items:
                        endurance = it.get('max_endurance', 0)
                        if endurance > 0 and endurance != 65535:
                            it['max_endurance'] = 65535
                            it['is_destroy_when_broken'] = 0
                            dura_count += 1
                            dura_keys.append(it.get('key'))
                    log.info("ApplyToGame Infinity Durability: patched %d items", dura_count)
                    rpt.stage("inf_durability", f"set max_endurance=65535 on {dura_count} items")
                    rpt.expect('inf_dura', {'target': 65535, 'items': dura_keys})
                try:
                    final_data = self._rebuild_full_iteminfo()
                    log.info("Rebuilt iteminfo: %d bytes, pabgh %d entries",
                             len(final_data),
                             struct.unpack_from('<H', self._buff_rebuilt_pabgh, 0)[0]
                             if self._buff_rebuilt_pabgh else 0)
                except Exception as _ser_err:
                    log.warning("Rebuild failed (%s), trying direct serialize", _ser_err)
                    try:
                        final_data = bytearray(crimson_rs.serialize_iteminfo(
                            self._buff_rust_items))
                        unparsed = getattr(self, '_buff_unparsed_raw', []) or []
                        for _raw in unparsed:
                            final_data.extend(_raw)
                        from item_creator import build_iteminfo_pabgh
                        self._buff_rebuilt_pabgh = build_iteminfo_pabgh(
                            bytes(final_data))
                        log.info("Direct serialize OK: %d bytes", len(final_data))
                    except Exception as _ser2:
                        log.warning("Direct serialize also failed (%s), "
                                    "using byte buffer", _ser2)
                        final_data = bytearray(self._buff_data)
                        self._buff_rebuilt_pabgh = None
                rpt.stage("rust_serialize", f"{len(final_data)} bytes")
                if self._apply_vfx_changes(final_data):
                    rpt.stage("vfx_lab", f"applied (blob now {len(final_data)} bytes)")
                cd_patches = getattr(self, '_cd_patches', {})
                cd_expected: dict = {}
                if cd_patches:
                    cd_hit = 0
                    for item_key, (_, _, new_val) in cd_patches.items():
                        cd_off, _ = self._cd_detect(item_key, bytes(final_data))
                        if cd_off is not None:
                            final_data[cd_off:cd_off + 4] = struct.pack('<I', new_val)
                            cd_hit += 1
                            cd_expected[item_key] = (cd_off, new_val)
                    rpt.stage("cooldowns", f"{cd_hit}/{len(cd_patches)} offsets patched")
                    rpt.expect('cooldowns', cd_expected)
                tmog_applied = self._apply_transmog_swaps(final_data)
                if getattr(self, '_transmog_swaps', None):
                    tmog_expected: list[dict] = []
                    from armor_catalog import parse_transmog_items
                    fresh = parse_transmog_items(bytes(final_data))
                    fresh_by_key = {a.item_id: a for a in fresh}
                    for sw in self._transmog_swaps:
                        src_obj = sw.get('src')
                        src_hash = None
                        if hasattr(src_obj, 'hashes') and src_obj.hashes:
                            src_hash = src_obj.hashes[0][1]
                        tgt_key = sw['tgt'].item_id if hasattr(sw.get('tgt'), 'item_id') else sw.get('tgt_key')
                        fresh_tgt = fresh_by_key.get(tgt_key)
                        offsets = [o for o, _ in (fresh_tgt.hashes if fresh_tgt else [])]
                        tmog_expected.append({
                            'tgt_key': tgt_key,
                            'src_hash': src_hash,
                            'offsets': offsets[:1],
                        })
                    rpt.stage("transmog", f"{tmog_applied} byte patches for {len(self._transmog_swaps)} swap(s)")
                    rpt.expect('transmog', tmog_expected)
                try:
                    rpt.verify(bytes(final_data))
                except Exception as _ve:
                    log.warning("Verify step failed (non-fatal): %s", _ve)
                    rpt.stage("verify", f"SKIPPED: {_ve}")
                rpt.write()
                final_data = bytes(final_data)
            except Exception as e:
                log.warning("Rebuild failed, using byte buffer: %s", e)
                rpt.stage("rust_serialize", f"FAILED: {e}")
                rpt.write()
                final_data = bytes(self._buff_data)
        else:
            final_data = bytes(self._buff_data)
            rpt.stage("rust_serialize", "SKIPPED (no rust items) — using byte buffer")
            rpt.write()

        changes = []
        if self._buff_modified:
            changes.append("stat buffs")
        if apply_stacks:
            changes.append(f"max stacks")

        buff_dir = f"{self._buff_overlay_spin.value():04d}"
        staged = getattr(self, "_staged_skill_files", None) or {}
        staged_equip_info = getattr(self, "_staged_equip_files", None) or {}
        staged_charinfo = getattr(self, "_staged_charinfo_files", None) or {}
        staged_line = ""
        if staged:
            staged_line += f"Staged skill files: {', '.join(sorted(staged.keys()))}\n"
        if staged_equip_info:
            staged_line += f"Staged equip files: {', '.join(sorted(staged_equip_info.keys()))}\n"
        if staged_charinfo:
            staged_line += f"Staged charinfo files: {', '.join(sorted(staged_charinfo.keys()))}\n"

        # Detect existing overlay — the Extract step reads FROM this overlay
        # when our sentinel is present, so "replacement" here really means
        # "re-deploy the combined state (previous edits + this session's edits)".
        overlay_dir_path = os.path.join(game_path, buff_dir)
        overlay_sentinel = os.path.join(overlay_dir_path, ".se_itembuffs")
        if os.path.isfile(overlay_sentinel):
            merge_line = (
                f"An existing {buff_dir}/ overlay (from a prior session) was\n"
                f"used as the baseline on Extract, so your previous edits\n"
                f"(UP v2, Make Dyeable, prior buffs, etc.) are already folded\n"
                f"into the iteminfo being written. Safe to re-apply — nothing\n"
                f"gets lost.\n\n"
            )
        else:
            merge_line = ""

        reply = QMessageBox.question(
            self, "Apply to Game",
            f"Pack modified iteminfo.pabgb into {buff_dir}/ override directory?\n\n"
            f"Changes: {' + '.join(changes)}\n"
            f"Data: {len(final_data):,} bytes\n"
            f"{staged_line}"
            f"{merge_line}"
            f"Uses PackGroupBuilder with Compression.NONE (required so small\n"
            f"index files like skill.pabgh don't inflate under LZ4 and get\n"
            f"rejected by the game). PAPGT is updated with the pamt's\n"
            f"self-reported checksum.\n\n"
            f"Original 0008/0.paz is NOT modified.\n"
            f"To undo: click Restore Original.\n\n"
            f"The game must be restarted for changes to take effect.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        self._buff_status_label.setText("Packing overlay (uncompressed)...")
        QApplication.processEvents()

        try:
            import crimson_rs
            import shutil
            import tempfile

            INTERNAL_DIR = "gamedata/binary__/client/bin"

            # Save vanilla PAPGT backup BEFORE any modifications
            papgt_path = os.path.join(game_path, "meta", "0.papgt")
            papgt_vanilla = papgt_path + ".vanilla"
            if not os.path.isfile(papgt_vanilla) and os.path.isfile(papgt_path):
                shutil.copy2(papgt_path, papgt_vanilla)

            with tempfile.TemporaryDirectory() as tmp_dir:
                group_dir = os.path.join(tmp_dir, buff_dir)
                builder = crimson_rs.PackGroupBuilder(
                    group_dir,
                    crimson_rs.Compression.NONE,
                    crimson_rs.Crypto.NONE,
                )
                builder.add_file(INTERNAL_DIR, "iteminfo.pabgb", final_data)
                try:
                    _pabgh = getattr(self, '_buff_rebuilt_pabgh', None)
                    if not _pabgh:
                        _pabgh = bytes(crimson_rs.extract_file(
                            game_path, '0008', INTERNAL_DIR, 'iteminfo.pabgh'))
                    builder.add_file(INTERNAL_DIR, "iteminfo.pabgh", _pabgh)
                    log.info("Apply to Game: bundled pabgh (%d bytes, %d entries)",
                             len(_pabgh), struct.unpack_from('<H', _pabgh, 0)[0])
                except Exception as _e:
                    log.warning("Apply to Game: pabgh regen failed (%s) — rings/cloaks may lose sockets", _e)

                staged_skill = getattr(self, "_staged_skill_files", None) or {}
                for fname in ("skill.pabgb", "skill.pabgh"):
                    if fname in staged_skill:
                        builder.add_file(INTERNAL_DIR, fname, staged_skill[fname])
                        log.info("Bundling staged %s (%d bytes) into overlay",
                                 fname, len(staged_skill[fname]))

                # equipslotinfo is NOT bundled into this overlay -- it is
                # deployed separately to 0059/ by _buff_deploy_equipslotinfo_0059.
                # Bundling both in 0058/ empirically breaks UP v2 for muskets/blasters.

                staged_charinfo = getattr(self, "_staged_charinfo_files", None) or {}
                for fname in ("characterinfo.pabgb", "characterinfo.pabgh"):
                    if fname in staged_charinfo:
                        builder.add_file(INTERNAL_DIR, fname, staged_charinfo[fname])
                        log.info("Bundling staged %s (%d bytes) into overlay",
                                 fname, len(staged_charinfo[fname]))

                pamt_bytes = bytes(builder.finish())

                pamt_checksum = crimson_rs.parse_pamt_bytes(pamt_bytes)["checksum"]
                log.info("Built %s: pamt=%d bytes, pamt_checksum=0x%08X",
                         buff_dir, len(pamt_bytes), pamt_checksum)

                papgt_path = os.path.join(game_path, "meta", "0.papgt")

                # Read CURRENT papgt to preserve other overlay entries
                # (0059 equipslotinfo, 0039 field edits, etc.).
                # Only remove+re-add OUR group entry.
                papgt = crimson_rs.parse_papgt_file(papgt_path)
                papgt['entries'] = [
                    e for e in papgt['entries'] if e.get('group_name') != buff_dir
                ]
                papgt = crimson_rs.add_papgt_entry(
                    papgt, buff_dir, pamt_checksum, 0, 16383
                )

                game_mod = os.path.join(game_path, buff_dir)
                if os.path.isdir(game_mod):
                    shutil.rmtree(game_mod)
                os.makedirs(game_mod, exist_ok=True)

                shutil.copy2(
                    os.path.join(group_dir, "0.paz"),
                    os.path.join(game_mod, "0.paz"),
                )
                shutil.copy2(
                    os.path.join(group_dir, "0.pamt"),
                    os.path.join(game_mod, "0.pamt"),
                )
                crimson_rs.write_papgt_file(papgt, papgt_path)

                with open(os.path.join(game_mod, ".se_itembuffs"), "w") as mf:
                    mf.write("Created by CrimsonSaveEditor ItemBuffs tab\n")

            paz_size = os.path.getsize(os.path.join(game_mod, "0.paz"))
            staged_extra = ""
            if getattr(self, "_staged_skill_files", None):
                staged_extra += f"\nSkill filters: {', '.join(sorted(self._staged_skill_files.keys()))}"
            if getattr(self, "_staged_charinfo_files", None):
                staged_extra += f"\nKliff Gun Fix: characterinfo patched"
            msg = (
                f"Packed to {buff_dir}/ (uncompressed overlay, {paz_size:,} bytes)\n"
                f"PAPGT updated with pamt_checksum=0x{pamt_checksum:08X}\n"
                f"Original 0008/0.paz untouched{staged_extra}"
            )
            msg += stack_msg

            try:
                from shared_state import record_overlay
                files = ["iteminfo.pabgb", "iteminfo.pabgh"]
                if getattr(self, "_staged_skill_files", None):
                    files.extend(sorted(self._staged_skill_files.keys()))
                if getattr(self, "_staged_charinfo_files", None):
                    files.extend(sorted(self._staged_charinfo_files.keys()))
                if getattr(self, "_staged_equip_files", None):
                    files.extend(sorted(self._staged_equip_files.keys()))
                record_overlay(game_path, buff_dir, "ItemBuffs", files)
            except Exception:
                pass

            self._buff_status_label.setText(f"Success: packed to {buff_dir}/")
            QMessageBox.information(self, "Applied Successfully", msg)
            self.paz_refresh_requested.emit()

        except Exception as e:
            import traceback; traceback.print_exc()
            self._buff_status_label.setText(f"Failed: {e}")
            QMessageBox.critical(self, "Apply Failed", str(e))


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
                import shutil
                shutil.copy2(sebak, papgt_path)
                return f"PAPGT: fell back to .sebak restore ({e})"
            return f"PAPGT rebuild failed: {e}"


    def _buff_reset_vanilla_papgt(self) -> None:
        if not self._buff_ensure_patcher():
            return
        game_path = self._buff_patcher.game_path
        if not _can_write_game_dir(game_path):
            QMessageBox.warning(
                self, "No Write Access",
                f"Cannot write to:\n{game_path}\n\n"
                "Right-click → Run as administrator",
            )
            return

        import shutil
        papgt_path = os.path.join(game_path, "meta", "0.papgt")
        vanilla = papgt_path + ".vanilla"
        sebak = papgt_path + ".sebak"
        buff_dir = f"{self._buff_overlay_spin.value():04d}"
        game_mod = os.path.join(game_path, buff_dir)

        source = vanilla if os.path.isfile(vanilla) else (sebak if os.path.isfile(sebak) else None)
        if source is None and not os.path.isdir(game_mod):
            QMessageBox.warning(
                self, "No Backup",
                "Neither .papgt.vanilla nor .papgt.sebak exists, and no overlay "
                "directory to remove.\n"
                "Use Steam > Verify Integrity of Game Files instead.",
            )
            return

        parts: list[str] = []
        if os.path.isdir(game_mod):
            parts.append(f"Delete {buff_dir}/ overlay directory")
        if source is not None:
            parts.append(f"Restore meta/0.papgt from {os.path.basename(source)}")
        parts.append("Disables ALL overlay registrations — re-apply other mods afterward.")

        reply = QMessageBox.question(
            self, "Reset to Vanilla PAPGT",
            "\n".join(parts) + "\n\nProceed?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        messages: list[str] = []
        if os.path.isdir(game_mod):
            try:
                shutil.rmtree(game_mod)
                messages.append(f"Removed {buff_dir}/")
            except Exception as e:
                messages.append(f"Failed to remove {buff_dir}/: {e}")

        if source is not None:
            try:
                shutil.copy2(source, papgt_path)
                messages.append(f"Restored meta/0.papgt from {os.path.basename(source)}")
            except Exception as e:
                messages.append(f"PAPGT restore failed: {e}")

        # Prune any PAPGT entries pointing to missing directories
        try:
            import crimson_rs as _crs
            papgt = _crs.parse_papgt_file(papgt_path)
            before = len(papgt['entries'])
            papgt['entries'] = [
                e for e in papgt['entries']
                if os.path.isdir(os.path.join(game_path, e.get('group_name', '')))
            ]
            after = len(papgt['entries'])
            if before != after:
                _crs.write_papgt_file(papgt, papgt_path)
                messages.append(f"Pruned {before - after} dead PAPGT entries")
        except Exception as _pe:
            messages.append(f"PAPGT prune failed: {_pe}")

        self._buff_status_label.setText("Vanilla PAPGT restored — launch to verify.")
        QMessageBox.information(self, "Reset Done", "\n".join(messages))
        self.paz_refresh_requested.emit()


    def _buff_restore_original(self) -> None:
        if not self._buff_ensure_patcher():
            return
        game_path = self._buff_patcher.game_path
        if not _can_write_game_dir(game_path):
            QMessageBox.warning(
                self, "No Write Access",
                f"Cannot write to:\n{game_path}\n\n"
                "Right-click → Run as administrator",
            )
            return

        import shutil
        buff_dir = f"{self._buff_overlay_spin.value():04d}"
        game_mod = os.path.join(game_path, buff_dir)
        legacy_mod = os.path.join(game_path, "0038")
        papgt_path = os.path.join(game_path, "meta", "0.papgt")
        vanilla = papgt_path + ".vanilla"
        sebak = papgt_path + ".sebak"

        paz_backup = self._buff_patcher.paz_path + ".backup"
        has_inplace_backup = os.path.isfile(paz_backup)
        has_any = (os.path.isdir(game_mod) or os.path.isdir(legacy_mod)
                   or os.path.isdir(equip_group_dir) or has_inplace_backup
                   or bool(getattr(self, '_staged_charinfo_files', None)))
        if not has_any:
            QMessageBox.information(
                self, "Nothing to Restore",
                "No backups or mod directories found. ItemBuffs may not have been applied yet.",
            )
            return

        has_charinfo = bool(getattr(self, '_staged_charinfo_files', None))

        parts: list[str] = []
        if os.path.isdir(game_mod):
            parts.append(f"Delete {buff_dir}/ (overlay directory — includes iteminfo, "
                         "skill, and characterinfo if Kliff Gun Fix was applied)")
        if os.path.isdir(legacy_mod):
            parts.append("Delete 0038/ (legacy overlay)")
        if has_charinfo:
            parts.append("Clear staged Kliff Gun Fix (characterinfo)")
        if os.path.isfile(vanilla):
            parts.append("Restore meta/0.papgt from .vanilla backup (removes ALL overlay "
                         "registrations — re-apply any other mods afterward)")
        elif os.path.isfile(sebak):
            parts.append("Restore meta/0.papgt from .sebak backup")
        if has_inplace_backup:
            parts.append("Restore 0008/0.paz from .backup files (legacy in-place mode)")

        reply = QMessageBox.question(
            self, "Restore Original",
            "\n".join(parts) + "\n\nProceed?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        self._buff_status_label.setText("Restoring...")
        QApplication.processEvents()

        messages: list[str] = []

        # Also clean up UP v2's equipslotinfo group (0059) and legacy 0061
        equip_group_dir = os.path.join(game_path, "0059")
        equip_legacy_dir = os.path.join(game_path, "0061")
        for d in (game_mod, legacy_mod, equip_group_dir, equip_legacy_dir):
            if os.path.isdir(d):
                try:
                    group_name = os.path.basename(d)
                    shutil.rmtree(d)
                    messages.append(f"Removed {group_name}/")
                    try:
                        from overlay_coordinator import post_restore
                        post_restore(game_path, group_name)
                    except Exception:
                        pass
                except Exception as e:
                    messages.append(f"Failed to remove {os.path.basename(d)}/: {e}")

        # Surgically remove only OUR overlay entries from PAPGT instead of
        # restoring from a backup that may contain stale entries from other
        # tools. This preserves DMM, SkillTree, FieldEdit, and any other
        # mod overlays while cleaning up only what ItemBuffs deployed.
        removed_groups = set()
        for d in (game_mod, legacy_mod, equip_group_dir, equip_legacy_dir):
            removed_groups.add(os.path.basename(d))

        try:
            import crimson_rs as _crs
            papgt = _crs.parse_papgt_file(papgt_path)
            before = len(papgt['entries'])
            papgt['entries'] = [
                e for e in papgt['entries']
                if e.get('group_name') not in removed_groups
            ]
            # Also prune any other entries pointing to missing directories
            papgt['entries'] = [
                e for e in papgt['entries']
                if os.path.isdir(os.path.join(game_path, e.get('group_name', '')))
            ]
            after = len(papgt['entries'])
            _crs.write_papgt_file(papgt, papgt_path)
            messages.append(f"PAPGT: removed {before - after} entries "
                           f"({before} -> {after}), dead references cleaned")
        except Exception as e:
            messages.append(f"PAPGT cleanup failed: {e}\n"
                           "Use Steam → Verify Integrity of Game Files if the game won't start.")

        if has_inplace_backup:
            messages.append("WARNING: Legacy 0008/0.paz.backup found — this is from an old version.\n"
                           "Use Steam → Verify Integrity to fix 0008/ if needed.")

        self._buff_data = None
        self._buff_modified = False
        self._buff_items = []
        self._buff_current_item = None
        if hasattr(self, "_staged_skill_files"):
            self._staged_skill_files = {}
        if hasattr(self, "_staged_charinfo_files"):
            self._staged_charinfo_files = {}
        if hasattr(self, "_staged_equip_files"):
            self._staged_equip_files = {}
        if hasattr(self, "_buff_items_table"):
            self._buff_items_table.setRowCount(0)
        if hasattr(self, "_buff_stats_table"):
            self._buff_stats_table.setRowCount(0)

        removed_folders = []
        for d in (game_mod, legacy_mod, equip_group_dir, equip_legacy_dir):
            dn = os.path.basename(d)
            if not os.path.isdir(d) and f"Removed {dn}/" in "\n".join(messages):
                removed_folders.append(dn)

        affected = []
        if f"Removed {buff_dir}/" in "\n".join(messages):
            removed_folders.append(buff_dir)
            affected.append(f"  {buff_dir}/  —  ItemBuffs (iteminfo, stat buffs, QoL, "
                            "dye, sockets, abyss unlock, transmog)")
            if has_charinfo:
                affected.append(f"  {buff_dir}/  —  Kliff Gun Fix (characterinfo)")
        if "Removed 0059/" in "\n".join(messages):
            removed_folders.append("0059")
            affected.append("  0059/  —  Universal Proficiency (equipslotinfo)")
        if "Removed 0038/" in "\n".join(messages):
            affected.append("  0038/  —  Legacy ItemBuffs overlay")
        if "Removed 0061/" in "\n".join(messages):
            affected.append("  0061/  —  Legacy equipslotinfo overlay")

        if affected:
            messages.append("")
            messages.append("REMOVED OVERLAYS AND AFFECTED FEATURES:")
            messages.extend(affected)
            messages.append("")
            messages.append("If you use the FieldEdit tab separately, those overlays\n"
                            "are NOT affected by this restore.")

        self._buff_status_label.setText("Restored successfully — launch the game to verify.")
        QMessageBox.information(self, "Restored", "\n".join(messages))
        self.paz_refresh_requested.emit()


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


    def _buff_items_context_menu(self, pos) -> None:
        item_widget = self._buff_items_table.itemAt(pos)
        if not item_widget:
            return
        row = item_widget.row()
        name_cell = self._buff_items_table.item(row, 1)
        if not name_cell:
            return
        item = name_cell.data(Qt.UserRole)
        if not item:
            return

        from PySide6.QtWidgets import QMenu
        menu = QMenu(self)

        if getattr(self, '_showing_favorites', False):
            fav_action = menu.addAction("Remove from Favorites ⭐")
            call_fav = lambda: self._remove_from_favorites(item)
        else:
            fav_action = menu.addAction("Add to Favorites ⭐")
            call_fav = lambda: self._add_to_favorites(item)

        add_action = menu.addAction("Add to Equipment Set...")

        # "Find similar" submenu — shows category/equip_type/item_type peers.
        similar_menu = None
        sim_cat_action = sim_equip_action = sim_type_action = None
        sim_passive_action = sim_buff_action = None
        diff_action = dump_action = None
        rust_info = self._buff_rust_lookup.get(item.item_key) if self._buff_rust_lookup else None
        if rust_info and self._index is not None:
            menu.addSeparator()
            similar_menu = menu.addMenu("Find similar items")
            cat_label = self._index.category_label(rust_info.get("category_info") or 0)
            sim_cat_action = similar_menu.addAction(f"In same category ({cat_label})")
            eti = rust_info.get("equip_type_info") or 0
            if eti:
                sim_equip_action = similar_menu.addAction(
                    f"With same equip_type_info (0x{eti:08X})")
            sim_type_action = similar_menu.addAction(
                f"With same item_type ({rust_info.get('item_type', '?')})")
            sim_passive_action = similar_menu.addAction(
                "With same passives")
            sim_buff_action = similar_menu.addAction(
                "With same buffs")
            menu.addSeparator()
            inspect_action = menu.addAction("Inspect (full field tree)...")
            diff_action = menu.addAction("Diff against another item...")
            dump_action = menu.addAction("Dump item info to disk...")

        action = menu.exec(self._buff_items_table.viewport().mapToGlobal(pos))
        if action == fav_action:
            call_fav()
        elif action == add_action:
            self._set_add_item(item)
        elif rust_info and self._index is not None:
            if action == sim_cat_action:
                self._show_similar_items(rust_info, mode="category")
            elif action == sim_equip_action:
                self._show_similar_items(rust_info, mode="equip_type")
            elif action == sim_type_action:
                self._show_similar_items(rust_info, mode="item_type")
            elif action == sim_passive_action:
                self._show_similar_items(rust_info, mode="passives")
            elif action == sim_buff_action:
                self._show_similar_items(rust_info, mode="buffs")
            elif action == inspect_action:
                self._buff_open_item_inspector(item_key=rust_info["key"])
            elif action == diff_action:
                self._buff_open_item_diff_dialog(initial_a=rust_info["key"])
            elif action == dump_action:
                self._dump_item_info(rust_info)

    def _add_to_favorites(self, item) -> None:
        self._buff_status_label.setText(f"{item.name}({item.item_key}) added to favorites.")
        QMessageBox.information(self, "New Favorite :",
            f"{item.name}({item.item_key}) added to favorites.")
        self._favorite_items.append({"key": item.item_key})
        self.config_save_requested.emit()

    def _remove_from_favorites(self, item) -> None:
        QMessageBox.information(self, "Removing Favorite :",
            f"{item.name}({item.item_key}) removed from favorites.")
        self._favorite_items = [
            i for i in self._favorite_items if i['key'] != item.item_key]
        self._config["favorite_items"] = self._favorite_items
        self.config_save_requested.emit()
        if getattr(self, '_show_favorite_items', None):
            self._show_favorite_items() \
                if len(self._favorite_items) > 0 \
                else self._render_items_into_table([], "No Favorites Found.")

    def _show_similar_items(self, rust_info: dict, mode: str) -> None:
        """Reload the items table with everything similar to `rust_info`."""
        if self._index is None:
            return

        if mode == "favorites":
            peers = self._favorite_items
        else:
            peers = self._index.find_similar(rust_info, mode=mode)
        if not peers:
            QMessageBox.information(
                self, "No Similar Items",
                f"No other items found with the same {mode}.")
            return
        # Map peers back to SaveItem entries from _buff_items so the existing
        # table render path stays untouched.
        peer_keys = {p["key"] for p in peers}
        results = [it for it in self._buff_items if it.item_key in peer_keys]
        if not results:
            QMessageBox.information(
                self, "No Similar Items",
                "Found peers in iteminfo but none are in the current scan results.")
            return
        # Re-render via the existing table-builder by reusing _buff_search_items
        # would require a search query, so we render directly here using a tiny
        # helper that mirrors the same column layout.
        self._render_items_into_table(results,
            f"{len(results)} item(s) similar to "
            f"{rust_info.get('string_key') or rust_info.get('key')} "
            f"(by {mode}).")

    def _dump_item_info(self, rust_info: dict) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Dump Item", f"{rust_info['key']}.json",
            "Item Dumps (*.json)")
        if not path:
            return
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(rust_info, f, indent=2)
        self._buff_status_label.setText(
            f"Dumped {rust_info.get('string_key', '')}({rust_info['key']}) to {path}."
        )

    def _import_item_info(self) -> None:
        if not hasattr(self, '_buff_rust_items') or not self._buff_rust_items:
            QMessageBox.warning(self, "Import ITEMINFO",
                "Extract with Rust parser first (click 'Extract').\n"
                "The import will be applied on top of fresh game data.")
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Import ITEMINFO",
            os.path.dirname(os.path.abspath(__file__)),
            "JSON Files (*.json)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                item_info = json.load(f)
            key = item_info.get("key")
            if not key:
                raise KeyError("No 'key' field found in JSON.")
        except KeyError as e:
            QMessageBox.critical(self, "Import", f"Invalid format:\n{e}")
            return
        except Exception as e:
            QMessageBox.critical(self, "Import", f"Failed to read file:\n{e}")
            return
        if self._index is not None:
            warnings = self._index.validate_edit(item_info)
            if warnings:
                msg = "Heads up — these look risky:\n\n  " + "\n  ".join(
                    f"• {w}" for w in warnings) + "\n\nApply anyway?"
                reply = QMessageBox.question(
                    self, "Validation Warnings", msg,
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
                if reply != QMessageBox.Yes:
                    return
        self._safely_replace_buff_item(key, item_info)
        try:
            import crimson_rs
            new_data = crimson_rs.serialize_iteminfo(self._buff_rust_items)
            self._buff_data = bytearray(new_data)
            self._buff_rust_items = crimson_rs.parse_iteminfo_from_bytes(new_data)
            self._buff_rust_lookup = {it['key']: it for it in self._buff_rust_items}
            self._rebuild_index()
            self._buff_items = self._buff_patcher.find_items(bytes(self._buff_data))
            log.info("Import ITEMINFO: synced byte buffer (%d bytes)", len(new_data))
        except Exception as e:
            log.warning("Import ITEMINFO: byte buffer sync failed: %s", e)
        self._buff_modified = True
        self._buff_refresh_stats()
        QMessageBox.information(self, "Raw ITEMINFO Imported",
            f"Imported: {key} from {path}\n\n"
            f"The imported values are now baked into your iteminfo data.")

    def _render_items_into_table(self, results: list, status_text: str) -> None:
        """Mini renderer used by 'Find similar' — keeps the same columns/icons
        as `_buff_search_items` without needing a search query."""
        table = self._buff_items_table
        table.setSortingEnabled(False)
        table.setRowCount(len(results))
        for row, item in enumerate(results):
            icon_cell = QTableWidgetItem()
            if self._buff_icons_enabled:
                px = self._icon_cache.get_pixmap(item.item_key)
                if px:
                    icon_cell.setIcon(QIcon(px))
            table.setItem(row, 0, icon_cell)
            display_name = self._name_db.get_name(item.item_key)
            if display_name.startswith("Unknown"):
                display_name = item.name
            name_cell = QTableWidgetItem(display_name)
            name_cell.setData(Qt.UserRole, item)
            rust_info = self._buff_rust_lookup.get(item.item_key)
            if rust_info:
                tip = (f"Internal: {item.name}\nKey: {item.item_key}\n"
                       f"Category: {rust_info.get('category_info', '?')}\n"
                       f"Equip type: {rust_info.get('equip_type_info', '?')}\n"
                       f"Item type: {rust_info.get('item_type', '?')}\n"
                       f"Tier: {rust_info.get('item_tier', '?')}")
                name_cell.setToolTip(tip)
            table.setItem(row, 1, name_cell)
            type_cell = QTableWidgetItem("")
            table.setItem(row, 2, type_cell)
            tier = (rust_info or {}).get('item_tier', 0) if rust_info else 0
            tier_names = {0: "-", 1: "Common", 2: "Uncommon", 3: "Rare", 4: "Epic", 5: "Legendary"}
            table.setItem(row, 3, QTableWidgetItem(tier_names.get(tier, str(tier))))
            edl_count = len(((rust_info or {}).get('enchant_data_list') or [])) if rust_info else 0
            table.setItem(row, 4, QTableWidgetItem(f"+{edl_count - 1}" if edl_count > 1 else "-"))
            table.setItem(row, 5, QTableWidgetItem("\u2014"))
        table.setSortingEnabled(True)
        self._buff_status_label.setText(status_text)
        if len(results) == 1:
            table.selectRow(0)
            self._buff_item_selected()


    def _buff_build_operations(self, item) -> List[StatOperation]:
        from paz_patcher import _stat_size_class, BUFF_NAMES, BUFF_HASHES

        if self._buff_data is None:
            return []

        arrays = ItemBuffPatcher.find_stat_arrays(bytes(self._buff_data), item)
        if not arrays:
            return []

        all_entries = []
        for arr in arrays:
            all_entries.extend(arr.entries)

        preset_idx = self._buff_preset_combo.currentIndex()
        ops = []

        if preset_idx == 0:
            for entry in all_entries:
                val = 15 if entry.size_class == "rate" else 999_999
                ops.append(StatOperation(
                    stat_name=entry.name, stat_hash=entry.hash_val,
                    size_class=entry.size_class, operation="set_value", value=val,
                ))
        elif preset_idx in (1, 2, 3, 4, 5):
            target_classes = {
                1: ("flat2", "flat1"), 2: ("flat2",), 3: ("flat2",),
                4: ("flat1",), 5: ("rate",),
            }[preset_idx]
            for entry in all_entries:
                if entry.size_class in target_classes:
                    val = 15 if entry.size_class == "rate" else 999_999
                    ops.append(StatOperation(
                        stat_name=entry.name, stat_hash=entry.hash_val,
                        size_class=entry.size_class, operation="set_value", value=val,
                    ))
        elif preset_idx in (6, 7):
            target = BUFF_HASHES["Damage Dealt (DDD)"] if preset_idx == 6 else BUFF_HASHES["Defense (DPV)"]
            target_name = "DDD (Damage)" if preset_idx == 6 else "DPV (Defense)"
            for entry in all_entries:
                if entry.size_class == "flat2" and entry.hash_val != target:
                    ops.append(StatOperation(
                        stat_name=entry.name, stat_hash=entry.hash_val,
                        size_class="flat2", operation="swap_hash",
                        value=entry.value, target_hash=target,
                    ))
        else:
            buff_name = self._buff_type_combo.currentText()
            buff_hash = BUFF_HASHES.get(buff_name)
            if buff_hash is None:
                return []
            value = self._buff_value_spin.value()
            target_class = _stat_size_class(buff_hash)
            for entry in all_entries:
                if entry.size_class == target_class:
                    if entry.hash_val == buff_hash:
                        ops.append(StatOperation(
                            stat_name=entry.name, stat_hash=entry.hash_val,
                            size_class=target_class, operation="set_value", value=value,
                        ))
                    else:
                        ops.append(StatOperation(
                            stat_name=entry.name, stat_hash=entry.hash_val,
                            size_class=target_class, operation="swap_hash",
                            value=value, target_hash=buff_hash,
                        ))
        return ops


    def _set_add_item(self, item) -> None:
        ops = self._buff_build_operations(item)
        if not ops:
            QMessageBox.information(self, "No Operations",
                                    "Select a preset first, then right-click the item.")
            return

        display_name = self._name_db.get_name(item.item_key)
        if display_name.startswith("Unknown"):
            display_name = item.name

        sets = self._set_mgr.scan_local()
        choices = [es.name for es in sets] + ["-- Create New Set --"]

        from PySide6.QtWidgets import QInputDialog
        choice, ok = QInputDialog.getItem(
            self, "Add to Equipment Set",
            f"Add '{display_name}' with {len(ops)} operations to:",
            choices, 0, False,
        )
        if not ok:
            return

        if choice == "-- Create New Set --":
            name, ok2 = QInputDialog.getText(self, "New Set", "Set name:")
            if not ok2 or not name.strip():
                return
            author, ok3 = QInputDialog.getText(self, "New Set", "Author:")
            if not ok3:
                author = ""
            desc, ok4 = QInputDialog.getText(self, "New Set", "Description:")
            if not ok4:
                desc = ""
            import datetime
            es = EquipmentSet(
                name=name.strip(), author=author.strip(),
                description=desc.strip(),
                created=datetime.date.today().isoformat(),
            )
        else:
            es = next((s for s in sets if s.name == choice), None)
            if not es:
                return

        set_item = SetItem(
            item_key=item.item_key,
            item_name=display_name,
            operations=ops,
        )

        es.items = [si for si in es.items if si.item_key != item.item_key]
        es.items.append(set_item)

        self._set_mgr.save_set(es, es.filename if es.filename else "")
        self._set_refresh_local()
        self._set_status.setText(f"Added {display_name} to '{es.name}' ({len(ops)} ops)")


    def _set_apply(self) -> None:
        es = self._set_get_selected()
        if not es:
            QMessageBox.information(self, "No Set", "Select an equipment set first.")
            return

        if self._buff_data is None:
            QMessageBox.warning(self, "No Data", "Extract iteminfo first (use Extract button above).")
            return

        if not self._buff_items:
            QMessageBox.warning(self, "No Items", "Extract and search for items first.")
            return

        item_by_key = {}
        for it in self._buff_items:
            item_by_key[it.item_key] = it

        lines = []
        for si in es.items:
            found = "YES" if si.item_key in item_by_key else "NO"
            lines.append(f"  {si.item_name} (key={si.item_key}): {len(si.operations)} ops [{found}]")

        reply = QMessageBox.question(
            self, f"Apply Set: {es.name}",
            f"Apply '{es.name}' by {es.author}?\n\n"
            f"{len(es.items)} items:\n" + "\n".join(lines) + "\n\n"
            f"Changes held in memory until 'Export JSON Patch'.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        applied = 0
        skipped = 0
        total_ops = 0

        for si in es.items:
            item_rec = item_by_key.get(si.item_key)
            if not item_rec:
                skipped += 1
                continue

            arrays = ItemBuffPatcher.find_stat_arrays(bytes(self._buff_data), item_rec)
            all_entries = []
            for arr in arrays:
                all_entries.extend(arr.entries)

            if not all_entries:
                skipped += 1
                continue

            entries_by_class = {}
            for e in all_entries:
                entries_by_class.setdefault(e.size_class, []).append(e)

            ops_by_class = {}
            for op in si.operations:
                ops_by_class.setdefault(op.size_class, []).append(op)

            for cls, ops in ops_by_class.items():
                entries = entries_by_class.get(cls, [])
                for i, op in enumerate(ops):
                    if i >= len(entries):
                        break
                    entry = entries[i]
                    if op.operation == "set_value":
                        ItemBuffPatcher.overwrite_stat_value(self._buff_data, entry, op.value)
                        total_ops += 1
                    elif op.operation == "swap_hash":
                        if ItemBuffPatcher.swap_stat_hash(self._buff_data, entry, op.target_hash):
                            ItemBuffPatcher.overwrite_stat_value(self._buff_data, entry, op.value)
                            total_ops += 1

            applied += 1

        self._buff_modified = True
        if self._buff_current_item:
            self._buff_refresh_stats()

        msg = f"Applied '{es.name}': {applied}/{len(es.items)} items, {total_ops} operations"
        if skipped:
            msg += f" ({skipped} items not found in iteminfo)"
        self._set_status.setText(msg)
        self._buff_status_label.setText(msg + ". Click 'Export JSON Patch' to write.")


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
        
