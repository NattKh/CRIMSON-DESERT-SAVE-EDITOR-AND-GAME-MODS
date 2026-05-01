from __future__ import annotations

import importlib.util
import json
import os
import shutil
import struct
import sys
import tempfile
from typing import Any, Callable, Dict, List, Optional, Tuple

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from gui.theme import COLORS
from gui.utils import make_scope_label


INTERNAL_DIR = "gamedata/binary__/client/bin"
OVERLAY_GROUP = "0062"
PABGEDITOR_PARSER = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "pabgb_parser_local.py",
)


def _pack_builder(crimson_rs, group_dir: str):
    compression = getattr(getattr(crimson_rs, "Compression", None), "NONE", 0)
    crypto = getattr(getattr(crimson_rs, "Crypto", None), "NONE", 0)
    return crimson_rs.PackGroupBuilder(group_dir, compression, crypto)


class BagSpaceTab(QWidget):
    status_message = Signal(str)
    config_save_requested = Signal()

    def __init__(
        self,
        config: dict,
        rebuild_papgt_fn: Optional[Callable[..., Any]] = None,
        parent=None,
    ):
        super().__init__(parent)
        self._config = config
        self._rebuild_papgt_fn = rebuild_papgt_fn
        self._game_path = config.get("game_install_path", "")
        self._inventory_data: Optional[bytearray] = None
        self._inventory_pabgh: Optional[bytes] = None
        self._records: List[Dict[str, Any]] = []
        self._dirty = False
        self._updating_table = False
        self._build_ui()

    @property
    def _overlay_group(self) -> str:
        if hasattr(self, '_overlay_spin'):
            return f"{self._overlay_spin.value():04d}"
        return OVERLAY_GROUP

    def set_game_path(self, path: str) -> None:
        self._game_path = path or ""

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)
        layout.addWidget(make_scope_label("game"))

        top_row = QHBoxLayout()
        load_btn = QPushButton("Load Inventory Fields")
        load_btn.setObjectName("accentBtn")
        load_btn.clicked.connect(self._load_from_game)
        top_row.addWidget(load_btn)

        vanilla_btn = QPushButton("Load Vanilla")
        vanilla_btn.clicked.connect(self._load_vanilla)
        top_row.addWidget(vanilla_btn)

        set_btn = QPushButton("Set Character 240 / 700")
        set_btn.clicked.connect(lambda: self._set_character_slots(240, 700))
        top_row.addWidget(set_btn)

        force_btn = QPushButton("Force Character 700 / 700")
        force_btn.clicked.connect(lambda: self._set_character_slots(700, 700))
        top_row.addWidget(force_btn)

        top_row.addWidget(QLabel("Mod#:"))
        self._overlay_spin = QSpinBox()
        self._overlay_spin.setRange(1, 9999)
        self._overlay_spin.setValue(self._config.get("bagspace_overlay_dir", 61))
        self._overlay_spin.setFixedWidth(70)
        self._overlay_spin.setToolTip(
            "Overlay folder number for BagSpace mods.\n"
            "Each tab should use a different number to avoid conflicts.\n"
            "Default: 0062")
        self._overlay_spin.valueChanged.connect(
            lambda v: self._config.update({"bagspace_overlay_dir": int(v)}))
        top_row.addWidget(self._overlay_spin)

        apply_btn = QPushButton("Apply to Game")
        apply_btn.setObjectName("accentBtn")
        apply_btn.clicked.connect(self._apply_to_game)
        top_row.addWidget(apply_btn)

        export_btn = QPushButton("Export as Mod")
        export_btn.clicked.connect(self._export_as_mod)
        top_row.addWidget(export_btn)

        export_field_btn = QPushButton("Export Field JSON")
        export_field_btn.setStyleSheet("background-color: #00695C; color: white; font-weight: bold;")
        export_field_btn.setToolTip(
            "Export edits as Format 3 field-name JSON.\n"
            "Uses field names — survives game updates.")
        export_field_btn.clicked.connect(self._export_field_json)
        top_row.addWidget(export_field_btn)

        import_field_btn = QPushButton("Import Field JSON")
        import_field_btn.setToolTip(
            "Import a Format 3 field-name JSON and apply its intents.")
        import_field_btn.clicked.connect(self._import_field_json)
        top_row.addWidget(import_field_btn)

        restore_btn = QPushButton("Restore")
        restore_btn.clicked.connect(self._restore_overlay)
        top_row.addWidget(restore_btn)
        top_row.addStretch()
        layout.addLayout(top_row)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setStyleSheet(f"color: {COLORS['text_dim']}; padding: 4px;")
        layout.addWidget(self._status)

        self._table = QTableWidget(0, 6)
        self._table.setHorizontalHeaderLabels(
            ["Key", "Record", "Field", "Value", "Offset", "Type"]
        )
        self._table.setAlternatingRowColors(True)
        self._table.setEditTriggers(
            QAbstractItemView.DoubleClicked
            | QAbstractItemView.EditKeyPressed
            | QAbstractItemView.SelectedClicked
        )
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setSortingEnabled(True)
        self._table.itemChanged.connect(self._on_table_item_changed)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        layout.addWidget(self._table, 1)

        if self._game_path:
            self._status.setText(f"Game path: {self._game_path}")
        else:
            self._status.setText("Set the game path in Game Patches first.")

    def _load_parser(self):
        parser_path = PABGEDITOR_PARSER
        if not os.path.isfile(parser_path):
            bundled = os.path.join(getattr(sys, '_MEIPASS', ''), 'pabgb_parser_local.py')
            if os.path.isfile(bundled):
                parser_path = bundled
            else:
                raise RuntimeError(
                    f"PABGEditor parser not found.\n"
                    f"Checked: {PABGEDITOR_PARSER}\n"
                    f"And: {bundled}")
        spec = importlib.util.spec_from_file_location(
            "pabgeditor_pabgb_parser", parser_path
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("Could not load PABGEditor parser module.")
        module = importlib.util.module_from_spec(spec)
        sys.modules.setdefault("pabgeditor_pabgb_parser", module)
        spec.loader.exec_module(module)
        return module

    def _load_from_game(self) -> None:
        self._load_group(prefer_overlay=True)

    def _load_vanilla(self) -> None:
        self._load_group(prefer_overlay=False)

    def _load_group(self, prefer_overlay: bool) -> None:
        game_path = self._game_path or self._config.get("game_install_path", "")
        if not game_path:
            QMessageBox.warning(self, "No Game Path", "Set the game install path first.")
            return

        try:
            import crimson_rs

            source_group = (
                self._overlay_group
                if prefer_overlay and self._has_bagspace_overlay(game_path)
                else "0008"
            )
            data = bytes(
                crimson_rs.extract_file(
                    game_path, source_group, INTERNAL_DIR, "inventory.pabgb"
                )
            )
            try:
                pabgh = bytes(
                    crimson_rs.extract_file(
                        game_path, source_group, INTERNAL_DIR, "inventory.pabgh"
                    )
                )
            except Exception:
                pabgh = bytes(
                    crimson_rs.extract_file(
                        game_path, "0008", INTERNAL_DIR, "inventory.pabgh"
                    )
                )

            self._inventory_data = bytearray(data)
            self._inventory_pabgh = pabgh
            self._parse_and_show(source_group)
        except Exception as e:
            QMessageBox.critical(self, "Load Failed", str(e))
            self._status.setText(f"Load failed: {e}")

    def _parse_and_show(self, source_group: str) -> None:
        if self._inventory_data is None:
            return
        parser = self._load_parser()
        self._records = parser.parse_inventory_pabgb(
            bytes(self._inventory_data), self._inventory_pabgh
        )
        self._refresh_table()
        shown = sum(1 for r in self._records if "default_slots" in r)
        self._dirty = False
        self._status.setText(
            f"Loaded {shown} slot fields from {source_group}/inventory.pabgb "
            f"using PABGEditor parser."
        )

    def _refresh_table(self) -> None:
        table = self._table
        self._updating_table = True
        table.setSortingEnabled(False)
        rows: List[Tuple[Any, ...]] = []
        for rec in self._records:
            if "default_slots" not in rec or "max_slots" not in rec:
                continue
            rows.append(
                (
                    rec["key"],
                    rec["name"],
                    "_defaultSlotCount",
                    rec["default_slots"],
                    rec["default_offset"],
                    "u16",
                )
            )
            rows.append(
                (
                    rec["key"],
                    rec["name"],
                    "_maxSlotCount",
                    rec["max_slots"],
                    rec["max_offset"],
                    "u16",
                )
            )

        table.setRowCount(len(rows))
        for row, (key, name, field, value, offset, field_type) in enumerate(rows):
            values = [
                str(key),
                str(name),
                str(field),
                str(value),
                f"0x{offset:06X}",
                str(field_type),
            ]
            for col, text in enumerate(values):
                item = QTableWidgetItem(text)
                if col in (0, 3, 4):
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if col == 3:
                    item.setData(Qt.UserRole, int(offset))
                    item.setData(Qt.UserRole + 1, str(name))
                    item.setData(Qt.UserRole + 2, str(field))
                else:
                    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                if name == "Character":
                    item.setForeground(QBrush(QColor(COLORS["accent"])))
                table.setItem(row, col, item)
        table.setSortingEnabled(True)
        self._updating_table = False

    def _on_table_item_changed(self, item: QTableWidgetItem) -> None:
        if self._updating_table or item.column() != 3:
            return
        if self._inventory_data is None:
            return

        offset = item.data(Qt.UserRole)
        record_name = item.data(Qt.UserRole + 1) or "record"
        field_name = item.data(Qt.UserRole + 2) or "field"
        if offset is None:
            return

        try:
            value = int(item.text().strip(), 0)
            if not 0 <= value <= 65535:
                raise ValueError("slot value must fit in u16")
        except Exception:
            self._updating_table = True
            try:
                old_value = struct.unpack_from("<H", self._inventory_data, int(offset))[0]
                item.setText(str(old_value))
            finally:
                self._updating_table = False
            self._status.setText("Invalid slot value. Use a number from 0 to 65535.")
            return

        old_value = struct.unpack_from("<H", self._inventory_data, int(offset))[0]
        struct.pack_into("<H", self._inventory_data, int(offset), value)
        self._records = self._load_parser().parse_inventory_pabgb(
            bytes(self._inventory_data), self._inventory_pabgh
        )
        self._dirty = True
        self._refresh_table()
        self._status.setText(
            f"{record_name} {field_name}: {old_value} -> {value} staged."
        )
        self.status_message.emit(
            f"BagSpace staged {record_name} {field_name}: {value}"
        )

    def _set_character_slots(self, default_slots: int = 240, max_slots: int = 700) -> None:
        if self._inventory_data is None:
            self._load_from_game()
            if self._inventory_data is None:
                return

        char = next((r for r in self._records if r.get("name") == "Character"), None)
        if not char or "default_offset" not in char or "max_offset" not in char:
            QMessageBox.warning(
                self,
                "Character Record Missing",
                "Could not find Character slot offsets in inventory.pabgb.",
            )
            return

        old_default = int(char["default_slots"])
        old_max = int(char["max_slots"])
        struct.pack_into("<H", self._inventory_data, int(char["default_offset"]), default_slots)
        struct.pack_into("<H", self._inventory_data, int(char["max_offset"]), max_slots)
        self._records = self._load_parser().parse_inventory_pabgb(
            bytes(self._inventory_data), self._inventory_pabgh
        )
        self._dirty = True
        self._refresh_table()
        self._status.setText(
            f"Character bag slots staged: {old_default}/{old_max} -> "
            f"{default_slots}/{max_slots}."
        )
        self.status_message.emit(
            f"BagSpace Character slots staged: {default_slots}/{max_slots}"
        )

    def get_staged_files(self) -> dict[str, bytes]:
        if not self._dirty or self._inventory_data is None:
            return {}
        try:
            pabgb, pabgh = self._final_inventory_data()
            return {"inventory.pabgb": bytes(pabgb), "inventory.pabgh": bytes(pabgh)}
        except Exception:
            return {}

    def _final_inventory_data(self) -> Tuple[bytes, bytes]:
        if self._inventory_data is None:
            self._load_from_game()
        if self._inventory_data is None:
            raise RuntimeError("inventory.pabgb is not loaded.")
        if self._inventory_data is None:
            raise RuntimeError("inventory.pabgb is not loaded.")
        if self._inventory_pabgh is None:
            raise RuntimeError("inventory.pabgh is not loaded.")
        return bytes(self._inventory_data), bytes(self._inventory_pabgh)

    def _apply_to_game(self) -> None:
        game_path = self._game_path or self._config.get("game_install_path", "")
        if not game_path:
            QMessageBox.warning(self, "No Game Path", "Set the game install path first.")
            return

        grp = self._overlay_group
        reply = QMessageBox.question(
            self,
            "Apply BagSpace",
            "Apply the staged Character bag slot values to the game as a PAZ overlay?\n\n"
            f"Overlay group: {grp}\n"
            "Restart the game after applying.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        try:
            data, pabgh = self._final_inventory_data()
            import crimson_rs

            with tempfile.TemporaryDirectory() as tmp_dir:
                group_dir = os.path.join(tmp_dir, grp)
                os.makedirs(group_dir, exist_ok=True)
                builder = _pack_builder(crimson_rs, group_dir)
                builder.add_file(INTERNAL_DIR, "inventory.pabgb", data)
                builder.add_file(INTERNAL_DIR, "inventory.pabgh", pabgh)
                pamt_bytes = bytes(builder.finish())
                checksum = crimson_rs.parse_pamt_bytes(pamt_bytes)["checksum"]

                dst = os.path.join(game_path, grp)
                if os.path.isdir(dst):
                    shutil.rmtree(dst)
                os.makedirs(dst, exist_ok=True)
                shutil.copy2(os.path.join(group_dir, "0.paz"), os.path.join(dst, "0.paz"))
                shutil.copy2(os.path.join(group_dir, "0.pamt"), os.path.join(dst, "0.pamt"))
                with open(os.path.join(dst, ".se_bagspace"), "w", encoding="utf-8") as f:
                    f.write("Created by CrimsonGameMods BagSpace tab\n")
                    f.write(f"inventory.pabgb Character default/max: {self._character_summary()}\n")

            papgt_path = os.path.join(game_path, "meta", "0.papgt")
            papgt = crimson_rs.parse_papgt_file(papgt_path)
            papgt["entries"] = [
                e for e in papgt["entries"] if e.get("group_name") != grp
            ]
            papgt = crimson_rs.add_papgt_entry(
                papgt, grp, checksum, 0, 16383
            )
            crimson_rs.write_papgt_file(papgt, papgt_path)

            try:
                from shared_state import record_overlay
                record_overlay(game_path, grp, "BagSpace",
                               ["inventoryinfo.pabgb", "inventoryinfo.pabgh"])
            except Exception:
                pass

            self._dirty = False
            self._status.setText(f"Applied BagSpace overlay to {grp}/.")
            self.status_message.emit(f"BagSpace overlay deployed to {grp}/")
            QMessageBox.information(
                self,
                "BagSpace Applied",
                f"BagSpace overlay deployed to:\n{os.path.join(game_path, grp)}\n\n"
                "Restart the game to apply changes.",
            )
        except Exception as e:
            self._status.setText(f"Apply failed: {e}")
            QMessageBox.critical(self, "Apply Failed", str(e))

    def _export_as_mod(self) -> None:
        try:
            data, pabgh = self._final_inventory_data()
        except Exception as e:
            QMessageBox.critical(self, "Export Failed", str(e))
            return

        default_dir = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "packs")
        os.makedirs(default_dir, exist_ok=True)
        parent_dir = QFileDialog.getExistingDirectory(
            self, "Choose folder to create BagSpace mod in", default_dir
        )
        if not parent_dir:
            return

        out_path = os.path.join(parent_dir, "BagSpace_Character_240_700")
        try:
            if os.path.isdir(out_path):
                shutil.rmtree(out_path)
            files_dir = os.path.join(out_path, "files", *INTERNAL_DIR.split("/"))
            os.makedirs(files_dir, exist_ok=True)
            with open(os.path.join(files_dir, "inventory.pabgb"), "wb") as f:
                f.write(data)
            with open(os.path.join(files_dir, "inventory.pabgh"), "wb") as f:
                f.write(pabgh)

            modinfo = {
                "id": "bagspace_character_240_700",
                "name": "BagSpace Character 240/700",
                "version": "1.0.0",
                "game_version": "1.00.03",
                "author": "CrimsonGameMods",
                "description": (
                    "Sets Character inventory slots to "
                    f"{self._character_summary()}."
                ),
            }
            with open(os.path.join(out_path, "modinfo.json"), "w", encoding="utf-8") as f:
                json.dump(modinfo, f, indent=2)

            self._status.setText(f"Exported BagSpace mod to {out_path}.")
            QMessageBox.information(
                self,
                "Mod Exported",
                f"Mod exported to:\n{out_path}\n\n"
                "Contents:\n"
                "  files/gamedata/binary__/client/bin/inventory.pabgb\n"
                "  files/gamedata/binary__/client/bin/inventory.pabgh\n"
                "  modinfo.json",
            )
        except Exception as e:
            self._status.setText(f"Export failed: {e}")
            QMessageBox.critical(self, "Export Failed", str(e))

    def _restore_overlay(self) -> None:
        game_path = self._game_path or self._config.get("game_install_path", "")
        if not game_path:
            QMessageBox.warning(self, "No Game Path", "Set the game install path first.")
            return

        grp = self._overlay_group
        overlay_dir = os.path.join(game_path, grp)
        if not os.path.isdir(overlay_dir):
            QMessageBox.information(
                self,
                "Restore BagSpace",
                f"No BagSpace overlay found at:\n{overlay_dir}",
            )
            return

        reply = QMessageBox.question(
            self,
            "Restore BagSpace",
            f"Remove the BagSpace overlay folder and PAPGT entry?\n\n{overlay_dir}",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        try:
            shutil.rmtree(overlay_dir)
            try:
                from overlay_coordinator import post_restore
                post_restore(game_path, grp)
            except Exception:
                pass
            papgt_path = os.path.join(game_path, "meta", "0.papgt")
            if os.path.isfile(papgt_path):
                import crimson_rs

                papgt = crimson_rs.parse_papgt_file(papgt_path)
                before = len(papgt.get("entries", []))
                papgt["entries"] = [
                    e for e in papgt.get("entries", [])
                    if e.get("group_name") != grp
                ]
                if len(papgt["entries"]) != before:
                    crimson_rs.write_papgt_file(papgt, papgt_path)

            self._inventory_data = None
            self._inventory_pabgh = None
            self._records = []
            self._dirty = False
            self._table.setRowCount(0)
            self._status.setText("BagSpace overlay removed. Load Inventory Fields for fresh vanilla data.")
            self.status_message.emit("BagSpace overlay removed")
            QMessageBox.information(
                self,
                "BagSpace Restored",
                "BagSpace overlay removed.\n\nLoad Inventory Fields to pull fresh vanilla data.",
            )
        except Exception as e:
            self._status.setText(f"Restore failed: {e}")
            QMessageBox.critical(self, "Restore Failed", str(e))

    def _has_bagspace_overlay(self, game_path: str) -> bool:
        return os.path.isfile(os.path.join(game_path, self._overlay_group, ".se_bagspace"))

    def _character_summary(self) -> str:
        char = next((r for r in self._records if r.get("name") == "Character"), None)
        if not char or "default_slots" not in char or "max_slots" not in char:
            return "unknown"
        return f"{char['default_slots']}/{char['max_slots']}"

    def _export_field_json(self) -> None:
        if not self._records or self._inventory_data is None:
            QMessageBox.warning(self, "Export", "Load inventory data first.")
            return

        game_path = self._game_path or self._config.get("game_install_path", "")
        if not game_path:
            QMessageBox.warning(self, "Export", "Set the game install path first.")
            return

        try:
            import crimson_rs
            van_data = bytes(crimson_rs.extract_file(
                game_path, '0008', INTERNAL_DIR, 'inventory.pabgb'))
            van_pabgh = bytes(crimson_rs.extract_file(
                game_path, '0008', INTERNAL_DIR, 'inventory.pabgh'))
            parser = self._load_parser()
            vanilla_records = parser.parse_inventory_pabgb(van_data, van_pabgh)
        except Exception as e:
            QMessageBox.critical(self, "Export", f"Failed to load vanilla baseline:\n{e}")
            return

        van_by_name = {r['name']: r for r in vanilla_records if 'default_slots' in r}

        intents = []
        for rec in self._records:
            if 'default_slots' not in rec:
                continue
            name = rec['name']
            van = van_by_name.get(name)
            if not van:
                continue
            if rec['default_slots'] != van['default_slots']:
                intents.append({
                    'entry': name, 'key': rec.get('key', 0),
                    'field': 'default_slots', 'op': 'set',
                    'new': rec['default_slots'],
                })
            if rec['max_slots'] != van['max_slots']:
                intents.append({
                    'entry': name, 'key': rec.get('key', 0),
                    'field': 'max_slots', 'op': 'set',
                    'new': rec['max_slots'],
                })

        if not intents:
            QMessageBox.information(self, "Export", "No changes to export.")
            return

        path, _ = QFileDialog.getSaveFileName(
            self, "Export Field JSON", "BagSpace.field.json",
            "Field JSON (*.field.json *.json);;All Files (*)")
        if not path:
            return

        doc = {
            'modinfo': {
                'title': 'BagSpace Mod',
                'version': '1.0',
                'author': 'CrimsonGameMods BagSpace',
                'description': f'{len(intents)} field-level intent(s)',
                'note': 'Format 3 — uses field names, survives game updates',
            },
            'format': 3,
            'target': 'inventory.pabgb',
            'intents': intents,
        }
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(doc, f, indent=2, ensure_ascii=False, default=str)
        self._status.setText(
            f"Exported {len(intents)} intents to {os.path.basename(path)}")
        QMessageBox.information(self, "Export Field JSON",
            f"Exported {len(intents)} field-level intents.\n\nFile: {path}")

    def _import_field_json(self) -> None:
        if not self._records or self._inventory_data is None:
            QMessageBox.warning(self, "Import", "Load inventory data first.")
            return

        path, _ = QFileDialog.getOpenFileName(
            self, "Import Field JSON", "",
            "Field JSON (*.field.json *.json);;All Files (*)")
        if not path:
            return
        with open(path, 'r', encoding='utf-8') as f:
            doc = json.load(f)
        if doc.get('format') != 3 or not doc.get('intents'):
            QMessageBox.warning(self, "Import", "Not a valid Format 3 Field JSON file.")
            return

        rec_by_name = {r['name']: r for r in self._records if 'default_slots' in r}
        applied = skipped = 0
        for intent in doc['intents']:
            target = rec_by_name.get(intent.get('entry'))
            if not target:
                skipped += 1
                continue
            field = intent.get('field', '')
            if intent.get('op') != 'set' or field not in ('default_slots', 'max_slots'):
                skipped += 1
                continue
            val = int(intent['new'])
            offset_key = 'default_offset' if field == 'default_slots' else 'max_offset'
            struct.pack_into("<H", self._inventory_data, int(target[offset_key]), val)
            applied += 1

        if applied:
            self._records = self._load_parser().parse_inventory_pabgb(
                bytes(self._inventory_data), self._inventory_pabgh)
            self._dirty = True
            self._refresh_table()

        self._status.setText(f"Imported {applied} intents, {skipped} skipped.")
        QMessageBox.information(self, "Import Field JSON",
            f"Applied {applied} intent(s), skipped {skipped}.\n\n"
            f"Click Apply to Game to deploy.")
