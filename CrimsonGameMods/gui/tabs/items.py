from __future__ import annotations

import json as _json
import logging
import os
import struct
import threading

from data_db import get_connection

from typing import Callable, Dict, List, Optional, Tuple

from PySide6.QtCore import Qt, QSize, Signal
from PySide6.QtGui import QBrush, QColor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QColorDialog, QComboBox,
    QDialog, QDialogButtonBox, QFileDialog, QFrame, QGroupBox,
    QHBoxLayout, QHeaderView, QInputDialog, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMenu, QMessageBox, QPushButton,
    QSizePolicy, QSpinBox, QSplitter, QTableWidget, QTableWidgetItem, QTextEdit,
    QVBoxLayout, QWidget,
)

from models import SaveData, SaveItem, UndoEntry
try:
    from parc_inserter3 import insert_item_to_inventory, insert_item_to_store, clone_block_section, insert_items_batch
except Exception:
    insert_item_to_inventory = insert_item_to_store = clone_block_section = insert_items_batch = None
from gui.dialogs import ItemSearchDialog
from item_db import ItemNameDB
from localization import tr
from icon_cache import ICON_SIZE
from gui.theme import COLORS, CATEGORY_COLORS
from gui.utils import make_scope_label, make_help_btn, _num_item

log = logging.getLogger(__name__)


class DatabaseBrowserTab(QWidget):

    dirty = Signal()
    status_message = Signal(str)
    toggle_icons_requested = Signal()
    items_changed = Signal()

    def __init__(
        self,
        name_db: ItemNameDB,
        icon_cache,
        get_items_fn: Callable[[], List[SaveItem]],
        icons_enabled: bool = False,
        goto_knowledge_fn: Optional[Callable] = None,
        goto_quest_fn: Optional[Callable] = None,
        app_dir_fn: Optional[Callable[[], str]] = None,
        show_guide_fn: Optional[Callable] = None,
        config: Optional[dict] = None,
        parent=None,
    ):
        super().__init__(parent)
        self._name_db = name_db
        self._icon_cache = icon_cache
        self._get_items_fn = get_items_fn
        self._icons_enabled = icons_enabled
        self._goto_knowledge_fn = goto_knowledge_fn or (lambda: None)
        self._goto_quest_fn = goto_quest_fn or (lambda: None)
        self._show_goto_knowledge = goto_knowledge_fn is not None
        self._show_goto_quest = goto_quest_fn is not None
        self._app_dir_fn = app_dir_fn or (lambda: ".")
        self._show_guide_fn = show_guide_fn or (lambda k: None)
        self._config = config or {}
        self._build_ui()

    def update_icons(self, enabled: bool) -> None:
        self._icons_enabled = enabled
        self._db_show_icons_btn.setText("Hide Icons" if enabled else "Show Icons")
        self._db_table.setColumnWidth(0, (ICON_SIZE + 16) if enabled else 0)
        self._filter_database()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        layout.addWidget(make_scope_label("readonly"))

        db_info = QLabel(tr("Item ID lookup database — search for item keys by name or category. Read-only."))
        db_info.setStyleSheet(f"color: {COLORS['text_dim']}; padding: 4px;")
        layout.addWidget(db_info)

        top = QHBoxLayout()

        top.addWidget(QLabel(tr("Search:")))
        self._db_search = QLineEdit()
        self._db_search.setPlaceholderText(tr("Search by name or key..."))
        self._db_search.textChanged.connect(self._filter_database)
        top.addWidget(self._db_search, 2)

        top.addWidget(QLabel(tr("Category:")))
        self._db_category = QComboBox()
        self._db_category.addItem("All")
        self._db_category.currentTextChanged.connect(self._filter_database)
        top.addWidget(self._db_category)

        # sync_btn = QPushButton(tr("Sync from GitHub"))
        # sync_btn.clicked.connect(self._sync_github)
        # top.addWidget(sync_btn)

        sync_local_btn = QPushButton("Sync Items Local")
        sync_local_btn.setToolTip(
            "Extract item database directly from your game installation.\n"
            "Reads iteminfo + English localization from game PAZ files.\n"
            "Requires game path to be set.")
        sync_local_btn.clicked.connect(self._sync_items_local)
        top.addWidget(sync_local_btn)

        self._db_freshness_badge = QLabel("")
        self._db_freshness_badge.setStyleSheet(
            f"color: white; background: {COLORS['accent']}; "
            f"border-radius: 4px; padding: 2px 8px; font-weight: bold; font-size: 11px;")
        self._db_freshness_badge.setVisible(False)
        top.addWidget(self._db_freshness_badge)

        self._db_show_icons_btn = QPushButton("Hide Icons" if self._icons_enabled else "Show Icons")
        self._db_show_icons_btn.setToolTip(tr("Download and display all item icons from GitHub.\nFirst time requires internet. Icons cached locally."))
        self._db_show_icons_btn.clicked.connect(self.toggle_icons_requested)
        top.addWidget(self._db_show_icons_btn)

        if getattr(self, "_show_goto_knowledge", False):
            goto_knowledge_btn = QPushButton(tr("Knowledge Tab"))
            goto_knowledge_btn.setToolTip(tr("Jump to Knowledge tab"))
            goto_knowledge_btn.clicked.connect(self._goto_knowledge_fn)
            top.addWidget(goto_knowledge_btn)

        if getattr(self, "_show_goto_quest", False):
            goto_quest_btn = QPushButton(tr("Quest Editor"))
            goto_quest_btn.setToolTip(tr("Jump to Quest Editor tab"))
            goto_quest_btn.clicked.connect(self._goto_quest_fn)
            top.addWidget(goto_quest_btn)

        top.addWidget(make_help_btn("database", self._show_guide_fn))

        layout.addLayout(top)

        self._db_table = QTableWidget()
        self._db_table.setColumnCount(6)
        self._db_table.setHorizontalHeaderLabels([
            "", "ItemKey", "Name", "Internal Name", "Category", "Max Stack"
        ])
        self._db_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._db_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Interactive)
        self._db_table.horizontalHeader().setMinimumSectionSize(0)
        self._db_table.setColumnWidth(0, (ICON_SIZE + 16) if self._icons_enabled else 0)
        self._db_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Interactive)
        self._db_table.setColumnWidth(2, 200)
        self._db_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._db_table.setSortingEnabled(True)
        self._db_table.verticalHeader().setDefaultSectionSize(22)
        self._db_table.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
        self._db_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self._db_table.customContextMenuRequested.connect(self._db_context_menu)
        layout.addWidget(self._db_table, 1)

        rename_row = QHBoxLayout()
        rename_row.addWidget(QLabel(tr("Rename selected:")))
        self._db_rename_input = QLineEdit()
        self._db_rename_input.setPlaceholderText(tr("New name..."))
        rename_row.addWidget(self._db_rename_input, 1)
        rename_btn = QPushButton(tr("Rename"))
        rename_btn.clicked.connect(self._rename_item)
        rename_row.addWidget(rename_btn)

        copy_key_btn = QPushButton(tr("Copy Key"))
        copy_key_btn.setToolTip(tr("Copy selected item's key to clipboard"))
        copy_key_btn.clicked.connect(self._copy_item_key)
        rename_row.addWidget(copy_key_btn)

        layout.addLayout(rename_row)

        self._db_info_label = QLabel("")
        layout.addWidget(self._db_info_label)

        self._db_tab_widget = self

        self._populate_database()


    def _populate_database(self) -> None:
        categories = set()
        for info in self._name_db.items.values():
            categories.add(info.category)
        current_cat = self._db_category.currentText()
        self._db_category.blockSignals(True)
        self._db_category.clear()
        self._db_category.addItem("All")
        for cat in sorted(categories):
            self._db_category.addItem(cat)
        idx = self._db_category.findText(current_cat)
        if idx >= 0:
            self._db_category.setCurrentIndex(idx)
        self._db_category.blockSignals(False)

        self._filter_database()


    def _filter_database(self) -> None:
        table = self._db_table
        table.setSortingEnabled(False)
        table.setRowCount(0)

        search = self._db_search.text().strip()
        cat_filter = self._db_category.currentText()

        if search:
            items = self._name_db.search(search)
        else:
            items = self._name_db.get_all_sorted()

        if cat_filter != "All":
            items = [i for i in items if i.category == cat_filter]

        table.setRowCount(len(items))
        for row, info in enumerate(items):
            color = QColor(CATEGORY_COLORS.get(info.category, COLORS["text"]))

            icon_item = QTableWidgetItem()
            if self._icons_enabled:
                px = self._icon_cache.get_pixmap(info.item_key)
                if px:
                    icon_item.setIcon(QIcon(px))
                elif self._icon_cache.has_icon(info.item_key):
                    self._icon_cache.request_icon(info.item_key, lambda k, px: None)
            table.setItem(row, 0, icon_item)

            key_item = QTableWidgetItem(str(info.item_key))
            key_item.setData(Qt.UserRole, info.item_key)
            table.setItem(row, 1, key_item)

            name_item = QTableWidgetItem(info.name)
            name_item.setForeground(QBrush(color))
            table.setItem(row, 2, name_item)

            table.setItem(row, 3, QTableWidgetItem(info.internal_name))

            cat_item = QTableWidgetItem(info.category)
            cat_item.setForeground(QBrush(color))
            table.setItem(row, 4, cat_item)

            table.setItem(row, 5, QTableWidgetItem(str(info.max_stack)))

        table.setSortingEnabled(True)
        self._db_info_label.setText(
            f"Showing {len(items)} of {len(self._name_db.items)} items  |  "
            f"DB version: {self._name_db.version}  |  "
            f"Path: {self._name_db.loaded_path or '(none)'}"
        )


    def _rename_item(self) -> None:
        rows = set(idx.row() for idx in self._db_table.selectedIndexes())
        if not rows:
            QMessageBox.information(self, tr("Rename"), tr("Select an item first."))
            return

        new_name = self._db_rename_input.text().strip()
        if not new_name:
            QMessageBox.information(self, tr("Rename"), tr("Enter a new name."))
            return

        row = min(rows)
        key_widget = self._db_table.item(row, 1)
        if not key_widget:
            return

        key = key_widget.data(Qt.UserRole)
        old_name = self._name_db.get_name(key)
        self._name_db.rename_item(key, new_name)
        self._name_db.save()

        for item in self._get_items_fn():
            if item.item_key == key:
                item.name = new_name

        self._filter_database()
        self.items_changed.emit()
        self.status_message.emit(f"Renamed item {key}: '{old_name}' -> '{new_name}'")
        self._db_rename_input.clear()


    def _copy_item_key(self) -> None:
        rows = set(idx.row() for idx in self._db_table.selectedIndexes())
        if not rows:
            QMessageBox.information(self, tr("Copy Key"), tr("Select an item first."))
            return
        row = min(rows)
        key_w = self._db_table.item(row, 1)
        if key_w:
            QApplication.clipboard().setText(key_w.text())
            name_w = self._db_table.item(row, 1)
            name = name_w.text() if name_w else key_w.text()
            self.status_message.emit(f"Copied key {key_w.text()} ({name}) to clipboard")


    def _db_context_menu(self, pos):
        rows = sorted(set(idx.row() for idx in self._db_table.selectedIndexes()))
        if not rows:
            return

        from PySide6.QtWidgets import QMenu
        menu = QMenu(self)

        copy_act = menu.addAction(f"Copy Key(s) ({len(rows)} selected)")
        export_pack_act = menu.addAction(f"Export as DropSet Pack ({len(rows)} items)")

        action = menu.exec(self._db_table.viewport().mapToGlobal(pos))

        if action == copy_act:
            keys = []
            for r in rows:
                key_w = self._db_table.item(r, 1)
                if key_w:
                    keys.append(key_w.text())
            if keys:
                QApplication.clipboard().setText(", ".join(keys))
                self.status_message.emit(f"Copied {len(keys)} key(s) to clipboard")

        elif action == export_pack_act:
            self._db_export_dropset_pack(rows)


    def _db_export_dropset_pack(self, rows):
        import sys as _sys
        from PySide6.QtWidgets import QInputDialog, QFileDialog

        name, ok = QInputDialog.getText(
            self, "DropSet Pack Name", "Name for this drop pack:",
            text="My Drop Pack")
        if not ok or not name.strip():
            return

        items = []
        for r in rows:
            key_w = self._db_table.item(r, 1)
            name_w = self._db_table.item(r, 2)
            cat_w = self._db_table.item(r, 4)
            if not key_w:
                continue
            try:
                item_key = int(key_w.text())
            except ValueError:
                continue
            item_name = name_w.text() if name_w else f"Item_{item_key}"
            items.append({
                "item_key": item_key,
                "name": item_name,
                "rate_pct": 100,
                "qty": 1,
            })

        if not items:
            return

        packs_dir = os.path.join(os.path.dirname(os.path.abspath(_sys.argv[0])), "dropset_packs")
        os.makedirs(packs_dir, exist_ok=True)
        default_fn = name.strip().replace(" ", "_") + ".json"

        path, _ = QFileDialog.getSaveFileName(
            self, "Save DropSet Pack",
            os.path.join(packs_dir, default_fn),
            "JSON Files (*.json)")
        if not path:
            return

        pack = {
            "type": "dropset_pack",
            "version": 1,
            "name": name.strip(),
            "author": "CrimsonSaveEditor",
            "description": f"{len(items)} items selected from Item Database",
            "items": items,
        }

        with open(path, "w", encoding="utf-8") as f:
            _json.dump(pack, f, indent=2)

        self.status_message.emit(f"Exported {len(items)} items as DropSet pack: {os.path.basename(path)}")
        QMessageBox.information(self, tr("Pack Exported"),
            f"Saved {len(items)} items to:\n{path}\n\n"
            f"This pack will appear in the DropSets tab Pack dropdown.")


    def _sync_github(self) -> None:
        self.status_message.emit("Syncing from GitHub...")
        QApplication.processEvents()

        ok, msg = self._name_db.sync_from_github()
        if ok:
            self._populate_database()
            for item in self._get_items_fn():
                item.name = self._name_db.get_name(item.item_key)
                item.category = self._name_db.get_category(item.item_key)
            self.items_changed.emit()
        self.status_message.emit(msg)
        QMessageBox.information(self, tr("GitHub Sync"), msg)

    def _sync_items_local(self) -> None:
        game_path = self._config.get("game_install_path", "")
        if not game_path or not os.path.isdir(game_path):
            QMessageBox.warning(
                self, "Sync Items Local",
                "Game path not set.\nSet your game installation path first.")
            return

        self.status_message.emit("Extracting items from game client...")
        QApplication.processEvents()

        ok, msg = self._name_db.sync_from_local_game(game_path)
        if ok:
            self._populate_database()
            for item in self._get_items_fn():
                item.name = self._name_db.get_name(item.item_key)
                item.category = self._name_db.get_category(item.item_key)
            self.items_changed.emit()
        self.status_message.emit(msg.split('\n')[0] if msg else "")
        self._check_item_db_freshness()
        QMessageBox.information(self, "Sync Items Local", msg)

    def _check_item_db_freshness(self) -> None:
        if not hasattr(self, '_db_freshness_badge'):
            return
        game_path = self._config.get("game_install_path", "")
        if not game_path or not os.path.isdir(game_path):
            self._db_freshness_badge.setVisible(False)
            return
        try:
            import crimson_rs
            dp = "gamedata/binary__/client/bin"
            pabgh = crimson_rs.extract_file(game_path, "0008", dp, "iteminfo.pabgh")
            game_count = struct.unpack_from('<H', pabgh, 0)[0]
            local_count = len(self._name_db.items)
            diff = game_count - local_count
            if diff > 0:
                self._db_freshness_badge.setText(f"{diff} new items in game")
                self._db_freshness_badge.setStyleSheet(
                    f"color: white; background: {COLORS['accent']}; "
                    f"border-radius: 4px; padding: 2px 8px; font-weight: bold; font-size: 11px;")
                self._db_freshness_badge.setToolTip(
                    f"Game has {game_count} items, your database has {local_count}.\n"
                    f"Click 'Sync Items Local' to update.")
                self._db_freshness_badge.setVisible(True)
            elif diff < 0:
                self._db_freshness_badge.setText(f"DB has {-diff} extra items")
                self._db_freshness_badge.setStyleSheet(
                    "color: white; background: #FF8800; "
                    "border-radius: 4px; padding: 2px 8px; font-weight: bold; font-size: 11px;")
                self._db_freshness_badge.setToolTip(
                    f"Game has {game_count} items, your database has {local_count}.\n"
                    f"Database may include items from mods or a newer game version.")
                self._db_freshness_badge.setVisible(True)
            else:
                self._db_freshness_badge.setText("Up to date")
                self._db_freshness_badge.setStyleSheet(
                    "color: white; background: #4CAF50; "
                    "border-radius: 4px; padding: 2px 8px; font-weight: bold; font-size: 11px;")
                self._db_freshness_badge.setToolTip(
                    f"Item database matches game ({game_count} items).")
                self._db_freshness_badge.setVisible(True)
        except Exception:
            self._db_freshness_badge.setVisible(False)

    def _sync_all_icons(self) -> None:
        # OFFLINE BUILD: icon downloads removed — ship the icons_local folder instead.
        QMessageBox.information(self, tr("Sync Icons"),
            tr("Online icon download was removed. Copy the icons_local folder next to the exe instead."))


class RepurchaseTab(QWidget):

    dirty = Signal()
    status_message = Signal(str)
    undo_entry_added = Signal(object)
    items_changed = Signal()

    def __init__(
        self,
        name_db: ItemNameDB,
        icon_cache,
        icons_enabled: bool = False,
        experimental_mode: bool = False,
        app_dir_fn: Optional[Callable[[], str]] = None,
        show_guide_fn: Optional[Callable] = None,
        parent=None,
    ):
        super().__init__(parent)
        self._name_db = name_db
        self._icon_cache = icon_cache
        self._icons_enabled = icons_enabled
        self._experimental_mode = experimental_mode
        self._app_dir_fn = app_dir_fn or (lambda: ".")
        self._show_guide_fn = show_guide_fn or (lambda k: None)
        self._save_data: Optional[SaveData] = None
        self._items: List[SaveItem] = []
        self._is_dirty = False
        self._repurch_items: List[SaveItem] = []
        self._build_ui()

    def load(self, save_data: SaveData, items: List[SaveItem]) -> None:
        self._save_data = save_data
        self._items = items
        self._is_dirty = False
        self._populate_repurchase()

    def unload(self) -> None:
        self._save_data = None
        self._items = []
        self._repurch_items = []
        self._is_dirty = False
        self._repurch_table.setRowCount(0)
        self._repurch_count.setText(tr("0 items"))

    def set_experimental_mode(self, enabled: bool) -> None:
        self._experimental_mode = enabled
        self._vendor_template_swap_btn.setVisible(enabled)

    def scan_repurchase_items(self) -> List[SaveItem]:
        return self._scan_repurchase_items()

    def _mark_dirty(self) -> None:
        self._is_dirty = True
        self.dirty.emit()

    def clear_dirty(self) -> None:
        self._is_dirty = False

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        layout.addWidget(make_scope_label("save"))

        info = QLabel(
            "VENDOR REPURCHASE — BEST WAY TO GET NEW ITEMS\n\n"
            "HOW TO USE:\n"
            "1. Sell a junk item to any NPC vendor in-game, save your game\n"
            "2. Open the save here, find your sold item in this list\n"
            "3. Use one of the buttons below, save, load in-game, buy it back\n\n"
            "BUTTONS:\n"
            "• Clone Selected to Vendor — Creates a new vendor entry. Search by item name.\n"
            "  The selected item is used as the donor template.\n"
            "• Swap Selected Item — Simple key swap (changes the item key only)\n"
            "• Vendor Template Swap — Full template replace using verified game data\n"
            "• Set Stack — Change the stack count of the selected item\n\n"
            "IMPORTANT — TYPE-FOR-TYPE RULE:\n"
            "Swaps MUST be same type: Glove->Glove, Helm->Helm, Sword->Sword.\n"
            "Cross-type swaps produce unequippable items with invalid icons.\n"
            "If you get a wrong-type item, sell it, then Clone it to the correct type."
        )
        info.setWordWrap(True)
        info.setStyleSheet(
            f"color: {COLORS['text']}; background-color: #1a2a1a; "
            f"border: 1px solid {COLORS['success']}; border-radius: 4px; "
            f"padding: 8px; font-size: 11px; line-height: 1.4;"
        )
        help_row = QHBoxLayout()
        help_row.addWidget(info, 1)
        help_row.addWidget(make_help_btn("repurchase", self._show_guide_fn))
        layout.addLayout(help_row)

        top = QHBoxLayout()
        top.addWidget(QLabel(tr("Search:")))
        self._repurch_search = QLineEdit()
        self._repurch_search.setPlaceholderText(tr("Filter by name, key, or category..."))
        self._repurch_search.textChanged.connect(self._filter_repurchase)
        top.addWidget(self._repurch_search, 1)
        layout.addLayout(top)

        self._repurch_table = QTableWidget()
        self._repurch_table.setColumnCount(9)
        self._repurch_table.setHorizontalHeaderLabels([
            "", "Vendor", "Name", "ItemNo", "Category", "ItemKey", "Slot", "Stack", "Enchant"
        ])
        self._repurch_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._repurch_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        rh = self._repurch_table.horizontalHeader()
        rh.setSectionResizeMode(0, QHeaderView.Fixed)
        self._repurch_table.setColumnWidth(0, 0)
        rh.setSectionResizeMode(1, QHeaderView.Interactive)
        self._repurch_table.setColumnWidth(1, 180)
        rh.setMinimumSectionSize(80)
        rh.setSectionResizeMode(2, QHeaderView.Interactive)
        self._repurch_table.setColumnWidth(2, 180)
        self._repurch_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._repurch_table.setSortingEnabled(True)
        self._repurch_table.verticalHeader().setDefaultSectionSize(24)
        self._repurch_table.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
        layout.addWidget(self._repurch_table, 1)

        bottom = QHBoxLayout()
        bottom.addWidget(QLabel(tr("New Stack:")))
        self._repurch_stack = QSpinBox()
        self._repurch_stack.setRange(1, 999999999)
        self._repurch_stack.setValue(1)
        bottom.addWidget(self._repurch_stack)

        set_btn = QPushButton(tr("Set Stack"))
        set_btn.setObjectName("accentBtn")
        set_btn.clicked.connect(self._set_repurch_stack)
        bottom.addWidget(set_btn)

        swap_btn = QPushButton(tr("Swap Selected Item"))
        swap_btn.setObjectName("accentBtn")
        swap_btn.setToolTip(tr("Swap this vendor item, then buy it back in-game for a clean item with correct icon"))
        swap_btn.clicked.connect(self._swap_repurch_item)
        bottom.addWidget(swap_btn)

        add_vendor_btn = QPushButton(tr("Add to Vendor"))
        add_vendor_btn.setObjectName("accentBtn")
        add_vendor_btn.setToolTip(
            "Turn any vendor item into a different item. Requires at least one junk item "
            "in the vendor repurchase list — sell junk to a vendor first."
        )
        add_vendor_btn.clicked.connect(self._add_to_vendor)
        bottom.addWidget(add_vendor_btn)

        clone_vendor_btn = QPushButton(tr("Clone Selected to Vendor"))
        clone_vendor_btn.setObjectName("accentBtn")
        clone_vendor_btn.setToolTip(
            "Clone the selected item into a new vendor buyback entry.\n"
            "Search by item name to pick the target item.\n"
            "MUST be same type: Glove->Glove, Helm->Helm, Sword->Sword.\n"
            "Buy it back in-game — the game creates the correct item."
        )
        clone_vendor_btn.clicked.connect(self._clone_vendor_item)
        bottom.addWidget(clone_vendor_btn)


        self._vendor_template_swap_btn = QPushButton(tr("Vendor Template Swap"))
        self._vendor_template_swap_btn.setObjectName("accentBtn")
        self._vendor_template_swap_btn.setToolTip(
            "Swap selected vendor item using template matching — validates item type, "
            "stack limits, and shows warnings for mismatches."
        )
        self._vendor_template_swap_btn.clicked.connect(self._vendor_template_swap)
        self._vendor_template_swap_btn.setVisible(self._experimental_mode)
        bottom.addWidget(self._vendor_template_swap_btn)

        bottom.addStretch()
        self._repurch_count = QLabel(tr("0 items"))
        self._repurch_count.setStyleSheet(f"color: {COLORS['accent']}; font-weight: bold;")
        bottom.addWidget(self._repurch_count)
        layout.addLayout(bottom)

        self._repurch_items: List[SaveItem] = []


    def _scan_repurchase_items(self) -> List[SaveItem]:
        if not self._save_data:
            return []

        blob = self._save_data.decompressed_blob
        items: List[SaveItem] = []

        store_names = {}
        try:
            _db = get_connection()
            store_names = {
                row['store_id']: row['name']
                for row in _db.execute("SELECT store_id, name FROM stores")
            }
        except Exception:
            pass

        try:
            import parc_serializer as ps
            parc = ps.parse_parc_blob(bytes(blob))

            store_toc = None
            for e in parc.toc_entries:
                if e.class_index < len(parc.types):
                    if "Store" in parc.types[e.class_index].name:
                        store_toc = e
                        break

            if store_toc is None:
                return []

            raw = parc.block_raw[store_toc.index]
            abs_off = store_toc.data_offset

            store_data_type = None
            for i, t in enumerate(parc.types):
                if t.name == "StoreDataSaveData":
                    store_data_type = i
                    break

            sentinel = b'\xff\xff\xff\xff\xff\xff\xff\xff'
            vendor_ranges = []

            if store_data_type is not None:
                stores_with_items = []
                for i in range(len(raw) - 18):
                    mbc = struct.unpack_from("<H", raw, i)[0]
                    if mbc not in (1, 2):
                        continue
                    mask_end = i + 2 + mbc
                    sent_start = mask_end + 3
                    if sent_start + 8 > len(raw):
                        continue
                    if raw[sent_start:sent_start + 8] != sentinel:
                        continue
                    type_idx = struct.unpack_from("<H", raw, mask_end)[0]
                    if type_idx != store_data_type:
                        continue
                    mask_val = int.from_bytes(raw[i + 2:mask_end], "little")
                    if not (mask_val & 0x08):
                        continue
                    payload_off = struct.unpack_from("<I", raw, sent_start + 8)[0]
                    rel_payload = payload_off - abs_off
                    if 0 <= rel_payload < len(raw) - 10:
                        store_key = struct.unpack_from("<H", raw, rel_payload + 4)[0]
                        stores_with_items.append((i, store_key, rel_payload))

                stores_with_items.sort(key=lambda x: x[0])
                for si, (loc, skey, poff) in enumerate(stores_with_items):
                    end = stores_with_items[si + 1][0] if si + 1 < len(stores_with_items) else len(raw)
                    raw_name = store_names.get(str(skey), f"Store_{skey}")
                    friendly = raw_name.replace("Store_", "").replace("_", " ")
                    vendor_ranges.append((poff, end, skey, friendly))

            for off in range(20, len(raw) - 40):
                if struct.unpack_from("<I", raw, off)[0] != 1:
                    continue
                ino = struct.unpack_from("<q", raw, off + 4)[0]
                if ino < 1 or ino > 9999999:
                    continue
                key = struct.unpack_from("<I", raw, off + 12)[0]
                if key < 1 or key > 0x7FFFFFFF:
                    continue
                slot = struct.unpack_from("<H", raw, off + 16)[0]
                stk = struct.unpack_from("<q", raw, off + 18)[0]
                if stk < 1 or stk > 9999999:
                    continue
                if off >= 16 and struct.unpack_from("<q", raw, off - 16)[0] != -1:
                    continue

                enc = struct.unpack_from("<H", raw, off + 26)[0]
                he = enc != 0xFFFF
                end_val = struct.unpack_from("<H", raw, off + 30)[0]
                shp_val = struct.unpack_from("<H", raw, off + 32)[0]

                vendor_name = "Unknown Vendor"
                for rstart, rend, skey, fname in vendor_ranges:
                    if rstart <= off < rend:
                        vendor_name = fname
                        break

                blk_size = 0
                try:
                    from item_template_db import get_template as _gt
                    _own_tmpl = _gt(key)
                    if _own_tmpl:
                        blk_size = _own_tmpl['size']
                except Exception:
                    pass
                if blk_size == 0:
                    locator_off = off - 24
                    if locator_off >= 0:
                        loc_mbc = struct.unpack_from("<H", raw, locator_off)[0]
                        if loc_mbc == 3:
                            loc_mask = raw[locator_off + 2:locator_off + 5]
                            _fsizes = [4, 8, 4, 2, 8, 2, 8, 2, 2, 8, 8, 1, 1]
                            payload_sz = sum(
                                _fsizes[fi] for fi in range(len(_fsizes))
                                if (fi // 8) < len(loc_mask) and (loc_mask[fi // 8] & (1 << (fi % 8)))
                            )
                            blk_size = 24 + payload_sz

                it = SaveItem(
                    offset=abs_off + off,
                    item_no=ino,
                    item_key=key,
                    slot_no=slot,
                    stack_count=stk,
                    enchant_level=enc if he else 0,
                    endurance=end_val,
                    sharpness=shp_val,
                    has_enchant=he,
                    is_equipment=he,
                    source=vendor_name,
                    section=0,
                    name=self._name_db.get_name(key),
                    category=self._name_db.get_category(key),
                    block_size=blk_size,
                )
                items.append(it)
        except Exception as e:
            log.warning(tr("Repurchase scan failed: %s"), e)

        return items


    def _populate_repurchase(self) -> None:
        self._repurch_items = self._scan_repurchase_items()
        log.info("Repurchase scan: found %d vendor items", len(self._repurch_items))
        for it in self._repurch_items[:5]:
            log.info("  vendor item: key=%d name=%s vendor=%s off=0x%X",
                     it.item_key, it.name, it.source, it.offset)
        self._filter_repurchase()


    def _filter_repurchase(self) -> None:
        table = self._repurch_table
        table.setSortingEnabled(False)
        table.setRowCount(0)

        search = self._repurch_search.text().lower().strip()
        filtered = self._repurch_items

        if search:
            filtered = [
                i for i in filtered
                if search in i.name.lower()
                or search in str(i.item_key)
                or search in i.category.lower()
                or search in i.source.lower()
                or search in self._name_db.get_internal_name(i.item_key).lower()
            ]

        table.setRowCount(len(filtered))
        for row, item in enumerate(filtered):
            color = QColor(CATEGORY_COLORS.get(item.category, COLORS["text"]))

            icon_item = QTableWidgetItem()
            if self._icons_enabled:
                px = self._icon_cache.get_pixmap(item.item_key)
                if px:
                    icon_item.setIcon(QIcon(px))
            table.setItem(row, 0, icon_item)

            vendor_w = QTableWidgetItem(item.source)
            vendor_w.setForeground(QBrush(QColor(COLORS["accent"])))
            table.setItem(row, 1, vendor_w)

            name_w = QTableWidgetItem(item.name)
            name_w.setForeground(QBrush(color))
            name_w.setData(Qt.UserRole, id(item))
            table.setItem(row, 2, name_w)

            table.setItem(row, 3, _num_item(item.item_no))
            cat_w = QTableWidgetItem(item.category)
            cat_w.setForeground(QBrush(color))
            table.setItem(row, 4, cat_w)
            table.setItem(row, 5, _num_item(item.item_key))
            table.setItem(row, 6, _num_item(item.slot_no))
            table.setItem(row, 7, _num_item(item.stack_count))
            enc_text = f"+{item.enchant_level}" if item.has_enchant else "-"
            table.setItem(row, 8, QTableWidgetItem(enc_text))

        table.setSortingEnabled(True)
        self._repurch_count.setText(f"{len(filtered)} items")


    def _get_repurch_selected(self) -> List[SaveItem]:
        rows = set(idx.row() for idx in self._repurch_table.selectedIndexes())
        result = []
        for row in sorted(rows):
            item_id = None
            for col in range(self._repurch_table.columnCount()):
                w = self._repurch_table.item(row, col)
                if w is not None:
                    d = w.data(Qt.UserRole)
                    if d is not None:
                        item_id = d
                        break
            if item_id is None:
                continue
            for item in self._repurch_items:
                if id(item) == item_id:
                    result.append(item)
                    break
        return result


    def _set_repurch_stack(self) -> None:
        if not self._save_data:
            return
        selected = self._get_repurch_selected()
        if not selected:
            QMessageBox.information(self, tr("Set Stack"), tr("Select items first."))
            return
        new_stack = self._repurch_stack.value()
        edits = []
        for item in selected:
            old = apply_stack_edit(self._save_data.decompressed_blob, item, new_stack)
            edits.append((item.offset + 18, old, self._save_data.decompressed_blob[item.offset + 18:item.offset + 26]))
        if edits:
            self.undo_entry_added.emit(UndoEntry(
                description=f"Repurchase: set stack to {new_stack} for {len(edits)} items",
                patches=[(o, old, new) for o, old, new in edits],
            ))
            self._mark_dirty()
            self._populate_repurchase()
            self.status_message.emit(f"Repurchase: set stack to {new_stack} for {len(edits)} items")


    def _template_add_to_vendor(self) -> None:
        if not self._save_data:
            QMessageBox.warning(self, tr("Template Add"), tr("No save loaded."))
            return

        dlg = AddItemDialog(self._name_db, self)
        if dlg.exec() != QDialog.Accepted:
            return

        target_key = dlg.selected_key
        target_count = dlg.selected_count
        target_name = self._name_db.get_name(target_key)

        reply = QMessageBox.question(
            self, tr("Template Add to Vendor"),
            f"Insert into vendor buyback list:\n\n"
            f"  {target_name} (key={target_key})\n"
            f"  Count: {target_count}\n\n"
            f"Uses PARC insertion with real game template data.\n"
            f"Buy it back from the vendor in-game.\nContinue?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        try:
            from parc_inserter2 import add_item_to_store

            blob = bytes(self._save_data.decompressed_blob)
            max_no = max((it.item_no for it in self._items), default=100)
            new_item_no = max_no + 100

            new_blob = add_item_to_store(
                blob,
                new_item_key=target_key,
                new_item_no=new_item_no,
                new_stack=target_count,
            )

            if not new_blob:
                QMessageBox.warning(self, tr("Template Add"), tr("Insertion failed — no vendor with sold items found.\nSell a junk item to any vendor first."))
                return

            self._save_data.decompressed_blob = bytearray(new_blob)
            self._save_data.parse_cache = None
            self.undo_entry_added.emit(UndoEntry(
                description=f"Template Add to Vendor: {target_name} x{target_count}",
                patches=[],
            ))
            self._mark_dirty()
            self._populate_repurchase()
            self.items_changed.emit()
            self.status_message.emit(f"Added {target_name} x{target_count} to vendor. Buy it back in-game!")

        except Exception as e:
            log.exception("Unhandled exception")
            QMessageBox.warning(self, tr("Template Add Failed"), f"Insertion failed:\n{e}")


    def _vendor_template_swap(self) -> None:
        if not self._save_data:
            QMessageBox.warning(self, tr("Vendor Template Swap"), tr("No save loaded."))
            return

        rows = set(idx.row() for idx in self._repurch_table.selectedIndexes())
        if not rows:
            QMessageBox.warning(self, tr("Vendor Template Swap"), tr("Select a vendor item first."))
            return

        row = min(rows)
        if row >= len(self._repurch_items):
            return

        item = self._repurch_items[row]
        old_name = self._name_db.get_name(item.item_key)

        dlg = AddItemDialog(self._name_db, self)
        dlg.setWindowTitle(tr("Vendor Template Swap — Pick Target Item"))
        if dlg.exec() != QDialog.Accepted:
            return

        new_key = dlg.selected_key
        new_name = self._name_db.get_name(new_key)

        try:
            _db = get_connection()
            item_limits = {str(row['item_key']): _json.loads(row['data']) for row in _db.execute("SELECT item_key, data FROM item_limits")}
        except Exception:
            item_limits = {}

        old_limits = item_limits.get(str(item.item_key), {})
        new_limits = item_limits.get(str(new_key), {})
        old_slot = old_limits.get('slotType', -1)
        new_slot = new_limits.get('slotType', -1)
        old_is_equip = old_slot not in (-1, 65535)
        new_is_equip = new_slot not in (-1, 65535)
        new_stack_limit = new_limits.get('stackLimit', 999999)

        template = None
        try:
            from item_template_db import get_template
            template = get_template(new_key)
        except Exception:
            pass

        current_item_size = item.block_size if hasattr(item, 'block_size') and item.block_size > 0 else 0
        can_template_replace = False
        if template and current_item_size > 0 and template['size'] == current_item_size:
            tmpl_mask = template.get('mask', '')
            current_mask = ''
            try:
                blob = self._save_data.decompressed_blob
                loc_start = item.offset - 24
                loc_mbc = struct.unpack_from("<H", blob, loc_start)[0]
                if loc_mbc == 3:
                    current_mask = blob[loc_start + 2:loc_start + 5].hex()
            except Exception:
                pass
            can_template_replace = (tmpl_mask == current_mask) if (tmpl_mask and current_mask) else (template['size'] == current_item_size)

        if can_template_replace:
            method = f"FULL TEMPLATE REPLACE ({template['size']}B, mask={template.get('mask','?')})"
        elif template:
            reason = f"size {template['size']}B vs {current_item_size}B" if template['size'] != current_item_size else "mask mismatch"
            method = f"Key swap ({reason})"
        else:
            method = "Key swap (no template)"

        warnings = []
        if old_is_equip != new_is_equip:
            warnings.append(f"Type mismatch: {'Equipment' if old_is_equip else 'Item'} -> {'Equipment' if new_is_equip else 'Item'}")
        if item.stack_count > new_stack_limit > 0:
            warnings.append(f"Stack {item.stack_count} exceeds target limit {new_stack_limit}")
        warn_text = "\n\nWarnings:\n" + "\n".join(f"  - {w}" for w in warnings) if warnings else ""

        reply = QMessageBox.question(
            self, tr("Vendor Template Swap"),
            f"  {old_name} -> {new_name}\n\n"
            f"  Method: {method}\n"
            f"  Target type: {'Equipment' if new_is_equip else 'Item'}\n"
            f"  Stack limit: {new_stack_limit}"
            f"{warn_text}\n\nContinue?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        patches = []

        if can_template_replace:
            import struct as _struct
            entry = bytearray(bytes.fromhex(template['hex']))
            fp = template.get('field_positions', {})

            if '_itemNo' in fp:
                _struct.pack_into('<q', entry, fp['_itemNo']['rel_offset'], item.item_no)
            if '_itemKey' in fp:
                _struct.pack_into('<I', entry, fp['_itemKey']['rel_offset'], new_key)
            if '_slotNo' in fp:
                _struct.pack_into('<H', entry, fp['_slotNo']['rel_offset'], item.slot_no)
            if '_stackCount' in fp:
                stack = min(item.stack_count, new_stack_limit) if new_stack_limit > 0 else item.stack_count
                _struct.pack_into('<q', entry, fp['_stackCount']['rel_offset'], stack)
            if '_transferredItemKey' in fp:
                _struct.pack_into('<I', entry, fp['_transferredItemKey']['rel_offset'], new_key)

            mbc = _struct.unpack_from('<H', entry, 0)[0]
            po_offset = 2 + mbc + 2 + 1 + 8
            item_abs_start = item.offset - (po_offset + 8)

            _struct.pack_into('<I', entry, po_offset, item_abs_start + po_offset + 4)

            current_type_idx = _struct.unpack_from('<H', self._save_data.decompressed_blob, item_abs_start + 2 + mbc)[0]
            _struct.pack_into('<H', entry, 2 + mbc, current_type_idx)

            _SENT = b'\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF'
            for off in range(len(entry) - 15):
                m = _struct.unpack_from('<H', entry, off)[0]
                if m < 1 or m > 8:
                    continue
                s = off + 2 + m + 3
                if s + 12 > len(entry):
                    continue
                if entry[s:s + 8] != _SENT:
                    continue
                t = _struct.unpack_from('<H', entry, off + 2 + m)[0]
                if t > 200 or entry[off + 2 + m + 2] != 0:
                    continue
                pp = s + 8
                wrapper_end = off + 2 + m + 2 + 1 + 4 + 4 + 4
                _struct.pack_into('<I', entry, pp, item_abs_start + wrapper_end)

            for off in range(len(entry) - 18):
                if entry[off + 6:off + 14] == _SENT:
                    _struct.pack_into('<I', entry, off + 14, item_abs_start + off + 18)

            old_bytes = bytes(self._save_data.decompressed_blob[item_abs_start:item_abs_start + len(entry)])
            self._save_data.decompressed_blob[item_abs_start:item_abs_start + len(entry)] = entry
            patches.append((item_abs_start, old_bytes, bytes(entry)))
            method_used = "FULL TEMPLATE REPLACE"
        else:
            patches = smart_item_swap(self._save_data.decompressed_blob, item, new_key)
            if new_stack_limit > 0 and item.stack_count > new_stack_limit:
                old_stack_bytes = apply_stack_edit(
                    self._save_data.decompressed_blob, item, new_stack_limit
                )
                patches.append((
                    item.offset + 18,
                    old_stack_bytes,
                    self._save_data.decompressed_blob[item.offset + 18:item.offset + 26]
                ))
            method_used = "Key swap (fallback)"

        item.name = new_name
        item.category = self._name_db.get_category(new_key)
        item.item_key = new_key

        self.undo_entry_added.emit(UndoEntry(
            description=f"Vendor Template Swap: {old_name} -> {new_name} [{method_used}]",
            patches=patches,
        ))
        self._mark_dirty()
        self._populate_repurchase()
        self.items_changed.emit()
        self.status_message.emit(f"Vendor Template Swap: {old_name} -> {new_name} [{method_used}]")


    def _add_to_vendor(self) -> None:
        if not self._save_data:
            QMessageBox.warning(self, tr("Add to Vendor"), tr("No save loaded."))
            return

        if not self._repurch_items:
            QMessageBox.warning(
                self, tr("Add to Vendor"),
                "No items in vendor repurchase lists.\n\n"
                "Sell some junk items to any vendor in-game first, "
                "then save and reload here."
            )
            return

        dlg = QDialog(self)
        dlg.setWindowTitle(tr("Add to Vendor — Pick Item"))
        dlg.setMinimumSize(600, 400)
        dl = QVBoxLayout(dlg)
        dl.addWidget(QLabel(tr("Search for the item you want to add to a vendor:")))

        search = QLineEdit()
        search.setPlaceholderText(tr("Search item database..."))
        dl.addWidget(search)

        target_list = QTableWidget()
        target_list.setColumnCount(4)
        target_list.setHorizontalHeaderLabels(["", "Key", "Name", "Category"])
        target_list.setSelectionBehavior(QAbstractItemView.SelectRows)
        target_list.setSelectionMode(QAbstractItemView.SingleSelection)
        target_list.horizontalHeader().setSectionResizeMode(0, QHeaderView.Interactive)
        target_list.setColumnWidth(0, ICON_SIZE + 16 if self._icons_enabled else 0)
        target_list.horizontalHeader().setSectionResizeMode(2, QHeaderView.Interactive)
        target_list.setColumnWidth(2, 200)
        target_list.setEditTriggers(QAbstractItemView.NoEditTriggers)
        target_list.setSortingEnabled(True)
        target_list.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
        dl.addWidget(target_list, 1)

        def refresh_list():
            text = search.text().strip()
            results = self._name_db.search(text) if text else self._name_db.get_all_sorted()[:200]
            target_list.setSortingEnabled(False)
            target_list.setRowCount(len(results))
            for r, info in enumerate(results):
                icon_item = QTableWidgetItem()
                if self._icons_enabled:
                    px = self._icon_cache.get_pixmap(info.item_key)
                    if px:
                        icon_item.setIcon(QIcon(px))
                target_list.setItem(r, 0, icon_item)
                target_list.setItem(r, 1, QTableWidgetItem(str(info.item_key)))
                n = QTableWidgetItem(info.name)
                n.setForeground(QBrush(QColor(CATEGORY_COLORS.get(info.category, COLORS["text"]))))
                target_list.setItem(r, 2, n)
                target_list.setItem(r, 3, QTableWidgetItem(info.category))
            target_list.setSortingEnabled(True)

        search.textChanged.connect(lambda: refresh_list())
        refresh_list()

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        dl.addWidget(bb)

        if dlg.exec() != QDialog.Accepted:
            return
        rows = set(idx.row() for idx in target_list.selectedIndexes())
        if not rows:
            return
        row = min(rows)
        key_w = target_list.item(row, 1)
        if not key_w:
            return
        new_key = int(key_w.text())
        new_name = self._name_db.get_name(new_key)

        dlg2 = QDialog(self)
        dlg2.setWindowTitle(f"Add to Vendor — Pick Sacrifice for: {new_name}")
        dlg2.setMinimumSize(600, 400)
        dl2 = QVBoxLayout(dlg2)
        target_cat = self._name_db.get_category(new_key)
        dl2.addWidget(QLabel(
            f"Target: {new_name} (key={new_key}, category={target_cat})\n\n"
            "Pick a vendor item of the SAME TYPE to replace.\n"
            "Same-type swaps are shown in green. Cross-type swaps may crash the save."
        ))

        sacrifice_search = QLineEdit()
        sacrifice_search.setPlaceholderText(tr("Search vendor items..."))
        dl2.addWidget(sacrifice_search)

        sac_list = QListWidget()
        sac_texts = []
        sorted_items = sorted(
            self._repurch_items,
            key=lambda x: (0 if x.category == target_cat else 1, x.name)
        )
        for it in sorted_items:
            text = f"{it.name}  |  [{it.category}]  key={it.item_key}  no={it.item_no}  [{it.source}]"
            lw = QListWidgetItem(text)
            if it.category == target_cat:
                lw.setForeground(QBrush(QColor(COLORS["success"])))
            else:
                lw.setForeground(QBrush(QColor(COLORS["text_dim"])))
            sac_list.addItem(lw)
            sac_texts.append(text.lower())
        self._add_vendor_sorted = sorted_items

        def filter_sac(text):
            s = text.lower().strip()
            for i in range(sac_list.count()):
                sac_list.item(i).setHidden(s != "" and s not in sac_texts[i])

        sacrifice_search.textChanged.connect(filter_sac)
        dl2.addWidget(sac_list, 1)

        bb2 = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb2.accepted.connect(dlg2.accept)
        bb2.rejected.connect(dlg2.reject)
        dl2.addWidget(bb2)

        if dlg2.exec() != QDialog.Accepted:
            return
        sel = sac_list.currentRow()
        if sel < 0 or sel >= len(self._add_vendor_sorted):
            return
        sacrifice = self._add_vendor_sorted[sel]

        if sacrifice.category != target_cat:
            reply = QMessageBox.warning(
                self, tr("Type Mismatch"),
                f"The sacrifice item ({sacrifice.category}) is a different type "
                f"than the target ({target_cat}).\n\n"
                f"Cross-type swaps may CRASH your save.\n"
                f"Continue anyway?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return

        blob = self._save_data.decompressed_blob
        old_key = sacrifice.item_key
        old_key_bytes = struct.pack("<I", old_key)
        new_key_bytes = struct.pack("<I", new_key)
        patches = []

        pos = sacrifice.offset + 12
        if blob[pos:pos + 4] == old_key_bytes:
            patches.append((pos, old_key_bytes, new_key_bytes))
            blob[pos:pos + 4] = new_key_bytes

        region_end = min(sacrifice.offset + 300, len(blob) - 4)
        for pos in range(sacrifice.offset + 16, region_end):
            if blob[pos:pos + 4] == old_key_bytes:
                patches.append((pos, old_key_bytes, new_key_bytes))
                blob[pos:pos + 4] = new_key_bytes

        self.undo_entry_added.emit(UndoEntry(
            description=f"Add to vendor: {sacrifice.name} -> {new_name}",
            patches=patches,
        ))
        self._mark_dirty()
        self._populate_repurchase()
        self.items_changed.emit()
        self.status_message.emit(
            f"Added {new_name} to vendor (replaced {sacrifice.name}). "
            f"Visit the vendor in-game and buy it back!"
        )


    def _parc_add_to_vendor(self) -> None:
        if not self._save_data:
            QMessageBox.warning(self, tr("PARC Vendor Insert"), tr("No save loaded."))
            return
        if insert_item_to_store is None:
            QMessageBox.critical(self, tr("Vendor Insert"), tr("PARC inserter module failed to load."))
            return

        dlg = ItemSearchDialog(
            self._name_db,
            title="Add New Item to Vendor",
            prompt=(
                "Search for the item to add to vendor buyback.\n"
                "You must have sold at least 1 item to a vendor first.\n"
                "Buy it back in-game after saving."
            ),
            parent=self,
            template_only=True,
        )
        if dlg.exec() != QDialog.Accepted or dlg.selected_key == 0:
            return

        key = dlg.selected_key
        from PySide6.QtWidgets import QInputDialog
        stack, ok2 = QInputDialog.getInt(
            self, "Vendor Insert — Stack Count",
            f"How many {dlg.selected_name}?",
            1, 1, 99999,
        )
        if not ok2:
            return

        name = dlg.selected_name or self._name_db.get_name(key)
        reply = QMessageBox.question(
            self, tr("PARC Vendor Insert"),
            f"Insert {name} (key={key}) x{stack} into vendor buyback?\n\n"
            f"May require 2 reloads but not usually. Buy it back from the vendor in-game.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        self.status_message.emit("Inserting to vendor via PARC...")
        QApplication.processEvents()

        try:
            blob = bytearray(self._save_data.decompressed_blob)
            ok, new_blob, msg = insert_item_to_store(
                blob, item_key=key, stack_count=stack,
            )
            if ok:
                self._save_data.decompressed_blob = bytearray(new_blob)
                self._save_data.parse_cache = None
                self._mark_dirty()
                self._populate_repurchase()
                self.status_message.emit(f"PARC Vendor Insert: {name} x{stack} — {msg}")
                QMessageBox.information(self, tr("Vendor Item Added"),
                    f"Added {name} x{stack} to vendor buyback.\n\n{msg}\n\n"
                    f"Save (Ctrl+S), reload in-game (2x), check vendor buyback.")
            else:
                self.status_message.emit(f"PARC Vendor Insert failed: {msg}")
                QMessageBox.critical(self, tr("Insert Failed"), msg)
        except Exception as e:
            self.status_message.emit(f"PARC Vendor Insert error: {e}")
            QMessageBox.critical(self, tr("Insert Error"), str(e))


    def _swap_repurch_item(self) -> None:
        if not self._save_data:
            return
        selected = self._get_repurch_selected()
        if not selected:
            QMessageBox.information(self, tr("Swap"), tr("Select an item from the repurchase list first."))
            return
        item = selected[0]

        from item_scanner import smart_item_swap

        dlg = QDialog(self)
        dlg.setWindowTitle(f"Swap Vendor Item: {item.name}")
        dlg.setMinimumSize(600, 400)
        dl = QVBoxLayout(dlg)
        dl.addWidget(QLabel(f"Current: {item.name} (key={item.item_key})\nPick what you want this to become:"))

        search = QLineEdit()
        search.setPlaceholderText(tr("Search item database..."))
        dl.addWidget(search)

        target_list = QTableWidget()
        target_list.setColumnCount(3)
        target_list.setHorizontalHeaderLabels(["Key", "Name", "Category"])
        target_list.setSelectionBehavior(QAbstractItemView.SelectRows)
        target_list.setSelectionMode(QAbstractItemView.SingleSelection)
        target_list.horizontalHeader().setSectionResizeMode(1, QHeaderView.Interactive)
        target_list.setColumnWidth(1, 200)
        target_list.setEditTriggers(QAbstractItemView.NoEditTriggers)
        target_list.setSortingEnabled(True)
        dl.addWidget(target_list, 1)

        def refresh_list():
            text = search.text().strip()
            results = self._name_db.search(text) if text else self._name_db.get_all_sorted()[:200]
            target_list.setSortingEnabled(False)
            target_list.setRowCount(len(results))
            for r, info in enumerate(results):
                target_list.setItem(r, 0, QTableWidgetItem(str(info.item_key)))
                n = QTableWidgetItem(info.name)
                n.setForeground(QBrush(QColor(CATEGORY_COLORS.get(info.category, COLORS["text"]))))
                target_list.setItem(r, 1, n)
                target_list.setItem(r, 2, QTableWidgetItem(info.category))
            target_list.setSortingEnabled(True)

        search.textChanged.connect(lambda: refresh_list())
        refresh_list()

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        dl.addWidget(bb)

        if dlg.exec() != QDialog.Accepted:
            return
        rows = set(idx.row() for idx in target_list.selectedIndexes())
        if not rows:
            return
        row = min(rows)
        key_w = target_list.item(row, 0)
        if not key_w:
            return
        new_key = int(key_w.text())
        new_name = self._name_db.get_name(new_key)

        blob = self._save_data.decompressed_blob
        old_key = item.item_key
        old_key_bytes = struct.pack("<I", old_key)
        new_key_bytes = struct.pack("<I", new_key)
        patches = []

        pos = item.offset + 12
        if blob[pos:pos + 4] == old_key_bytes:
            patches.append((pos, old_key_bytes, new_key_bytes))
            blob[pos:pos + 4] = new_key_bytes

        region_end = min(item.offset + 300, len(blob) - 4)
        for pos in range(item.offset + 16, region_end):
            if blob[pos:pos + 4] == old_key_bytes:
                patches.append((pos, old_key_bytes, new_key_bytes))
                blob[pos:pos + 4] = new_key_bytes

        self.undo_entry_added.emit(UndoEntry(
            description=f"Repurchase swap: {item.name} -> {new_name}",
            patches=patches,
        ))
        self._mark_dirty()
        self._populate_repurchase()
        self.items_changed.emit()
        self.status_message.emit(f"Vendor swap: {item.name} -> {new_name} ({len(patches)} patches). Buy it back in-game for correct icon!")


    def _clone_vendor_item(self) -> None:
        try:
            if not self._save_data:
                QMessageBox.warning(self, tr("Clone Item"), tr("No save loaded."))
                return
            if insert_item_to_store is None:
                QMessageBox.critical(self, tr("Clone Item"), tr("PARC inserter not available."))
                return

            selected = self._get_repurch_selected()
            if not selected:
                QMessageBox.information(self, tr("Clone Item"),
                    "Select an item from the repurchase list first.\n"
                    "This item will be used as the donor template.")
                return

            donor = selected[0]
            donor_name = donor.name
            donor_cat = self._name_db.get_category(donor.item_key)

            dlg = ItemSearchDialog(
                self._name_db,
                title=f"Clone to Vendor — Pick Target Item",
                prompt=(
                    f"Donor: {donor_name} (category: {donor_cat})\n\n"
                    f"IMPORTANT: Swap must be type-for-type!\n"
                    f"Glove -> Glove, Helm -> Helm, Sword -> Sword.\n"
                    f"Cross-type swaps will be unequippable.\n\n"
                    f"Search for the item you want:"
                ),
                parent=self,
            )
            if dlg.exec() != QDialog.Accepted or dlg.selected_key == 0:
                return

            key = dlg.selected_key
            stack = 1

            target_name = dlg.selected_name or self._name_db.get_name(key)
            reply = QMessageBox.question(
                self, tr("Clone to Vendor"),
                f"Clone '{donor_name}' as '{target_name}' (key={key})?\n\n"
                f"It will appear in the vendor's buyback list.\n"
                f"Buy it back in-game — the game creates the correct item.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return

            self.status_message.emit(f"Cloning {donor_name} -> {target_name}...")
            QApplication.processEvents()

            blob = bytearray(self._save_data.decompressed_blob)
            ok_ins, new_blob, msg = insert_item_to_store(
                blob, item_key=key, stack_count=stack,
            )
            if ok_ins:
                self._save_data.decompressed_blob = bytearray(new_blob)
                self._save_data.parse_cache = None
                self._mark_dirty()
                self._populate_repurchase()
                self.status_message.emit(f"Cloned: {target_name} x{stack}")
                QMessageBox.information(self, tr("Item Cloned"),
                    f"Added {target_name} x{stack} to vendor buyback.\n\n{msg}\n\n"
                    f"Save (Ctrl+S), reload in-game (may require 2 reloads but not usually),\n"
                    f"go to the vendor and buy it back.")
            else:
                self.status_message.emit(f"Clone failed: {msg}")
                QMessageBox.critical(self, tr("Clone Failed"), msg)
        except Exception as e:
            log.exception("Unhandled exception")
            QMessageBox.critical(self, tr("Clone Error"), str(e))


class DyeTab(QWidget):

    dirty = Signal()
    status_message = Signal(str)
    undo_entry_added = Signal(object)
    _dye_parsed = Signal(list)

    def __init__(
        self,
        name_db: ItemNameDB,
        icon_cache=None,
        experimental_mode: bool = False,
        app_dir_fn: Optional[Callable[[], str]] = None,
        backup_fn: Optional[Callable] = None,
        show_guide_fn: Optional[Callable] = None,
        parent=None,
    ):
        super().__init__(parent)
        self._name_db = name_db
        self._icon_cache = icon_cache
        self._experimental_mode = experimental_mode
        self._app_dir_fn = app_dir_fn or (lambda: ".")
        self._backup_fn = backup_fn
        self._show_guide_fn = show_guide_fn or (lambda k: None)
        self._save_data: Optional[SaveData] = None
        self._items: List[SaveItem] = []
        self._is_dirty = False
        self._dye_items: List[SaveItem] = []
        self._dye_current_item: Optional[SaveItem] = None
        self._dye_current_part: Optional[str] = None
        self._dye_updating: bool = False
        self._dye_parsed.connect(self._dye_populate)
        self._build_ui()

    def load(self, save_data: SaveData, items: List[SaveItem]) -> None:
        self._save_data = save_data
        self._items = items
        self._is_dirty = False
        self._dye_load()

    def unload(self) -> None:
        self._save_data = None
        self._items = []
        self._dye_items = []
        self._is_dirty = False
        self._dye_gear_table.setRowCount(0)

    def set_experimental_mode(self, enabled: bool) -> None:
        self._experimental_mode = enabled
        if hasattr(self, '_dye_add_btn'):
            self._dye_add_btn.setVisible(enabled)

    def _mark_dirty(self) -> None:
        self._is_dirty = True
        self.dirty.emit()

    def clear_dirty(self) -> None:
        self._is_dirty = False

    def _load_dye_slot_db(self) -> dict:
        import json as _json
        p = os.path.join(self._app_dir_fn(), 'dye_slot_counts.json')
        if os.path.isfile(p):
            try:
                return _json.load(open(p, 'r'))
            except Exception:
                pass
        return {}


    def _save_dye_slot_db(self, db: dict) -> None:
        import json as _json
        p = os.path.join(self._app_dir_fn(), 'dye_slot_counts.json')
        try:
            with open(p, 'w') as f:
                _json.dump(db, f, indent=2)
        except Exception:
            pass


    def _dye_auto_learn_slots(self) -> None:
        if not self._dye_items:
            return
        db = self._load_dye_slot_db()
        changed = False
        for item in self._dye_items:
            if item['has_dye'] and item['dye_entries']:
                key = str(item['item_key'])
                count = len(item['dye_entries'])
                if key not in db or db[key].get('slots', 0) != count:
                    db[key] = {
                        'name': item['item_name'],
                        'slots': count,
                        'source': 'auto_learned',
                    }
                    changed = True
        if changed:
            self._save_dye_slot_db(db)


    def _dye_sync_slot_db(self) -> None:
        # OFFLINE BUILD: dye slot DB download removed — local database only.
        self._dye_status.setText(tr("Online sync removed (offline build) — using local dye slot database."))

    DYE_COLOR_GROUPS = {
        0xC88211F5: "Herenon",
        0xDC274476: "Demonstone I",
        0x068F0CCE: "Demonstone II",
        0x40707E94: "Demonstone III",
        0x001835E0: "Kwean",
        0xA7EC4D9B: "Delmore",
        0x2D0517C9: "Calpheon",
        0x2A85F874: "Port",
        0x4F40E9D2: "Tom",
        0x47564F94: "Barmak",
    }

    DYE_MATERIALS = {
        0: "Default", 1: "Cloth", 2: "Leather", 3: "Metal",
        4: "Wool", 5: "Velvet", 6: "Silk", 7: "Linen",
        8: "Fur", 9: "Chain", 10: "Crystal",
    }

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        layout.addWidget(make_scope_label("save"))

        info = QLabel(
            "Edit equipment dye colors. Items must be dyed in-game first (each slot at least once) "
            "before they appear here. Select an item, pick a color, Ctrl+S to save."
        )
        info.setWordWrap(True)
        info.setStyleSheet(
            f"color: {COLORS['accent']}; padding: 6px; "
            f"border: 1px solid {COLORS['border']}; border-radius: 4px; "
            f"background-color: rgba(218,168,80,0.08);"
        )
        layout.addWidget(info)

        top = QHBoxLayout()
        load_btn = QPushButton(tr("Load Dye Data"))
        load_btn.setObjectName("accentBtn")
        load_btn.clicked.connect(self._dye_load)
        top.addWidget(load_btn)

        refresh_btn = QPushButton(tr("Refresh"))
        refresh_btn.setToolTip(tr("Reload dye data from the current save"))
        refresh_btn.clicked.connect(self._dye_load)
        top.addWidget(refresh_btn)

        self._dye_status = QLabel("")
        self._dye_status.setStyleSheet(f"color: {COLORS['text_dim']}; padding: 4px;")
        top.addWidget(self._dye_status, 1)
        layout.addLayout(top)

        splitter = QSplitter(Qt.Horizontal)

        gear_frame = QFrame()
        gear_layout = QVBoxLayout(gear_frame)
        gear_layout.setContentsMargins(0, 0, 0, 0)
        gear_layout.addWidget(QLabel(tr("Gears (Equipped Items):")))

        self._dye_gear_table = QTableWidget()
        self._dye_gear_table.setColumnCount(5)
        self._dye_gear_table.setHorizontalHeaderLabels(["Slot", "", "Item", "Parts", "Dyed"])
        self._dye_gear_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._dye_gear_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._dye_gear_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._dye_gear_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Fixed)
        self._dye_gear_table.setColumnWidth(1, ICON_SIZE + 8)
        self._dye_gear_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Interactive)
        self._dye_gear_table.setColumnWidth(2, 200)
        self._dye_gear_table.verticalHeader().setDefaultSectionSize(max(ICON_SIZE + 4, 28))
        self._dye_gear_table.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
        self._dye_gear_table.selectionModel().selectionChanged.connect(self._dye_gear_selected)
        gear_layout.addWidget(self._dye_gear_table)
        splitter.addWidget(gear_frame)

        right_frame = QFrame()
        right_layout = QVBoxLayout(right_frame)
        right_layout.setContentsMargins(0, 0, 0, 0)

        right_layout.addWidget(QLabel(tr("Parts (Dye Slots):")))
        self._dye_parts_table = QTableWidget()
        self._dye_parts_table.setColumnCount(6)
        self._dye_parts_table.setHorizontalHeaderLabels(["Slot", "Color", "R", "G", "B", "Material"])
        self._dye_parts_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._dye_parts_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._dye_parts_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._dye_parts_table.verticalHeader().setDefaultSectionSize(28)
        self._dye_parts_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Fixed)
        self._dye_parts_table.setColumnWidth(1, 60)
        self._dye_parts_table.cellClicked.connect(self._dye_part_clicked)
        right_layout.addWidget(self._dye_parts_table)

        ctrl = QHBoxLayout()

        pick_btn = QPushButton(tr("Pick Color"))
        pick_btn.setObjectName("accentBtn")
        pick_btn.setToolTip(tr("Open color picker for the selected part"))
        pick_btn.clicked.connect(self._dye_pick_color)
        ctrl.addWidget(pick_btn)

        self._dye_material_combo = QComboBox()
        for k, v in sorted(self.DYE_MATERIALS.items()):
            self._dye_material_combo.addItem(f"{k}: {v}", k)
        self._dye_material_combo.currentIndexChanged.connect(self._dye_material_changed)
        self._dye_material_combo.setVisible(False)

        self._dye_grime_spin = QSpinBox()
        self._dye_grime_spin.setRange(0, 127)
        self._dye_grime_spin.setValue(0)
        self._dye_grime_spin.valueChanged.connect(self._dye_grime_changed)
        self._dye_grime_spin.setVisible(False)

        self._dye_group_combo = QComboBox()
        self._dye_group_combo.addItem("None", 0)
        for k, v in self.DYE_COLOR_GROUPS.items():
            self._dye_group_combo.addItem(v, k)
        self._dye_group_combo.currentIndexChanged.connect(self._dye_group_changed)
        self._dye_group_combo.setVisible(False)

        ctrl.addStretch()
        right_layout.addLayout(ctrl)

        quick_row = QHBoxLayout()
        quick_row.addWidget(QLabel(tr("Quick:")))
        quick_colors = [
            ("Red", 242, 0, 0), ("Orange", 242, 143, 0), ("Yellow", 242, 231, 0),
            ("Lime", 157, 228, 0), ("Green", 0, 242, 0), ("Cyan", 29, 171, 242),
            ("Blue", 0, 0, 242), ("Purple", 190, 33, 242), ("White", 255, 255, 255),
            ("Black", 20, 20, 20), ("Gold", 218, 168, 80),
        ]
        for name, r, g, b in quick_colors:
            btn = QPushButton()
            btn.setFixedSize(24, 24)
            btn.setStyleSheet(
                f"background-color: rgb({r},{g},{b}); border: 2px solid {COLORS['border']}; "
                f"border-radius: 4px;"
            )
            btn.setToolTip(name)
            btn.clicked.connect(lambda checked, cr=r, cg=g, cb=b: self._dye_apply_quick_color(cr, cg, cb))
            quick_row.addWidget(btn)
        quick_row.addStretch()

        apply_all_btn = QPushButton(tr("Apply Color to All Parts"))
        apply_all_btn.setToolTip(tr("Set the current color to all dye slots on this item"))
        apply_all_btn.clicked.connect(self._dye_apply_to_all_parts)
        quick_row.addWidget(apply_all_btn)

        self._dye_add_btn = QPushButton(tr("Add Dye Slot to Item (WIP)"))
        self._dye_add_btn.setToolTip(
            "EXPERIMENTAL: Add dye data to an undyed item.\n"
            "You MUST know the correct slot count or the save will crash.\n"
            "Safer method: dye each slot in-game first, then use this editor to change colors."
        )
        self._dye_add_btn.clicked.connect(self._dye_add_to_item)
        self._dye_add_btn.setVisible(self._experimental_mode)
        quick_row.addWidget(self._dye_add_btn)

        right_layout.addLayout(quick_row)

        splitter.addWidget(right_frame)
        splitter.setSizes([280, 500])
        layout.addWidget(splitter, 1)


        self._dye_items = []
        self._dye_current_item = None
        self._dye_current_part = None
        self._dye_updating = False


    def _dye_load(self) -> None:
        if not self._save_data:
            QMessageBox.warning(self, tr("Dye"), tr("Load a save file first."))
            return

        self._dye_status.setText(tr("Parsing dye data..."))
        self._dye_gear_table.setRowCount(0)

        raw = bytes(self._save_data.decompressed_blob)
        name_db = self._name_db

        def _parse() -> None:
            try:
                from parc_inserter2 import parse_and_collect
                result, _, _ = parse_and_collect(raw)
            except Exception as e:
                self._dye_parsed.emit([])
                return

            dye_items = []
            for obj in result['objects']:
                if obj.class_name != 'EquipmentSaveData':
                    continue
                for f in obj.fields:
                    if f.name != '_list' or not f.list_elements:
                        continue
                    for slot_idx, elem in enumerate(f.list_elements):
                        if not elem.child_fields:
                            continue
                        for cf in elem.child_fields:
                            if cf.name != '_item' or not cf.child_fields:
                                continue
                            item_key = 0
                            dye_list = None
                            for icf in cf.child_fields:
                                if icf.name == '_itemKey' and icf.present:
                                    item_key = struct.unpack_from('<I', raw, icf.start_offset)[0]
                                if icf.name == '_itemDyeDataList' and icf.present:
                                    dye_list = icf

                            if item_key == 0:
                                continue

                            item_name = name_db.get_name(item_key)
                            dye_entries = []

                            if dye_list and dye_list.list_elements:
                                for di, dye_elem in enumerate(dye_list.list_elements):
                                    orig_mask = 0
                                    try:
                                        if dye_elem.start_offset + 2 < len(raw):
                                            orig_mask = raw[dye_elem.start_offset + 2]
                                    except Exception:
                                        orig_mask = 0
                                    entry = {'slot': di, 'r': 0, 'g': 0, 'b': 0, 'a': 255,
                                             'group': 0, 'material': 0, 'grime': 0,
                                             'orig_mask': orig_mask, 'offsets': {}}
                                    for dcf in (dye_elem.child_fields or []):
                                        if not dcf.present:
                                            continue
                                        sz = dcf.end_offset - dcf.start_offset
                                        if dcf.name == '_dyeSlotNo':
                                            entry['slot'] = raw[dcf.start_offset]
                                            entry['offsets']['slot'] = dcf.start_offset
                                        elif dcf.name == '_dyeColorR':
                                            entry['r'] = raw[dcf.start_offset]
                                            entry['offsets']['r'] = dcf.start_offset
                                        elif dcf.name == '_dyeColorG':
                                            entry['g'] = raw[dcf.start_offset]
                                            entry['offsets']['g'] = dcf.start_offset
                                        elif dcf.name == '_dyeColorB':
                                            entry['b'] = raw[dcf.start_offset]
                                            entry['offsets']['b'] = dcf.start_offset
                                        elif dcf.name == '_dyeColorA':
                                            entry['a'] = raw[dcf.start_offset]
                                            entry['offsets']['a'] = dcf.start_offset
                                        elif dcf.name == '_dyeColorGroupInfoKey':
                                            entry['group'] = struct.unpack_from('<I', raw, dcf.start_offset)[0]
                                            entry['offsets']['group'] = dcf.start_offset
                                        elif dcf.name == '_texturePalleteKey':
                                            if sz == 2:
                                                entry['material'] = struct.unpack_from('<H', raw, dcf.start_offset)[0]
                                            else:
                                                entry['material'] = struct.unpack_from('<I', raw, dcf.start_offset)[0]
                                            entry['offsets']['material'] = (dcf.start_offset, sz)
                                        elif dcf.name == '_grimeOpacity':
                                            entry['grime'] = raw[dcf.start_offset]
                                            entry['offsets']['grime'] = dcf.start_offset
                                    dye_entries.append(entry)

                            dye_items.append({
                                'slot': slot_idx,
                                'item_key': item_key,
                                'item_name': item_name,
                                'dye_entries': dye_entries,
                                'has_dye': len(dye_entries) > 0,
                            })

            self._dye_parsed.emit(dye_items)

        threading.Thread(target=_parse, daemon=True).start()

    def _dye_populate(self, dye_items: list) -> None:
        self._dye_items = dye_items

        self._dye_gear_table.setRowCount(len(dye_items))
        for row, item in enumerate(dye_items):
            self._dye_gear_table.setItem(row, 0, QTableWidgetItem(str(item['slot'])))

            icon_w = QTableWidgetItem()
            if self._icon_cache:
                px = self._icon_cache.get_pixmap(item['item_key'])
                if px:
                    icon_w.setIcon(QIcon(px))
            self._dye_gear_table.setItem(row, 1, icon_w)

            name_w = QTableWidgetItem(item['item_name'])
            if item['has_dye']:
                name_w.setForeground(QBrush(QColor(COLORS['accent'])))
            self._dye_gear_table.setItem(row, 2, name_w)
            self._dye_gear_table.setItem(row, 3, QTableWidgetItem(str(len(item['dye_entries']))))
            dyed_w = QTableWidgetItem("Yes" if item['has_dye'] else "-")
            if item['has_dye']:
                dyed_w.setForeground(QBrush(QColor(COLORS['success'])))
            self._dye_gear_table.setItem(row, 4, dyed_w)

        dyed_count = sum(1 for i in dye_items if i['has_dye'])
        self._dye_status.setText(
            f"Loaded {len(dye_items)} equipped items, {dyed_count} with dye data"
        )
        self._dye_auto_learn_slots()


    def _dye_gear_selected(self) -> None:
        rows = self._dye_gear_table.selectionModel().selectedRows()
        if not rows:
            return
        row = rows[0].row()
        if row >= len(self._dye_items):
            return

        self._dye_current_item = self._dye_items[row]
        self._dye_current_part = None
        self._dye_refresh_parts()


    def _dye_refresh_parts(self) -> None:
        if not self._dye_current_item:
            return
        entries = self._dye_current_item['dye_entries']

        self._dye_updating = True
        self._dye_parts_table.setRowCount(len(entries))
        for i, e in enumerate(entries):
            self._dye_parts_table.setItem(i, 0, QTableWidgetItem(str(e['slot'])))

            swatch = QTableWidgetItem()
            swatch.setBackground(QBrush(QColor(e['r'], e['g'], e['b'])))
            swatch.setText(f"#{e['r']:02X}{e['g']:02X}{e['b']:02X}")
            self._dye_parts_table.setItem(i, 1, swatch)

            self._dye_parts_table.setItem(i, 2, QTableWidgetItem(str(e['r'])))
            self._dye_parts_table.setItem(i, 3, QTableWidgetItem(str(e['g'])))
            self._dye_parts_table.setItem(i, 4, QTableWidgetItem(str(e['b'])))
            mat_name = self.DYE_MATERIALS.get(e['material'], str(e['material']))
            self._dye_parts_table.setItem(i, 5, QTableWidgetItem(mat_name))

            if self._dye_current_part and self._dye_current_part is e:
                self._dye_parts_table.selectRow(i)

        self._dye_updating = False

        if not entries:
            self._dye_status.setText(f"'{self._dye_current_item['item_name']}' — no dye data (item was never dyed in-game)")
        else:
            self._dye_status.setText(f"'{self._dye_current_item['item_name']}': {len(entries)} dye parts — click a part then pick a color")


    def _dye_part_clicked(self, row: int, col: int) -> None:
        if not self._dye_current_item:
            return
        entries = self._dye_current_item['dye_entries']
        if row >= len(entries):
            return

        self._dye_current_part = entries[row]
        self._dye_updating = True

        mat = self._dye_current_part['material']
        for i in range(self._dye_material_combo.count()):
            if self._dye_material_combo.itemData(i) == mat:
                self._dye_material_combo.setCurrentIndex(i)
                break

        self._dye_grime_spin.setValue(self._dye_current_part['grime'])

        grp = self._dye_current_part['group']
        found = False
        for i in range(self._dye_group_combo.count()):
            if self._dye_group_combo.itemData(i) == grp:
                self._dye_group_combo.setCurrentIndex(i)
                found = True
                break
        if not found:
            self._dye_group_combo.setCurrentIndex(0)

        self._dye_updating = False
        self._dye_status.setText(
            f"Part {self._dye_current_part['slot']}: "
            f"RGB({self._dye_current_part['r']},{self._dye_current_part['g']},{self._dye_current_part['b']}) "
            f"Material={self.DYE_MATERIALS.get(mat, str(mat))}"
        )


    def _dye_get_current_part_or_first(self) -> dict | None:
        if self._dye_current_part:
            return self._dye_current_part
        if self._dye_current_item and self._dye_current_item['dye_entries']:
            self._dye_current_part = self._dye_current_item['dye_entries'][0]
            return self._dye_current_part
        return None


    def _dye_find_cli(self) -> Optional[str]:
        import sys as _sys
        candidates = [
            os.path.join(os.path.dirname(os.path.abspath(_sys.argv[0])), "dye_cli.exe"),
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "dye_cli.exe"),
        ]
        if hasattr(_sys, '_MEIPASS'):
            candidates.insert(0, os.path.join(_sys._MEIPASS, "dye_cli.exe"))
        for c in candidates:
            if os.path.isfile(c):
                return c
        return None

    def _dye_reselect(self, cur_slot: int, cur_part_slot: int) -> None:
        for row, item in enumerate(self._dye_items):
            if item['slot'] == cur_slot:
                self._dye_gear_table.selectRow(row)
                self._dye_current_item = item
                self._dye_refresh_parts()
                for entry in item['dye_entries']:
                    if entry['slot'] == cur_part_slot:
                        self._dye_current_part = entry
                        break
                break

    def _dye_edit_inplace(self, p: dict, **kwargs) -> bool:
        if not self._save_data or not p.get('offsets'):
            return False

        offsets = p['offsets']

        for field, new_val in kwargs.items():
            if field in ('r', 'g', 'b', 'grime') and new_val != 0:
                if offsets.get(field) is None:
                    return False
            elif field == 'group' and new_val != 0:
                if offsets.get('group') is None:
                    return False
            elif field == 'material' and new_val != 0:
                if offsets.get('material') is None:
                    return False

        blob = self._save_data.decompressed_blob

        for field, new_val in kwargs.items():
            if field in ('r', 'g', 'b', 'a', 'grime'):
                off = offsets.get(field)
                if off is not None:
                    blob[off] = new_val & 0xFF
                p[field] = new_val
            elif field == 'slot':
                off = offsets.get('slot')
                if off is not None:
                    blob[off] = new_val & 0xFF
                p[field] = new_val
            elif field == 'group':
                off = offsets.get('group')
                if off is not None:
                    struct.pack_into('<I', blob, off, new_val)
                p[field] = new_val
            elif field == 'material':
                mat_info = offsets.get('material')
                if mat_info is not None:
                    off, sz = mat_info
                    if sz == 2:
                        struct.pack_into('<H', blob, off, new_val)
                    else:
                        struct.pack_into('<I', blob, off, new_val)
                p[field] = new_val

        self._mark_dirty()
        return True

    def _dye_rebuild_native(self, cli: str, item_key: int, entries: list,
                            cur_slot: int, cur_part_slot: int) -> bool:
        import subprocess, tempfile, sys as _sys

        self._dye_status.setText(tr("Rebuilding dye data (native)..."))
        QApplication.processEvents()

        tmp_path = None
        try:
            blob = bytes(self._save_data.decompressed_blob)
            tmp = tempfile.NamedTemporaryFile(suffix='.bin', delete=False)
            tmp_path = tmp.name
            tmp.write(blob)
            tmp.close()

            args = [cli, tmp_path, str(item_key), "edit"]
            for i, entry in enumerate(entries):
                for field in ('r', 'g', 'b', 'a', 'grime', 'group', 'material'):
                    val = entry.get(field, 0)
                    args.extend([str(i), field, str(val)])

            result = subprocess.run(
                args, capture_output=True, text=True, timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0,
            )

            if result.returncode != 0:
                msg = result.stderr.strip() if result.stderr else f"exit code {result.returncode}"
                self._dye_status.setText(f"Native rebuild failed: {msg}")
                try: os.unlink(tmp_path)
                except Exception: pass
                return False

            with open(tmp_path, 'rb') as f:
                new_blob = f.read()
            try: os.unlink(tmp_path)
            except Exception: pass

            self._save_data.decompressed_blob = bytearray(new_blob)
            self._save_data.parse_cache = None
            self._mark_dirty()

            self._dye_load()
            self._dye_reselect(cur_slot, cur_part_slot)

            msg = result.stderr.strip() if result.stderr else "OK"
            self._dye_status.setText(f"Dye rebuilt (native): {msg}")
            return True
        except Exception as e:
            log.exception("Native dye rebuild failed")
            self._dye_status.setText(f"Native rebuild error: {e}")
            if tmp_path:
                try: os.unlink(tmp_path)
                except Exception: pass
            return False

    def _dye_rebuild_python(self, item_key: int, entries: list,
                            cur_slot: int, cur_part_slot: int) -> bool:
        self._dye_status.setText(tr("Rebuilding dye data (Python fallback)..."))
        QApplication.processEvents()

        try:
            import parc_inserter3 as pi

            updated = []
            for entry in entries:
                updated.append({
                    'slot': entry.get('slot', 0),
                    'r': entry.get('r', 0),
                    'g': entry.get('g', 0),
                    'b': entry.get('b', 0),
                    'a': entry.get('a', 255),
                    'grime': entry.get('grime', 0),
                    'has_grime': 'grime' in entry.get('offsets', {}),
                    'group': entry.get('group', 0),
                    'material': entry.get('material', 0),
                    'orig_mask': entry.get('orig_mask', 0),
                })

            blob = bytearray(self._save_data.decompressed_blob)
            ok, new_blob, msg = pi.rebuild_dye_list(blob, item_key, updated)
            if ok:
                self._save_data.decompressed_blob = bytearray(new_blob)
                self._save_data.parse_cache = None
                self._mark_dirty()
                self._dye_load()
                self._dye_reselect(cur_slot, cur_part_slot)
                return True
            else:
                self._dye_status.setText(f"Rebuild failed: {msg}")
                return False
        except Exception as e:
            log.exception("Python dye rebuild failed")
            self._dye_status.setText(f"Rebuild error: {e}")
            return False

    def _dye_rebuild_list(self) -> bool:
        if not self._save_data or not self._dye_current_item:
            return False
        if not self._dye_current_item['dye_entries']:
            return False

        item_key = self._dye_current_item['item_key']
        entries = self._dye_current_item['dye_entries']

        cur_slot = self._dye_current_item['slot']
        cur_part_slot = self._dye_current_part['slot'] if self._dye_current_part else 0

        cli = self._dye_find_cli()
        if cli:
            return self._dye_rebuild_native(cli, item_key, entries, cur_slot, cur_part_slot)
        else:
            return self._dye_rebuild_python(item_key, entries, cur_slot, cur_part_slot)

    def _dye_set_color(self, r: int, g: int, b: int) -> None:
        p = self._dye_get_current_part_or_first()
        if not p:
            return

        if self._dye_edit_inplace(p, r=r, g=g, b=b, a=255):
            self._dye_refresh_parts()
            self._dye_status.setText(f"Set part {p.get('slot', '?')} to #{r:02X}{g:02X}{b:02X} — Ctrl+S to save")
            return

        p['r'] = r
        p['g'] = g
        p['b'] = b
        p['a'] = 255
        self._dye_status.setText(tr("Rebuilding dye data (mask change)..."))
        QApplication.processEvents()
        if self._dye_rebuild_list():
            self._dye_status.setText(f"Set part {p.get('slot', '?')} to #{r:02X}{g:02X}{b:02X} — Ctrl+S to save")
        else:
            self._dye_status.setText(tr("Failed to set color"))


    def _dye_pick_color(self) -> None:
        p = self._dye_get_current_part_or_first()
        if not p:
            self._dye_status.setText(tr("No dye parts on this item"))
            return
        from PySide6.QtWidgets import QColorDialog
        current = QColor(p['r'], p['g'], p['b'])
        color = QColorDialog.getColor(current, self, "Pick Dye Color")
        if not color.isValid():
            return
        self._dye_set_color(color.red(), color.green(), color.blue())


    def _dye_apply_quick_color(self, r: int, g: int, b: int) -> None:
        if not self._dye_current_item or not self._dye_current_item['dye_entries']:
            self._dye_status.setText(tr("Select an item with dye data first"))
            return
        self._dye_set_color(r, g, b)


    def _dye_material_changed(self, index: int) -> None:
        if self._dye_updating or not self._dye_current_part:
            return
        mat = self._dye_material_combo.itemData(index)
        if mat is not None:
            if not self._dye_edit_inplace(self._dye_current_part, material=mat):
                self._dye_current_part['material'] = mat
                self._dye_rebuild_list()
            self._dye_refresh_parts()


    def _dye_grime_changed(self, value: int) -> None:
        if self._dye_updating or not self._dye_current_part:
            return
        if not self._dye_edit_inplace(self._dye_current_part, grime=value):
            self._dye_current_part['grime'] = value
            self._dye_rebuild_list()


    def _dye_group_changed(self, index: int) -> None:
        if self._dye_updating or not self._dye_current_part:
            return
        grp = self._dye_group_combo.itemData(index)
        if grp is not None:
            if not self._dye_edit_inplace(self._dye_current_part, group=grp):
                self._dye_current_part['group'] = grp
                self._dye_rebuild_list()


    def _dye_add_to_item(self) -> None:
        if not self._save_data or not self._dye_current_item:
            self._dye_status.setText(tr("Select an equipped item first"))
            return
        if self._dye_current_item['has_dye']:
            self._dye_status.setText(tr("This item already has dye data — edit it directly"))
            return

        item_key = self._dye_current_item['item_key']
        item_name = self._dye_current_item['item_name']
        equip_slot = self._dye_current_item.get('slot', -1)

        try:
            import sys
            sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'Includes', 'desktopeditor'))
            from save_parser import parse_schema
            schema = parse_schema(bytes(self._save_data.decompressed_blob))
            has_dye_type = any(t.name == 'ItemDyeSaveData' for t in schema['types'])
        except Exception:
            has_dye_type = False

        if not has_dye_type:
            QMessageBox.warning(self, tr("Cannot Add Dye"),
                "ItemDyeSaveData type not found in this save.\n\n"
                "The game adds this type when you dye any item for the first time.\n"
                "Please dye any item in-game (even just apply and undo), save, then try again.")
            return

        dye_db = self._load_dye_slot_db()
        known_counts = {}
        for item in self._dye_items:
            if item['has_dye'] and item['dye_entries']:
                known_counts[item['item_key']] = len(item['dye_entries'])

        db_count = dye_db.get(str(item_key), {}).get('slots', 0)
        if db_count > 0:
            suggestion = db_count
        elif known_counts:
            suggestion = max(known_counts.values())
        else:
            suggestion = 4

        from PySide6.QtWidgets import QInputDialog
        hint = f"Dye slots for '{item_name}'?"
        if db_count > 0:
            hint += f"\n\nKnown slot count for this item: {db_count}"
        if known_counts:
            known_str = ", ".join(f"{self._name_db.get_name(k)}={v}" for k, v in known_counts.items())
            hint += f"\nFrom this save: {known_str}"
        hint += (
            "\n\nWARNING: Wrong slot count WILL crash the save!\n"
            "Safest method: dye this item once in-game first,\n"
            "then use the editor to change colors."
        )
        slots, ok = QInputDialog.getInt(self, "Add Dye", hint, suggestion, 1, 16)
        if not ok:
            return

        reply = QMessageBox.question(
            self, tr("Add Dye Data (PARC Insertion)"),
            f"Insert {slots} dye slot(s) into '{item_name}' (key={item_key})?\n\n"
            f"This modifies the save structure (PARC insertion).\n"
            f"Default color: White (no group/material — matches game's minimal dye)\n\n"
            f"A backup will be created automatically before insertion.\n"
            f"You can change colors after insertion.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        if self._backup_fn:
            try:
                self._backup_fn()
                self._dye_status.setText(tr("Backup created. Inserting dye data..."))
            except Exception:
                pass
        else:
            self._dye_status.setText(tr("Inserting dye data..."))
        QApplication.processEvents()

        try:
            import parc_inserter3 as pi
            blob = bytearray(self._save_data.decompressed_blob)
            ok, new_blob, msg = pi.insert_dye_to_item(
                blob, item_key=item_key, num_slots=slots,
                r=255, g=255, b=255, a=255,
                color_group=0xC88211F5, material=0, grime=0,
            )
            if ok:
                self._save_data.decompressed_blob = bytearray(new_blob)
                self._save_data.parse_cache = None
                self._mark_dirty()
                self._dye_load()
                self._dye_status.setText(f"Dye inserted for '{item_name}' — Ctrl+S to save")
                QMessageBox.information(self, tr("Dye Inserted"),
                    f"{msg}\n\nCtrl+S to save. Restart game to see changes.")
            else:
                QMessageBox.warning(self, tr("Insertion Failed"), msg)
                self._dye_status.setText(f"Failed: {msg}")
        except Exception as e:
            log.exception("Unhandled exception")
            QMessageBox.critical(self, tr("Error"), str(e))
            self._dye_status.setText(f"Error: {e}")


    def _dye_apply_to_all_parts(self) -> None:
        if not self._dye_current_item or not self._dye_current_item['dye_entries']:
            self._dye_status.setText(tr("Select an item with dye data first"))
            return

        src = self._dye_get_current_part_or_first()
        if not src:
            return

        entries = self._dye_current_item['dye_entries']
        for entry in entries:
            entry['r'] = src['r']
            entry['g'] = src['g']
            entry['b'] = src['b']
            entry['a'] = src['a']
            entry['material'] = src['material']
            entry['group'] = src['group']
            entry['grime'] = src['grime']

        if self._dye_rebuild_list():
            self._dye_status.setText(
                f"Applied #{src['r']:02X}{src['g']:02X}{src['b']:02X} to all {len(entries)} parts — Ctrl+S to save"
            )


    SIMPLE_MOUNT_BASE_HEX = (
        "0600051900180000370000ffffffffffffffff"
        "f53312000000000088510f00ff010000000000"
        "0001001c390000ffffffffffffffff17341200"
        "000000000100002d0000ffffffffffffffff2d"
        "34120000000000040000000100002d0000ffff"
        "ffffffffffff473412000000000004000000"
        "0100002d0000ffffffffffffffff6134120000"
        "0000000400000052000000ec9893ce00000000"
        "ec9893ce00000000010101010101018f000000"
    )

    FULL_MOUNT_BASE_HEX = (
        "06000519801f0003370000ffffffffffffffff"
        "19571200000000008e510f0096190000000000"
        "0001001c390000ffffffffffffffff3b571200"
        "000000000100002d0000ffffffffffffffff51"
        "57120000000000040000000100002d0000ffff"
        "ffffffffffff6b57120000000000040000000100002d"
        "0000ffffffffffffffff855712000000000004"
        "00000052000000"
        "94fb6ecd0000000094fb6ecd00000000"
        "01274426c6955719448cd270c5031022c0"
        "0100000001010101010101"
        "c2010000000000000000000000000000b4000000"
    )

    FULL_TEMPLATE_KEYS = set()

    MOUNT_TEMPLATES = {
        1003918: ("Silver Fang (Wolf)",
            "06000519801f0003370000ffffffffffffffff19571200000000008e510f00961900000000000001001c390000ffffffffffffffff3b571200000000000100002d0000ffffffffffffffff5157120000000000040000000100002d0000ffffffffffffffff6b57120000000000040000000100002d0000ffffffffffffffff8557120000000000040000005200000094fb6ecd0000000094fb6ecd0000000001274426c6955719448cd270c5031022c00100000001010101010101c2010000000000000000000000000000b4000000"),
        1003917: ("White Bear",
            "0600051900180000370000ffffffffffffffffe8571200000000008d510f00971900000000000001001c390000ffffffffffffffff0a581200000000000100002d0000ffffffffffffffff2058120000000000040000000100002d0000ffffffffffffffff3a58120000000000040000000100002d0000ffffffffffffffff545812000000000004000000520000009cdc79cd000000009cdc79cd00000000010101010101018f000000"),
        1003919: ("Snowwhite Deer",
            "06000519801f0003370000ffffffffffffffff92581200000000008f510f00991900000000000001001c390000ffffffffffffffffb4581200000000000100002d0000ffffffffffffffffca58120000000000040000000100002d0000ffffffffffffffffe458120000000000040000000100002d0000fffffffffffffffffe581200000000000400000052000000005399cd00000000005399cd0000000001886526c61c8719449e1f71c5adde0ac0010000000101010101010190010000000000000000000000000000b4000000"),
        1003912: ("Rock Tusk (Warthog)",
            "0600051900180000370000fffffffffffffffff53312000000000088510f00ff0100000000000001001c390000ffffffffffffffff17341200000000000100002d0000ffffffffffffffff2d34120000000000040000000100002d0000ffffffffffffffff4734120000000000040000000100002d0000ffffffffffffffff61341200000000000400000052000000ec9893ce00000000ec9893ce00000000010101010101018f000000"),
        1003915: ("Icicle Edge (Alpine Ibex)",
            "0600051900180000370000ffffffffffffffff9f341200000000008b510f00000200000000000001001c390000ffffffffffffffffc1341200000000000100002d0000ffffffffffffffffd734120000000000040000000100002d0000fffffffffffffffff134120000000000040000000100002d0000ffffffffffffffff0b351200000000000400000052000000ec9893ce00000000ec9893ce00000000010101010101018f000000"),
        1000799: ("Dragon",
            "06000519801f0003380000ffffffffffffffff"
            "591e1200000000005f450f00f90300000000000001"
            "001c3a0000ffffffffffffffff7b1e120000000000"
            "0100002e0000ffffffffffffffff911e12000000000004000000"
            "0100002e0000ffffffffffffffffab1e12000000000004000000"
            "0100002e0000ffffffffffffffffc51e12000000000004000000"
            "520000001cdd8dc3000000001cdd8dc30000000001eb7b45c6"
            "90098144e2862dc59e9c673f0100000001010101010101"
            "c40900000000000000000000000000b4000000"),
        1000523: ("Elephant", None),
        1000733: ("Camel", None),
        1000254: ("Iguana", None),
        1000363: ("Iguana 2", None),
        1000265: ("CarmaBirdsaurus", None),
        1000253: ("Cucubird", None),
        1002059: ("Cucubird 2", None),
        1001893: ("Boss Deer", None),
        1000520: ("Boss Wolf", None),
        1000270: ("Wild Bear", None),
        1003911: ("Bear 1000", None),
        1000264: ("Wild Wolf", None),
        1003913: ("Wolf 1000", None),
        1000453: ("Wild Deer", None),
        1003914: ("Deer 1000", None),
        1000491: ("Wild Warthog", None),
        29448: ("Wild Alpine Ibex", None),
        1003748: ("Sandfang Wolf", None),
        1000532: ("Machine Bear", None),
        1002269: ("Machine CarmaBirdsaurus", None),
        1001467: ("ATAG Alternative", None),
        1000981: ("BlackBear Cannon", None),
        1001358: ("IronwingRENX", None),
        1001984: ("ATAG Mech 1", None),
        1001985: ("ATAG Mech 2", None),
        1001986: ("ATAG Mech 3", None),
        1000017: ("ATAG Mech 4", None),
        1003562: ("ATAG Mech 5", None),
        1003563: ("ATAG Mech 6", None),
        1003564: ("ATAG Mech 7",
            "06000519801f02003b0000ffffffffffffffff43f41500000000002c500f00a31900000000000001001c3d0000ffffffffffffffff65f41500000000000100002e0000ffffffffffffffff7bf4150000000000040000000100002e0000ffffffffffffffff95f4150000000000040000000100002e0000ffffffffffffffffaff41500000000000400000052000000ec9893ce00000000ec9893ce00000000010101010101018f000000"),
        1002041: ("Balloon Summoner 1", None),
        1002042: ("Balloon Summoner 2", None),
        1002043: ("Balloon Summoner 3", None),
        2306: ("Marni Airship", None),
        1001929: ("Pirate Ship", None),
        1001082: ("Boat 2306", None),
        1001115: ("Boat 2307", None),
        1003568: ("Boat 2308", None),
        1003569: ("Boat 2309", None),
        1003570: ("Boat 2310", None),
        1003571: ("Middle Boat 2410", None),
        1003572: ("Middle Boat 2411", None),
        1003573: ("Middle Boat 2412", None),
        1003574: ("Middle Boat 2413", None),
        1003120: ("Kliff's Horse (Tiuta)", None),
        1001173: ("Demian's Horse (Tiuta)", None),
        1001172: ("Oongka's Horse (Tiuta)", None),
        1000343: ("Marius's Horse", None),
        2103: ("Monorail", None),
    }
