"""SkillTree Editor tab — cross-character skill / moveset swapping.

Modifies skilltreeinfo.pabgb root package IDs so one character can
use another character's melee moveset. Deploys as PAZ overlay to
group 0063.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import struct
import sys
import tempfile
from typing import Callable, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QSpinBox,
    QComboBox, QFileDialog, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
    QMessageBox, QPushButton, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from gui.theme import COLORS

log = logging.getLogger(__name__)

OVERLAY_GROUP = "0063"
INTERNAL_DIR = "gamedata/binary__/client/bin"


class SkillTreeTab(QWidget):
    """Tab for viewing and swapping skill tree root packages."""

    status_message = Signal(str)
    config_save_requested = Signal()

    def __init__(
        self,
        config: dict,
        rebuild_papgt_fn: Optional[Callable[[str, str], str]] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._rebuild_papgt_fn = rebuild_papgt_fn
        self._game_path: str = ""

        # Parser state — skilltreeinfo
        self._records: list = []
        self._original_pabgh: bytes = b""
        self._original_pabgb: bytes = b""
        # Parser state — skilltreegroupinfo
        self._group_records: list = []
        self._original_grp_pabgh: bytes = b""
        self._original_grp_pabgb: bytes = b""
        self._loaded = False

        # Parser state — skillinfo (skill.pabgb stamina/cooldown editor)
        self._skill_entries: list[dict] = []
        self._skill_vanilla_entries: list[dict] = []
        self._skill_pabgh: bytes = b""
        self._skill_pabgb: bytes = b""
        self._skill_loaded = False

        self._build_ui()

    # -- public --------------------------------------------------------

    def set_game_path(self, path: str) -> None:
        self._game_path = path

    # -- UI construction -----------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)

        # --- top row: Extract + Apply + Restore ---
        top_row = QHBoxLayout()
        self._btn_extract = QPushButton("Extract from Game")
        self._btn_extract.clicked.connect(self._on_extract)
        top_row.addWidget(self._btn_extract)

        top_row.addStretch()

        self._btn_apply = QPushButton("Apply to Game")
        self._btn_apply.setStyleSheet(
            f"background-color: {COLORS['accent']}; color: white; "
            f"font-weight: bold; padding: 6px 16px;"
        )
        self._btn_apply.clicked.connect(self._on_apply)
        self._btn_apply.setEnabled(False)
        top_row.addWidget(self._btn_apply)

        # Overlay group number — configurable. Default 64 because 63 is
        # taken by Stacker's equipslotinfo overlay (applying both would
        # clobber). User can still pick 63 if they don't use Stacker.
        top_row.addWidget(QLabel("Overlay:"))
        self._overlay_spin = QSpinBox()
        self._overlay_spin.setRange(1, 9999)
        self._overlay_spin.setValue(self._config.get("skilltree_overlay_dir", 64))
        self._overlay_spin.setFixedWidth(70)
        self._overlay_spin.setToolTip(
            "Overlay group number (0064 = default). 0063 is reserved for\n"
            "Stacker's equipslotinfo — changing this avoids the clash.\n"
            "Apply writes to <game>/NNNN/; Restore removes the same NNNN/.")
        self._overlay_spin.valueChanged.connect(
            lambda v: self._config.update({"skilltree_overlay_dir": int(v)}))
        top_row.addWidget(self._overlay_spin)

        self._btn_restore = QPushButton("Restore")
        self._btn_restore.clicked.connect(self._on_restore)
        top_row.addWidget(self._btn_restore)

        root.addLayout(top_row)

        # --- preset buttons by category ---
        self._preset_btns: list[QPushButton] = []

        # Kliff row
        kliff_row = QHBoxLayout()
        kliff_row.setSpacing(4)
        lbl = QLabel("Kliff:")
        lbl.setStyleSheet(f"color: {COLORS['accent']}; font-weight: bold;")
        kliff_row.addWidget(lbl)
        kliff_row.addWidget(self._make_preset_btn(
            "Damiane Skills", "#7B1FA2",
            "Give Kliff Damiane's full skill tree + moveset.\n\n"
            "Redirects Kliff's skill tree to load Damiane's tree data.\n"
            "Kliff sees Damiane skills (Marksmanship, Rapier, Pistol, etc.)\n"
            "and uses her combat animations.\n\n"
            "Weapon trees: Sword/Shield/Bow/Spear -> Rapier/Pistol/Longsword",
            {50: 0x332D},
        ))
        kliff_row.addWidget(self._make_preset_btn(
            "Oongka Skills", "#E65100",
            "Give Kliff Oongka's full skill tree + moveset.\n\n"
            "Redirects Kliff's skill tree to load Oongka's tree data.\n"
            "Kliff sees Oongka skills (Greataxe, Blaster, Axe, etc.)\n"
            "and uses his combat animations.\n\n"
            "Weapon trees: Sword/Shield/Bow/Spear -> Greataxe/Blaster/Axe",
            {50: 0x3391},
        ))
        kliff_row.addStretch()
        root.addLayout(kliff_row)

        # Damiane row
        dami_row = QHBoxLayout()
        dami_row.setSpacing(4)
        lbl = QLabel("Damiane:")
        lbl.setStyleSheet(f"color: {COLORS['accent']}; font-weight: bold;")
        dami_row.addWidget(lbl)
        dami_row.addWidget(self._make_preset_btn(
            "Kliff Skills", "#1565C0",
            "Give Damiane Kliff's full skill tree + moveset.\n\n"
            "Redirects Damiane's skill tree to load Kliff's tree data.\n"
            "Damiane sees Kliff skills (Sword, Shield, Bow, Spear, etc.)\n"
            "and uses his combat animations.\n\n"
            "Weapon trees: Rapier/Pistol/Longsword -> Sword/Shield/Bow/Spear",
            {52: 0x32C9},
        ))
        dami_row.addWidget(self._make_preset_btn(
            "Oongka Skills", "#E65100",
            "Give Damiane Oongka's full skill tree + moveset.\n\n"
            "Redirects Damiane's skill tree to load Oongka's tree data.\n"
            "Damiane sees Oongka skills (Greataxe, Blaster, Axe, etc.)\n"
            "and uses his combat animations.\n\n"
            "Weapon trees: Rapier/Pistol/Longsword -> Greataxe/Blaster/Axe",
            {52: 0x3391},
        ))
        dami_row.addStretch()
        root.addLayout(dami_row)

        # Oongka row
        oong_row = QHBoxLayout()
        oong_row.setSpacing(4)
        lbl = QLabel("Oongka:")
        lbl.setStyleSheet(f"color: {COLORS['accent']}; font-weight: bold;")
        oong_row.addWidget(lbl)
        oong_row.addWidget(self._make_preset_btn(
            "Kliff Skills", "#1565C0",
            "Give Oongka Kliff's full skill tree + moveset.\n\n"
            "Redirects Oongka's skill tree to load Kliff's tree data.\n"
            "Oongka sees Kliff skills (Sword, Shield, Bow, Spear, etc.)\n"
            "and uses his combat animations.\n\n"
            "Weapon trees: Greataxe/Blaster/Axe -> Sword/Shield/Bow/Spear",
            {51: 0x32C9},
        ))
        oong_row.addWidget(self._make_preset_btn(
            "Damiane Skills", "#7B1FA2",
            "Give Oongka Damiane's full skill tree + moveset.\n\n"
            "Redirects Oongka's skill tree to load Damiane's tree data.\n"
            "Oongka sees Damiane skills (Marksmanship, Rapier, Pistol, etc.)\n"
            "and uses her combat animations.\n\n"
            "Weapon trees: Greataxe/Blaster/Axe -> Rapier/Pistol/Longsword",
            {51: 0x332D},
        ))
        oong_row.addStretch()
        root.addLayout(oong_row)

        # All row
        all_row = QHBoxLayout()
        all_row.setSpacing(4)
        lbl = QLabel("All:")
        lbl.setStyleSheet(f"color: {COLORS['accent']}; font-weight: bold;")
        all_row.addWidget(lbl)
        all_row.addWidget(self._make_preset_btn(
            "Share Kliff", "#1565C0",
            "All 3 characters use Kliff's full skill tree + moveset.\n\n"
            "Redirects Oongka and Damiane to load Kliff's trees.\n"
            "Everyone sees Kliff skills and uses Sword/Shield/Bow/Spear.",
            {50: 0x32C9, 51: 0x32C9, 52: 0x32C9},
        ))
        all_row.addWidget(self._make_preset_btn(
            "Share Damiane", "#7B1FA2",
            "All 3 characters use Damiane's full skill tree + moveset.\n\n"
            "Redirects Kliff and Oongka to load Damiane's trees.\n"
            "Everyone sees Damiane skills and uses Rapier/Pistol/Longsword.",
            {50: 0x332D, 51: 0x332D, 52: 0x332D},
        ))
        all_row.addWidget(self._make_preset_btn(
            "Share Oongka", "#E65100",
            "All 3 characters use Oongka's full skill tree + moveset.\n\n"
            "Redirects Kliff and Damiane to load Oongka's trees.\n"
            "Everyone sees Oongka skills and uses Greataxe/Blaster/Axe.",
            {50: 0x3391, 51: 0x3391, 52: 0x3391},
        ))
        all_row.addWidget(self._make_preset_btn(
            "Reset Vanilla", "#424242",
            "Reset all 3 characters to their original skill trees.\n\n"
            "Restores vanilla group mappings and root packages.\n"
            "Undoes any preset selection (still need to Apply to Game).",
            None,  # special: reset to vanilla
        ))
        all_row.addStretch()
        root.addLayout(all_row)

        # --- table ---
        self._table = QTableWidget()
        self._table.setColumnCount(6)
        self._table.setHorizontalHeaderLabels([
            "Key", "Name", "Character", "Category", "Size", "Melee Root",
        ])
        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setAlternatingRowColors(True)
        root.addWidget(self._table)

        # ═══════════════════════════════════════════════════════════════
        # Skill Editor — Stamina & Cooldown Mods  (skill.pabgb)
        # ═══════════════════════════════════════════════════════════════
        self._skill_group = QGroupBox("Skill Editor — Stamina & Cooldown Mods")
        self._skill_group.setStyleSheet(
            f"QGroupBox {{ font-weight: bold; color: {COLORS['accent']}; "
            f"border: 1px solid {COLORS.get('border', '#555')}; "
            f"border-radius: 4px; margin-top: 8px; padding-top: 14px; }}"
            f"QGroupBox::title {{ subcontrol-origin: margin; left: 10px; }}"
        )
        sg_layout = QVBoxLayout(self._skill_group)

        # --- buttons row ---
        skill_btn_row = QHBoxLayout()

        self._btn_skill_load = QPushButton("Load SkillInfo")
        self._btn_skill_load.setToolTip(
            "Extract skill.pabgb + skill.pabgh from the game.\n"
            "Populates the table below with all skill entries.")
        self._btn_skill_load.clicked.connect(self._on_skill_load)
        skill_btn_row.addWidget(self._btn_skill_load)

        self._btn_skill_import = QPushButton("Import Legacy Mod")
        self._btn_skill_import.setToolTip(
            "Import a CrimsonWings-style Format 2 JSON targeting skill.pabgb.\n"
            "Automatically finds the right baseline and translates byte patches\n"
            "into field-level changes.")
        self._btn_skill_import.clicked.connect(self._on_skill_import_legacy)
        self._btn_skill_import.setEnabled(False)
        skill_btn_row.addWidget(self._btn_skill_import)

        self._btn_skill_export = QPushButton("Export Field JSON")
        self._btn_skill_export.setToolTip(
            "Export current modifications as Format 3 field-name JSON.\n"
            "This format survives game updates.")
        self._btn_skill_export.clicked.connect(self._on_skill_export_json)
        self._btn_skill_export.setEnabled(False)
        skill_btn_row.addWidget(self._btn_skill_export)

        self._btn_skill_export_legacy = QPushButton("Export Legacy JSON")
        self._btn_skill_export_legacy.setToolTip(
            "Export as Format 2 byte-diff JSON (CrimsonWings style).\n"
            "Compatible with legacy mod loaders.")
        self._btn_skill_export_legacy.clicked.connect(self._on_skill_export_legacy)
        self._btn_skill_export_legacy.setEnabled(False)
        skill_btn_row.addWidget(self._btn_skill_export_legacy)

        self._btn_skill_export_mod = QPushButton("Export as Mod Folder")
        self._btn_skill_export_mod.setToolTip(
            "Export patched skill.pabgb + skill.pabgh as a mod folder\n"
            "with modinfo.json — ready to distribute.")
        self._btn_skill_export_mod.clicked.connect(self._on_skill_export_mod_folder)
        self._btn_skill_export_mod.setEnabled(False)
        skill_btn_row.addWidget(self._btn_skill_export_mod)

        skill_btn_row.addStretch()
        sg_layout.addLayout(skill_btn_row)

        # --- stamina preset row ---
        preset_row = QHBoxLayout()
        preset_row.setSpacing(4)
        preset_lbl = QLabel("Stamina Presets:")
        preset_lbl.setStyleSheet(f"color: {COLORS['accent']}; font-weight: bold;")
        preset_row.addWidget(preset_lbl)

        stamina_presets = [
            ("10%", "CrimsonWings_Stamina_10pct.json",
             "10% stamina drain — barely noticeable reduction."),
            ("25%", "CrimsonWings_Stamina_25pct.json",
             "25% stamina drain — mild reduction."),
            ("50%", "CrimsonWings_Stamina_50pct.json",
             "50% stamina drain — half drain rate."),
            ("75%", "CrimsonWings_Stamina_75pct.json",
             "75% stamina drain — significant reduction."),
            ("Infinite", "CrimsonWings_Stamina_infinite.json",
             "Infinite stamina — near-zero drain, massive recovery."),
        ]
        self._stamina_preset_files = {}
        for label, filename, tip in stamina_presets:
            btn = QPushButton(label)
            btn.setToolTip(f"Apply Stamina Preset: {tip}\n\n"
                           f"Loads SkillInfo if not loaded, imports the preset,\n"
                           f"then click Apply to Game to deploy.")
            btn.setStyleSheet(
                "QPushButton { background-color: #00695C; color: white; "
                "font-weight: bold; padding: 4px 10px; }")
            btn.clicked.connect(
                lambda _c=False, fn=filename: self._on_stamina_preset(fn))
            preset_row.addWidget(btn)
            self._stamina_preset_files[filename] = None

        preset_row.addStretch()
        sg_layout.addLayout(preset_row)

        # --- bulk skill mod buttons ---
        bulk_row = QHBoxLayout()
        bulk_row.setSpacing(4)
        bulk_lbl = QLabel("Bulk Mods:")
        bulk_lbl.setStyleSheet(f"color: {COLORS['accent']}; font-weight: bold;")
        bulk_row.addWidget(bulk_lbl)

        for label, tip, handler in [
            ("Zero Cooldown", "Set cooldown to 0 on ALL skills.",
             self._bulk_zero_cooldown),
            ("Free Skills", "Zero out all resource costs (stamina, MP, etc.).",
             self._bulk_free_skills),
        ]:
            btn = QPushButton(label)
            btn.setToolTip(tip + "\n\nLoads SkillInfo if not loaded.\nClick Apply to Game to deploy.")
            btn.setStyleSheet(
                "QPushButton { background-color: #B71C1C; color: white; "
                "font-weight: bold; padding: 4px 10px; }")
            btn.clicked.connect(handler)
            bulk_row.addWidget(btn)

        bulk_row.addStretch()
        sg_layout.addLayout(bulk_row)

        # --- skill table ---
        self._skill_table = QTableWidget()
        self._skill_table.setColumnCount(6)
        self._skill_table.setHorizontalHeaderLabels([
            "Name", "Key", "Cooltime", "MaxLevel", "BuffLevels", "Modified",
        ])
        sh = self._skill_table.horizontalHeader()
        sh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._skill_table.setEditTriggers(
            QTableWidget.EditTrigger.DoubleClicked |
            QTableWidget.EditTrigger.EditKeyPressed)
        self._skill_table.cellChanged.connect(self._on_skill_cell_changed)
        self._skill_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows)
        self._skill_table.setAlternatingRowColors(True)
        sg_layout.addWidget(self._skill_table)

        # --- skill status ---
        self._lbl_skill_status = QLabel("")
        sg_layout.addWidget(self._lbl_skill_status)

        root.addWidget(self._skill_group)

        # --- status ---
        self._lbl_status = QLabel("")
        root.addWidget(self._lbl_status)

    def _make_preset_btn(
        self, label: str, color: str, tooltip: str,
        swaps: Optional[dict[int, int]],
    ) -> QPushButton:
        """Create a styled preset button with hover tooltip."""
        btn = QPushButton(label)
        btn.setToolTip(tooltip)
        btn.setStyleSheet(
            f"background-color: {color}; color: white; "
            f"font-weight: bold; padding: 4px 10px;"
        )
        btn.setEnabled(False)
        btn.clicked.connect(
            lambda checked=False, s=swaps: self._apply_preset(s)
        )
        self._preset_btns.append(btn)
        return btn

    # -- extract -------------------------------------------------------

    def _on_extract(self) -> None:
        game_path = self._game_path or self._config.get("game_install_path", "")
        if not game_path:
            QMessageBox.warning(self, "No game path",
                                "Set the game install path in the Patches tab first.")
            return

        try:
            import crimson_rs
            dp = INTERNAL_DIR
            pabgb = crimson_rs.extract_file(game_path, "0008", dp,
                                            "skilltreeinfo.pabgb")
            pabgh = crimson_rs.extract_file(game_path, "0008", dp,
                                            "skilltreeinfo.pabgh")
            grp_gb = crimson_rs.extract_file(game_path, "0008", dp,
                                             "skilltreegroupinfo.pabgb")
            grp_gh = crimson_rs.extract_file(game_path, "0008", dp,
                                             "skilltreegroupinfo.pabgh")
        except Exception as e:
            QMessageBox.critical(self, "Extract failed", str(e))
            return

        self._original_pabgh = bytes(pabgh)
        self._original_pabgb = bytes(pabgb)
        self._original_grp_pabgh = bytes(grp_gh)
        self._original_grp_pabgb = bytes(grp_gb)

        from skilltreeinfo_parser import parse_all, parse_groups
        self._records = parse_all(self._original_pabgh, self._original_pabgb)
        self._group_records = parse_groups(
            self._original_grp_pabgh, self._original_grp_pabgb
        )
        self._loaded = True
        self._populate_table()
        for btn in self._preset_btns:
            btn.setEnabled(True)
        self._btn_apply.setEnabled(True)
        self.status_message.emit(
            f"Loaded {len(self._records)} skill tree entries "
            f"({len(self._original_pabgb)} bytes)"
        )

    def _populate_table(self) -> None:
        from skilltreeinfo_parser import ROOT_PACKAGES, CHAR_MELEE_ROOT

        self._table.setRowCount(len(self._records))
        self._root_combos: dict[int, QComboBox] = {}

        pkg_labels = {v: k for k, v in ROOT_PACKAGES.items()}

        for row, rec in enumerate(self._records):
            # Key
            item_key = QTableWidgetItem(str(rec.key))
            item_key.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._table.setItem(row, 0, item_key)

            # Name -- display localized name with internal name in tooltip
            item_name = QTableWidgetItem(rec.display_name)
            item_name.setToolTip(rec.name)
            self._table.setItem(row, 1, item_name)

            # Character
            item_char = QTableWidgetItem(rec.character)
            item_char.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._table.setItem(row, 2, item_char)

            # Category
            item_cat = QTableWidgetItem(rec.category)
            item_cat.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._table.setItem(row, 3, item_cat)

            # Size
            item_size = QTableWidgetItem(f"{len(rec.to_bytes())}B")
            item_size.setTextAlignment(Qt.AlignmentFlag.AlignRight |
                                       Qt.AlignmentFlag.AlignVCenter)
            self._table.setItem(row, 4, item_size)

            # Melee Root -- combo for main trees, text for others
            pkgs = rec.find_root_packages()
            if rec.is_main_tree and pkgs:
                combo = QComboBox()
                for label, pkg_id in ROOT_PACKAGES.items():
                    combo.addItem(f"{label} (0x{pkg_id:04X})", pkg_id)
                # Set current to whatever the record has
                current_root = pkgs[0][1]
                for i in range(combo.count()):
                    if combo.itemData(i) == current_root:
                        combo.setCurrentIndex(i)
                        break
                self._table.setCellWidget(row, 5, combo)
                self._root_combos[rec.key] = combo
            elif pkgs:
                labels = [f"{pkg_labels.get(v, '?')} @0x{o:X}" for o, v in pkgs]
                self._table.setItem(row, 5,
                                    QTableWidgetItem("; ".join(labels)))
            else:
                self._table.setItem(row, 5, QTableWidgetItem("--"))

        self._table.resizeColumnsToContents()
        # Re-stretch name and root columns
        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)

    # -- presets -------------------------------------------------------

    def _apply_preset(self, swaps: Optional[dict[int, int]]) -> None:
        """Apply a preset by updating the table combo boxes."""
        if not self._loaded:
            return
        from skilltreeinfo_parser import CHAR_MELEE_ROOT

        if swaps is None:
            # Reset to vanilla
            swaps = dict(CHAR_MELEE_ROOT)

        for key, new_root in swaps.items():
            if key in self._root_combos:
                combo = self._root_combos[key]
                for i in range(combo.count()):
                    if combo.itemData(i) == new_root:
                        combo.setCurrentIndex(i)
                        break

    # -- apply to game -------------------------------------------------

    def _on_apply(self) -> None:
        if not self._loaded and not self._skill_loaded:
            QMessageBox.warning(self, "Not loaded",
                                "Extract skill tree data or load SkillInfo first.")
            return

        game_path = self._game_path or self._config.get("game_install_path", "")
        if not game_path:
            QMessageBox.warning(self, "No game path",
                                "Set the game install path first.")
            return

        try:
            self._apply_to_game(game_path)
        except Exception as e:
            log.exception("SkillTree apply failed")
            QMessageBox.critical(self, "Apply failed", str(e))

    def get_staged_files(self) -> dict[str, bytes]:
        if not self._loaded or not self._original_pabgh:
            return {}
        try:
            from skilltreeinfo_parser import (
                CHAR_MELEE_ROOT, parse_all, serialize_all,
                parse_groups, serialize_groups,
            )
            records = parse_all(self._original_pabgh, self._original_pabgb)
            groups = parse_groups(self._original_grp_pabgh, self._original_grp_pabgb)
            any_change = False
            root_combos = getattr(self, '_root_combos', {})
            for rec in records:
                if rec.key not in root_combos:
                    continue
                combo = root_combos[rec.key]
                new_root = combo.currentData()
                native_root = CHAR_MELEE_ROOT.get(rec.key)
                if native_root is not None and new_root != native_root:
                    rec.patch_root_package(native_root, new_root)
                    any_change = True
            if not any_change:
                return {}
            result = {}
            pabgh, pabgb = serialize_all(records)
            grp_gh, grp_gb = serialize_groups(groups)
            result["skilltreeinfo.pabgb"] = bytes(pabgb)
            result["skilltreeinfo.pabgh"] = bytes(pabgh)
            result["skilltreegroupinfo.pabgb"] = bytes(grp_gb)
            result["skilltreegroupinfo.pabgh"] = bytes(grp_gh)
            return result
        except Exception:
            return {}

    def _apply_to_game(self, game_path: str) -> None:
        import crimson_rs
        from skilltreeinfo_parser import (
            CHAR_MELEE_ROOT, VANILLA_GROUP_KEYS,
            parse_all, serialize_all, parse_groups, serialize_groups,
        )

        any_change = False
        changes: list[str] = []
        records = []
        groups = []

        # Re-parse from originals to get clean state (only if skilltree was loaded)
        if self._loaded and self._original_pabgh:
            records = parse_all(self._original_pabgh, self._original_pabgb)
            groups = parse_groups(self._original_grp_pabgh, self._original_grp_pabgb)

        # --- Apply root package combo selections (skilltreeinfo) ---
        root_combos = getattr(self, '_root_combos', {})
        for rec in records:
            if rec.key not in root_combos:
                continue
            combo = root_combos[rec.key]
            new_root = combo.currentData()
            native_root = CHAR_MELEE_ROOT.get(rec.key)
            if native_root is None:
                continue
            if new_root != native_root:
                count = rec.patch_root_package(native_root, new_root)
                if count > 0:
                    any_change = True
                    changes.append(
                        f"{rec.name}: root 0x{native_root:04X} -> "
                        f"0x{new_root:04X} ({count} refs)"
                    )

        # --- Apply group key redirects (skilltreegroupinfo) ---
        # Detect which main tree combos point to a different character
        # and redirect the corresponding group
        main_key_to_char = {50: "Kliff", 51: "Oongka", 52: "Damiane"}
        char_to_main_key = {"Kliff": 50, "Oongka": 51, "Damiane": 52}
        char_to_weapon_keys = {
            "Kliff": [1, 2, 3, 4],
            "Oongka": [11, 12, 13],
            "Damiane": [21, 22, 23],
        }
        char_to_main_grp = {
            "Kliff": 1000000, "Oongka": 1000001, "Damiane": 1000002,
        }
        char_to_wpn_grp = {
            "Kliff": 1000007, "Oongka": 1000011, "Damiane": 1000014,
        }

        for rec_key, combo in root_combos.items():
            new_root = combo.currentData()
            native_root = CHAR_MELEE_ROOT.get(rec_key)
            if native_root is None or new_root == native_root:
                continue

            # Figure out which character this record belongs to and
            # which character's tree we're swapping in
            owner_char = main_key_to_char.get(rec_key)
            source_char = None
            for ch, root in CHAR_MELEE_ROOT.items():
                if root == new_root:
                    source_char = main_key_to_char.get(ch)
                    break
            if not owner_char or not source_char:
                continue

            # Redirect main skill group
            main_grp_key = char_to_main_grp[owner_char]
            source_main_key = char_to_main_key[source_char]
            for grp in groups:
                if grp.key == main_grp_key:
                    vanilla = VANILLA_GROUP_KEYS.get(main_grp_key, grp.tree_keys)
                    if grp.tree_keys != [source_main_key]:
                        grp.tree_keys = [source_main_key]
                        any_change = True
                        changes.append(
                            f"{grp.name}: tree keys "
                            f"{vanilla} -> [{source_main_key}]"
                        )
                    break

            # Redirect weapon skill group
            wpn_grp_key = char_to_wpn_grp[owner_char]
            source_wpn_keys = char_to_weapon_keys[source_char]
            for grp in groups:
                if grp.key == wpn_grp_key:
                    vanilla = VANILLA_GROUP_KEYS.get(wpn_grp_key, grp.tree_keys)
                    if grp.tree_keys != source_wpn_keys:
                        grp.tree_keys = list(source_wpn_keys)
                        any_change = True
                        changes.append(
                            f"{grp.name}: tree keys "
                            f"{vanilla} -> {source_wpn_keys}"
                        )
                    break

        # Check if we have skill.pabgb edits too
        has_skill_edits = self._has_skill_modifications()

        if not any_change and not has_skill_edits:
            QMessageBox.information(self, "No changes",
                                    "All trees are at their vanilla values and\n"
                                    "no skill edits are pending.\n"
                                    "Nothing to deploy.")
            return

        overlay_group = f"{self._overlay_spin.value():04d}"

        # Build overlay with PackGroupBuilder(NONE)
        with tempfile.TemporaryDirectory() as tmp_dir:
            group_dir = os.path.join(tmp_dir, overlay_group)
            os.makedirs(group_dir, exist_ok=True)

            builder = crimson_rs.PackGroupBuilder(
                group_dir,
                crimson_rs.Compression.NONE,
                crimson_rs.Crypto.NONE,
            )

            # Pack skilltreeinfo + skilltreegroupinfo if tree swaps changed
            if any_change:
                new_pabgh, new_pabgb = serialize_all(records)
                new_grp_gh, new_grp_gb = serialize_groups(groups)
                builder.add_file(INTERNAL_DIR, "skilltreeinfo.pabgb", new_pabgb)
                builder.add_file(INTERNAL_DIR, "skilltreeinfo.pabgh", new_pabgh)
                builder.add_file(INTERNAL_DIR, "skilltreegroupinfo.pabgb", new_grp_gb)
                builder.add_file(INTERNAL_DIR, "skilltreegroupinfo.pabgh", new_grp_gh)

            # Pack skill.pabgb + skill.pabgh if skill edits are active
            if has_skill_edits:
                import skillinfo_parser as sip
                skill_pabgh, skill_pabgb = sip.serialize_all(self._skill_entries)
                builder.add_file(INTERNAL_DIR, "skill.pabgb", skill_pabgb)
                builder.add_file(INTERNAL_DIR, "skill.pabgh", skill_pabgh)
                mod_count = self._count_skill_modifications()
                changes.append(f"skill.pabgb: {mod_count} skill(s) modified")

            pamt_bytes = bytes(builder.finish())

            # Get PAMT self-reported checksum
            pamt_checksum = crimson_rs.parse_pamt_bytes(pamt_bytes)[
                "checksum"
            ]

            # Deploy files to game directory
            game_mod = os.path.join(game_path, overlay_group)
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

        # Update PAPGT -- read CURRENT, dedupe, add our entry
        papgt_path = os.path.join(game_path, "meta", "0.papgt")
        papgt = crimson_rs.parse_papgt_file(papgt_path)
        papgt["entries"] = [
            e for e in papgt["entries"]
            if e.get("group_name") != overlay_group
        ]
        papgt = crimson_rs.add_papgt_entry(
            papgt, overlay_group, pamt_checksum, 0, 16383
        )
        crimson_rs.write_papgt_file(papgt, papgt_path)

        try:
            from shared_state import record_overlay
            overlay_files = []
            if any_change:
                overlay_files.extend([
                    "skilltreeinfo.pabgb", "skilltreeinfo.pabgh",
                    "skilltreegroupinfo.pabgb", "skilltreegroupinfo.pabgh",
                ])
            if has_skill_edits:
                overlay_files.extend(["skill.pabgb", "skill.pabgh"])
            record_overlay(game_path, overlay_group, "SkillTree swaps",
                           overlay_files)
        except Exception:
            pass

        # Write marker file
        with open(os.path.join(game_mod, ".se_skilltree"), "w") as f:
            f.write("Created by CrimsonGameMods SkillTree tab\n")
            for c in changes:
                f.write(f"  {c}\n")

        summary = "\n".join(changes)
        self._lbl_status.setText(f"Deployed to {overlay_group}/")
        self.status_message.emit(
            f"SkillTree overlay deployed to {overlay_group}/ "
            f"({len(changes)} swap(s))"
        )
        QMessageBox.information(
            self, "Deployed",
            f"Skill tree overlay deployed to {overlay_group}/\n\n"
            f"{summary}\n\n"
            f"Restart the game to apply changes.",
        )

    # -- restore -------------------------------------------------------

    def _on_restore(self) -> None:
        game_path = self._game_path or self._config.get("game_install_path", "")
        if not game_path:
            QMessageBox.warning(self, "No game path",
                                "Set the game install path first.")
            return

        overlay_group = f"{self._overlay_spin.value():04d}"
        game_mod = os.path.join(game_path, overlay_group)
        if not os.path.isdir(game_mod):
            QMessageBox.information(self, "Nothing to restore",
                                    f"No {overlay_group}/ overlay found.")
            return

        try:
            # Remove PAPGT entry first
            if self._rebuild_papgt_fn:
                msg = self._rebuild_papgt_fn(game_path, overlay_group)
                log.info("PAPGT restore: %s", msg)

            # Remove overlay directory
            shutil.rmtree(game_mod)
            try:
                from overlay_coordinator import post_restore
                post_restore(game_path, overlay_group)
            except Exception:
                pass

            self._lbl_status.setText("Restored -- overlay removed")
            self.status_message.emit(
                f"SkillTree overlay {overlay_group}/ removed"
            )
            QMessageBox.information(
                self, "Restored",
                f"Removed {overlay_group}/ overlay.\n"
                f"Restart the game to revert to vanilla skill trees.",
            )
        except Exception as e:
            log.exception("SkillTree restore failed")
            QMessageBox.critical(self, "Restore failed", str(e))

    # ══════════════════════════════════════════════════════════════════
    # Skill Editor — skill.pabgb stamina / cooldown mods
    # ══════════════════════════════════════════════════════════════════

    def _on_skill_load(self) -> None:
        """Extract skill.pabgb + skill.pabgh from the game."""
        game_path = self._game_path or self._config.get("game_install_path", "")
        if not game_path:
            QMessageBox.warning(self, "No game path",
                                "Set the game install path in the Patches tab first.")
            return

        try:
            import crimson_rs
            dp = INTERNAL_DIR
            pabgb = bytes(crimson_rs.extract_file(game_path, "0008", dp,
                                                   "skill.pabgb"))
            pabgh = bytes(crimson_rs.extract_file(game_path, "0008", dp,
                                                   "skill.pabgh"))
        except Exception as e:
            QMessageBox.critical(self, "Extract failed", str(e))
            return

        self._skill_pabgh = pabgh
        self._skill_pabgb = pabgb

        try:
            import skillinfo_parser as sip
            self._skill_entries = sip.parse_all(pabgh, pabgb)
            # Deep copy vanilla baseline for diffing
            self._skill_vanilla_entries = sip.parse_all(pabgh, pabgb)
        except Exception as e:
            QMessageBox.critical(self, "Parse failed",
                                 f"skillinfo_parser.parse_all failed:\n{e}")
            return

        self._skill_loaded = True
        self._btn_skill_import.setEnabled(True)
        self._btn_skill_export.setEnabled(True)
        self._btn_skill_export_legacy.setEnabled(True)
        self._btn_skill_export_mod.setEnabled(True)
        self._btn_apply.setEnabled(True)
        self._populate_skill_table()
        self._lbl_skill_status.setText(
            f"Loaded {len(self._skill_entries)} skills "
            f"({len(pabgb):,} bytes)")
        self.status_message.emit(
            f"Loaded {len(self._skill_entries)} skill entries from skill.pabgb")

    def _populate_skill_table(self) -> None:
        """Fill the skill table from self._skill_entries."""
        import skillinfo_parser as sip

        entries = self._skill_entries
        self._skill_table.setRowCount(len(entries))

        self._skill_table_updating = True
        for row, e in enumerate(entries):
            # Name (read-only)
            item = QTableWidgetItem(e['name'])
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            dn = e.get('dev_skill_name', b'')
            if isinstance(dn, bytes):
                dn = dn.decode('utf-8', 'replace')
            item.setToolTip(dn if dn else e['name'])
            self._skill_table.setItem(row, 0, item)

            # Key (read-only)
            ki = QTableWidgetItem(str(e['key']))
            ki.setFlags(ki.flags() & ~Qt.ItemFlag.ItemIsEditable)
            ki.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._skill_table.setItem(row, 1, ki)

            # Cooltime (editable) — field may be named _cooltime or missing in new parser
            ct_val = e.get('_cooltime', e.get('_learnLevel', 0))
            ct = QTableWidgetItem(str(ct_val))
            ct.setTextAlignment(Qt.AlignmentFlag.AlignRight |
                                Qt.AlignmentFlag.AlignVCenter)
            self._skill_table.setItem(row, 2, ct)

            # MaxLevel (editable)
            ml = QTableWidgetItem(str(e.get('max_level', e.get('_maxLevel', 0))))
            ml.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._skill_table.setItem(row, 3, ml)

            # BuffLevels (read-only)
            bl = QTableWidgetItem(str(e.get('_buffLevelCount', 0)))
            bl.setFlags(bl.flags() & ~Qt.ItemFlag.ItemIsEditable)
            bl.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._skill_table.setItem(row, 4, bl)

            # Modified (read-only)
            mod = self._is_skill_entry_modified(row)
            mi = QTableWidgetItem("Yes" if mod else "")
            mi.setFlags(mi.flags() & ~Qt.ItemFlag.ItemIsEditable)
            if mod:
                mi.setForeground(Qt.GlobalColor.yellow)
            mi.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._skill_table.setItem(row, 5, mi)
        self._skill_table_updating = False

        self._skill_table.resizeColumnsToContents()
        sh = self._skill_table.horizontalHeader()
        sh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)

    def _on_skill_cell_changed(self, row: int, col: int) -> None:
        if getattr(self, '_skill_table_updating', False):
            return
        if not self._skill_loaded or row >= len(self._skill_entries):
            return
        e = self._skill_entries[row]
        item = self._skill_table.item(row, col)
        if not item:
            return
        try:
            val = int(item.text())
        except ValueError:
            self._lbl_skill_status.setText(f"Invalid value — must be an integer.")
            return

        if col == 2:  # Cooltime
            e['_cooltime'] = val
            e.pop('_raw', None)
        elif col == 3:  # MaxLevel
            e['max_level'] = val
            e.pop('_raw', None)
        else:
            return

        # Update Modified column
        self._skill_table_updating = True
        mod = self._is_skill_entry_modified(row)
        mi = self._skill_table.item(row, 5)
        if mi:
            mi.setText("Yes" if mod else "")
            if mod:
                mi.setForeground(Qt.GlobalColor.yellow)
        self._skill_table_updating = False
        self._lbl_skill_status.setText(
            f"{e['name']}: {'cooltime' if col == 2 else 'max_level'} = {val}")

    def _is_skill_entry_modified(self, idx: int) -> bool:
        """Check if skill entry at idx differs from vanilla."""
        if idx >= len(self._skill_vanilla_entries):
            return True
        import skillinfo_parser as sip
        cur = sip.serialize_entry(self._skill_entries[idx])
        van = sip.serialize_entry(self._skill_vanilla_entries[idx])
        return cur != van

    def _has_skill_modifications(self) -> bool:
        """Return True if any skill entry has been modified."""
        if not self._skill_loaded or not self._skill_entries:
            return False
        import skillinfo_parser as sip
        for i, e in enumerate(self._skill_entries):
            if i >= len(self._skill_vanilla_entries):
                return True
            if sip.serialize_entry(e) != sip.serialize_entry(
                    self._skill_vanilla_entries[i]):
                return True
        return False

    def _count_skill_modifications(self) -> int:
        """Count how many skill entries are modified."""
        if not self._skill_loaded:
            return 0
        import skillinfo_parser as sip
        count = 0
        for i, e in enumerate(self._skill_entries):
            if i >= len(self._skill_vanilla_entries):
                count += 1
            elif sip.serialize_entry(e) != sip.serialize_entry(
                    self._skill_vanilla_entries[i]):
                count += 1
        return count

    # -- Import Legacy Mod -----------------------------------------------

    def _on_skill_import_legacy(self, preset_path: str = None) -> None:
        """Import a CrimsonWings-style Format 2 JSON targeting skill.pabgb."""
        if not self._skill_loaded:
            QMessageBox.warning(self, "Not loaded",
                                "Load SkillInfo first.")
            return

        if preset_path:
            path = preset_path
        else:
            path, _ = QFileDialog.getOpenFileName(
                self, "Import Legacy skill.pabgb Mod", "",
                "JSON files (*.json);;All Files (*)")
        if not path:
            return

        try:
            with open(path, encoding='utf-8') as f:
                doc = json.load(f)
        except Exception as e:
            QMessageBox.critical(self, "Import Failed",
                                 f"Could not read JSON:\n{e}")
            return

        # Extract changes
        changes = []
        for p in doc.get('patches', []):
            changes.extend(p.get('changes', []))
        if not changes:
            QMessageBox.warning(self, "Import Failed",
                                "No changes found in the JSON file.")
            return

        # Find best baseline
        baselines = self._discover_skill_baselines()
        if not baselines:
            QMessageBox.critical(self, "Import Failed",
                                 "No skill.pabgb baselines found in game_baselines/.\n"
                                 "Place a vanilla skill.pabgb in game_baselines/<version>/")
            return

        best_ver, best_data, best_pabgh = self._score_skill_baselines(
            baselines, changes)
        if best_data is None:
            QMessageBox.critical(self, "Import Failed",
                                 "Could not match any baseline to this mod.")
            return

        # Apply byte patches to baseline (reverse-offset-sorted for inserts)
        patched_data = bytearray(best_data)
        applied = 0
        sorted_changes = sorted(
            changes,
            key=lambda c: self._parse_offset(c.get('offset', 0)),
            reverse=True)
        for c in sorted_changes:
            applied += _apply_one_skill_patch(patched_data, best_data, c)

        # Parse both vanilla baseline and patched with skillinfo_parser
        # Use the baseline's OWN pabgh — different game versions have
        # different entry counts and offsets.
        import skillinfo_parser as sip
        try:
            vanilla_entries = sip.parse_all(best_pabgh, best_data)
            patched_entries = sip.parse_all(best_pabgh,
                                            bytes(patched_data))
        except Exception as e:
            QMessageBox.critical(self, "Import Failed",
                                 f"Failed to parse patched data:\n{e}")
            return

        # Build name lookup for patched entries
        patched_by_name = {e['name']: e for e in patched_entries}
        vanilla_by_name = {e['name']: e for e in vanilla_entries}

        # Diff: find entries where serialized bytes differ
        modified_names = []
        for name, van_e in vanilla_by_name.items():
            pat_e = patched_by_name.get(name)
            if pat_e is None:
                continue
            if sip.serialize_entry(van_e) != sip.serialize_entry(pat_e):
                modified_names.append(name)

        if not modified_names:
            QMessageBox.information(self, "Import Complete",
                                    f"Applied {applied} patches but no skill "
                                    f"entries changed (mod may be for a different "
                                    f"game version).")
            return

        # Transfer changes to current game entries.
        # Only copy fields that ACTUALLY DIFFER between old vanilla and old
        # patched — don't blindly overwrite the whole entry, since the new
        # game version may have different structure in unchanged fields.
        current_by_name = {e['name']: i for i, e in
                           enumerate(self._skill_entries)}
        transferred = 0
        for name in modified_names:
            van_e = vanilla_by_name[name]
            pat_e = patched_by_name[name]
            idx = current_by_name.get(name)
            if idx is None:
                continue
            cur_e = self._skill_entries[idx]
            changed_fields = 0
            for field in pat_e:
                if field in ('key', 'name', 'name_len', '_raw'):
                    continue
                if pat_e.get(field) != van_e.get(field):
                    cur_e[field] = pat_e[field]
                    changed_fields += 1
            cur_e.pop('_raw', None)
            if changed_fields > 0:
                transferred += 1

        self._populate_skill_table()
        self._lbl_skill_status.setText(
            f"Imported: {transferred} skill(s) modified from {os.path.basename(path)}")
        self.status_message.emit(
            f"Imported legacy mod: {transferred}/{len(modified_names)} "
            f"skills transferred (baseline: {best_ver})")
        QMessageBox.information(
            self, "Import Complete",
            f"Baseline: {best_ver}\n"
            f"Patches applied: {applied}/{len(changes)}\n"
            f"Skills modified: {transferred}\n\n"
            f"Click 'Apply to Game' to deploy.")

    def _discover_skill_baselines(self) -> list[tuple[str, str]]:
        """Find all available skill.pabgb baselines."""
        baselines: list[tuple[str, str]] = []
        for base in [os.path.dirname(os.path.abspath(__file__)),
                     getattr(sys, '_MEIPASS', ''), os.getcwd()]:
            for rel in [os.path.join(base, '..', '..', 'game_baselines'),
                        os.path.join(base, 'game_baselines')]:
                bd = os.path.normpath(rel)
                if not os.path.isdir(bd):
                    continue
                for ver in sorted(os.listdir(bd)):
                    candidate = os.path.join(bd, ver, 'skill.pabgb')
                    if os.path.isfile(candidate):
                        baselines.append((ver, candidate))
            if baselines:
                break

        # Also consider the currently loaded vanilla as a baseline
        if self._skill_pabgb and not baselines:
            baselines.append(("current", "__loaded__"))

        return baselines

    def _score_skill_baselines(
        self, baselines: list[tuple[str, str]], changes: list[dict],
    ) -> tuple[str, bytes | None, bytes | None]:
        """Score baselines by how many 'original' hex values match.
        Returns (version, raw_pabgb, raw_pabgh) of the best match."""
        best_ver = ""
        best_score = -1
        best_data: bytes | None = None
        best_pabgh: bytes | None = None

        for ver, path in baselines:
            if path == "__loaded__":
                data = self._skill_pabgb
                pabgh = self._skill_pabgh
            else:
                try:
                    with open(path, 'rb') as f:
                        data = f.read()
                    pabgh_path = path.replace('.pabgb', '.pabgh')
                    with open(pabgh_path, 'rb') as f:
                        pabgh = f.read()
                except Exception:
                    continue

            score = 0
            for c in changes:
                off = c.get('offset')
                orig_hex = c.get('original', '')
                if off is None or not orig_hex:
                    continue
                off = self._parse_offset(off)
                try:
                    orig_bytes = bytes.fromhex(orig_hex)
                except ValueError:
                    continue
                if off + len(orig_bytes) <= len(data):
                    if data[off:off + len(orig_bytes)] == orig_bytes:
                        score += 1

            if score > best_score:
                best_score = score
                best_ver = ver
                best_data = data
                best_pabgh = pabgh

        return best_ver, best_data, best_pabgh

    @staticmethod
    def _parse_offset(off) -> int:
        """Parse an offset value that may be int or hex string."""
        if isinstance(off, int):
            return off
        if isinstance(off, str):
            s = off.strip()
            if s.lower().startswith('0x'):
                return int(s, 16)
            try:
                return int(s, 16)
            except ValueError:
                return int(s)
        return int(off)

    # -- Export Field JSON -----------------------------------------------

    def _bulk_ensure_loaded(self) -> bool:
        if not self._skill_loaded:
            self._on_skill_load()
        return self._skill_loaded

    def _bulk_zero_cooldown(self) -> None:
        if not self._bulk_ensure_loaded():
            return
        count = 0
        for e in self._skill_entries:
            if e.get('_cooltime', 0) != 0:
                e['_cooltime'] = 0
                e.pop('_raw', None)
                count += 1
        self._populate_skill_table()
        self._lbl_skill_status.setText(f"Zero Cooldown: {count} skills modified")
        QMessageBox.information(self, "Zero Cooldown",
            f"Set cooldown to 0 on {count} skills.\n\nClick Apply to Game to deploy.")

    def _bulk_free_skills(self) -> None:
        if not self._bulk_ensure_loaded():
            return
        import skillinfo_parser as sip
        count = 0
        for e in self._skill_entries:
            res_list = e.get('_useResourceStatList', [])
            if res_list:
                for res in res_list:
                    if isinstance(res, dict) and res.get('value', 0) != 0:
                        res['value'] = 0
                        count += 1
                e.pop('_raw', None)
        self._populate_skill_table()
        self._lbl_skill_status.setText(f"Free Skills: {count} resource costs zeroed")
        QMessageBox.information(self, "Free Skills",
            f"Zeroed {count} resource costs across all skills.\n\n"
            f"Click Apply to Game to deploy.")

    def _bulk_max_level(self) -> None:
        if not self._bulk_ensure_loaded():
            return
        count = 0
        for e in self._skill_entries:
            if e.get('_maxLevel', 0) != 30 or e.get('max_level', 0) != 30:
                e['_maxLevel'] = 30
                e['max_level'] = 30
                e.pop('_raw', None)
                count += 1
        self._populate_skill_table()
        self._lbl_skill_status.setText(f"Max Level 30: {count} skills modified")
        QMessageBox.information(self, "Max Level 30",
            f"Set max level to 30 on {count} skills.\n\nClick Apply to Game to deploy.")

    def _bulk_permanent_buffs(self) -> None:
        if not self._bulk_ensure_loaded():
            return
        count = 0
        for e in self._skill_entries:
            flag = e.get('_buffSustainFlag', 0)
            if flag != 1:
                e['_buffSustainFlag'] = 1
                e['buff_sustain_flag'] = 1
                e.pop('_raw', None)
                count += 1
        self._populate_skill_table()
        self._lbl_skill_status.setText(f"Permanent Buffs: {count} skills set to sustain")
        QMessageBox.information(self, "Permanent Buffs",
            f"Set _buffSustainFlag=1 on {count} skills.\n\n"
            f"Food buffs, combat buffs, and other timed effects\n"
            f"should now persist permanently.\n\n"
            f"Click Apply to Game to deploy.")

    def _bulk_crowwing_to_rocket(self) -> None:
        """Test: full body swap between same-size skills across characters."""
        if not self._bulk_ensure_loaded():
            return
        import skillinfo_parser as sip

        by_name = {e['name']: e for e in self._skill_entries}

        # Swap entire entry bodies between same-size skill pairs
        # This swaps ALL data (buff levels, resource costs, everything)
        # while keeping the entry header (key, name) intact
        swaps = [
            ('Skill_Kliff_MpIce', 'Skill_Damian_JumpStep'),
            ('Skill_ElementalReinforce', 'Skill_Damian_ShieldThrow_II'),
            ('Skill_Nature', 'Skill_Damian_ElementalReinforce'),
        ]

        count = 0
        details = []
        for src_name, tgt_name in swaps:
            src = by_name.get(src_name)
            tgt = by_name.get(tgt_name)
            if not src or not tgt:
                continue

            src_bytes = sip.serialize_entry(src)
            tgt_bytes = sip.serialize_entry(tgt)
            if len(src_bytes) != len(tgt_bytes):
                continue

            # Swap all fields EXCEPT identity (key, name, name_len, name_bytes)
            save_keys = ('key', 'name', 'name_len', 'name_bytes')
            src_saved = {k: src[k] for k in save_keys}
            tgt_saved = {k: tgt[k] for k in save_keys}

            # Copy all tgt fields into src
            for k in list(tgt.keys()):
                if k not in save_keys:
                    src[k] = tgt[k]
            # Restore identity
            for k, v in src_saved.items():
                src[k] = v
            src.pop('_raw', None)

            count += 1
            details.append(f"{src_name} now has {tgt_name}'s data")

        self._populate_skill_table()
        detail_str = '\n'.join(details) if details else 'No swaps applied'
        self._lbl_skill_status.setText(f"Skill Swap: {count} pairs swapped")
        QMessageBox.information(self, "Skill Body Swap",
            f"Swapped full skill data between {count} pairs:\n\n"
            f"{detail_str}\n\n"
            f"Click Apply to Game to try it.")

    def _bulk_unlock_all(self) -> None:
        if not self._bulk_ensure_loaded():
            return
        count = 0
        for e in self._skill_entries:
            char_list = e.get('_usableCharacterInfoList', [])
            if char_list:
                e['_usableCharacterInfoList'] = []
                e.pop('_raw', None)
                count += 1
        self._populate_skill_table()
        self._lbl_skill_status.setText(f"Unlock All: {count} skills unlocked for all characters")
        QMessageBox.information(self, "Unlock All",
            f"Cleared character restrictions on {count} skills.\n\n"
            f"All skills are now usable by all characters.\n"
            f"Click Apply to Game to deploy.")

    def _on_stamina_preset(self, filename: str) -> None:
        """One-click stamina preset: load skillinfo if needed, find preset, import it."""
        if not self._skill_loaded:
            self._on_skill_load()
        if not self._skill_loaded:
            return

        preset_path = None
        for base_dir in [
            os.path.dirname(os.path.abspath(__file__)),
            getattr(sys, '_MEIPASS', ''),
            os.getcwd(),
        ]:
            for rel in [
                os.path.join(base_dir, '..', '..', 'stamina_presets', filename),
                os.path.join(base_dir, 'stamina_presets', filename),
                os.path.join(base_dir, '..', '..', filename),
                os.path.join(base_dir, filename),
            ]:
                p = os.path.normpath(rel)
                if os.path.isfile(p):
                    preset_path = p
                    break
            if preset_path:
                break

        if not preset_path:
            from PySide6.QtWidgets import QFileDialog
            preset_path, _ = QFileDialog.getOpenFileName(
                self, f"Locate {filename}", "",
                "JSON Files (*.json);;All Files (*)")
        if not preset_path:
            return

        self._apply_skill_value_patches(preset_path)

    def _apply_skill_value_patches(self, path: str) -> None:
        """Apply a legacy skill JSON mod by patching values in-place.

        Instead of cross-version blob transfer, finds the original byte
        pattern in each entry's current _buff_data_raw and replaces it.
        No structural changes — same file size, safe roundtrip.
        """
        if not self._skill_loaded:
            QMessageBox.warning(self, "Not loaded", "Load SkillInfo first.")
            return

        try:
            with open(path, encoding='utf-8') as f:
                doc = json.load(f)
        except Exception as e:
            QMessageBox.critical(self, "Import Failed", f"Could not read JSON:\n{e}")
            return

        changes = []
        for p in doc.get('patches', []):
            changes.extend(p.get('changes', []))
        if not changes:
            QMessageBox.warning(self, "Import Failed", "No changes found.")
            return

        by_name = {e['name']: e for e in self._skill_entries}
        patched = 0
        skipped = 0
        missing_entries = set()
        missing_values = []

        for c in changes:
            name = c.get('entry', '')
            orig_hex = c.get('original', '')
            patch_hex = c.get('patched', '')
            if not name or not orig_hex or not patch_hex:
                skipped += 1
                continue
            e = by_name.get(name)
            if not e:
                missing_entries.add(name)
                skipped += 1
                continue

            orig_bytes = bytes.fromhex(orig_hex)
            patch_bytes = bytes.fromhex(patch_hex)

            # Search the full serialized entry, not just _buff_data_raw
            # (the new parser splits body into named fields, so the values
            # may be in _useResourceStatList, _buffLevelList, etc.)
            import skillinfo_parser as sip
            full = sip.serialize_entry(e)
            pos = full.find(orig_bytes)
            if pos >= 0:
                patched_full = bytearray(full)
                patched_full[pos:pos + len(orig_bytes)] = patch_bytes
                # Re-parse the patched entry to update all named fields
                idx = [(ek['key'], 0) for ek in [e]]
                try:
                    new_e = sip.parse_skill_entry(bytes(patched_full), 0, len(patched_full))
                    # Preserve key/name from original
                    new_e['key'] = e['key']
                    new_e['name'] = e['name']
                    new_e['name_len'] = e['name_len']
                    new_e['name_bytes'] = e.get('name_bytes', e['name'].encode('ascii'))
                    by_name[name] = new_e
                    # Update in the list
                    for i, se in enumerate(self._skill_entries):
                        if se['name'] == name:
                            self._skill_entries[i] = new_e
                            break
                    patched += 1
                except Exception:
                    # Fallback: patch _raw directly
                    e['_raw'] = bytes(patched_full)
                    e.pop('_buff_data_raw', None)
                    patched += 1
            else:
                missing_values.append(name)
                skipped += 1

        self._populate_skill_table()
        title = (doc.get('modinfo') or {}).get('title', os.path.basename(path))
        self._lbl_skill_status.setText(
            f"{title}: {patched} values patched, {skipped} skipped.")

        detail = f"Patched {patched}/{len(changes)} values in-place.\n"
        if missing_entries:
            detail += f"\n{len(missing_entries)} entries not found in current game:\n"
            for n in sorted(missing_entries)[:5]:
                detail += f"  {n}\n"
        if missing_values:
            detail += f"\n{len(missing_values)} values not found (game version changed):\n"
            for n in missing_values[:5]:
                detail += f"  {n}\n"
        detail += "\nClick Apply to Game to deploy."

        QMessageBox.information(self, f"{title}", detail)

    def _on_skill_export_json(self) -> None:
        """Export current skill modifications as Format 3 field-name JSON."""
        if not self._skill_loaded:
            QMessageBox.warning(self, "Not loaded", "Load SkillInfo first.")
            return

        import skillinfo_parser as sip
        intents = []
        for i, e in enumerate(self._skill_entries):
            if i >= len(self._skill_vanilla_entries):
                continue
            van = self._skill_vanilla_entries[i]
            if sip.serialize_entry(e) == sip.serialize_entry(van):
                continue
            # Build a field-level diff
            entry_intents = _diff_skill_entry(van, e)
            intents.extend(entry_intents)

        if not intents:
            QMessageBox.information(self, "Export Field JSON",
                                    "No modifications to export.")
            return

        default_name = "skill_mod.field.json"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Skill Field JSON", default_name,
            "Field JSON (*.field.json *.json);;All Files (*)")
        if not path:
            return

        doc = {
            'modinfo': {
                'title': 'Skill Mod',
                'version': '1.0',
                'author': 'CrimsonGameMods SkillTree',
                'description': f'{len(intents)} field-level intent(s)',
                'note': 'Format 3 -- uses field names, survives game updates',
            },
            'format': 3,
            'target': 'skill.pabgb',
            'intents': intents,
        }

        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(doc, f, indent=2, ensure_ascii=False, default=str)
            self._lbl_skill_status.setText(
                f"Exported {len(intents)} intents to {os.path.basename(path)}")
            QMessageBox.information(
                self, "Export Field JSON",
                f"Exported {len(intents)} field-level intents.\n\n"
                f"File: {path}")
        except Exception as e:
            QMessageBox.critical(self, "Export Failed", str(e))

    def _on_skill_export_legacy(self) -> None:
        """Export skill modifications as Format 2 hybrid JSON (entry + absolute offset)."""
        if not self._skill_loaded:
            QMessageBox.warning(self, "Not loaded", "Load SkillInfo first.")
            return

        import skillinfo_parser as sip

        van_pabgh = self._skill_pabgh
        van_pabgb = self._skill_pabgb
        van_index = sip.parse_skill_pabgh(van_pabgh)

        entry_offsets = {}
        for key, off in van_index:
            for ve in self._skill_vanilla_entries:
                if ve.get('key') == key:
                    entry_offsets[ve.get('name', '')] = off
                    break

        changes = []
        for i, e in enumerate(self._skill_entries):
            if i >= len(self._skill_vanilla_entries):
                continue
            van = self._skill_vanilla_entries[i]
            van_bytes = sip.serialize_entry(van)
            mod_bytes = sip.serialize_entry(e)
            if van_bytes == mod_bytes:
                continue
            entry_name = e.get('name', f'entry_{i}')
            entry_abs = entry_offsets.get(entry_name, 0)
            # Standard PABGB convention: entry body base = after key(4) +
            # name_len(4) + name_string(N). The null terminator is counted
            # as byte 0 of the body, not part of the header.
            name_len = e.get('name_len', len(entry_name))
            hdr_size = 4 + 4 + name_len
            body_base_abs = entry_abs + hdr_size
            j = hdr_size
            while j < min(len(van_bytes), len(mod_bytes)):
                if van_bytes[j] != mod_bytes[j]:
                    run_start = j
                    while j < min(len(van_bytes), len(mod_bytes)) and van_bytes[j] != mod_bytes[j]:
                        j += 1
                    rel = run_start - hdr_size
                    changes.append({
                        'entry': entry_name,
                        'rel_offset': rel,
                        'offset': body_base_abs + rel,
                        'original': van_bytes[run_start:j].hex(),
                        'patched': mod_bytes[run_start:j].hex(),
                    })
                else:
                    j += 1

        if not changes:
            QMessageBox.information(self, "Export Legacy JSON",
                                    "No modifications to export.")
            return

        path, _ = QFileDialog.getSaveFileName(
            self, "Export Legacy Skill JSON", "skill_mod.json",
            "JSON Files (*.json);;All Files (*)")
        if not path:
            return

        doc = {
            'modinfo': {
                'title': 'Skill Mod',
                'version': '1.0',
                'author': 'CrimsonGameMods SkillTree',
                'description': f'{len(changes)} byte patch(es)',
            },
            'format': 2,
            'patches': [{
                'game_file': 'gamedata/skill.pabgb',
                'source_group': '0008',
                'changes': changes,
            }],
        }

        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(doc, f, indent=2, ensure_ascii=False)
            self._lbl_skill_status.setText(
                f"Exported {len(changes)} legacy patches to {os.path.basename(path)}")
            QMessageBox.information(
                self, "Export Legacy JSON",
                f"Exported {len(changes)} byte-level patches (hybrid format).\n\n"
                f"File: {path}")
        except Exception as e:
            QMessageBox.critical(self, "Export Failed", str(e))

    def _on_skill_export_mod_folder(self) -> None:
        """Export patched skill.pabgb/pabgh as a mod folder with modinfo.json."""
        if not self._skill_loaded:
            QMessageBox.warning(self, "Not loaded", "Load SkillInfo first.")
            return

        if not self._has_skill_modifications():
            QMessageBox.information(self, "Export Mod",
                                    "No modifications to export.")
            return

        from PySide6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(self, "Export Mod Folder",
            "Mod name:", text="Skill Mod")
        if not ok or not name.strip():
            return
        name = name.strip()

        import skillinfo_parser as sip
        new_pabgh, new_pabgb = sip.serialize_all(self._skill_entries)

        exe_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
        folder = "".join(c if (c.isalnum() or c in "-_") else "_" for c in name)
        out = os.path.join(exe_dir, "packs", folder)
        os.makedirs(out, exist_ok=True)
        files_dir = os.path.join(out, "files", "gamedata", "binary__", "client", "bin")
        os.makedirs(files_dir, exist_ok=True)

        with open(os.path.join(files_dir, "skill.pabgb"), "wb") as f:
            f.write(new_pabgb)
        with open(os.path.join(files_dir, "skill.pabgh"), "wb") as f:
            f.write(new_pabgh)

        with open(os.path.join(out, "modinfo.json"), "w", encoding="utf-8") as f:
            json.dump({
                "id": name.lower().replace(" ", "_"),
                "name": name,
                "version": "1.0.0",
                "game_version": "1.00.04",
                "author": "CrimsonGameMods",
                "description": f"Skill mod: {name} ({self._count_skill_modifications()} skills modified)",
            }, f, indent=2)

        mod_count = self._count_skill_modifications()
        self._lbl_skill_status.setText(f"Exported mod to packs/{folder}/")
        QMessageBox.information(self, "Exported",
            f"Mod written to:\n{out}\n\n"
            f"{mod_count} modified skill(s) included.")


# ── Module-level helpers ─────────────────────────────────────────────────

def _apply_one_skill_patch(patched: bytearray, vanilla: bytes,
                           change: dict) -> int:
    """Apply a single legacy JSON v2 patch to skill.pabgb. Returns 1 if applied."""
    ptype = change.get('type', 'replace')
    if ptype == 'replace':
        entry = change.get('entry')
        if entry and 'rel_offset' in change:
            name_bytes = entry.encode('ascii')
            search = struct.pack('<I', len(name_bytes)) + name_bytes + b'\x00'
            pos = vanilla.find(search)
            if pos < 0:
                return 0
            entry_start = pos - 4
            abs_off = entry_start + change['rel_offset']
        elif 'offset' in change:
            off_val = change['offset']
            abs_off = int(off_val, 16) if isinstance(off_val, str) else int(off_val)
        else:
            return 0
        patch_bytes = bytes.fromhex(change.get('patched', ''))
        orig_bytes = bytes.fromhex(change.get('original', ''))
        if not patch_bytes:
            return 0
        end = abs_off + max(len(orig_bytes), len(patch_bytes))
        if end > len(patched):
            return 0
        patched[abs_off:abs_off + len(orig_bytes)] = patch_bytes
        return 1
    elif ptype == 'insert':
        entry = change.get('entry')
        if entry and 'rel_offset' in change:
            name_bytes = entry.encode('ascii')
            search = struct.pack('<I', len(name_bytes)) + name_bytes + b'\x00'
            pos = vanilla.find(search)
            if pos < 0:
                return 0
            abs_off = (pos - 4) + change['rel_offset']
        elif 'offset' in change:
            off_val = change['offset']
            abs_off = int(off_val, 16) if isinstance(off_val, str) else int(off_val)
        else:
            return 0
        insert_bytes = bytes.fromhex(change.get('bytes', ''))
        if not insert_bytes:
            return 0
        patched[abs_off:abs_off] = insert_bytes
        return 1
    return 0


def _diff_skill_entry(vanilla: dict, modified: dict) -> list[dict]:
    """Produce Format 3 field-level intents for one skill entry diff."""
    intents = []
    name = modified['name']
    key = modified['key']

    SKIP = {'key', 'name_len', 'name_bytes', 'name', '_raw', '_pad_01',
            '_buffLevelCount', 'max_level', 'dev_skill_name', 'dev_skill_desc',
            'video_path_hash', 'buff_sustain_flag', 'skill_group_key_list',
            '_buff_data_raw'}

    for field in modified:
        if field in SKIP:
            continue
        old_val = vanilla.get(field)
        new_val = modified.get(field)
        if old_val == new_val:
            continue
        if isinstance(new_val, bytes):
            intents.append({
                'entry': name, 'key': key, 'field': field, 'op': 'set',
                'new': new_val.hex(),
            })
        elif isinstance(new_val, (list, dict)):
            if field == '_buffLevelList' and new_val is not None:
                _diff_buff_levels(intents, name, key, old_val, new_val)
            else:
                intents.append({
                    'entry': name, 'key': key, 'field': field, 'op': 'set',
                    'new': new_val,
                })
        else:
            intents.append({
                'entry': name, 'key': key, 'field': field, 'op': 'set',
                'new': new_val,
            })

    return intents


def _diff_buff_levels(intents: list, name: str, key: int,
                      old_levels, new_levels) -> None:
    """Diff _buffLevelList at the per-buff-data field level."""
    if old_levels is None or new_levels is None:
        if old_levels != new_levels:
            intents.append({
                'entry': name, 'key': key,
                'field': '_buffLevelList', 'op': 'set',
                'new': new_levels,
            })
        return

    if len(old_levels) != len(new_levels):
        intents.append({
            'entry': name, 'key': key,
            'field': '_buffLevelList', 'op': 'set',
            'new': new_levels,
        })
        return

    for li, (old_lv, new_lv) in enumerate(zip(old_levels, new_levels)):
        if not isinstance(old_lv, dict) or not isinstance(new_lv, dict):
            if old_lv != new_lv:
                intents.append({
                    'entry': name, 'key': key,
                    'field': f'_buffLevelList[{li}]', 'op': 'set',
                    'new': new_lv,
                })
            continue

        old_bd = old_lv.get('buff_data', [])
        new_bd = new_lv.get('buff_data', [])
        if len(old_bd) != len(new_bd):
            intents.append({
                'entry': name, 'key': key,
                'field': f'_buffLevelList[{li}].buff_data', 'op': 'set',
                'new': new_bd,
            })
            continue

        for bi, (ob, nb) in enumerate(zip(old_bd, new_bd)):
            if not isinstance(ob, dict) or not isinstance(nb, dict):
                if ob != nb:
                    intents.append({
                        'entry': name, 'key': key,
                        'field': f'_buffLevelList[{li}].buff_data[{bi}]',
                        'op': 'set', 'new': nb,
                    })
                continue
            for bf in nb:
                ov = ob.get(bf)
                nv = nb.get(bf)
                if ov == nv:
                    continue
                field_path = f'_buffLevelList[{li}].buff_data[{bi}].{bf}'
                if isinstance(nv, bytes):
                    intents.append({
                        'entry': name, 'key': key,
                        'field': field_path, 'op': 'set',
                        'new': nv.hex(),
                    })
                else:
                    intents.append({
                        'entry': name, 'key': key,
                        'field': field_path, 'op': 'set',
                        'new': nv,
                    })
