"""ReserveSlot tab — editor for reserveslot.pabgb (F1/F2 action wheel slots).

Each of the 27 entries controls one radial-menu slot category. The key
modding feature is _enableVehicleList on VehicleSlot entries — adding
vehicle category hashes makes additional mount types (Dragon, ATAG, etc.)
appear in the regular mount wheel.
"""
from __future__ import annotations

import logging
import os
import shutil
import sys
import tempfile

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QSpinBox,
    QCheckBox, QGroupBox, QScrollArea, QMessageBox, QFrame, QSizePolicy,
    QApplication, QComboBox, QTableWidget, QTableWidgetItem, QHeaderView,
    QAbstractItemView, QListWidget, QListWidgetItem,
)

from gui.theme import COLORS

log = logging.getLogger(__name__)


class ReserveSlotTab(QWidget):
    config_save_requested = Signal()
    status_message = Signal(str)

    def __init__(self, config: dict, game_path_getter, rebuild_papgt_fn=None, parent=None):
        super().__init__(parent)
        self._config = config
        self._get_game_path = game_path_getter
        self._rebuild_papgt_fn = rebuild_papgt_fn
        self._entries = []
        self._vanilla_pabgh = b""
        self._vanilla_pabgb = b""
        self._modified = False
        self._row_widgets: list[dict] = []
        self._build_ui()

    def set_game_path(self, path: str) -> None:
        pass

    def set_experimental_mode(self, enabled: bool) -> None:
        pass

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)

        tutorial = QLabel(
            "<b>ReserveSlot — F1/F2 Action Wheel Editor</b><br>"
            "Each row is a radial-menu slot. <b>Vehicle slots</b> have an "
            "editable mount-type list: add Dragon/ATAG/etc. to VehicleSlot "
            "to make all mounts available from the main mount wheel.<br>"
            "<b>Workflow:</b> 1. Load. 2. Edit vehicle lists or use quick presets. "
            "3. Apply to Game or Export Field JSON."
        )
        tutorial.setTextFormat(Qt.RichText)
        tutorial.setWordWrap(True)
        tutorial.setStyleSheet(
            f"color: {COLORS['text']}; background-color: {COLORS['panel']}; "
            f"padding: 8px; border: 2px solid {COLORS['accent']}; "
            f"border-radius: 6px;")
        layout.addWidget(tutorial)

        top_row = QHBoxLayout()
        load_btn = QPushButton("Load reserveslot")
        load_btn.setObjectName("accentBtn")
        load_btn.clicked.connect(self._load)
        top_row.addWidget(load_btn)

        preset_label = QLabel("Quick Presets:")
        preset_label.setStyleSheet(f"color: {COLORS['accent']}; font-weight: bold;")
        top_row.addWidget(preset_label)

        all_mounts_btn = QPushButton("All Mounts Everywhere")
        all_mounts_btn.setToolTip(
            "Add every known vehicle hash to every VehicleSlot entry.\n"
            "Dragon, ATAG, MachineBird, etc. all appear in the main mount wheel.")
        all_mounts_btn.clicked.connect(self._preset_all_mounts)
        top_row.addWidget(all_mounts_btn)

        vanilla_btn = QPushButton("Vanilla")
        vanilla_btn.setToolTip("Reset to vanilla vehicle lists.")
        vanilla_btn.clicked.connect(self._preset_vanilla)
        top_row.addWidget(vanilla_btn)

        top_row.addStretch()

        top_row.addWidget(QLabel("Overlay:"))
        self._overlay_spin = QSpinBox()
        self._overlay_spin.setRange(1, 9999)
        self._overlay_spin.setValue(self._config.get("reserveslot_overlay_dir", 66))
        self._overlay_spin.setFixedWidth(70)
        self._overlay_spin.setToolTip(
            "Overlay group number (0066 = default). Change if another mod\n"
            "already owns this slot.")
        self._overlay_spin.valueChanged.connect(
            lambda v: self._config.update({"reserveslot_overlay_dir": int(v)}))
        top_row.addWidget(self._overlay_spin)

        apply_btn = QPushButton("Apply to Game")
        apply_btn.setStyleSheet("background-color: #B71C1C; color: white; font-weight: bold;")
        apply_btn.clicked.connect(self._apply_to_game)
        top_row.addWidget(apply_btn)

        export_btn = QPushButton("Export Field JSON")
        export_btn.setStyleSheet("background-color: #00695C; color: white; font-weight: bold;")
        export_btn.setToolTip("Export edits as Format 3 field-name JSON.")
        export_btn.clicked.connect(self._export_field_json)
        top_row.addWidget(export_btn)

        restore_btn = QPushButton("Restore")
        restore_btn.setStyleSheet("background-color: #37474F; color: white; font-weight: bold;")
        restore_btn.clicked.connect(self._restore)
        top_row.addWidget(restore_btn)

        layout.addLayout(top_row)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self._rows_host = QWidget()
        self._rows_layout = QVBoxLayout(self._rows_host)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(4)
        self._rows_layout.addStretch(1)
        scroll.setWidget(self._rows_host)
        layout.addWidget(scroll, 1)

        self._status = QLabel("Click 'Load reserveslot' to begin.")
        self._status.setStyleSheet(f"color: {COLORS['text_dim']}; padding: 4px;")
        layout.addWidget(self._status)

    def _load(self) -> None:
        try:
            import reserveslot_parser as rsp
            import crimson_rs
        except Exception as e:
            QMessageBox.critical(self, "Load", f"Import failed:\n{e}")
            return

        gp = self._get_game_path()
        if not gp:
            QMessageBox.warning(self, "Load", "Set the game install path first.")
            return

        try:
            h = crimson_rs.extract_file(gp, "0008",
                "gamedata/binary__/client/bin", "reserveslot.pabgh")
            b = crimson_rs.extract_file(gp, "0008",
                "gamedata/binary__/client/bin", "reserveslot.pabgb")
        except Exception as e:
            QMessageBox.critical(self, "Load", f"Extract failed:\n{e}")
            return

        if not rsp.roundtrip_test(h, b):
            QMessageBox.warning(self, "Load",
                "Parser roundtrip mismatch — aborting. File format may have changed.")
            return

        self._vanilla_pabgh = h
        self._vanilla_pabgb = b
        self._entries = rsp.parse_all(h, b)
        self._modified = False
        self._rebuild_rows()
        self._status.setText(
            f"Loaded {len(self._entries)} slots. Edit vehicle lists and click Apply / Export.")

    def _rebuild_rows(self) -> None:
        while self._rows_layout.count() > 1:
            item = self._rows_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._row_widgets.clear()

        import reserveslot_parser as rsp

        for entry in self._entries:
            is_vehicle = entry.using_type == 1
            name = entry.name
            type_label = entry.using_type_name

            box = QGroupBox(f"{name}  (key {entry.key}, type: {type_label})")
            box.setStyleSheet(
                "QGroupBox { font-weight: bold; border: 1px solid " + COLORS['border']
                + "; margin-top: 6px; padding-top: 10px; }")

            vbox = QVBoxLayout(box)

            memo_text = entry.memo.decode("utf-8", errors="replace") if entry.memo else ""
            info_parts = [f"timeLimit={entry.time_limit}"]
            if entry.cool_time:
                info_parts.append(f"coolTime={entry.cool_time}")
            if entry.fill_data_list:
                info_parts.append(f"fillData={len(entry.fill_data_list)} entries")
            if entry.is_self_player_only:
                info_parts.append("selfPlayerOnly")
            if entry.is_blocked:
                info_parts.append("BLOCKED")
            info_str = ", ".join(info_parts)

            caption = QLabel(f"{info_str}")
            caption.setStyleSheet(
                f"color: {COLORS['text_dim']}; font-size: 11px; padding: 2px 6px;")
            caption.setWordWrap(True)
            vbox.addWidget(caption)

            widgets = {"entry": entry}

            if is_vehicle:
                veh_row = QHBoxLayout()

                veh_label = QLabel("Allowed mounts:")
                veh_label.setStyleSheet(f"color: {COLORS['accent']}; font-weight: bold;")
                veh_row.addWidget(veh_label)

                for h_val in rsp.ALL_KNOWN_VEHICLE_HASHES:
                    h_name = rsp.VEHICLE_NAMES.get(h_val, f"0x{h_val:04X}")
                    cb = QCheckBox(h_name)
                    cb.setChecked(h_val in entry.enable_vehicle_list)
                    cb.setToolTip(f"Hash: 0x{h_val:04X} ({h_val})")
                    cb.stateChanged.connect(self._mark_modified)
                    veh_row.addWidget(cb)
                    widgets[f"veh_{h_val}"] = cb

                veh_row.addStretch()
                vbox.addLayout(veh_row)

            self._row_widgets.append(widgets)
            self._rows_layout.insertWidget(self._rows_layout.count() - 1, box)

    def _mark_modified(self) -> None:
        self._modified = True
        self._status.setText("Modified — click Apply to Game or Export to write.")

    def _collect_edits(self) -> None:
        import reserveslot_parser as rsp
        for row in self._row_widgets:
            entry = row["entry"]
            if entry.using_type != 1:
                continue
            new_vehicles = []
            for h_val in rsp.ALL_KNOWN_VEHICLE_HASHES:
                cb = row.get(f"veh_{h_val}")
                if cb and cb.isChecked():
                    new_vehicles.append(h_val)
            entry.enable_vehicle_list = new_vehicles

    def _preset_all_mounts(self) -> None:
        if not self._entries:
            QMessageBox.information(self, "Preset", "Load reserveslot first.")
            return
        import reserveslot_parser as rsp
        for entry in self._entries:
            if entry.using_type == 1:
                entry.enable_vehicle_list = list(rsp.ALL_KNOWN_VEHICLE_HASHES)
        self._rebuild_rows()
        self._modified = True
        self._status.setText("All mount types added to every vehicle slot. Click Apply.")

    def _preset_vanilla(self) -> None:
        if not self._vanilla_pabgh:
            QMessageBox.information(self, "Preset", "Load reserveslot first.")
            return
        import reserveslot_parser as rsp
        self._entries = rsp.parse_all(self._vanilla_pabgh, self._vanilla_pabgb)
        self._rebuild_rows()
        self._modified = False
        self._status.setText("Reset to vanilla.")

    def _serialize(self) -> tuple[bytes, bytes]:
        import reserveslot_parser as rsp
        self._collect_edits()
        return rsp.serialize_all(self._entries)

    def get_staged_files(self) -> dict[str, bytes]:
        if not self._entries or not self._modified:
            return {}
        try:
            pabgb, pabgh = self._serialize()
            return {"reserveslot.pabgb": bytes(pabgb), "reserveslot.pabgh": bytes(pabgh)}
        except Exception:
            return {}

    def _apply_to_game(self) -> None:
        if not self._entries:
            QMessageBox.information(self, "Apply", "Load first.")
            return
        import crimson_rs

        gp = self._get_game_path()
        if not gp:
            QMessageBox.warning(self, "Apply", "Set the game install path first.")
            return

        new_h, new_b = self._serialize()
        INTERNAL_DIR = "gamedata/binary__/client/bin"
        overlay_group = f"{self._overlay_spin.value():04d}"

        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                group_dir = os.path.join(tmp_dir, overlay_group)
                builder = crimson_rs.PackGroupBuilder(
                    group_dir, crimson_rs.Compression.NONE, crimson_rs.Crypto.NONE)
                builder.add_file(INTERNAL_DIR, "reserveslot.pabgb", new_b)
                builder.add_file(INTERNAL_DIR, "reserveslot.pabgh", new_h)
                pamt_bytes = bytes(builder.finish())
                pamt_checksum = crimson_rs.parse_pamt_bytes(pamt_bytes)["checksum"]

                game_overlay = os.path.join(gp, overlay_group)
                os.makedirs(game_overlay, exist_ok=True)
                for f in os.listdir(group_dir):
                    shutil.copy2(os.path.join(group_dir, f),
                                 os.path.join(game_overlay, f))

                papgt_path = os.path.join(gp, "meta", "0.papgt")
                bak = papgt_path + ".reserveslot_bak"
                if not os.path.exists(bak) and os.path.isfile(papgt_path):
                    shutil.copy2(papgt_path, bak)

                cur = crimson_rs.parse_papgt_file(papgt_path) \
                    if os.path.isfile(papgt_path) else {"entries": []}
                cur["entries"] = [e for e in cur["entries"]
                                  if e.get("group_name") != overlay_group]
                cur = crimson_rs.add_papgt_entry(
                    cur, overlay_group, pamt_checksum,
                    is_optional=0, language=0x3FFF)
                crimson_rs.write_papgt_file(cur, papgt_path)

            try:
                from shared_state import record_overlay
                record_overlay(gp, overlay_group, "ReserveSlot",
                               ["reserveslot.pabgb", "reserveslot.pabgh"])
            except Exception:
                pass

            self._modified = False
            self._status.setText(
                f"Applied to {overlay_group}/ — restart the game to see changes.")
            QMessageBox.information(self, "Applied",
                f"Overlay deployed at {overlay_group}/.\n"
                f"Restart Crimson Desert to see the new mount wheel.\n"
                f"Click Restore to undo.")
        except PermissionError:
            QMessageBox.critical(self, "Apply",
                "Permission denied. Run the editor as Administrator.")
        except Exception as e:
            import traceback; traceback.print_exc()
            QMessageBox.critical(self, "Apply", f"Failed:\n{e}")

    def _export_field_json(self) -> None:
        if not self._entries or not self._vanilla_pabgh:
            QMessageBox.warning(self, "Export", "Load reserveslot first.")
            return
        import json
        import reserveslot_parser as rsp

        self._collect_edits()
        vanilla = rsp.parse_all(self._vanilla_pabgh, self._vanilla_pabgb)
        van_by_key = {e.key: e for e in vanilla}

        intents = []
        for entry in self._entries:
            van = van_by_key.get(entry.key)
            if not van:
                continue
            if entry.enable_vehicle_list != van.enable_vehicle_list:
                intents.append({
                    "key": entry.key,
                    "name": entry.name,
                    "field": "_enableVehicleList",
                    "value": entry.enable_vehicle_list,
                    "vanilla": van.enable_vehicle_list,
                })

        if not intents:
            QMessageBox.information(self, "Export", "No changes to export.")
            return

        from PySide6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Field JSON", "reserveslot_mod.json",
            "JSON Files (*.json)")
        if not path:
            return

        doc = {
            "format": 3,
            "table": "reserveslot",
            "tool": "CrimsonGameMods ReserveSlot Editor",
            "intents": intents,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=2, ensure_ascii=False)
        self._status.setText(f"Exported {len(intents)} change(s) to {os.path.basename(path)}")

    def _restore(self) -> None:
        gp = self._get_game_path()
        if not gp:
            return
        overlay_group = f"{self._overlay_spin.value():04d}"
        overlay = os.path.join(gp, overlay_group)
        papgt = os.path.join(gp, "meta", "0.papgt")
        bak = papgt + ".reserveslot_bak"
        try:
            if os.path.isdir(overlay):
                shutil.rmtree(overlay)
                try:
                    from overlay_coordinator import post_restore
                    post_restore(gp, overlay_group)
                except Exception:
                    pass
            if os.path.exists(bak):
                shutil.copy2(bak, papgt)
            self._status.setText(f"Restored — {overlay_group}/ overlay removed.")
            QMessageBox.information(self, "Restored",
                "ReserveSlot overlay removed. Restart game to confirm.")
        except Exception as e:
            QMessageBox.critical(self, "Restore", f"Failed:\n{e}")
