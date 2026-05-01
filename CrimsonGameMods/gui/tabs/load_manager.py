"""Load Manager tab — PAPGT / overlay diagnostic dashboard.

Lets the user see every overlay group the game will load, cross-referenced
against what actually exists on disk, what's in the shared state, and what
the PAPGT master index says.  Highlights mismatches so the user can fix
them without guessing.
"""
from __future__ import annotations

import json
import logging
import os
import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QBrush, QFont
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTreeWidget, QTreeWidgetItem, QHeaderView, QSplitter,
    QTextEdit, QGroupBox, QMessageBox, QFrame, QTabWidget,
    QApplication, QFileDialog,
)

from gui.theme import COLORS

log = logging.getLogger(__name__)

_VANILLA_RANGE = set(str(g).zfill(4) for g in range(0, 36))

_OUR_GROUPS = {
    "0058": "ItemBuffs (iteminfo)",
    "0059": "ItemBuffs (equipslot)",
    "0060": "Stores",
    "0061": "MercPets",
    "0062": "Stacker (merged items)",
    "0063": "Stacker (equipslot) / SkillTree",
    "0064": "ItemBuffs (localization)",
}

_DMM_PREFIXES = ("dmmsa", "dmmgen", "dmmequ", "dmmlang")


def _classify(group: str) -> str:
    if group in _VANILLA_RANGE:
        return "vanilla"
    if group in _OUR_GROUPS:
        return "ours"
    if group.startswith(_DMM_PREFIXES):
        return "dmm"
    return "unknown"


def _status_color(status: str) -> QColor:
    if status == "OK":
        return QColor(COLORS.get("success", "#9cc470"))
    if status.startswith("WARN"):
        return QColor(COLORS.get("warning", "#f0b040"))
    return QColor(COLORS.get("error", "#d44f40"))


class LoadManagerTab(QWidget):
    status_message = Signal(str)
    config_save_requested = Signal()

    def __init__(self, config: dict, parent=None):
        super().__init__(parent)
        self._config = config
        self._game_path = config.get("game_install_path", "")
        self._papgt_data = None
        self._pamt_cache: dict[str, dict] = {}
        self._state_data = None
        self._build_ui()

    def set_game_path(self, path: str) -> None:
        self._game_path = path or ""

    # ── UI ────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # -- header --
        hdr = QLabel(
            "<b>Load Manager</b> — diagnose PAPGT, overlays, and shared state. "
            "Click <b>Scan</b> to read the game directory."
        )
        hdr.setTextFormat(Qt.RichText)
        hdr.setWordWrap(True)
        hdr.setStyleSheet(
            f"color: {COLORS['text']}; background: {COLORS['panel']}; "
            f"padding: 8px; border: 2px solid {COLORS['accent']}; border-radius: 6px;"
        )
        root.addWidget(hdr)

        # -- toolbar --
        tb = QHBoxLayout()

        scan_btn = QPushButton("Scan")
        scan_btn.setStyleSheet(
            f"background: {COLORS['accent']}; color: #000; font-weight: bold; "
            f"padding: 6px 18px; border-radius: 4px;"
        )
        scan_btn.setToolTip("Read PAPGT + overlay directories + shared state")
        scan_btn.clicked.connect(self._scan)
        tb.addWidget(scan_btn)

        self._path_label = QLabel("")
        self._path_label.setStyleSheet(f"color: {COLORS['text_dim']};")
        tb.addWidget(self._path_label, 1)

        open_btn = QPushButton("Open Game Folder")
        open_btn.clicked.connect(self._open_game_folder)
        tb.addWidget(open_btn)

        root.addLayout(tb)

        # -- main splitter: left (group tree) + right (details) --
        splitter = QSplitter(Qt.Horizontal)

        # LEFT: group tree
        left = QWidget()
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(0, 0, 0, 0)
        left_lay.setSpacing(4)

        left_hdr = QLabel("Overlay Groups")
        left_hdr.setStyleSheet(f"font-weight: bold; color: {COLORS['accent']};")
        left_lay.addWidget(left_hdr)

        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(["Group", "Owner / Description", "Status", "PAMT Checksum"])
        self._tree.setRootIsDecorated(True)
        self._tree.setAlternatingRowColors(True)
        self._tree.setStyleSheet(
            f"QTreeWidget {{ background: {COLORS['bg']}; color: {COLORS['text']}; "
            f"border: 1px solid {COLORS['border']}; alternate-background-color: {COLORS['panel']}; }}"
            f"QTreeWidget::item:selected {{ background: {COLORS['selected']}; }}"
        )
        header = self._tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self._tree.currentItemChanged.connect(self._on_group_selected)
        left_lay.addWidget(self._tree, 1)

        # action buttons under the tree
        act_row = QHBoxLayout()

        self._btn_remove_papgt = QPushButton("Remove from PAPGT")
        self._btn_remove_papgt.setToolTip("Remove selected group's PAPGT entry (overlay dir stays)")
        self._btn_remove_papgt.setEnabled(False)
        self._btn_remove_papgt.clicked.connect(self._action_remove_papgt_entry)
        act_row.addWidget(self._btn_remove_papgt)

        self._btn_register_papgt = QPushButton("Register in PAPGT")
        self._btn_register_papgt.setToolTip("Add selected group to PAPGT using its PAMT checksum")
        self._btn_register_papgt.setEnabled(False)
        self._btn_register_papgt.clicked.connect(self._action_register_in_papgt)
        act_row.addWidget(self._btn_register_papgt)

        self._btn_delete_overlay = QPushButton("Delete Overlay Dir")
        self._btn_delete_overlay.setToolTip("Delete the overlay directory + remove from PAPGT")
        self._btn_delete_overlay.setStyleSheet("color: #d44f40;")
        self._btn_delete_overlay.setEnabled(False)
        self._btn_delete_overlay.clicked.connect(self._action_delete_overlay)
        act_row.addWidget(self._btn_delete_overlay)

        act_row.addStretch()
        left_lay.addLayout(act_row)

        splitter.addWidget(left)

        # RIGHT: detail tabs
        right = QWidget()
        right_lay = QVBoxLayout(right)
        right_lay.setContentsMargins(0, 0, 0, 0)
        right_lay.setSpacing(4)

        self._detail_tabs = QTabWidget()
        self._detail_tabs.setTabPosition(QTabWidget.South)

        # -- PAMT contents tab --
        self._pamt_tree = QTreeWidget()
        self._pamt_tree.setHeaderLabels(["Path / File", "Compressed", "Uncompressed", "Compression", "Crypto"])
        self._pamt_tree.setRootIsDecorated(True)
        self._pamt_tree.setAlternatingRowColors(True)
        self._pamt_tree.setStyleSheet(
            f"QTreeWidget {{ background: {COLORS['bg']}; color: {COLORS['text']}; "
            f"border: 1px solid {COLORS['border']}; alternate-background-color: {COLORS['panel']}; }}"
        )
        pamt_header = self._pamt_tree.header()
        pamt_header.setSectionResizeMode(0, QHeaderView.Stretch)
        for col in range(1, 5):
            pamt_header.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        self._detail_tabs.addTab(self._pamt_tree, "PAMT Contents")

        # -- raw JSON tab --
        self._json_edit = QTextEdit()
        self._json_edit.setReadOnly(True)
        self._json_edit.setStyleSheet(
            f"background: {COLORS['bg']}; color: {COLORS['text']}; "
            f"border: 1px solid {COLORS['border']}; font-family: Consolas, monospace; font-size: 12px;"
        )
        self._detail_tabs.addTab(self._json_edit, "PAPGT JSON")

        # -- state JSON tab --
        self._state_edit = QTextEdit()
        self._state_edit.setReadOnly(True)
        self._state_edit.setStyleSheet(
            f"background: {COLORS['bg']}; color: {COLORS['text']}; "
            f"border: 1px solid {COLORS['border']}; font-family: Consolas, monospace; font-size: 12px;"
        )
        self._detail_tabs.addTab(self._state_edit, "Shared State")

        right_lay.addWidget(self._detail_tabs, 1)

        splitter.addWidget(right)
        splitter.setSizes([420, 580])

        root.addWidget(splitter, 1)

        # -- diagnostics bar --
        diag_box = QGroupBox("Diagnostics")
        diag_box.setStyleSheet(
            f"QGroupBox {{ font-weight: bold; color: {COLORS['accent']}; "
            f"border: 1px solid {COLORS['border']}; margin-top: 6px; padding-top: 14px; }}"
        )
        diag_lay = QVBoxLayout(diag_box)
        self._diag_label = QLabel("Click Scan to run diagnostics.")
        self._diag_label.setWordWrap(True)
        self._diag_label.setTextFormat(Qt.RichText)
        self._diag_label.setStyleSheet(f"color: {COLORS['text']}; padding: 4px;")
        diag_lay.addWidget(self._diag_label)
        root.addWidget(diag_box)

    # ── Scan ──────────────────────────────────────────────────────────

    def _scan(self) -> None:
        gp = self._game_path or self._config.get("game_install_path", "")
        if not gp or not os.path.isdir(gp):
            QMessageBox.warning(self, "Load Manager",
                                "Set the game install path first (Game Mods tab).")
            return

        self._path_label.setText(gp)
        self._papgt_data = None
        self._pamt_cache.clear()
        self._state_data = None

        try:
            import crimson_rs
        except ImportError:
            QMessageBox.critical(self, "Load Manager",
                                 "crimson_rs module not available.")
            return

        # 1. Parse PAPGT
        papgt_path = os.path.join(gp, "meta", "0.papgt")
        if os.path.isfile(papgt_path):
            try:
                self._papgt_data = crimson_rs.parse_papgt_file(papgt_path)
            except Exception as e:
                self._papgt_data = None
                log.warning("PAPGT parse failed: %s", e)

        # 2. Load shared state
        try:
            from shared_state import load_state
            st = load_state(gp)
            self._state_data = st
        except Exception as e:
            log.warning("State load failed: %s", e)

        # 3. Build unified group list
        groups = self._collect_groups(gp)

        # 4. Parse PAMT for each non-vanilla group that has one
        for g in groups:
            if g["class"] == "vanilla":
                continue
            pamt_path = os.path.join(gp, g["name"], "0.pamt")
            if os.path.isfile(pamt_path):
                try:
                    self._pamt_cache[g["name"]] = crimson_rs.parse_pamt_file(pamt_path)
                except Exception:
                    pass

        # 5. Populate tree
        self._populate_tree(groups)

        # 6. Run diagnostics
        self._run_diagnostics(gp, groups)

        # 7. Show PAPGT JSON
        if self._papgt_data:
            self._json_edit.setPlainText(
                json.dumps(self._papgt_data, indent=2, default=str)
            )
        else:
            self._json_edit.setPlainText("(PAPGT not found or parse failed)")

        # 8. Show state JSON
        if self._state_data:
            from dataclasses import asdict
            self._state_edit.setPlainText(
                json.dumps(asdict(self._state_data), indent=2, default=str)
            )
        else:
            self._state_edit.setPlainText("(No shared state found)")

        self.status_message.emit(f"Load Manager: scanned {len(groups)} groups")

    def _collect_groups(self, gp: str) -> list[dict]:
        """Build a unified list of all groups from PAPGT + disk + state."""
        seen = {}

        # From PAPGT entries
        if self._papgt_data:
            for entry in self._papgt_data.get("entries", []):
                name = entry.get("group_name", "")
                if not name:
                    continue
                seen[name] = {
                    "name": name,
                    "class": _classify(name),
                    "in_papgt": True,
                    "papgt_checksum": entry.get("pack_meta_checksum", 0),
                    "papgt_optional": entry.get("is_optional", 0),
                    "papgt_language": entry.get("language", 0),
                    "dir_exists": False,
                    "has_paz": False,
                    "has_pamt": False,
                    "state_owner": "",
                    "state_content": "",
                    "state_files": [],
                }

        # From disk directories
        try:
            for entry in sorted(os.listdir(gp)):
                entry_path = os.path.join(gp, entry)
                if not os.path.isdir(entry_path):
                    continue
                has_paz = os.path.isfile(os.path.join(entry_path, "0.paz"))
                has_pamt = os.path.isfile(os.path.join(entry_path, "0.pamt"))
                if not has_paz and not has_pamt:
                    continue

                if entry in seen:
                    seen[entry]["dir_exists"] = True
                    seen[entry]["has_paz"] = has_paz
                    seen[entry]["has_pamt"] = has_pamt
                else:
                    seen[entry] = {
                        "name": entry,
                        "class": _classify(entry),
                        "in_papgt": False,
                        "papgt_checksum": 0,
                        "papgt_optional": 0,
                        "papgt_language": 0,
                        "dir_exists": True,
                        "has_paz": has_paz,
                        "has_pamt": has_pamt,
                        "state_owner": "",
                        "state_content": "",
                        "state_files": [],
                    }
        except OSError:
            pass

        # Enrich with shared state
        if self._state_data:
            for group_name, ov in self._state_data.overlays.items():
                if group_name in seen:
                    seen[group_name]["state_owner"] = ov.owner
                    seen[group_name]["state_content"] = ov.content
                    seen[group_name]["state_files"] = list(ov.files)

        # Sort: vanilla first (by number), then mod groups
        def _sort_key(g):
            try:
                return (0, int(g["name"]))
            except ValueError:
                return (1, g["name"])

        return sorted(seen.values(), key=_sort_key)

    def _populate_tree(self, groups: list[dict]) -> None:
        self._tree.clear()

        # Create category parents
        vanilla_parent = QTreeWidgetItem(self._tree, ["Vanilla (0000-0035)", "", "", ""])
        vanilla_parent.setFlags(vanilla_parent.flags() & ~Qt.ItemIsSelectable)
        vanilla_parent.setForeground(0, QBrush(QColor(COLORS["text_dim"])))
        vanilla_parent.setFont(0, QFont("", -1, QFont.Bold))

        mod_parent = QTreeWidgetItem(self._tree, ["Mod Overlays", "", "", ""])
        mod_parent.setFlags(mod_parent.flags() & ~Qt.ItemIsSelectable)
        mod_parent.setForeground(0, QBrush(QColor(COLORS["accent"])))
        mod_parent.setFont(0, QFont("", -1, QFont.Bold))

        vanilla_count = 0
        mod_count = 0

        for g in groups:
            status, status_detail = self._compute_status(g)
            owner = self._describe_owner(g)
            cksum_str = f"0x{g['papgt_checksum']:08X}" if g["in_papgt"] else ""

            if g["class"] == "vanilla":
                parent = vanilla_parent
                vanilla_count += 1
            else:
                parent = mod_parent
                mod_count += 1

            item = QTreeWidgetItem(parent, [g["name"], owner, status, cksum_str])
            item.setData(0, Qt.UserRole, g)

            # color the status column + tooltip explaining the issue
            color = _status_color(status)
            item.setForeground(2, QBrush(color))
            item.setFont(2, QFont("", -1, QFont.Bold))
            if status != "OK":
                item.setToolTip(2, status_detail)
                item.setToolTip(0, status_detail)

            if g["class"] == "vanilla":
                for col in range(4):
                    item.setForeground(col, QBrush(QColor(COLORS["text_dim"])))

        vanilla_parent.setText(0, f"Vanilla ({vanilla_count} groups)")
        mod_parent.setText(0, f"Mod Overlays ({mod_count} groups)")

        vanilla_parent.setExpanded(False)
        mod_parent.setExpanded(True)

    def _compute_status(self, g: dict) -> tuple[str, str]:
        """Return (short_status, detail_text)."""
        issues = []

        if g["in_papgt"] and not g["dir_exists"]:
            issues.append("PAPGT entry but no directory on disk")
        if g["dir_exists"] and not g["in_papgt"] and g["class"] != "vanilla":
            issues.append("Directory exists but not in PAPGT — game ignores it")
        if g["dir_exists"] and not g["has_paz"]:
            issues.append("Directory exists but 0.paz missing")
        if g["dir_exists"] and not g["has_pamt"]:
            issues.append("Directory exists but 0.pamt missing")

        # Checksum mismatch
        if g["in_papgt"] and g["has_pamt"] and g["name"] in self._pamt_cache:
            pamt_cksum = self._pamt_cache[g["name"]].get("checksum", 0)
            if pamt_cksum != g["papgt_checksum"]:
                issues.append(
                    f"Checksum mismatch: PAPGT=0x{g['papgt_checksum']:08X}, "
                    f"PAMT=0x{pamt_cksum:08X}"
                )

        # State vs disk mismatch
        if g["state_owner"] and not g["dir_exists"]:
            issues.append(f"State says {g['state_owner']} owns it but dir missing")

        if not issues:
            return "OK", "Everything consistent"
        if any("PAPGT entry but no directory" in i or "Checksum mismatch" in i for i in issues):
            return "ERROR", "; ".join(issues)
        return "WARN", "; ".join(issues)

    def _describe_owner(self, g: dict) -> str:
        if g["state_owner"]:
            parts = [g["state_owner"]]
            if g["state_content"]:
                parts.append(f"({g['state_content']})")
            return " ".join(parts)
        if g["name"] in _OUR_GROUPS:
            return f"CrimsonGameMods ({_OUR_GROUPS[g['name']]})"
        if g["class"] == "dmm":
            return "DMM"
        if g["class"] == "vanilla":
            return "Game"
        return "Unknown"

    # ── Detail pane ───────────────────────────────────────────────────

    def _on_group_selected(self, current: QTreeWidgetItem, _prev) -> None:
        if current is None:
            self._btn_remove_papgt.setEnabled(False)
            self._btn_register_papgt.setEnabled(False)
            self._btn_delete_overlay.setEnabled(False)
            return

        g = current.data(0, Qt.UserRole)
        if g is None:
            self._btn_remove_papgt.setEnabled(False)
            self._btn_register_papgt.setEnabled(False)
            self._btn_delete_overlay.setEnabled(False)
            self._pamt_tree.clear()
            return

        is_vanilla = g["class"] == "vanilla"
        self._btn_remove_papgt.setEnabled(g["in_papgt"] and not is_vanilla)
        self._btn_register_papgt.setEnabled(
            not g["in_papgt"] and g["dir_exists"] and g["has_pamt"] and not is_vanilla
        )
        self._btn_delete_overlay.setEnabled(g["dir_exists"] and not is_vanilla)

        self._show_pamt_details(g["name"])

    def _show_pamt_details(self, group: str) -> None:
        self._pamt_tree.clear()

        pamt = self._pamt_cache.get(group)
        if not pamt:
            root = QTreeWidgetItem(self._pamt_tree, ["(no PAMT data)", "", "", "", ""])
            return

        # Show checksum + chunks at top
        cksum_item = QTreeWidgetItem(
            self._pamt_tree,
            [f"Checksum: 0x{pamt.get('checksum', 0):08X}", "", "", "", ""]
        )
        cksum_item.setFont(0, QFont("", -1, QFont.Bold))
        cksum_item.setForeground(0, QBrush(QColor(COLORS["accent"])))

        for chunk in pamt.get("chunks", []):
            QTreeWidgetItem(
                self._pamt_tree,
                [f"Chunk: {chunk.get('file_name', '?')}",
                 self._fmt_size(chunk.get("file_size", 0)),
                 "", "", ""]
            )

        # Show directories and files
        comp_names = {0: "None", 1: "LZ4", 2: "Zstd"}
        crypto_names = {0: "None", 1: "ChaCha20"}
        for d in pamt.get("directories", []):
            dir_item = QTreeWidgetItem(
                self._pamt_tree, [d.get("path", "(root)"), "", "", "", ""]
            )
            dir_item.setFont(0, QFont("", -1, QFont.Bold))

            for f in d.get("files", []):
                comp = comp_names.get(f.get("compression", 0), str(f.get("compression", "?")))
                crypto = crypto_names.get(f.get("crypto", 0), str(f.get("crypto", "?")))
                QTreeWidgetItem(dir_item, [
                    f.get("name", "?"),
                    self._fmt_size(f.get("compressed_size", 0)),
                    self._fmt_size(f.get("uncompressed_size", 0)),
                    comp,
                    crypto,
                ])

            dir_item.setExpanded(True)

        self._pamt_tree.expandAll()

    @staticmethod
    def _fmt_size(n: int) -> str:
        if n <= 0:
            return ""
        if n < 1024:
            return f"{n} B"
        if n < 1024 * 1024:
            return f"{n / 1024:.1f} KB"
        return f"{n / (1024 * 1024):.2f} MB"

    # ── Diagnostics ───────────────────────────────────────────────────

    def _run_diagnostics(self, gp: str, groups: list[dict]) -> None:
        issues = []

        # PAPGT-level checks
        if not self._papgt_data:
            issues.append(("ERROR", "PAPGT file not found or unreadable"))
        else:
            papgt_groups = {e["group_name"] for e in self._papgt_data.get("entries", [])}

            for g in groups:
                if g["class"] == "vanilla":
                    continue
                status, detail = self._compute_status(g)
                if status != "OK":
                    issues.append((status, f"<b>{g['name']}</b>: {detail}"))

        # State orphans
        if self._state_data:
            for gname, ov in self._state_data.overlays.items():
                found = any(g["name"] == gname for g in groups if g["dir_exists"])
                if not found:
                    issues.append(("WARN",
                                   f"<b>{gname}</b>: in shared state ({ov.owner}: {ov.content}) "
                                   f"but no directory on disk"))

        if not issues:
            self._diag_label.setText(
                f'<span style="color:{COLORS["success"]};">'
                f'All {len(groups)} groups are consistent. No issues found.</span>'
            )
            return

        lines = []
        for severity, msg in issues:
            color = COLORS["error"] if severity == "ERROR" else COLORS["warning"]
            icon = "X" if severity == "ERROR" else "!"
            lines.append(f'<span style="color:{color};">[{icon}] {msg}</span>')

        self._diag_label.setText("<br>".join(lines))

    # ── Actions ───────────────────────────────────────────────────────

    def _selected_group(self) -> dict | None:
        item = self._tree.currentItem()
        if item is None:
            return None
        return item.data(0, Qt.UserRole)

    def _action_remove_papgt_entry(self) -> None:
        g = self._selected_group()
        if not g or not g["in_papgt"]:
            return

        gp = self._game_path or self._config.get("game_install_path", "")
        reply = QMessageBox.question(
            self, "Remove PAPGT Entry",
            f"Remove <b>{g['name']}</b> from PAPGT?\n\n"
            f"The overlay directory will stay on disk but the game won't load it.",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        try:
            from overlay_coordinator import safe_papgt_remove
            msg = safe_papgt_remove(gp, g["name"])
            self.status_message.emit(msg)
            self._scan()
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))

    def _action_register_in_papgt(self) -> None:
        g = self._selected_group()
        if not g or g["in_papgt"] or not g["has_pamt"]:
            return

        gp = self._game_path or self._config.get("game_install_path", "")
        pamt = self._pamt_cache.get(g["name"])
        if not pamt:
            QMessageBox.warning(self, "Register", "No PAMT data available for this group.")
            return

        checksum = pamt.get("checksum", 0)
        reply = QMessageBox.question(
            self, "Register in PAPGT",
            f"Register <b>{g['name']}</b> in PAPGT with checksum 0x{checksum:08X}?\n\n"
            f"The game will start loading this overlay.",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        try:
            from overlay_coordinator import safe_papgt_add
            msg = safe_papgt_add(gp, g["name"], checksum)
            self.status_message.emit(msg)
            self._scan()
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))

    def _action_delete_overlay(self) -> None:
        g = self._selected_group()
        if not g or not g["dir_exists"]:
            return

        if g["class"] == "vanilla":
            QMessageBox.warning(self, "Delete", "Cannot delete vanilla groups.")
            return

        gp = self._game_path or self._config.get("game_install_path", "")
        overlay_dir = os.path.join(gp, g["name"])

        steps = []
        if g["in_papgt"]:
            steps.append("- Remove PAPGT entry (game stops loading it)")
        steps.append(f"- Delete directory: {g['name']}/")
        if g["state_owner"]:
            steps.append("- Remove from shared state")

        reply = QMessageBox.warning(
            self, "Delete Overlay",
            f"Full cleanup for <b>{g['name']}</b>:\n\n"
            + "\n".join(steps) + "\n\n"
            f"Owner: {self._describe_owner(g)}\n\n"
            f"This cannot be undone.",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        errors = []
        try:
            import shutil

            # 1. Remove from PAPGT first
            if g["in_papgt"]:
                try:
                    from overlay_coordinator import safe_papgt_remove
                    msg = safe_papgt_remove(gp, g["name"])
                    log.info("PAPGT remove: %s", msg)
                except Exception as e:
                    errors.append(f"PAPGT removal failed: {e}")

            # 2. Remove from shared state
            try:
                from overlay_coordinator import post_restore
                post_restore(gp, g["name"])
            except Exception as e:
                errors.append(f"State cleanup failed: {e}")

            # 3. Delete directory
            if os.path.isdir(overlay_dir):
                shutil.rmtree(overlay_dir)
                if os.path.isdir(overlay_dir):
                    errors.append(f"Directory still exists after delete")

        except Exception as e:
            errors.append(str(e))

        if errors:
            QMessageBox.warning(
                self, "Partial Cleanup",
                f"Overlay {g['name']} cleanup had issues:\n\n"
                + "\n".join(errors) + "\n\n"
                "Re-scan to see current state — you may need to "
                "manually remove the PAPGT entry.",
            )

        self.status_message.emit(f"Deleted overlay {g['name']}")
        self._scan()

    def _open_game_folder(self) -> None:
        gp = self._game_path or self._config.get("game_install_path", "")
        if not gp or not os.path.isdir(gp):
            QMessageBox.warning(self, "Load Manager", "Game path not set.")
            return
        os.startfile(gp)
