from __future__ import annotations

import json
import logging
import os
import shutil
import struct
import sys
import tempfile
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QSpinBox,
    QApplication, QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog,
    QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMessageBox, QPushButton, QScrollArea, QSplitter, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from gui.theme import COLORS
from gui.utils import make_scope_label
from i18n import tr

log = logging.getLogger(__name__)


class FieldEditTab(QWidget):

    status_message = Signal(str)
    config_save_requested = Signal()

    def __init__(
        self,
        config: dict,
        rebuild_papgt_fn: Optional[Callable[[str, str], str]] = None,
        show_guide_fn=None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._rebuild_papgt_fn = rebuild_papgt_fn
        self._show_guide_fn = show_guide_fn

        self._field_edit_data = None
        self._field_edit_original = None
        self._field_edit_schema = None
        self._field_edit_entries: list = []
        self._field_edit_modified = False
        self._field_edit_editing = False
        self._vehicle_data = None
        self._vehicle_original = None
        self._vehicle_schema = None
        self._vehicle_entries: list = []
        self._vehicle_editing = False
        self._gptrigger_data = None
        self._gptrigger_original = None
        self._gptrigger_schema = None
        self._gptrigger_entries: list = []
        self._gptrigger_editing = False
        self._regioninfo_data = None
        self._regioninfo_original = None
        self._regioninfo_schema = None
        self._regioninfo_entries: list = []
        self._regioninfo_editing = False
        self._charinfo_data = None
        self._charinfo_original = None
        self._charinfo_schema = None
        self._charinfo_mount_entries: list = []
        self._charinfo_player_entries: list = []
        self._charinfo_editing = False
        self._weapon_pkg_editing = False
        self._string_resolver = None  # lazy-loaded StringResolver
        self._actionchart_index = None  # cached PAZ 0010 file list
        self._wantedinfo_data = None
        self._wantedinfo_original = None
        self._wantedinfo_schema = None
        self._mesh_swap_queue: list = []
        self._allygroup_data = None
        self._allygroup_original = None
        self._allygroup_schema = None
        self._relationinfo_data = None
        self._relationinfo_original = None
        self._relationinfo_schema = None
        self._factionrelgrp_data = None
        self._factionrelgrp_original = None
        self._factionrelgrp_schema = None

        self._build_ui()


    def set_game_path(self, path: str) -> None:
        if path:
            self._config["game_install_path"] = path

    def get_staged_files(self) -> dict[str, bytes]:
        result = {}
        _PAIRS = [
            ("fieldinfo",               "_field_edit_data",    "_field_edit_original"),
            ("vehicleinfo",             "_vehicle_data",       "_vehicle_original"),
            ("characterinfo",           "_charinfo_data",      "_charinfo_original"),
            ("gameplaytrigger",         "_gptrigger_data",     "_gptrigger_original"),
            ("regioninfo",              "_regioninfo_data",    "_regioninfo_original"),
            ("wantedinfo",              "_wantedinfo_data",    "_wantedinfo_original"),
            ("allygroupinfo",           "_allygroup_data",     "_allygroup_original"),
            ("relationinfo",            "_relationinfo_data",  "_relationinfo_original"),
            ("factionrelationgroupinfo", "_factionrelgrp_data", "_factionrelgrp_original"),
        ]
        for name, data_attr, orig_attr in _PAIRS:
            data = getattr(self, data_attr, None)
            orig = getattr(self, orig_attr, None)
            if data is not None and orig is not None and bytes(data) != bytes(orig):
                result[f"{name}.pabgb"] = bytes(data)
        return result

    def set_experimental_mode(self, enabled: bool) -> None:
        for w in getattr(self, '_dev_export_btns_field', []):
            w.setVisible(bool(enabled))

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        inner = QWidget()
        scroll.setWidget(inner)
        outer.addWidget(scroll)

        layout = QVBoxLayout(inner)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)
        layout.addWidget(make_scope_label("game"))

        info = QLabel(
            "Edit zone/field properties — enable mount summoning in towns, "
            "modify zone flags. Modifies fieldinfo / vehicleinfo / regioninfo / "
            "characterinfo / wantedinfo via a single PAZ overlay (0039/)."
        )
        info.setWordWrap(True)
        info.setStyleSheet(
            f"color: {COLORS['accent']}; padding: 8px; "
            f"border: 1px solid {COLORS['border']}; border-radius: 4px; "
            f"background-color: rgba(218,168,80,0.08);"
        )
        layout.addWidget(info)

        top_row = QHBoxLayout()

        load_btn = QPushButton(tr("Load FieldInfo"))
        load_btn.setObjectName("accentBtn")
        load_btn.setToolTip(tr("Extract fieldinfo.pabgb from game PAZ"))
        load_btn.clicked.connect(self._field_edit_load)
        top_row.addWidget(load_btn)

        mesh_swap_btn = QPushButton(tr("🎭 Mesh Swap (Pets / Mounts / NPCs)"))
        mesh_swap_btn.setStyleSheet(
            "background-color: #4A148C; color: white; font-weight: bold; "
            "padding: 8px 14px; font-size: 13px;")
        mesh_swap_btn.setToolTip(
            "Visual Transmog for ANY character in the game.\n\n"
            "• Make your pet cat look like a wolf\n"
            "• Make your horse look like a dragon\n"
            "• Make NPCs look like bosses\n"
            "• Make bosses look like chickens\n\n"
            "Opens a dedicated dialog with Pet / Animal / Mount / Hero / Boss\n"
            "categories so you can find what to swap without needing to know\n"
            "internal names. Target and Source filters are independent — filter\n"
            "your target to Pet (Cat) on the left, then filter source to Dragon\n"
            "on the right to make all cats look like wyverns.\n\n"
            "Queued into the 0039/ overlay — apply via Apply to Game or Export.")
        mesh_swap_btn.clicked.connect(self._field_edit_open_mesh_swap)
        top_row.addWidget(mesh_swap_btn)

        mount_btn = QPushButton(tr("Enable Mounts Everywhere"))
        mount_btn.setStyleSheet("background-color: #7B1FA2; color: white; font-weight: bold;")
        mount_btn.setToolTip(
            "Patches 3 game files to allow mounts everywhere:\n"
            "1. vehicleinfo: allow all mounts in safe zones\n"
            "2. regioninfo: remove dismount flags from towns / restricted areas\n"
            "3. characterinfo: extend ride duration, remove cooldowns")
        mount_btn.clicked.connect(self._field_edit_enable_mounts)
        top_row.addWidget(mount_btn)

        invincible_mounts_btn = QPushButton(tr("Invincible Mounts"))
        invincible_mounts_btn.setStyleSheet("background-color: #4527A0; color: white; font-weight: bold;")
        invincible_mounts_btn.setToolTip(
            "Sets four_flags.flag_a (_invincibility=1) on all mount\n"
            "characterinfo entries via dmm_parser (vehicle_info != 0\n"
            "or Riding_* string key). Mounts cannot be killed in combat.")
        invincible_mounts_btn.clicked.connect(self._field_edit_invincible_mounts)
        top_row.addWidget(invincible_mounts_btn)

        killall_btn = QPushButton(tr("Make All NPCs Killable"))
        killall_btn.setStyleSheet("background-color: #B71C1C; color: white; font-weight: bold;")
        killall_btn.setToolTip(
            "Sets _isAttackable=1 and _invincibility=0 on all non-mount NPCs\n"
            "via dmm_parser field-level edits (four_flags.flag_a/flag_b).\n\n"
            "Killing quest-essential NPCs may affect quest progression.\n"
            "Fully reversible via Restore.")
        killall_btn.clicked.connect(self._field_edit_make_killable)
        top_row.addWidget(killall_btn)

        apply_btn = QPushButton(tr("Apply to Game"))
        apply_btn.setObjectName("accentBtn")
        apply_btn.setToolTip(tr("Deploy modified fieldinfo to the game"))
        apply_btn.clicked.connect(self._field_edit_apply)
        top_row.addWidget(apply_btn)

        # Overlay group number — user-configurable so it doesn't collide
        # with whatever slot another mod owns.
        top_row.addWidget(QLabel(tr("Overlay:")))
        self._fieldedit_overlay_spin = QSpinBox()
        self._fieldedit_overlay_spin.setRange(1, 9999)
        self._fieldedit_overlay_spin.setValue(
            self._config.get("fieldedit_overlay_dir", 39))
        self._fieldedit_overlay_spin.setFixedWidth(70)
        self._fieldedit_overlay_spin.setToolTip(
            "Overlay group number (0039 = default). Change if another mod\n"
            "already uses this slot. Apply to Game writes to <game>/NNNN/;\n"
            "Restore removes the same NNNN/.")
        self._fieldedit_overlay_spin.valueChanged.connect(
            lambda v: self._config.update({"fieldedit_overlay_dir": int(v)}))
        top_row.addWidget(self._fieldedit_overlay_spin)

        export_field_json_v3_btn = QPushButton(tr("Export Field JSON v3"))
        export_field_json_v3_btn.setStyleSheet("background-color: #0277BD; color: white; font-weight: bold;")
        export_field_json_v3_btn.setToolTip("Export all FieldEdit changes as Format 3.1 field JSON.")
        export_field_json_v3_btn.clicked.connect(self._field_edit_export_field_json_v3)
        top_row.addWidget(export_field_json_v3_btn)

        import_field_json_v3_btn = QPushButton(tr("Import Field JSON v3"))
        import_field_json_v3_btn.setStyleSheet("background-color: #4527A0; color: white; font-weight: bold;")
        import_field_json_v3_btn.setToolTip(
            "Import a Format 3 field JSON mod and apply its intents\n"
            "to the current FieldEdit data for further editing.\n"
            "Supports: fieldinfo, vehicleinfo, gameplaytrigger,\n"
            "regioninfo, characterinfo, wantedinfo")
        import_field_json_v3_btn.clicked.connect(self._field_edit_import_field_json_v3)
        top_row.addWidget(import_field_json_v3_btn)

        # Export buttons — only visible in Advanced/Dev mode (unsupported)
        export_mod_btn = QPushButton(tr("Export as Mod"))
        export_mod_btn.setStyleSheet("background-color: #2E7D32; color: white; font-weight: bold;")
        export_mod_btn.setToolTip(
            "ADVANCED — UNSUPPORTED. Contact mod loader dev for help.\n\n"
            "Export as raw-pabgb mod for generic mod loaders.")
        export_mod_btn.clicked.connect(self._field_edit_export_mod)
        export_mod_btn.setVisible(False)
        top_row.addWidget(export_mod_btn)

        export_btn = QPushButton(tr("Export as CDUMM Mod"))
        export_btn.setStyleSheet("background-color: #1B5E20; color: white; font-weight: bold;")
        export_btn.setToolTip(
            "ADVANCED — UNSUPPORTED. Contact mod loader dev for help.\n\n"
            "Export as pre-packed PAZ mod for JMM / CDUMM / DMM.")
        export_btn.clicked.connect(self._field_edit_export)
        export_btn.setVisible(False)
        top_row.addWidget(export_btn)

        export_json_btn = QPushButton(tr("Export as JSON"))
        export_json_btn.setStyleSheet("background-color: #0D47A1; color: white; font-weight: bold;")
        export_json_btn.setToolTip(
            "ADVANCED — UNSUPPORTED. Contact mod loader dev for help.\n\n"
            "Export all changes as a portable JSON patch file.")
        export_json_btn.clicked.connect(self._field_edit_export_json)
        export_json_btn.setVisible(False)
        top_row.addWidget(export_json_btn)

        self._dev_export_btns_field = [export_mod_btn, export_btn, export_json_btn]

        # Export Mesh Swap as JSON Mod — removed. Redundant with Export as
        # JSON which already bakes mesh swaps via _apply_mesh_swaps() before
        # diffing. Apply to Game / Export as Mod / Export as CDUMM also
        # include mesh swaps. Keep the method around for backward-compat in
        # case anything still references it, but don't surface a button.

        restore_btn = QPushButton(tr("Restore"))
        restore_btn.setToolTip(tr("Remove field mod and restore vanilla"))
        restore_btn.clicked.connect(self._field_edit_restore)
        top_row.addWidget(restore_btn)

        self._field_edit_status = QLabel("")
        top_row.addWidget(self._field_edit_status, 1)
        layout.addLayout(top_row)

        ally_row = QHBoxLayout()
        ally_row.setSpacing(6)
        ally_label = QLabel(tr("Attack Anything (EXPERIMENTAL):"))
        ally_label.setStyleSheet("color: #FFB74D; font-weight: bold;")
        ally_row.addWidget(ally_label)

        wipe_btn = QPushButton(tr("Wipe Ally Lists (Path B)"))
        wipe_btn.setStyleSheet("background-color: #AD1457; color: white; font-weight: bold;")
        wipe_btn.setToolTip(
            "Path B — PROBABLE. Zeros _addOnAllyGroupList hashes across all\n"
            "50 AllyGroup entries. Effect: no group is allied with any other.\n"
            "Less nuclear than Path A; _relationTypeList preserved so combat\n"
            "rules still apply, just no mutual defense between allied factions.")
        wipe_btn.clicked.connect(self._field_edit_wipe_ally_lists)
        ally_row.addWidget(wipe_btn)

        intruder2_btn = QPushButton(tr("Intruder Flag (slot 2)"))
        intruder2_btn.setStyleSheet("background-color: #4A148C; color: white; font-weight: bold;")
        intruder2_btn.setToolTip(
            "Path C — EXPERIMENT. Sets u8 flag slot #2 = 1 on all 50 groups.\n"
            "Likely _isIntruder (flag distribution suggests it). Test in-game\n"
            "and tell me if NPCs attack each other — if yes, this is _isIntruder.")
        intruder2_btn.clicked.connect(lambda: self._field_edit_set_ally_flag(2))
        ally_row.addWidget(intruder2_btn)


        ally_row.addStretch()
        layout.addLayout(ally_row)

        self._field_edit_table = QTableWidget()
        self._field_edit_table.setColumnCount(6)
        self._field_edit_table.setHorizontalHeaderLabels([
            "Key", "Name", "Zone Type", "canCallVehicle",
            "alwaysCallVehicle_dev", "Position",
        ])
        self._field_edit_table.horizontalHeader().setStretchLastSection(True)
        self._field_edit_table.setSelectionBehavior(QTableWidget.SelectRows)
        self._field_edit_table.setAlternatingRowColors(True)
        self._field_edit_table.verticalHeader().setVisible(False)
        self._field_edit_table.cellChanged.connect(self._field_edit_cell_changed)
        self._field_edit_table.setVisible(False)

        vlabel = QLabel(tr("Vehicle Info (vehicleinfo.pabgb) — per-mount call restrictions:"))
        vlabel.setStyleSheet(f"color: {COLORS['accent']}; font-weight: bold; padding: 4px 0;")
        layout.addWidget(vlabel)

        self._vehicle_table = QTableWidget()
        self._vehicle_table.setColumnCount(7)
        self._vehicle_table.setHorizontalHeaderLabels([
            "Key", "Name", "Type", "VoxelType", "MountCallType",
            "CanCallSafeZone", "AltitudeCap",
        ])
        self._vehicle_table.horizontalHeader().setStretchLastSection(True)
        self._vehicle_table.setSelectionBehavior(QTableWidget.SelectRows)
        self._vehicle_table.setAlternatingRowColors(True)
        self._vehicle_table.verticalHeader().setVisible(False)
        self._vehicle_table.cellChanged.connect(self._vehicle_cell_changed)
        layout.addWidget(self._vehicle_table, 1)

        self._gt_filter_combo = QComboBox()
        self._gt_filter_combo.addItem("Safe zones only (type != 0)", "safe")
        self._gt_filter_combo.addItem("All entries", "all")
        self._gt_table = QTableWidget()
        self._gt_table.setColumnCount(4)
        self._gt_table.setHorizontalHeaderLabels(["Key", "Name", "Flags", "safeZoneType"])
        self._gt_table.setVisible(False)

        rlabel = QLabel(tr("Region Info (regioninfo.pabgb) — town/dismount flags per region:"))
        rlabel.setStyleSheet(f"color: {COLORS['accent']}; font-weight: bold; padding: 4px 0;")
        layout.addWidget(rlabel)

        ri_filter_row = QHBoxLayout()
        ri_filter_row.addWidget(QLabel(tr("Show:")))
        self._ri_filter_combo = QComboBox()
        self._ri_filter_combo.addItem("Towns & restricted only", "restricted")
        self._ri_filter_combo.addItem("All regions", "all")
        self._ri_filter_combo.setFixedWidth(200)
        self._ri_filter_combo.currentIndexChanged.connect(self._regioninfo_populate)
        ri_filter_row.addWidget(self._ri_filter_combo)
        ri_filter_row.addStretch()
        layout.addLayout(ri_filter_row)

        self._ri_table = QTableWidget()
        self._ri_table.setColumnCount(7)
        self._ri_table.setHorizontalHeaderLabels([
            "Key", "Name", "Type", "isTown",
            "limitVehicleRun", "isWild", "vehicleMercAllowType",
        ])
        self._ri_table.horizontalHeader().setStretchLastSection(True)
        self._ri_table.setSelectionBehavior(QTableWidget.SelectRows)
        self._ri_table.setAlternatingRowColors(True)
        self._ri_table.verticalHeader().setVisible(False)
        self._ri_table.cellChanged.connect(self._ri_cell_changed)
        layout.addWidget(self._ri_table, 1)

        mlabel = QLabel(tr("Mount Duration/Cooldown (characterinfo.pabgb) — per-mount ride limits:"))
        mlabel.setStyleSheet(f"color: {COLORS['accent']}; font-weight: bold; padding: 4px 0;")
        layout.addWidget(mlabel)

        self._mount_table = QTableWidget()
        self._mount_table.setColumnCount(5)
        self._mount_table.setHorizontalHeaderLabels([
            "Name", "Vehicle Type", "Duration (s)", "Cooldown (s)", "CoolType",
        ])
        self._mount_table.horizontalHeader().setStretchLastSection(True)
        self._mount_table.setSelectionBehavior(QTableWidget.SelectRows)
        self._mount_table.setAlternatingRowColors(True)
        self._mount_table.verticalHeader().setVisible(False)
        self._mount_table.cellChanged.connect(self._mount_cell_changed)
        layout.addWidget(self._mount_table, 1)

        wlabel = QLabel(tr(
            "Cross-Character Runtime Packages (characterinfo.pabgb) — "
            "swap how the player chars LOAD combat behavior at runtime. "
            "Double-click any cell to pick a source character. Hover the column "
            "headers for a per-field explanation."
        ))
        wlabel.setWordWrap(True)
        wlabel.setStyleSheet(
            f"color: {COLORS['accent']}; font-weight: bold; padding: 4px 0;"
        )
        wlabel.setToolTip(tr(
            "ANIMATION ARCHITECTURE\n"
            "Each character's animation is split into two simultaneous layers:\n"
            "  • LOWER body — locomotion (walk, run, dodge, jump, fall)\n"
            "  • UPPER body — combat (swing, shoot, combo, parry, grab)\n"
            "A 'GamePlay' XML wires them together — input map (button → action),\n"
            "sheath positions, camera presets, combat timings.\n\n"
            "EACH FIELD IS A 4-BYTE NAME HASH pointing at a package in PAZ 0010.\n"
            "Swap a field = the character LOADS that package at runtime.\n"
            "Example: Kliff._upperAC ← Damian's value → Kliff fires guns.\n"
            "Example: Kliff._upperAC ← Boss_Myurdin's value → Kliff swings sword combos.\n\n"
            "ALL 6 FIELDS CAN BE SWAPPED INDEPENDENTLY but cross-package interactions\n"
            "may break things — the input map (in GamePlay) decides which buttons can\n"
            "actually trigger which actions in the upper package."
        ))
        layout.addWidget(wlabel)

        wpreset_row = QHBoxLayout()
        self._weapon_reset_btn = QPushButton(tr("Reset to Vanilla"))
        self._weapon_reset_btn.setToolTip(tr(
            "Restores all 6 runtime-package fields on Kliff/Damian/Oongka to their\n"
            "vanilla values. Doesn't touch other char edits in the table above\n"
            "(mounts, NPCs, etc.)."
        ))
        self._weapon_reset_btn.clicked.connect(self._weapon_reset_vanilla)
        wpreset_row.addWidget(self._weapon_reset_btn)

        self._weapon_save_preset_btn = QPushButton(tr("Save Preset..."))
        self._weapon_save_preset_btn.setToolTip(tr(
            "Export current Kliff/Damian/Oongka 6-field configuration as a JSON\n"
            "preset. Includes vanilla baselines so you can roll back per-field.\n"
            "Share these with the community to ship character-behavior mods."
        ))
        self._weapon_save_preset_btn.clicked.connect(self._weapon_save_preset)
        wpreset_row.addWidget(self._weapon_save_preset_btn)

        self._weapon_load_preset_btn = QPushButton(tr("Load Preset..."))
        self._weapon_load_preset_btn.setToolTip(tr(
            "Apply a previously-saved JSON preset. Overwrites the current values in\n"
            "the table for any field the preset touches; remember to click 'Apply to\n"
            "Game' afterward to actually deploy."
        ))
        self._weapon_load_preset_btn.clicked.connect(self._weapon_load_preset)
        wpreset_row.addWidget(self._weapon_load_preset_btn)

        self._weapon_siblings_btn = QPushButton(tr("Find Package Siblings..."))
        self._weapon_siblings_btn.setToolTip(tr(
            "RESEARCH TOOL — discover 'package families'.\n\n"
            "Pick any of the 6 fields + a hash value (or copy from a player char) →\n"
            "lists every character (out of 6,872) that uses the same package.\n\n"
            "Example: query Kliff's _upperAC (Player_Kliff) → see only Kliff's variants\n"
            "use it. Query Damian's _gamePlay → discover which other chars share her\n"
            "input map. Useful before swapping to know what side-effects to expect."
        ))
        self._weapon_siblings_btn.clicked.connect(self._weapon_open_siblings_dialog)
        wpreset_row.addWidget(self._weapon_siblings_btn)

        self._weapon_slot_inspector_btn = QPushButton(tr("Slot Inspector / Patch..."))
        self._weapon_slot_inspector_btn.setStyleSheet(
            "background-color: #1B5E20; color: white; font-weight: bold;")
        self._weapon_slot_inspector_btn.setToolTip(tr(
            "SURGICAL XML PATCH for action-chart packages.\n\n"
            "The runtime-package SWAP above replaces the whole package — that\n"
            "breaks Kliff's skills/inventory and hits the 'compression ceiling'\n"
            "on the PHW descriptor when you stack too many weapons.\n\n"
            "This tool extends Kliff's EXISTING package by injecting individual\n"
            "<SubPackage> slots from other packages (Pistol from Damian, Sword1H\n"
            "from Boss_Myurdin, etc). One-line additions, no whole-package swap,\n"
            "skills/inventory survive.\n\n"
            "Generates a patched characteractionpackagedescription.xml ready to\n"
            "deploy as a PAZ 0010 overlay."
        ))
        self._weapon_slot_inspector_btn.clicked.connect(self._weapon_open_slot_inspector)
        wpreset_row.addWidget(self._weapon_slot_inspector_btn)

        self._weapon_actionchart_btn = QPushButton(tr("Action Chart Browser..."))
        self._weapon_actionchart_btn.setStyleSheet(
            "background-color: #4A148C; color: white; font-weight: bold;")
        self._weapon_actionchart_btn.setToolTip(tr(
            "Browse the .paa_metabin ANIMATION files in PAZ 0010 (the layer BELOW\n"
            "the runtime packages above — actual per-action animation metadata).\n\n"
            "Pick a character → category (Combo / Dodge / Grab / Link / Rage / etc.)\n"
            "→ extract or inspect any single .paa_metabin file.\n\n"
            "575 files for Myurdin alone. Useful for figuring out which specific\n"
            "actions a boss has before deciding what to swap at the package level."
        ))
        self._weapon_actionchart_btn.clicked.connect(self._weapon_open_action_chart_browser)
        wpreset_row.addWidget(self._weapon_actionchart_btn)

        wpreset_row.addStretch()
        layout.addLayout(wpreset_row)

        self._weapon_pkg_table = QTableWidget()
        self._WEAPON_COLS = (
            ('_upperActionChartPackageGroupName', 'Upper (fire/attack)',
             "UPPER-BODY COMBAT PACKAGE\n\n"
             "Holds attack animations: combos, swings, shots, charges, grabs, parries.\n"
             "Internally has multiple weapon-class slots (Bow, Musket, Pistol, Sword, Cannon, etc.)\n"
             "— a single package can serve every weapon the original character actually uses.\n\n"
             "Examples:\n"
             "  Player_Kliff       — bow + utility actions (no swords or guns)\n"
             "  Player_PHW (Damian) — swords, shields, muskets, pistols\n"
             "  Player_Oongka      — hammers, axes, fists\n"
             "  Boss_Myurdin_Intro_UpperAction — boss sword combos + slamneck/slamspin links\n\n"
             "Swap this to give the target char another character's attack animations.\n"
             "Caveat: only weapons the source package supports will work — Kliff with\n"
             "Myurdin's package needs to actually equip a sword first."),

            ('_lowerActionChartPackageGroupName', 'Lower (locomotion)',
             "LOWER-BODY LOCOMOTION PACKAGE\n\n"
             "Holds movement animations: walk, run, sprint, jump, fall, dodge, swim, climb.\n"
             "Plays UNDERNEATH the upper-body layer, so you can run forward (lower) while\n"
             "swinging a sword (upper) at the same time.\n\n"
             "Weapon-independent — your run cycle is the same whether you're holding a bow\n"
             "or a sword. Swap this to change how the character MOVES around the world.\n\n"
             "Examples:\n"
             "  Player_Kliff_Lower — Kliff's standard locomotion\n"
             "  CD_NHM_Lower       — generic male NPC locomotion (most bosses use this)"),

            ('_characterGamePlayDataName', 'GamePlay (sheath/attach)',
             "GAMEPLAY-DATA XML\n\n"
             "The wiring layer that ties everything together. Defines:\n"
             "  • Input map — which button triggers which action in the upper package\n"
             "  • Sheath positions — where weapons sit on the body when stowed\n"
             "  • Camera presets per weapon\n"
             "  • Combat timings (i-frames, recovery windows, grab rules)\n"
             "  • Routes 'Attack pressed while holding Musket' → 'fire Musket.Attack1'\n\n"
             "Examples:\n"
             "  phm_description_player_kliff   — Kliff's input map + camera config\n"
             "  phw_description_player_001     — Damian's (gun handling, dodge timings)\n"
             "  phm_description_player_001     — Oongka's\n"
             "  mon_myurdin                    — Myurdin's boss-side rules\n\n"
             "Swapping this is what unlocks attacks that the upper package contains but\n"
             "the original char's input map didn't bind. The dev's report flagged that\n"
             "the Kliff Gun Fix sets _gamePlay ← Oongka so Kliff can fire muskets."),

            ('_appearanceName', 'Appearance',
             "VISUAL APPEARANCE PACKAGE\n\n"
             "Points at the model + texture + material set used to RENDER the character.\n"
             "Independent of skeleton — you can swap appearance without changing the rig.\n\n"
             "Swap this for cosmetic mesh changes (similar to the Mesh Swap dialog above,\n"
             "but at the runtime-routing level rather than file-level patching)."),

            ('_skeletonName', 'Skeleton',
             "SKELETON .pab REFERENCE\n\n"
             "Hash → path of the rigged skeleton file. All animations play on top of this.\n"
             "All three player chars + Myurdin variants use 'phm_01.pab' (player-male humanoid),\n"
             "which is why animation swaps between them work without breaking the rig.\n\n"
             "Don't swap to a non-PHM skeleton (dragon, machine, etc.) unless you also\n"
             "swap appearance and prefab — the rig sockets won't match."),

            ('_skeletonVariationName', 'Skeleton Variation',
             "SKELETON VARIATION OVERRIDE\n\n"
             "Optional override for skeleton variant (size scaling, bone tweaks).\n"
             "Most chars leave this at the null-hash sentinel (0xeac5e173 = empty)."),
        )
        self._weapon_pkg_table.setColumnCount(1 + len(self._WEAPON_COLS))
        self._weapon_pkg_table.horizontalHeader().setStretchLastSection(True)
        self._weapon_pkg_table.setSelectionBehavior(QTableWidget.SelectItems)
        self._weapon_pkg_table.setAlternatingRowColors(True)
        self._weapon_pkg_table.verticalHeader().setVisible(False)

        # Header items with hover tooltips per column
        char_hdr = QTableWidgetItem(tr("Character"))
        char_hdr.setToolTip(tr(
            "The three player-controllable characters.\n"
            "Edits here only affect the player chars, not NPCs/bosses."
        ))
        self._weapon_pkg_table.setHorizontalHeaderItem(0, char_hdr)
        for i, (_field, label, tip) in enumerate(self._WEAPON_COLS, start=1):
            hdr = QTableWidgetItem(label)
            hdr.setToolTip(tip)
            self._weapon_pkg_table.setHorizontalHeaderItem(i, hdr)
        self._weapon_pkg_table.cellDoubleClicked.connect(self._weapon_cell_double_clicked)
        layout.addWidget(self._weapon_pkg_table)

        credit = QLabel(
            "FieldInfo: sub_1410403F0 | VehicleInfo: sub_14105D470 | "
            "Triggers: sub_141044180 | RegionInfo: sub_141053790 | "
            "CharacterInfo: sub_141045620")
        credit.setStyleSheet(f"color: {COLORS['text_dim']}; padding: 2px;")
        layout.addWidget(credit)


    def _check_stale_field_overlay(self, game_path: str) -> None:
        mod_group = f"{self._fieldedit_overlay_spin.value():04d}"
        overlay_dir = os.path.join(game_path, mod_group)
        if not os.path.isdir(overlay_dir):
            return

        try:
            import crimson_rs
            dp = "gamedata/binary__/client/bin"
            vanilla_ci = crimson_rs.extract_file(game_path, "0008", dp, "characterinfo.pabgb")
            vanilla_size = len(vanilla_ci)

            pamt_path = os.path.join(overlay_dir, "0.pamt")
            if not os.path.isfile(pamt_path):
                return

            pamt_data = open(pamt_path, "rb").read()
            pamt = crimson_rs.parse_pamt_bytes(pamt_data)

            overlay_size = None
            for d in pamt.get("directories", []):
                for f in d.get("files", []):
                    if f.get("name", "") == "characterinfo.pabgb":
                        overlay_size = f.get("uncompressed_size", 0)
                        break
                if overlay_size is not None:
                    break

            if overlay_size is None:
                return

            if overlay_size != vanilla_size:
                reply = QMessageBox.warning(
                    self, tr("Stale Mod Detected"),
                    f"A FieldEdit overlay ({mod_group}/) was created for a different\n"
                    f"game version (file size mismatch: overlay={overlay_size:,}B vs "
                    f"current={vanilla_size:,}B).\n\n"
                    f"This is likely why the game won't start.\n"
                    f"Remove the stale overlay now?",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
                if reply == QMessageBox.Yes:
                    self._field_edit_restore()
        except Exception as e:
            log.warning("Stale overlay check failed: %s", e)


    def _field_edit_load(self):
        game_path = self._config.get("game_install_path", "")
        if not game_path or not os.path.isdir(game_path):
            try:
                from crimson_rs.validate_game_dir import auto_detect_game_dir
                detected = auto_detect_game_dir()
                if detected and os.path.isdir(detected):
                    self._config["game_install_path"] = game_path = detected
                    self.config_save_requested.emit()
            except Exception:
                pass
        if not game_path or not os.path.isdir(game_path):
            QMessageBox.warning(self, tr("Game Path"),
                tr("Set the game install path first (top of the window)."))
            return

        self._check_stale_field_overlay(game_path)

        self._field_edit_status.setText(tr("Extracting fieldinfo..."))
        QApplication.processEvents()

        try:
            import crimson_rs
            dp = "gamedata/binary__/client/bin"
            body = crimson_rs.extract_file(game_path, "0008", dp, "fieldinfo.pabgb")
            schema = crimson_rs.extract_file(game_path, "0008", dp, "fieldinfo.pabgh")

            self._field_edit_data = bytearray(body)
            self._field_edit_original = bytes(body)
            self._field_edit_schema = bytes(schema)
            self._field_edit_modified = False

            from fieldinfo_parser import parse_pabgh_index, parse_entry
            idx = parse_pabgh_index(self._field_edit_schema)
            sorted_offs = sorted(set(idx.values()))
            entries = []
            for key, eoff in sorted(idx.items()):
                bi = sorted_offs.index(eoff)
                end = sorted_offs[bi + 1] if bi + 1 < len(sorted_offs) else len(self._field_edit_data)
                entry = parse_entry(bytes(self._field_edit_data), eoff, end)
                if entry:
                    entries.append(entry)

            self._field_edit_entries = entries
            self._field_edit_populate()

            try:
                vbody = crimson_rs.extract_file(game_path, "0008", dp, "vehicleinfo.pabgb")
                vschema = crimson_rs.extract_file(game_path, "0008", dp, "vehicleinfo.pabgh")
                self._vehicle_data = bytearray(vbody)
                self._vehicle_original = bytes(vbody)
                self._vehicle_schema = bytes(vschema)

                # Field-level parse via dmm_parser
                import dmm_parser as _dmp
                import copy as _copy
                self._vehicleinfo_dmm_items = _dmp.parse_table(
                    'vehicle_info', bytes(vbody), bytes(vschema))
                self._vehicleinfo_dmm_vanilla = _copy.deepcopy(self._vehicleinfo_dmm_items)
                self._vehicleinfo_dmm_dirty = False
                log.info("dmm_parser: loaded %d vehicleinfo entries (field-level)",
                         len(self._vehicleinfo_dmm_items))

                import ctypes as _ct_v
                ventries = []
                for vd in self._vehicleinfo_dmm_items:
                    mah = vd.get('max_allowable_height', 0)
                    ventries.append({
                        'key': vd['key'],
                        'name': vd.get('string_key', ''),
                        'vehicle_type': vd.get('call_vehicle_voxel_type', 0),
                        'voxel_type': vd.get('call_vehicle_voxel_type', 0),
                        'mount_call_type': (vd.get('rider_detect_info', 0) >> 8) & 0xFF,
                        'can_call_safe_zone': vd.get('send_damage_to', 0),
                        'altitude_cap': _ct_v.c_float.from_buffer_copy(
                            _ct_v.c_uint32(mah & 0xFFFFFFFF)).value,
                        'dmm_ref': vd,
                    })
                self._vehicle_entries = ventries
                self._vehicle_populate()
            except Exception as _ve:
                log.exception("Could not load vehicleinfo")

            try:
                gt_body = crimson_rs.extract_file(game_path, "0008", dp, "gameplaytrigger.pabgb")
                gt_gh = crimson_rs.extract_file(game_path, "0008", dp, "gameplaytrigger.pabgh")
                self._gptrigger_data = bytearray(gt_body)
                self._gptrigger_original = bytes(gt_body)
                self._gptrigger_schema = bytes(gt_gh)

                import struct as _st
                _gt_G = self._gptrigger_schema
                _gt_c16 = _st.unpack_from('<H', _gt_G, 0)[0]
                if 2 + _gt_c16 * 8 == len(_gt_G):
                    _gt_idx_start, _gt_count = 2, _gt_c16
                else:
                    _gt_count = _st.unpack_from('<I', _gt_G, 0)[0]
                    _gt_idx_start = 4
                _gt_idx = {}
                for _gi in range(_gt_count):
                    _gp = _gt_idx_start + _gi * 8
                    if _gp + 8 > len(_gt_G): break
                    _gt_idx[_st.unpack_from('<I', _gt_G, _gp)[0]] = _st.unpack_from('<I', _gt_G, _gp + 4)[0]

                gt_entries = []
                for _gk, _go in sorted(_gt_idx.items()):
                    try:
                        p = _go
                        _rk = _st.unpack_from('<I', self._gptrigger_data, p)[0]; p += 4
                        _sl = _st.unpack_from('<I', self._gptrigger_data, p)[0]; p += 4
                        if _sl > 200: continue
                        _nm = self._gptrigger_data[p:p+_sl].decode('utf-8', errors='replace'); p += _sl
                        _f1 = self._gptrigger_data[p]; p += 1
                        _f2 = self._gptrigger_data[p]; p += 1
                        _f3 = self._gptrigger_data[p]; p += 1
                        _szt = self._gptrigger_data[p]
                        gt_entries.append({
                            'key': _gk, 'name': _nm,
                            'flag1': _f1, 'flag2': _f2, 'flag3': _f3,
                            'safe_zone_type': _szt,
                            'safe_zone_type_offset': p,
                        })
                    except Exception:
                        pass

                self._gptrigger_entries = gt_entries
                self._gptrigger_populate()
                log.info("Loaded %d gameplaytrigger entries", len(gt_entries))
            except Exception:
                log.exception("Could not load gameplaytrigger")
                self._gptrigger_entries = []

            try:
                ri_body = crimson_rs.extract_file(game_path, "0008", dp, "regioninfo.pabgb")
                ri_gh = crimson_rs.extract_file(game_path, "0008", dp, "regioninfo.pabgh")
                self._regioninfo_data = bytearray(ri_body)
                self._regioninfo_original = bytes(ri_body)
                self._regioninfo_schema = bytes(ri_gh)

                import dmm_parser as _dmp_ri
                import copy as _copy_ri
                dmm_items = _dmp_ri.parse_table('region_info', bytes(ri_body), bytes(ri_gh))
                self._regioninfo_dmm_dirty = False
                if dmm_items:
                    self._regioninfo_dmm_items = dmm_items
                    self._regioninfo_dmm_vanilla = [dict(it) for it in dmm_items]
                    log.info("dmm_parser: loaded %d regioninfo entries (field-level)",
                             len(dmm_items))
                    ri_entries = []
                    for it in dmm_items:
                        ri_entries.append({
                            '_key': it.get('key', 0),
                            '_stringKey': it.get('string_key', ''),
                            '_isBlocked': it.get('is_blocked', 0),
                            '_isTown': it.get('is_town', 0),
                            '_limitVehicleRun': it.get('limit_vehicle_run', 0),
                            '_isWild': it.get('is_wild', 0),
                            '_vehicleMercenaryAllowType': it.get('vehicle_mercenary_allow_type', 0),
                            '_regionType': it.get('region_type', 0),
                            '_isNonePlayZone': it.get('is_none_play_zone', 0),
                            '_isUIMapDisable': it.get('is_ui_map_disable', 0),
                            '_dmm_ref': it,
                        })
                    log.info("Loaded %d regioninfo entries via dmm_parser", len(ri_entries))
                else:
                    self._regioninfo_dmm_items = None
                    from regioninfo_parser import parse_pabgh_index as ri_idx, parse_region_entry
                    idx_ri = ri_idx(self._regioninfo_schema)
                    sorted_ri = sorted(idx_ri.items(), key=lambda x: x[1])
                    ri_entries = []
                    for i_ri, (rk, ro) in enumerate(sorted_ri):
                        rend = sorted_ri[i_ri + 1][1] if i_ri + 1 < len(sorted_ri) else len(self._regioninfo_data)
                        re_ = parse_region_entry(bytes(self._regioninfo_data), ro, rend)
                        if re_ and '_error' not in re_:
                            re_['_abs_offset'] = ro
                            ri_entries.append(re_)
                    log.info("Loaded %d regioninfo entries via offset parser", len(ri_entries))
                self._regioninfo_entries = ri_entries
                self._regioninfo_populate()
            except Exception:
                log.exception("Could not load regioninfo")
                self._regioninfo_entries = []

            try:
                ci_body = crimson_rs.extract_file(game_path, "0008", dp, "characterinfo.pabgb")
                ci_gh = crimson_rs.extract_file(game_path, "0008", dp, "characterinfo.pabgh")
                self._charinfo_data = bytearray(ci_body)
                self._charinfo_original = bytes(ci_body)
                self._charinfo_schema = bytes(ci_gh)

                # Field-level parse via dmm_parser (primary for Enable Mounts + new features)
                import dmm_parser as _dmp
                import copy as _copy
                self._charinfo_dmm_items = _dmp.parse_table(
                    'character_info', bytes(ci_body), bytes(ci_gh))
                self._charinfo_dmm_vanilla = _copy.deepcopy(self._charinfo_dmm_items)
                self._charinfo_dmm_dirty = False
                log.info("dmm_parser: loaded %d characterinfo entries (field-level)",
                         len(self._charinfo_dmm_items))

                _WEAPON_DMM = {
                    '_upperActionChartPackageGroupName': 'appearance_name',
                    '_lowerActionChartPackageGroupName': 'character_prefab_path',
                    '_characterGamePlayDataName': 'skeleton_name',
                    '_appearanceName': 'lookup_22',
                    '_skeletonName': 'lookup_24',
                }
                self._weapon_dmm_map = _WEAPON_DMM

                import ctypes as _ct_ci
                mount_entries = []
                for cd in self._charinfo_dmm_items:
                    vi = cd.get('vehicle_info', 0)
                    sk = cd.get('string_key', '')
                    if vi != 0 or sk.startswith('Riding_'):
                        mount_entries.append({
                            'name': sk,
                            '_key': cd['key'],
                            '_stringKey': sk,
                            '_vehicleInfo': vi,
                            '_callMercenarySpawnDuration': cd.get('call_mercenary_spawn_duration', 0),
                            '_callMercenaryCoolTime': cd.get('call_mercenary_cool_time', 0),
                            '_mercenaryCoolTimeType': cd.get('mercenary_cool_time_type', 0),
                            'dmm_ref': cd,
                        })
                self._charinfo_mount_entries = mount_entries
                self._mount_populate()
                log.info("Loaded %d mount entries from dmm_parser", len(mount_entries))

                player_names = ('Kliff', 'Damiane', 'Oongka')
                self._charinfo_player_entries = []
                for cd in self._charinfo_dmm_items:
                    sk = cd.get('string_key', '')
                    if sk in player_names:
                        pe = {'name': sk, '_stringKey': sk, '_key': cd['key'], 'dmm_ref': cd}
                        for legacy_name, dmm_name in _WEAPON_DMM.items():
                            pe[legacy_name] = cd.get(dmm_name, 0)
                        self._charinfo_player_entries.append(pe)
                # Lazy-load name resolver so weapon-table cells show readable names
                try:
                    from stringinfo_resolver import StringResolver
                    self._string_resolver = StringResolver()
                    n = self._string_resolver.load_from_game(game_path)
                    log.info("StringResolver loaded %d hash→string entries", n)
                except Exception:
                    log.exception("StringResolver load failed (cells will show hex only)")
                    self._string_resolver = None
                self._weapon_pkg_populate()
                log.info("Loaded %d player chars for runtime weapon edits",
                         len(self._charinfo_player_entries))
            except Exception:
                log.exception("Could not load characterinfo mounts")
                self._charinfo_mount_entries = []
                self._charinfo_player_entries = []

            try:
                wi_body = crimson_rs.extract_file(game_path, "0008", dp, "wantedinfo.pabgb")
                wi_gh = crimson_rs.extract_file(game_path, "0008", dp, "wantedinfo.pabgh")
                self._wantedinfo_data = bytearray(wi_body)
                self._wantedinfo_original = bytes(wi_body)
                self._wantedinfo_schema = bytes(wi_gh)
                log.info("Loaded wantedinfo: %d bytes", len(wi_body))
            except Exception as _wie:
                self._wantedinfo_data = None
                log.warning("Could not load wantedinfo: %s", _wie)

            total_gt = sum(1 for e in self._gptrigger_entries if e['safe_zone_type'] != 0)
            ri_towns = sum(1 for e in self._regioninfo_entries if e.get('_isTown', 0))
            self._field_edit_status.setText(
                f"Loaded {len(entries)} zones + {len(self._vehicle_entries)} vehicles + "
                f"{len(self._gptrigger_entries)} triggers ({total_gt} safe zones) + "
                f"{len(self._regioninfo_entries)} regions ({ri_towns} towns) + "
                f"{len(self._charinfo_mount_entries)} mounts")

        except Exception as e:
            log.exception("FieldEdit load failed")
            self._field_edit_status.setText(f"Error: {e}")
            QMessageBox.critical(self, tr("Extract Failed"), str(e))


    def _field_edit_populate(self):
        self._field_edit_editing = True
        entries = self._field_edit_entries
        self._field_edit_table.setRowCount(len(entries))
        for row, e in enumerate(entries):
            item = QTableWidgetItem(str(e['key']))
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            self._field_edit_table.setItem(row, 0, item)
            name = e.get('name', '') or f"Zone_{e['key']}"
            item = QTableWidgetItem(name)
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            self._field_edit_table.setItem(row, 1, item)
            item = QTableWidgetItem(str(e.get('zone_type', '?')))
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            self._field_edit_table.setItem(row, 2, item)
            ccv = e.get('can_call_vehicle', 0)
            item = QTableWidgetItem(str(ccv))
            orig_ccv = self._field_edit_original[e['can_call_vehicle_offset']]
            if ccv != orig_ccv:
                item.setBackground(QColor(60, 40, 20))
            self._field_edit_table.setItem(row, 3, item)
            acv = e.get('always_call_vehicle_dev', 0)
            item = QTableWidgetItem(str(acv))
            orig_acv = self._field_edit_original[e['always_call_vehicle_dev_offset']]
            if acv != orig_acv:
                item.setBackground(QColor(80, 20, 60))
            if acv:
                item.setForeground(QColor(100, 255, 100))
            self._field_edit_table.setItem(row, 4, item)
            pos = e.get('position', (0, 0, 0))
            item = QTableWidgetItem(f"({pos[0]}, {pos[1]}, {pos[2]})")
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            self._field_edit_table.setItem(row, 5, item)
        self._field_edit_table.resizeColumnsToContents()
        self._field_edit_editing = False

    def _field_edit_cell_changed(self, row, col):
        if self._field_edit_editing or not self._field_edit_entries:
            return
        if col not in (3, 4):
            return
        if row >= len(self._field_edit_entries):
            return
        cell = self._field_edit_table.item(row, col)
        if not cell:
            return
        try:
            new_val = int(cell.text())
        except ValueError:
            return
        new_val = max(0, min(new_val, 1))
        e = self._field_edit_entries[row]
        if col == 3:
            off = e['can_call_vehicle_offset']
            self._field_edit_data[off] = new_val
            e['can_call_vehicle'] = new_val
        elif col == 4:
            off = e['always_call_vehicle_dev_offset']
            self._field_edit_data[off] = new_val
            e['always_call_vehicle_dev'] = new_val
        self._field_edit_modified = True
        self._field_edit_status.setText(f"Modified zone {e['key']}")

    def _vehicle_populate(self):
        self._vehicle_editing = True
        entries = self._vehicle_entries
        self._vehicle_table.setRowCount(len(entries))
        for row, e in enumerate(entries):
            item = QTableWidgetItem(str(e['key']))
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            self._vehicle_table.setItem(row, 0, item)
            item = QTableWidgetItem(e['name'])
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            self._vehicle_table.setItem(row, 1, item)
            item = QTableWidgetItem(str(e['vehicle_type']))
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            self._vehicle_table.setItem(row, 2, item)
            vt = e['voxel_type']
            item = QTableWidgetItem(f"{vt} ({'fly' if vt == 7 else 'ground'})")
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            self._vehicle_table.setItem(row, 3, item)
            mct = e['mount_call_type']
            item = QTableWidgetItem(str(mct))
            van = next((v for v in self._vehicleinfo_dmm_vanilla if v['key'] == e['key']), None)
            orig_mct = ((van or {}).get('rider_detect_info', 0) >> 8) & 0xFF if van else mct
            if mct != orig_mct:
                item.setBackground(QColor(60, 40, 20))
            item.setToolTip("0=siege/util, 1=rideable, 2=flying")
            self._vehicle_table.setItem(row, 4, item)
            ccsz = e['can_call_safe_zone']
            item = QTableWidgetItem(str(ccsz))
            orig_ccsz = (van or {}).get('send_damage_to', ccsz) if van else ccsz
            if ccsz != orig_ccsz:
                item.setBackground(QColor(80, 20, 60))
            if ccsz:
                item.setForeground(QColor(100, 255, 100))
            item.setToolTip("0=restricted, 1=can call in safe zones (towns)")
            self._vehicle_table.setItem(row, 5, item)
            alt = e['altitude_cap']
            if alt > 1e30:
                item = QTableWidgetItem("999999")
            else:
                item = QTableWidgetItem(f"{alt:.0f}")
            import ctypes as _ct_vp
            orig_mah = (van or {}).get('max_allowable_height', 0) if van else 0
            orig_alt = _ct_vp.c_float.from_buffer_copy(_ct_vp.c_uint32(orig_mah & 0xFFFFFFFF)).value
            if abs(alt - orig_alt) > 0.1:
                item.setBackground(QColor(60, 40, 20))
            item.setToolTip("Max flight altitude. Dragon=1350, rest=999999 (no cap)")
            self._vehicle_table.setItem(row, 6, item)
        self._vehicle_table.resizeColumnsToContents()
        self._vehicle_editing = False

    def _vehicle_cell_changed(self, row, col):
        if self._vehicle_editing or not self._vehicle_entries:
            return
        if col not in (4, 5, 6):
            return
        if row >= len(self._vehicle_entries):
            return
        cell = self._vehicle_table.item(row, col)
        if not cell:
            return
        e = self._vehicle_entries[row]
        vd = e.get('dmm_ref')
        if col == 6:
            try:
                new_val = float(cell.text())
            except ValueError:
                return
            new_val = max(0, new_val)
            if vd is not None:
                import ctypes as _ct_vc
                vd['max_allowable_height'] = _ct_vc.c_uint32.from_buffer_copy(
                    _ct_vc.c_float(new_val)).value
            e['altitude_cap'] = new_val
            self._field_edit_modified = True
            self._vehicleinfo_dmm_dirty = True
            self._field_edit_status.setText(f"Set {e['name']} altitude cap to {new_val:.0f}")
            return
        try:
            new_val = int(cell.text())
        except ValueError:
            return
        new_val = max(0, min(new_val, 255))
        if col == 4:
            if vd is not None:
                rdi = vd.get('rider_detect_info', 0)
                vd['rider_detect_info'] = (rdi & 0xFF) | (new_val << 8)
            e['mount_call_type'] = new_val
        elif col == 5:
            if vd is not None:
                vd['send_damage_to'] = new_val
            e['can_call_safe_zone'] = new_val
        self._field_edit_modified = True
        self._field_edit_status.setText(f"Modified vehicle {e['name']}")

    def _gptrigger_populate(self):
        self._gptrigger_editing = True
        show_all = self._gt_filter_combo.currentData() == "all"
        entries = self._gptrigger_entries
        filtered = entries if show_all else [e for e in entries if e['safe_zone_type'] != 0]
        self._gt_table.setRowCount(len(filtered))
        self._gt_filtered = filtered
        for row, e in enumerate(filtered):
            item = QTableWidgetItem(str(e['key']))
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            item.setData(Qt.UserRole, row)
            self._gt_table.setItem(row, 0, item)
            name = e['name'].replace('GamePlayTrigger_SafeZone_', '').replace('GamePlayTrigger_', '')
            item = QTableWidgetItem(name)
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            self._gt_table.setItem(row, 1, item)
            item = QTableWidgetItem(f"[{e['flag1']},{e['flag2']},{e['flag3']}]")
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            self._gt_table.setItem(row, 2, item)
            szt = e['safe_zone_type']
            item = QTableWidgetItem(str(szt))
            orig_szt = self._gptrigger_original[e['safe_zone_type_offset']]
            if szt != orig_szt:
                item.setBackground(QColor(80, 20, 60))
            if szt != 0:
                item.setForeground(QColor(255, 180, 80))
            self._gt_table.setItem(row, 3, item)
        self._gt_table.resizeColumnsToContents()
        self._gptrigger_editing = False

    def _regioninfo_populate(self):
        self._regioninfo_editing = True
        show_all = self._ri_filter_combo.currentData() == "all"
        entries = self._regioninfo_entries
        filtered = entries if show_all else [e for e in entries if e.get('_isTown', 0) or e.get('_limitVehicleRun', 0)]
        region_types = {3: 'World', 4: 'Continent', 5: 'Territory', 6: 'Area', 7: 'Node', 8: 'SubNode'}
        self._ri_filtered = filtered
        self._ri_table.setRowCount(len(filtered))
        for row, e in enumerate(filtered):
            item = QTableWidgetItem(str(e.get('_key', '?')))
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            item.setData(Qt.UserRole, row)
            self._ri_table.setItem(row, 0, item)
            name = e.get('_stringKey', f"Region_{e.get('_key', '?')}")
            item = QTableWidgetItem(name)
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            self._ri_table.setItem(row, 1, item)
            rt = e.get('_regionType', 0)
            item = QTableWidgetItem(f"{rt} ({region_types.get(rt, '?')})")
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            self._ri_table.setItem(row, 2, item)
            town = e.get('_isTown', 0)
            item = QTableWidgetItem(str(town))
            if town:
                item.setForeground(QColor(255, 180, 80))
            self._ri_table.setItem(row, 3, item)
            lvr = e.get('_limitVehicleRun', 0)
            item = QTableWidgetItem(str(lvr))
            if lvr:
                item.setForeground(QColor(255, 80, 80))
            self._ri_table.setItem(row, 4, item)
            item = QTableWidgetItem(str(e.get('_isWild', 0)))
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            self._ri_table.setItem(row, 5, item)
            vmat = e.get('_vehicleMercenaryAllowType', 0)
            item = QTableWidgetItem(str(vmat))
            self._ri_table.setItem(row, 6, item)
        self._ri_table.resizeColumnsToContents()
        self._regioninfo_editing = False

    def _ri_cell_changed(self, row, col):
        if self._regioninfo_editing or not self._regioninfo_entries:
            return
        if col not in (3, 4, 6):
            return
        item0 = self._ri_table.item(row, 0)
        if not item0: return
        ridx = item0.data(Qt.UserRole)
        if ridx is None or not hasattr(self, '_ri_filtered') or ridx >= len(self._ri_filtered):
            return
        e = self._ri_filtered[ridx]
        cell = self._ri_table.item(row, col)
        if not cell: return
        try:
            new_val = int(cell.text())
        except ValueError:
            return
        new_val = max(0, min(new_val, 255))
        field_map = {3: '_isTown', 4: '_limitVehicleRun', 6: '_vehicleMercenaryAllowType'}
        dmm_field_map = {3: 'is_town', 4: 'limit_vehicle_run', 6: 'vehicle_mercenary_allow_type'}

        dmm_ref = e.get('_dmm_ref')
        if dmm_ref:
            dmm_ref[dmm_field_map[col]] = new_val
            e[field_map[col]] = new_val
            from regioninfo_parser import serialize_all_dmm as ri_ser
            new_data = ri_ser(self._regioninfo_dmm_items)
            if new_data:
                self._regioninfo_data = bytearray(new_data)
        else:
            e[field_map[col]] = new_val
        self._field_edit_modified = True
        self._field_edit_status.setText(
            f"Modified region {e.get('_stringKey', '?')} {field_map[col]}={new_val}")

    def _mount_populate(self):
        self._charinfo_editing = True
        from characterinfo_full_parser import MOUNT_VEHICLE_TYPES
        entries = self._charinfo_mount_entries
        self._mount_table.setRowCount(len(entries))
        for row, e in enumerate(entries):
            item = QTableWidgetItem(e.get('name', '?'))
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            item.setData(Qt.UserRole, row)
            self._mount_table.setItem(row, 0, item)
            vtype = e.get('_vehicleInfo', 0)
            vname = MOUNT_VEHICLE_TYPES.get(vtype, str(vtype))
            item = QTableWidgetItem(vname)
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            self._mount_table.setItem(row, 1, item)
            dur = e.get('_callMercenarySpawnDuration', 0)
            item = QTableWidgetItem(str(dur))
            van_m = next((v for v in self._charinfo_dmm_vanilla if v['key'] == e.get('_key')), None)
            orig_dur = (van_m or {}).get('call_mercenary_spawn_duration', dur)
            if dur != orig_dur:
                item.setBackground(QColor(60, 40, 20))
            if dur > 0:
                item.setForeground(QColor(255, 180, 80))
            self._mount_table.setItem(row, 2, item)
            cool = e.get('_callMercenaryCoolTime', 0)
            item = QTableWidgetItem(str(cool))
            orig_cool = (van_m or {}).get('call_mercenary_cool_time', cool)
            if cool != orig_cool:
                item.setBackground(QColor(60, 40, 20))
            if cool > 0:
                item.setForeground(QColor(255, 100, 100))
            self._mount_table.setItem(row, 3, item)
            item = QTableWidgetItem(str(e.get('_mercenaryCoolTimeType', 0)))
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            self._mount_table.setItem(row, 4, item)
        self._mount_table.resizeColumnsToContents()
        self._charinfo_editing = False

    def _mount_cell_changed(self, row, col):
        if self._charinfo_editing or not self._charinfo_mount_entries:
            return
        if col not in (2, 3):
            return
        item0 = self._mount_table.item(row, 0)
        if not item0: return
        ridx = item0.data(Qt.UserRole)
        if ridx is None or ridx >= len(self._charinfo_mount_entries):
            return
        e = self._charinfo_mount_entries[ridx]
        cell = self._mount_table.item(row, col)
        if not cell: return
        try:
            new_val = int(cell.text())
        except ValueError:
            return
        new_val = max(0, new_val)
        cd = e.get('dmm_ref')
        if col == 2:
            if cd is not None:
                cd['call_mercenary_spawn_duration'] = new_val
            e['_callMercenarySpawnDuration'] = new_val
            label = f"duration={new_val}s"
        else:
            if cd is not None:
                cd['call_mercenary_cool_time'] = new_val
            e['_callMercenaryCoolTime'] = new_val
            label = f"cooldown={new_val}s"
        self._charinfo_dmm_dirty = True
        self._field_edit_modified = True
        self._field_edit_status.setText(f"Modified {e.get('name', '?')} {label}")


    def _weapon_player_order(self):
        order = ('Kliff', 'Damian', 'Oongka')
        return sorted(
            self._charinfo_player_entries or [],
            key=lambda e: order.index(e['name']) if e.get('name') in order else 99,
        )

    def _weapon_pkg_populate(self):
        self._weapon_pkg_editing = True
        try:
            entries = self._weapon_player_order()
            self._weapon_pkg_table.setRowCount(len(entries))

            self._weapon_pkg_entries = entries
            for row, e in enumerate(entries):
                name_item = QTableWidgetItem(e.get('name', '?'))
                name_item.setFlags(name_item.flags() & ~Qt.ItemIsEditable)
                name_item.setData(Qt.UserRole, row)
                self._weapon_pkg_table.setItem(row, 0, name_item)
                for col_idx, col in enumerate(self._WEAPON_COLS, start=1):
                    self._weapon_refresh_cell(row, col_idx, e, col[0])
            self._weapon_pkg_table.resizeColumnsToContents()
        finally:
            self._weapon_pkg_editing = False

    def _weapon_refresh_cell(self, row: int, col: int, entry: dict, field: str) -> None:
        dmm_field = getattr(self, '_weapon_dmm_map', {}).get(field, field)
        cd = entry.get('dmm_ref', {})
        cur = cd.get(dmm_field, 0) if cd else 0
        van = next((v for v in self._charinfo_dmm_vanilla if v['key'] == entry.get('_key')), None)
        orig = (van or {}).get(dmm_field, cur)
        cur_name = self._weapon_resolve_hash(cur)
        orig_name = self._weapon_resolve_hash(orig)
        text = cur_name if cur_name else f'0x{cur:08x}'
        item = QTableWidgetItem(text)
        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
        item.setData(Qt.UserRole, field)
        cur_display = f'{cur_name} (0x{cur:08x})' if cur_name else f'0x{cur:08x}'
        orig_display = f'{orig_name} (0x{orig:08x})' if orig_name else f'0x{orig:08x}'
        if cur != orig:
            item.setBackground(QColor(60, 40, 20))
            item.setForeground(QColor(255, 180, 80))
            item.setToolTip(
                f'{field}\nVanilla: {orig_display}\nCurrent: {cur_display} (modified)\n'
                f'Double-click to pick a new source character.'
            )
        else:
            item.setToolTip(
                f'{field}\nValue: {cur_display}\nDouble-click to pick a source character.'
            )
        self._weapon_pkg_table.setItem(row, col, item)

    def _weapon_resolve_hash(self, h: int):
        if self._string_resolver is None:
            return None
        return self._string_resolver.resolve(h)

    def _weapon_cell_double_clicked(self, row: int, col: int) -> None:
        if col == 0:
            return
        if not self._charinfo_data or not self._charinfo_player_entries:
            return
        name_item = self._weapon_pkg_table.item(row, 0)
        cell_item = self._weapon_pkg_table.item(row, col)
        if not name_item or not cell_item:
            return
        tidx = name_item.data(Qt.UserRole)
        field = cell_item.data(Qt.UserRole)
        if tidx is None or not field or not hasattr(self, '_weapon_pkg_entries'):
            return
        if tidx >= len(self._weapon_pkg_entries):
            return
        target = self._weapon_pkg_entries[tidx]

        from PySide6.QtWidgets import QListWidget, QListWidgetItem, QLineEdit, QComboBox

        dlg = QDialog(self)
        dlg.setWindowTitle(tr(f"Pick source for {target['name']}.{field}"))
        dlg.resize(720, 600)
        v = QVBoxLayout(dlg)

        info = QLabel(tr(
            f"Source value will be written into {target['name']}'s {field}.\n"
            f"Filter to narrow the list of candidates. Bosses/dragons/NPCs may "
            f"crash the game when used as source — experiment carefully."
        ))
        info.setWordWrap(True)
        v.addWidget(info)

        catalog_by_key = self._weapon_get_catalog()
        all_entries = self._weapon_get_all_entries()

        ctrl_row = QHBoxLayout()
        ctrl_row.addWidget(QLabel(tr("Category:")))
        cat_combo = QComboBox()
        CATEGORY_ORDER = ['All', 'Hero', 'Boss', 'MiddleBoss', 'Dragon',
                          'Mount', 'NPC', 'Pet', 'Other']
        for c in CATEGORY_ORDER:
            cat_combo.addItem(c)
        ctrl_row.addWidget(cat_combo)
        ctrl_row.addWidget(QLabel(tr("Search:")))
        search_edit = QLineEdit()
        search_edit.setPlaceholderText(tr("name or display..."))
        ctrl_row.addWidget(search_edit, 1)
        v.addLayout(ctrl_row)

        list_widget = QListWidget()
        v.addWidget(list_widget, 1)

        sample_label = QLabel(tr("(no selection)"))
        sample_label.setStyleSheet(f"color: {COLORS['text_dim']}; padding: 4px;")
        v.addWidget(sample_label)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        ok_btn = QPushButton(tr("Apply"))
        ok_btn.setEnabled(False)
        cancel_btn = QPushButton(tr("Cancel"))
        btn_row.addWidget(ok_btn)
        btn_row.addWidget(cancel_btn)
        v.addLayout(btn_row)
        cancel_btn.clicked.connect(dlg.reject)

        def _category_for(entry):
            ck = int(entry.get('entry_key', 0))
            cat_info = catalog_by_key.get(ck)
            if cat_info:
                return cat_info.get('category', 'Other'), cat_info.get('display_name', '')
            nm = (entry.get('name') or '').lower()
            if nm in ('kliff', 'damian', 'oongka'):
                return 'Hero', entry.get('name', '')
            return 'Other', ''

        def _refresh():
            list_widget.clear()
            cat = cat_combo.currentText()
            q = search_edit.text().strip().lower()
            shown = 0
            for e in all_entries:
                ec, disp = _category_for(e)
                if cat != 'All' and ec != cat:
                    continue
                nm = e.get('name', '')
                if q and q not in nm.lower() and q not in disp.lower():
                    continue
                _wdm = getattr(self, '_weapon_dmm_map', {})
                dmm_f = _wdm.get(field, field)
                cd_van = next((v for v in self._charinfo_dmm_vanilla if v['key'] == e.get('_key', e.get('key', 0))), None)
                if cd_van is None:
                    continue
                val = cd_van.get(dmm_f, 0)
                resolved = self._weapon_resolve_hash(val)
                val_display = resolved if resolved else f'0x{val:08x}'
                label = f"[{ec:<10}] {nm}  →  {val_display}"
                if disp:
                    label += f"   ({disp})"
                item = QListWidgetItem(label)
                item.setData(Qt.UserRole, val)
                item.setData(Qt.UserRole + 1, e.get('_key', e.get('key', 0)))
                list_widget.addItem(item)
                shown += 1
                if shown >= 800:
                    list_widget.addItem(QListWidgetItem(
                        f"  …{len(all_entries)-shown} more (refine the filter)"))
                    break

        cat_combo.currentIndexChanged.connect(_refresh)
        search_edit.textChanged.connect(_refresh)

        def _on_select():
            it = list_widget.currentItem()
            if not it:
                ok_btn.setEnabled(False)
                sample_label.setText("(no selection)")
                return
            data = it.data(Qt.UserRole)
            if not data:
                ok_btn.setEnabled(False)
                return
            src_e, val = data
            ok_btn.setEnabled(True)
            resolved = self._weapon_resolve_hash(val)
            val_display = f'{resolved} (0x{val:08x})' if resolved else f'0x{val:08x}'
            sample_label.setText(
                f"Will write {val_display} into {target['name']}.{field} "
                f"(source: {src_e.get('name', '?')})"
            )
        list_widget.currentItemChanged.connect(lambda *_: _on_select())
        list_widget.itemDoubleClicked.connect(lambda *_: ok_btn.click())
        ok_btn.clicked.connect(dlg.accept)

        _refresh()

        if dlg.exec() != QDialog.Accepted:
            return
        it = list_widget.currentItem()
        if not it:
            return
        val = it.data(Qt.UserRole)
        self._weapon_write_field(target, field, int(val), 'source')

    def _weapon_write_field(self, target: dict, field: str, value: int,
                            source_label: str) -> None:
        dmm_field = getattr(self, '_weapon_dmm_map', {}).get(field, field)
        cd = target.get('dmm_ref')
        if cd is not None:
            cd[dmm_field] = value
        target[field] = value
        self._field_edit_modified = True
        self._charinfo_dmm_dirty = True
        for row in range(self._weapon_pkg_table.rowCount()):
            ni = self._weapon_pkg_table.item(row, 0)
            ri = ni.data(Qt.UserRole) if ni else None
            if ri is not None and hasattr(self, '_weapon_pkg_entries') and ri < len(self._weapon_pkg_entries) and self._weapon_pkg_entries[ri] is target:
                for col_idx, col in enumerate(self._WEAPON_COLS, start=1):
                    if col[0] == field:
                        self._weapon_refresh_cell(row, col_idx, target, field)
                        break
                break
        self._field_edit_status.setText(
            f"Set {target['name']}.{field} ← {source_label} (0x{value:08x})"
        )

    def _weapon_get_catalog(self) -> dict:
        if hasattr(self, '_weapon_catalog_by_key'):
            return self._weapon_catalog_by_key
        result: dict = {}
        for base in [
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__)))), 'data'),
            getattr(sys, '_MEIPASS', '') or '',
            os.path.join(getattr(sys, '_MEIPASS', '') or '', 'data'),
            os.getcwd(),
            os.path.join(os.getcwd(), 'data'),
        ]:
            p = os.path.join(base, 'character_catalog.json')
            if os.path.isfile(p):
                try:
                    with open(p, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    for c in data.get('characters', []) or []:
                        result[int(c.get('character_key', 0))] = c
                    log.info("weapon catalog: loaded %d chars from %s",
                             len(result), p)
                    break
                except Exception:
                    log.exception("weapon catalog load failed: %s", p)
        self._weapon_catalog_by_key = result
        return result

    def _weapon_get_all_entries(self) -> list:
        if hasattr(self, '_weapon_all_entries') and self._weapon_all_entries:
            return self._weapon_all_entries
        try:
            from characterinfo_full_parser import parse_all_entries
            self._weapon_all_entries = parse_all_entries(
                bytes(self._charinfo_data), self._charinfo_schema)
        except Exception:
            log.exception("weapon all-entries parse failed")
            self._weapon_all_entries = list(self._charinfo_player_entries or [])
        return self._weapon_all_entries

    def _weapon_save_preset(self) -> None:
        if not self._charinfo_data or not self._charinfo_player_entries:
            QMessageBox.information(self, tr("Save Preset"),
                tr("Load FieldInfo first."))
            return
        path, _ = QFileDialog.getSaveFileName(
            self, tr("Save runtime-package preset"),
            "", "JSON files (*.json)")
        if not path:
            return
        if not path.lower().endswith('.json'):
            path += '.json'
        preset = {
            'kind': 'crimson_runtime_package_preset',
            'version': 1,
            'characters': [],
        }
        _wdm = getattr(self, '_weapon_dmm_map', {})
        for e in self._weapon_player_order():
            char_data = {'name': e['name'], 'fields': {}}
            cd = e.get('dmm_ref', {})
            van = next((v for v in self._charinfo_dmm_vanilla if v['key'] == e.get('_key')), {})
            for col in self._WEAPON_COLS:
                field = col[0]
                dmm_f = _wdm.get(field, field)
                cur = cd.get(dmm_f, 0)
                orig = van.get(dmm_f, 0)
                char_data['fields'][field] = {
                    'value': cur,
                    'vanilla': orig,
                    'modified': cur != orig,
                }
            preset['characters'].append(char_data)
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(preset, f, indent=2)
            self._field_edit_status.setText(
                f"Saved preset → {os.path.basename(path)}"
            )
        except Exception as e:
            QMessageBox.critical(self, tr("Save Preset"),
                f"Could not save:\n{e}")

    def _weapon_load_preset(self) -> None:
        if not self._charinfo_data or not self._charinfo_player_entries:
            QMessageBox.information(self, tr("Load Preset"),
                tr("Load FieldInfo first."))
            return
        path, _ = QFileDialog.getOpenFileName(
            self, tr("Load runtime-package preset"),
            "", "JSON files (*.json)")
        if not path:
            return
        try:
            with open(path, 'r', encoding='utf-8') as f:
                preset = json.load(f)
        except Exception as e:
            QMessageBox.critical(self, tr("Load Preset"),
                f"Could not read:\n{e}")
            return
        if preset.get('kind') != 'crimson_runtime_package_preset':
            QMessageBox.warning(self, tr("Load Preset"),
                tr("File is not a runtime-package preset."))
            return

        by_name = {e['name']: e for e in self._charinfo_player_entries}
        applied = 0
        skipped = 0
        _wdm = getattr(self, '_weapon_dmm_map', {})
        for cd_p in preset.get('characters', []) or []:
            target = by_name.get(cd_p.get('name'))
            if not target:
                skipped += 1
                continue
            for field, fdata in (cd_p.get('fields') or {}).items():
                dmm_f = _wdm.get(field)
                if not dmm_f:
                    continue
                value = int(fdata.get('value', 0))
                self._weapon_write_field(target, field, value, 'preset')
                applied += 1
        self._field_edit_modified = True
        self._weapon_pkg_populate()
        self._field_edit_status.setText(
            f"Loaded preset: {applied} field(s) applied"
            + (f", {skipped} char(s) skipped (not present)" if skipped else "")
        )

    def _weapon_open_siblings_dialog(self) -> None:
        if not self._charinfo_data:
            QMessageBox.information(self, tr("Find Siblings"),
                tr("Load FieldInfo first."))
            return

        from PySide6.QtWidgets import QListWidget, QListWidgetItem, QLineEdit, QComboBox

        dlg = QDialog(self)
        dlg.setWindowTitle(tr("Find Package Siblings"))
        dlg.resize(820, 640)
        v = QVBoxLayout(dlg)

        info = QLabel(tr(
            "Lists every character that shares a chosen Name-hash value. Useful for "
            "discovering 'package families' — e.g. all chars with Damian's upper-action "
            "package can fire guns, all chars with Kliff's gameplay-data behave like archers."
        ))
        info.setWordWrap(True)
        v.addWidget(info)

        all_entries = self._weapon_get_all_entries()
        catalog = self._weapon_get_catalog()

        # Field + value selection row
        field_row = QHBoxLayout()
        field_row.addWidget(QLabel(tr("Field:")))
        field_combo = QComboBox()
        for col in self._WEAPON_COLS:
            field, label = col[0], col[1]
            field_combo.addItem(label, field)
        field_row.addWidget(field_combo)

        field_row.addWidget(QLabel(tr("Value (hex):")))
        value_edit = QLineEdit()
        value_edit.setPlaceholderText("0x________ or pick a char below")
        value_edit.setFixedWidth(180)
        field_row.addWidget(value_edit)

        field_row.addWidget(QLabel(tr("…or copy from:")))
        copy_combo = QComboBox()
        copy_combo.addItem('—', None)
        for e in self._weapon_player_order():
            copy_combo.addItem(e['name'], e['name'])
        field_row.addWidget(copy_combo)
        field_row.addStretch()
        v.addLayout(field_row)

        results = QListWidget()
        v.addWidget(results, 1)

        summary = QLabel("")
        summary.setStyleSheet(f"color: {COLORS['accent']}; padding: 4px;")
        v.addWidget(summary)

        btns = QHBoxLayout()
        btns.addStretch()
        close_btn = QPushButton(tr("Close"))
        close_btn.clicked.connect(dlg.accept)
        btns.addWidget(close_btn)
        v.addLayout(btns)

        def _category_for(entry):
            ck = int(entry.get('entry_key', 0))
            cat_info = catalog.get(ck)
            if cat_info:
                return cat_info.get('category', 'Other'), cat_info.get('display_name', '')
            nm = (entry.get('name') or '').lower()
            if nm in ('kliff', 'damian', 'oongka'):
                return 'Hero', entry.get('name', '')
            return 'Other', ''

        def _do_search():
            results.clear()
            field = field_combo.currentData()
            txt = value_edit.text().strip()
            if not field or not txt:
                summary.setText("Enter a value (or pick a char from the dropdown).")
                return
            try:
                target_val = int(txt, 16) if txt.startswith('0x') else int(txt, 16)
            except ValueError:
                summary.setText(f"Could not parse '{txt}' as hex.")
                return
            matches = []
            _wdm2 = getattr(self, '_weapon_dmm_map', {})
            for e in all_entries:
                _df = _wdm2.get(field)
                _cd_v = next((v for v in self._charinfo_dmm_vanilla if v['key'] == e.get('_key', e.get('key', 0))), None)
                if not _df or _cd_v is None:
                    continue
                v_ = _cd_v.get(_df, 0)
                if v_ == target_val:
                    matches.append(e)
            cat_counts: dict[str, int] = {}
            for e in matches[:1000]:
                ec, disp = _category_for(e)
                cat_counts[ec] = cat_counts.get(ec, 0) + 1
                label = f"[{ec:<10}] {e.get('name','?')}"
                if disp:
                    label += f"   ({disp})"
                results.addItem(label)
            cat_str = ", ".join(f"{k}:{v}" for k, v in
                                sorted(cat_counts.items(), key=lambda kv: -kv[1]))
            resolved = self._weapon_resolve_hash(target_val)
            val_display = f'{resolved} (0x{target_val:08x})' if resolved else f'0x{target_val:08x}'
            summary.setText(
                f"{len(matches)} character(s) share {field} = {val_display} → {cat_str}"
            )

        def _on_copy(*_):
            src_name = copy_combo.currentData()
            if not src_name:
                return
            field = field_combo.currentData()
            src = next((e for e in self._charinfo_player_entries
                        if e.get('name') == src_name), None)
            if not src or not field:
                return
            _wdm3 = getattr(self, '_weapon_dmm_map', {})
            _df3 = _wdm3.get(field)
            _v3 = next((v for v in self._charinfo_dmm_vanilla if v['key'] == src.get('_key')), None)
            if not _df3 or _v3 is None:
                return
            val = _v3.get(_df3, 0)
            value_edit.setText(f'0x{val:08x}')
            _do_search()

        copy_combo.currentIndexChanged.connect(_on_copy)
        field_combo.currentIndexChanged.connect(
            lambda *_: _on_copy() if copy_combo.currentData() else None)
        value_edit.returnPressed.connect(_do_search)

        # Auto-fill: pick Kliff + upper as starting suggestion
        if copy_combo.count() > 1:
            copy_combo.setCurrentIndex(1)
            _on_copy()

        dlg.exec()

    def _weapon_open_slot_inspector(self) -> None:
        from PySide6.QtWidgets import (QListWidget, QListWidgetItem, QSplitter,
                                       QGroupBox, QPlainTextEdit, QComboBox,
                                       QCheckBox, QFrame)

        game_path = self._config.get("game_install_path", "")
        if not game_path or not os.path.isdir(game_path):
            QMessageBox.warning(self, tr("Slot Inspector"),
                tr("Set the game install path first."))
            return

        try:
            import crimson_rs
            xml_bytes = crimson_rs.extract_file(
                game_path, '0010', 'actionchart/xml/description',
                'characteractionpackagedescription.xml')
            xml_text = bytes(xml_bytes).decode('utf-8', errors='replace')
        except Exception as e:
            QMessageBox.critical(self, tr("Slot Inspector"),
                f"Could not extract characteractionpackagedescription.xml:\n{e}")
            return

        try:
            from actionchart_descriptor import (parse_descriptor, patch_descriptor,
                                                diff_packages)
        except Exception as e:
            QMessageBox.critical(self, tr("Slot Inspector"),
                f"actionchart_descriptor module not available:\n{e}")
            return

        packages = parse_descriptor(xml_text)
        # Working copy — modifications stay in `working` until user saves
        import copy as _copy
        working = _copy.deepcopy(packages)

        dlg = QDialog(self)
        dlg.setWindowTitle(tr(
            "Slot Inspector — extend an action-chart package surgically"))
        dlg.resize(1300, 800)
        v = QVBoxLayout(dlg)

        info = QLabel(tr(
            "Pick a TARGET package (left — usually Player_Kliff) and a SOURCE "
            "package (right — Player_PHW for guns, Boss_Myurdin_Intro_UpperAction "
            "for boss sword combos). Slots only present in SOURCE are shown in "
            "yellow. Check the ones you want to inject, then click 'Apply Selected "
            "Injections' to extend the target. Save the patched XML to deploy."
        ))
        info.setWordWrap(True)
        info.setStyleSheet(f"color: {COLORS['text_dim']}; padding: 6px;")
        v.addWidget(info)

        # Top picker row
        pick_row = QHBoxLayout()
        pick_row.addWidget(QLabel(tr("Target (will be modified):")))
        tgt_combo = QComboBox()
        tgt_combo.setMaxVisibleItems(30)
        for nm in sorted(packages.keys()):
            tgt_combo.addItem(f"{nm}  ({len(packages[nm].subpackages)} slots)", nm)
        # Default to Player_Kliff
        for i in range(tgt_combo.count()):
            if tgt_combo.itemData(i) == 'Player_Kliff':
                tgt_combo.setCurrentIndex(i); break
        pick_row.addWidget(tgt_combo, 1)

        pick_row.addWidget(QLabel(tr("Source (donor):")))
        src_combo = QComboBox()
        src_combo.setMaxVisibleItems(30)
        for nm in sorted(packages.keys()):
            src_combo.addItem(f"{nm}  ({len(packages[nm].subpackages)} slots)", nm)
        # Default to Player_PHW
        for i in range(src_combo.count()):
            if src_combo.itemData(i) == 'Player_PHW':
                src_combo.setCurrentIndex(i); break
        pick_row.addWidget(src_combo, 1)
        v.addLayout(pick_row)

        # Splitter: left = target slots, middle = diff/inject panel, right = source slots
        splitter = QSplitter(Qt.Horizontal)

        tgt_list = QListWidget()
        tgt_list.setAlternatingRowColors(True)
        tgt_box = QGroupBox(tr("Target slots (current)"))
        tgt_layout = QVBoxLayout(tgt_box)
        tgt_layout.addWidget(tgt_list)
        splitter.addWidget(tgt_box)

        diff_widget = QWidget()
        diff_layout = QVBoxLayout(diff_widget)
        diff_layout.addWidget(QLabel(tr("Source-only slots (injectable):")))
        diff_list = QListWidget()
        diff_list.setAlternatingRowColors(True)
        diff_layout.addWidget(diff_list, 1)

        check_all_btn = QPushButton(tr("Check all"))
        uncheck_all_btn = QPushButton(tr("Uncheck all"))
        check_row = QHBoxLayout()
        check_row.addWidget(check_all_btn)
        check_row.addWidget(uncheck_all_btn)
        diff_layout.addLayout(check_row)

        diff_summary = QLabel("")
        diff_summary.setStyleSheet(f"color: {COLORS['accent']}; padding: 4px;")
        diff_layout.addWidget(diff_summary)

        inject_btn = QPushButton(tr("→ Apply Selected Injections to Target"))
        inject_btn.setStyleSheet(
            "background-color: #2E7D32; color: white; font-weight: bold; padding: 6px;")
        inject_btn.setEnabled(False)
        diff_layout.addWidget(inject_btn)

        revert_btn = QPushButton(tr("⟲ Revert Target to Vanilla"))
        diff_layout.addWidget(revert_btn)

        splitter.addWidget(diff_widget)

        src_list = QListWidget()
        src_list.setAlternatingRowColors(True)
        src_box = QGroupBox(tr("Source slots (donor)"))
        src_layout = QVBoxLayout(src_box)
        src_layout.addWidget(src_list)
        splitter.addWidget(src_box)

        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 4)
        splitter.setStretchFactor(2, 3)
        v.addWidget(splitter, 1)

        # Bottom action row: save / deploy
        save_row = QHBoxLayout()
        save_xml_btn = QPushButton(tr("Save Patched XML to disk..."))
        save_xml_btn.setToolTip(tr(
            "Save the modified characteractionpackagedescription.xml to a folder\n"
            "of your choice. Deploy via CdModCreator or pack into a 0010 overlay."
        ))
        save_row.addWidget(save_xml_btn)

        save_row.addWidget(QLabel(tr("PAZ 0010 overlay group:")))
        ac_overlay_spin = QSpinBox()
        ac_overlay_spin.setRange(40, 9999)
        ac_overlay_spin.setValue(self._config.get('actionchart_overlay_dir', 40))
        ac_overlay_spin.setFixedWidth(70)
        ac_overlay_spin.setToolTip(tr(
            "Overlay group number for PAZ 0010. Default 40. Must NOT collide\n"
            "with FieldEdit's group (default 39) — different PAZ origins."
        ))
        ac_overlay_spin.valueChanged.connect(
            lambda val: self._config.update({'actionchart_overlay_dir': int(val)}))
        save_row.addWidget(ac_overlay_spin)

        deploy_btn = QPushButton(tr("Deploy as 0010 Overlay"))
        deploy_btn.setStyleSheet(
            "background-color: #4A148C; color: white; font-weight: bold;")
        deploy_btn.setToolTip(tr(
            "Pack the modified XML into a PAZ overlay and deploy to the game.\n"
            "Restart the game for the change to take effect.\n"
            "Use 'Restore' below to remove."
        ))
        save_row.addWidget(deploy_btn)

        restore_btn = QPushButton(tr("Restore (remove overlay)"))
        save_row.addWidget(restore_btn)

        save_row.addStretch()
        close_btn = QPushButton(tr("Close"))
        save_row.addWidget(close_btn)
        v.addLayout(save_row)

        status = QLabel("")
        status.setStyleSheet(f"color: {COLORS['text_dim']}; padding: 4px;")
        v.addWidget(status)

        close_btn.clicked.connect(dlg.accept)

        def _refresh_lists():
            tgt_name = tgt_combo.currentData()
            src_name = src_combo.currentData()
            if not tgt_name or not src_name:
                return
            tgt = working[tgt_name]
            src = working[src_name]
            vanilla_tgt = packages[tgt_name]

            # Target list — mark injected vs vanilla
            vanilla_pairs = {(sp.slot, sp.file) for sp in vanilla_tgt.subpackages}
            tgt_list.clear()
            for sp in tgt.subpackages:
                injected = (sp.slot, sp.file) not in vanilla_pairs
                tag = ' [INJECTED]' if injected else ''
                item = QListWidgetItem(f"{sp.slot:<14}  {sp.file}{tag}")
                if injected:
                    item.setForeground(QColor(120, 200, 120))
                tgt_list.addItem(item)

            # Source list
            src_list.clear()
            for sp in src.subpackages:
                src_list.addItem(QListWidgetItem(f"{sp.slot:<14}  {sp.file}"))

            # Diff (source-only slots)
            d = diff_packages(tgt, src)
            diff_list.clear()
            for slot, fname in d['b_only']:
                item = QListWidgetItem(f"{slot:<14}  {fname}")
                item.setData(Qt.UserRole, (slot, fname))
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(Qt.Unchecked)
                item.setForeground(QColor(255, 200, 80))
                diff_list.addItem(item)

            inject_btn.setEnabled(diff_list.count() > 0)
            diff_summary.setText(
                f"{tgt_name}: {len(tgt.subpackages)} slots  |  "
                f"{src_name}: {len(src.subpackages)} slots  |  "
                f"{len(d['b_only'])} injectable  |  "
                f"{len(d['shared'])} shared"
            )

        def _check_all(state):
            for i in range(diff_list.count()):
                item = diff_list.item(i)
                item.setCheckState(state)

        def _do_inject():
            tgt_name = tgt_combo.currentData()
            tgt = working[tgt_name]
            added = 0
            for i in range(diff_list.count()):
                item = diff_list.item(i)
                if item.checkState() != Qt.Checked:
                    continue
                slot, fname = item.data(Qt.UserRole)
                if tgt.add_subpackage(slot, fname):
                    added += 1
            status.setText(
                f"Injected {added} slot(s) into {tgt_name}. "
                f"Total: {len(tgt.subpackages)} slots. "
                f"Save / Deploy to make it stick."
            )
            _refresh_lists()

        def _do_revert():
            tgt_name = tgt_combo.currentData()
            working[tgt_name] = _copy.deepcopy(packages[tgt_name])
            status.setText(f"Reverted {tgt_name} to vanilla.")
            _refresh_lists()

        def _do_save_xml():
            modified = {nm: pk for nm, pk in working.items()
                        if pk.subpackages != packages[nm].subpackages}
            if not modified:
                QMessageBox.information(dlg, tr("Save Patched XML"),
                    tr("Nothing changed — make injections first."))
                return
            patched = patch_descriptor(xml_text, modified)
            path, _ = QFileDialog.getSaveFileName(
                dlg, tr("Save patched descriptor XML"),
                "characteractionpackagedescription.xml",
                "XML files (*.xml)")
            if not path:
                return
            try:
                with open(path, 'w', encoding='utf-8', newline='\n') as f:
                    f.write(patched)
                status.setText(
                    f"Saved → {path}  "
                    f"({len(patched.encode('utf-8')):,}B, +"
                    f"{len(patched) - len(xml_text)} chars vs vanilla)"
                )
            except Exception as e:
                QMessageBox.critical(dlg, tr("Save"), f"Failed:\n{e}")

        def _do_deploy():
            modified = {nm: pk for nm, pk in working.items()
                        if pk.subpackages != packages[nm].subpackages}
            if not modified:
                QMessageBox.information(dlg, tr("Deploy"),
                    tr("Nothing changed — make injections first."))
                return
            patched = patch_descriptor(xml_text, modified)
            mod_group = f"{ac_overlay_spin.value():04d}"
            reply = QMessageBox.question(dlg, tr("Deploy 0010 Overlay"),
                f"Pack the modified XML into PAZ overlay group {mod_group}/?\n\n"
                f"Modified packages: {', '.join(modified.keys())}\n"
                f"Patched XML: +{len(patched) - len(xml_text)} chars\n\n"
                f"Restart the game for changes to take effect.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
            if reply != QMessageBox.Yes:
                return
            try:
                import crimson_rs.pack_mod
                from pathlib import Path
                gp = Path(game_path)
                with tempfile.TemporaryDirectory() as tmp_dir:
                    inner = os.path.join(tmp_dir, 'actionchart', 'xml', 'description')
                    os.makedirs(inner, exist_ok=True)
                    out_xml = os.path.join(
                        inner, 'characteractionpackagedescription.xml')
                    with open(out_xml, 'w', encoding='utf-8', newline='\n') as f:
                        f.write(patched)
                    pack_out = os.path.join(tmp_dir, 'output')
                    os.makedirs(pack_out, exist_ok=True)
                    crimson_rs.pack_mod.pack_mod(
                        game_dir=game_path,
                        mod_folder=tmp_dir,
                        output_dir=pack_out,
                        group_name=mod_group,
                    )
                    # Backup PAPGT and copy overlay
                    papgt = gp / 'meta' / '0.papgt'
                    backup = papgt.with_suffix(f'.papgt.actionchart_{mod_group}_bak')
                    if papgt.exists() and not backup.exists():
                        shutil.copy2(papgt, backup)
                    dest = gp / mod_group
                    dest.mkdir(exist_ok=True)
                    shutil.copyfile(
                        os.path.join(pack_out, mod_group, '0.paz'),
                        dest / '0.paz')
                    shutil.copyfile(
                        os.path.join(pack_out, mod_group, '0.pamt'),
                        dest / '0.pamt')
                    shutil.copyfile(
                        os.path.join(pack_out, 'meta', '0.papgt'), papgt)
                status.setText(
                    f"Deployed PAZ 0010 overlay to {mod_group}/. Restart the game."
                )
            except Exception as e:
                log.exception("Slot Inspector deploy failed")
                QMessageBox.critical(dlg, tr("Deploy"), f"Failed:\n{e}")

        def _do_restore():
            mod_group = f"{ac_overlay_spin.value():04d}"
            reply = QMessageBox.question(dlg, tr("Restore"),
                f"Remove the {mod_group}/ overlay and restore vanilla PAPGT?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
            if reply != QMessageBox.Yes:
                return
            try:
                from pathlib import Path
                gp = Path(game_path)
                dest = gp / mod_group
                if dest.exists():
                    shutil.rmtree(dest)
                papgt = gp / 'meta' / '0.papgt'
                backup = papgt.with_suffix(f'.papgt.actionchart_{mod_group}_bak')
                if backup.exists():
                    shutil.copy2(backup, papgt)
                status.setText(f"Removed {mod_group}/ overlay; restored PAPGT.")
            except Exception as e:
                QMessageBox.critical(dlg, tr("Restore"), f"Failed:\n{e}")

        check_all_btn.clicked.connect(lambda: _check_all(Qt.Checked))
        uncheck_all_btn.clicked.connect(lambda: _check_all(Qt.Unchecked))
        inject_btn.clicked.connect(_do_inject)
        revert_btn.clicked.connect(_do_revert)
        save_xml_btn.clicked.connect(_do_save_xml)
        deploy_btn.clicked.connect(_do_deploy)
        restore_btn.clicked.connect(_do_restore)
        tgt_combo.currentIndexChanged.connect(_refresh_lists)
        src_combo.currentIndexChanged.connect(_refresh_lists)

        _refresh_lists()
        dlg.exec()


    _ACTION_CATEGORIES = (
        ('Combo (combo group)',  ('_att_comg_', '_att_nor_coma_', '_att_nor_jumpcom')),
        ('Charge / Hard Dash',   ('_att_nor_move_harddash', '_att_nor_jumpatt')),
        ('Grab / Catch',          ('_att_nor_grab', '_att_lk_catch', '_att_nor_grab_a')),
        ('Link / Transition',     ('_link_',)),
        ('Dodge / Avoid',         ('_nor_move_avoid_', '_nor_dodge', '_evade')),
        ('Stance / Idle',         ('_nor_std_', '_nor_stance', '_stance_change')),
        ('Rage / Special',        ('_rageatt_', '_rage_', '_special_')),
        ('Movement',              ('_nor_move_', '_walk_', '_run_', '_dash_')),
        ('Hit / Damage',          ('_hit_', '_damage_', '_dmg_')),
        ('Death / Down',          ('_dead_', '_down_', '_facedown')),
        ('Parry / Guard',         ('_parry_', '_parrying', '_guard_', '_syshield')),
    )

    def _weapon_open_action_chart_browser(self) -> None:
        from PySide6.QtWidgets import (QListWidget, QListWidgetItem, QLineEdit,
                                       QComboBox, QTreeWidget, QTreeWidgetItem,
                                       QSplitter, QGroupBox, QPlainTextEdit)

        game_path = self._config.get("game_install_path", "")
        if not game_path or not os.path.isdir(game_path):
            QMessageBox.warning(self, tr("Action Chart Browser"),
                tr("Set the game install path first."))
            return

        # Cache the PAZ 0010 file list (one-time, ~580 dirs)
        if not getattr(self, '_actionchart_index', None):
            try:
                import crimson_rs
                pamt_path = os.path.join(game_path, '0010', '0.pamt')
                if not os.path.isfile(pamt_path):
                    QMessageBox.critical(self, tr("Action Chart Browser"),
                        f"PAZ 0010 not found at {pamt_path}")
                    return
                pamt = crimson_rs.parse_pamt_file(pamt_path)
                files = []
                for d in pamt.get('directories', []):
                    dpath = d.get('path', '')
                    if 'animmeta' not in dpath.lower():
                        continue
                    for f in d.get('files', []):
                        nm = f.get('name', '')
                        if nm.endswith('.paa_metabin'):
                            files.append({
                                'dir': dpath,
                                'name': nm,
                                'size': f.get('uncompressed_size', 0),
                            })
                self._actionchart_index = files
                log.info("Action Chart Browser: indexed %d .paa_metabin files",
                         len(files))
            except Exception as e:
                log.exception("Action Chart Browser: PAZ 0010 enumeration failed")
                QMessageBox.critical(self, tr("Action Chart Browser"),
                    f"Could not enumerate PAZ 0010:\n{e}")
                return

        files = self._actionchart_index

        # Build character→files map by extracting char tokens from filenames
        # Filenames look like: cd_<charprefix>_<weapon>_NN_NN_<action>_..._NN.paa_metabin
        # e.g.  cd_myurdin_swd_01_01_att_comg_1_move_f_swing_8_00.paa_metabin
        # Or:   cd_phm_lk_myurdin_sword_01_01_att_lk_catch_00.paa_metabin
        char_buckets: dict[str, list] = {}
        for fi in files:
            nm = fi['name']
            # Bucket by the first 3-4 tokens so we group reasonably
            stem = nm[:-len('.paa_metabin')] if nm.endswith('.paa_metabin') else nm
            tokens = stem.split('_')
            # Find the "subject" token — first non-cd_ token that isn't a letter prefix
            subject = None
            i = 0
            if tokens and tokens[0] == 'cd':
                i = 1
            # Skip generic frame prefixes (phm/phw/nhm) when there's a more specific token
            generic = {'phm', 'phw', 'nhm', 'nhw', 'pgm', 'pgw'}
            cand_idx = i
            while cand_idx < len(tokens) and tokens[cand_idx] in generic:
                cand_idx += 1
            if cand_idx < len(tokens):
                # "lk" prefix means lock-on — skip it
                if tokens[cand_idx] == 'lk' and cand_idx + 1 < len(tokens):
                    cand_idx += 1
                subject = tokens[cand_idx]
            else:
                subject = tokens[i] if i < len(tokens) else 'unknown'
            char_buckets.setdefault(subject, []).append(fi)

        # Build dialog
        dlg = QDialog(self)
        dlg.setWindowTitle(tr("Action Chart Browser — PAZ 0010 .paa_metabin files"))
        dlg.resize(1200, 760)
        v = QVBoxLayout(dlg)

        info = QLabel(tr(
            "Browse 575+ Myurdin animation files (and every other character's). "
            "Each .paa_metabin is a single action node — a swing, a dodge, a grab, a stance change. "
            "These live in PAZ 0010 separate from the runtime-package routing above. "
            "Pick a character → category → action to extract or inspect it."
        ))
        info.setWordWrap(True)
        info.setStyleSheet(f"color: {COLORS['text_dim']}; padding: 4px;")
        v.addWidget(info)

        # Top control row
        ctrl = QHBoxLayout()
        ctrl.addWidget(QLabel(tr("Character / subject:")))
        char_combo = QComboBox()
        char_combo.setEditable(True)
        char_combo.setInsertPolicy(QComboBox.NoInsert)
        char_combo.setMaxVisibleItems(30)
        for subj in sorted(char_buckets.keys()):
            char_combo.addItem(f"{subj}  ({len(char_buckets[subj])} files)", subj)
        ctrl.addWidget(char_combo, 1)

        ctrl.addWidget(QLabel(tr("Category:")))
        cat_combo = QComboBox()
        cat_combo.addItem('All', 'all')
        for label, _ in self._ACTION_CATEGORIES:
            cat_combo.addItem(label, label)
        cat_combo.addItem('Other', 'other')
        ctrl.addWidget(cat_combo)

        ctrl.addWidget(QLabel(tr("Filter:")))
        search_edit = QLineEdit()
        search_edit.setPlaceholderText(tr("substring of filename..."))
        ctrl.addWidget(search_edit, 1)
        v.addLayout(ctrl)

        # Splitter: file list on left, detail pane on right
        splitter = QSplitter(Qt.Horizontal)

        list_widget = QListWidget()
        list_widget.setAlternatingRowColors(True)
        splitter.addWidget(list_widget)

        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(4, 4, 4, 4)
        detail_label = QLabel(tr("(select a file to inspect)"))
        detail_label.setStyleSheet(f"color: {COLORS['accent']}; font-weight: bold;")
        rv.addWidget(detail_label)
        detail_text = QPlainTextEdit()
        detail_text.setReadOnly(True)
        detail_text.setStyleSheet("font-family: Consolas, monospace;")
        rv.addWidget(detail_text, 1)

        action_row = QHBoxLayout()
        extract_btn = QPushButton(tr("Extract to ./extracted_actionchart/"))
        extract_btn.setEnabled(False)
        action_row.addWidget(extract_btn)
        find_similar_btn = QPushButton(tr("Find Similar in Other Characters"))
        find_similar_btn.setEnabled(False)
        action_row.addWidget(find_similar_btn)
        action_row.addStretch()
        rv.addLayout(action_row)

        splitter.addWidget(right)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        v.addWidget(splitter, 1)

        status = QLabel("")
        status.setStyleSheet(f"color: {COLORS['text_dim']}; padding: 4px;")
        v.addWidget(status)

        close_row = QHBoxLayout()
        close_row.addStretch()
        close_btn = QPushButton(tr("Close"))
        close_btn.clicked.connect(dlg.accept)
        close_row.addWidget(close_btn)
        v.addLayout(close_row)

        def _category_for_filename(name: str) -> str:
            for label, prefixes in self._ACTION_CATEGORIES:
                for p in prefixes:
                    if p in name:
                        return label
            return 'Other'

        def _refresh_list():
            list_widget.clear()
            subj = char_combo.currentData()
            if not subj:
                # Editable combo — try the text
                subj = char_combo.currentText().strip().split()[0] if char_combo.currentText() else None
            if subj not in char_buckets:
                status.setText(f"No files for subject '{subj}'.")
                return
            cat_filter = cat_combo.currentData()
            search_q = search_edit.text().strip().lower()
            shown = 0
            for fi in char_buckets[subj]:
                cat = _category_for_filename(fi['name'])
                if cat_filter != 'all':
                    if cat_filter == 'other' and cat != 'Other':
                        continue
                    if cat_filter != 'other' and cat != cat_filter:
                        continue
                if search_q and search_q not in fi['name'].lower():
                    continue
                item = QListWidgetItem(f"[{cat[:18]:<18}] {fi['name']}  ({fi['size']}B)")
                item.setData(Qt.UserRole, fi)
                list_widget.addItem(item)
                shown += 1
                if shown >= 1500:
                    list_widget.addItem(QListWidgetItem(
                        f"  …refine the filter (>{shown} shown)"))
                    break
            status.setText(
                f"{shown} of {len(char_buckets[subj])} files for '{subj}'"
                + (f" matching '{search_q}'" if search_q else "")
            )

        def _on_select(*_):
            it = list_widget.currentItem()
            if not it:
                detail_label.setText("(no selection)")
                detail_text.clear()
                extract_btn.setEnabled(False)
                find_similar_btn.setEnabled(False)
                return
            fi = it.data(Qt.UserRole)
            if not fi:
                return
            detail_label.setText(fi['name'])
            lines = []
            lines.append(f"PAZ:        0010")
            lines.append(f"Directory:  {fi['dir']}")
            lines.append(f"File:       {fi['name']}")
            lines.append(f"Size:       {fi['size']:,} B")
            lines.append("")
            lines.append("Filename tokens (action breakdown):")
            stem = fi['name'][:-len('.paa_metabin')]
            for j, tok in enumerate(stem.split('_')):
                lines.append(f"  [{j:2d}] {tok}")
            lines.append("")
            lines.append("Action category: " + _category_for_filename(fi['name']))
            lines.append("")
            lines.append("Format: PA Reflection (0xFFFF magic, AnimationMetaData object)")
            lines.append("Editable via the same parser as save files (save_parser.py)")
            detail_text.setPlainText('\n'.join(lines))
            extract_btn.setEnabled(True)
            find_similar_btn.setEnabled(True)

        def _do_extract():
            it = list_widget.currentItem()
            if not it: return
            fi = it.data(Qt.UserRole)
            if not fi: return
            try:
                import crimson_rs
                data = crimson_rs.extract_file(game_path, '0010',
                                               fi['dir'], fi['name'])
                out_dir = os.path.join(os.getcwd(), 'extracted_actionchart')
                os.makedirs(out_dir, exist_ok=True)
                out_path = os.path.join(out_dir, fi['name'])
                with open(out_path, 'wb') as f:
                    f.write(bytes(data))
                status.setText(f"Extracted → {out_path} ({len(data):,}B)")
            except Exception as e:
                status.setText(f"Extract failed: {e}")
                log.exception("Action Chart Browser extract failed")

        def _do_find_similar():
            it = list_widget.currentItem()
            if not it: return
            fi = it.data(Qt.UserRole)
            if not fi: return
            # Strip subject from filename, search for the action suffix in other chars
            stem = fi['name'][:-len('.paa_metabin')]
            tokens = stem.split('_')
            # Take last 4 tokens as the "action signature"
            sig = '_'.join(tokens[-4:]) if len(tokens) >= 4 else stem
            matches = [f for f in files if sig in f['name'] and f['name'] != fi['name']]
            list_widget.clear()
            for fm in matches[:500]:
                cat = _category_for_filename(fm['name'])
                item = QListWidgetItem(f"[similar:{sig[:20]}] {fm['name']}")
                item.setData(Qt.UserRole, fm)
                list_widget.addItem(item)
            status.setText(
                f"Found {len(matches)} files containing '{sig}' (across all chars)"
            )

        char_combo.currentIndexChanged.connect(_refresh_list)
        cat_combo.currentIndexChanged.connect(_refresh_list)
        search_edit.textChanged.connect(_refresh_list)
        list_widget.currentItemChanged.connect(_on_select)
        extract_btn.clicked.connect(_do_extract)
        find_similar_btn.clicked.connect(_do_find_similar)

        # Default to "myurdin" if present so the user lands on the example
        for i in range(char_combo.count()):
            if char_combo.itemData(i) == 'myurdin':
                char_combo.setCurrentIndex(i)
                break
        _refresh_list()
        dlg.exec()


    def _weapon_apply_kliff_gun_preset(self) -> None:
        if not self._charinfo_player_entries:
            QMessageBox.information(
                self, tr("Kliff Gun Fix"),
                tr("Load game data first (click Load FieldInfo).")
            )
            return
        by_name = {e['name']: e for e in self._charinfo_player_entries}
        if not all(n in by_name for n in ('Kliff', 'Damiane', 'Oongka')):
            QMessageBox.warning(
                self, tr("Kliff Gun Fix"),
                tr("Could not find all three player chars in characterinfo.pabgb.")
            )
            return
        kliff = by_name['Kliff']
        damiane = by_name['Damiane']
        oongka = by_name['Oongka']

        _WDM = self._weapon_dmm_map
        van_damiane = next((v for v in self._charinfo_dmm_vanilla
                            if v.get('string_key') == 'Damiane'), None)
        van_oongka = next((v for v in self._charinfo_dmm_vanilla
                           if v.get('string_key') == 'Oongka'), None)

        upper_field = _WDM['_upperActionChartPackageGroupName']
        gp_field = _WDM['_characterGamePlayDataName']
        damian_upper = (van_damiane or {}).get(upper_field, 0)
        oongka_gp = (van_oongka or {}).get(gp_field, 0)

        self._weapon_write_field(kliff, '_upperActionChartPackageGroupName',
                                 damian_upper, 'Damiane')
        self._weapon_write_field(kliff, '_characterGamePlayDataName',
                                 oongka_gp, 'Oongka')

        self._weapon_pkg_populate()
        self._field_edit_status.setText(
            "Kliff gun fix staged: upper ← Damiane (0x%08x), gamePlay ← Oongka (0x%08x). "
            "Click Apply to deploy." % (damian_upper, oongka_gp)
        )

    def _weapon_reset_vanilla(self) -> None:
        if not self._charinfo_player_entries:
            return
        _WDM = self._weapon_dmm_map
        for e in self._charinfo_player_entries:
            van = next((v for v in self._charinfo_dmm_vanilla
                        if v['key'] == e.get('_key')), None)
            if not van:
                continue
            for col in self._WEAPON_COLS:
                field = col[0]
                dmm_f = _WDM.get(field, field)
                vanilla_val = van.get(dmm_f, 0)
                cd = e.get('dmm_ref')
                if cd is not None:
                    cd[dmm_f] = vanilla_val
                e[field] = vanilla_val
        self._charinfo_dmm_dirty = True
        self._weapon_pkg_populate()
        self._field_edit_status.setText(
            "Reset Kliff/Damiane/Oongka runtime-package fields to vanilla."
        )


    def _field_edit_make_killable(self):
        dmm_items = getattr(self, '_charinfo_dmm_items', None)
        if not dmm_items:
            QMessageBox.information(self, tr("Make Killable"), tr("Load game data first (click Load FieldInfo)."))
            return
        reply = QMessageBox.question(
            self, tr("Make All NPCs Killable"),
            "Sets _isAttackable=1 and _invincibility=0 on all non-mount\n"
            "characters via dmm_parser field-level edits.\n\n"
            "Mounts are excluded. Killing quest-essential NPCs may\n"
            "break story progression.\n\n"
            "Continue?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply != QMessageBox.Yes:
            return
        made_attackable = 0
        made_vincible = 0
        skipped_mounts = 0
        for it in dmm_items:
            if it.get('vehicle_info', 0) != 0 or it.get('string_key', '').startswith('Riding_'):
                skipped_mounts += 1
                continue
            ff = it.get('four_flags', {})
            if not ff:
                continue
            if ff.get('flag_b', 1) == 0:
                ff['flag_b'] = 1
                made_attackable += 1
            if ff.get('flag_a', 0) == 1:
                ff['flag_a'] = 0
                made_vincible += 1
        if made_attackable == 0 and made_vincible == 0:
            QMessageBox.information(self, tr("Make Killable"), tr("All NPCs are already attackable."))
            return
        self._charinfo_dmm_dirty = True
        self._field_edit_modified = True
        log.info("make_killable (dmm_parser): attackable+=%d vincible+=%d skipped=%d",
                 made_attackable, made_vincible, skipped_mounts)
        self._field_edit_status.setText(
            f"Made killable: {made_attackable} attackable + {made_vincible} vincible "
            f"({skipped_mounts} mounts skipped) — field-level via dmm_parser")


    def _ally_ensure_loaded(self) -> bool:
        if (getattr(self, '_ally_dmm', None) is not None
                and getattr(self, '_relation_dmm', None) is not None
                and getattr(self, '_factionrel_dmm', None) is not None):
            return True
        game_path = self._config.get("game_install_path", "")
        if not game_path or not os.path.isdir(game_path):
            QMessageBox.critical(self, tr("Game Path"),
                                 tr("Set the game install path first."))
            return False
        try:
            import crimson_rs, dmm_parser, copy
            dp = "gamedata/binary__/client/bin"

            ag_gb = bytes(crimson_rs.extract_file(game_path, "0008", dp, "allygroupinfo.pabgb"))
            ag_gh = bytes(crimson_rs.extract_file(game_path, "0008", dp, "allygroupinfo.pabgh"))
            self._ally_dmm = dmm_parser.parse_table('ally_group_info', ag_gb, ag_gh)
            self._ally_dmm_vanilla = copy.deepcopy(self._ally_dmm)
            self._allygroup_data = bytearray(ag_gb)
            self._allygroup_original = bytes(ag_gb)
            self._allygroup_schema = bytes(ag_gh)

            ri_gb = bytes(crimson_rs.extract_file(game_path, "0008", dp, "relationinfo.pabgb"))
            ri_gh = bytes(crimson_rs.extract_file(game_path, "0008", dp, "relationinfo.pabgh"))
            self._relation_dmm = dmm_parser.parse_table('relation_info', ri_gb, ri_gh)
            self._relation_dmm_vanilla = copy.deepcopy(self._relation_dmm)
            self._relationinfo_data = bytearray(ri_gb)
            self._relationinfo_original = bytes(ri_gb)
            self._relationinfo_schema = bytes(ri_gh)

            fg_gb = bytes(crimson_rs.extract_file(game_path, "0008", dp, "factionrelationgroup.pabgb"))
            fg_gh = bytes(crimson_rs.extract_file(game_path, "0008", dp, "factionrelationgroup.pabgh"))
            self._factionrel_dmm = dmm_parser.parse_table('faction_relation_group_info', fg_gb, fg_gh)
            self._factionrel_dmm_vanilla = copy.deepcopy(self._factionrel_dmm)
            self._factionrelgrp_data = bytearray(fg_gb)
            self._factionrelgrp_original = bytes(fg_gb)
            self._factionrelgrp_schema = bytes(fg_gh)

            log.info("Alliance loaded via dmm_parser: ally=%d relation=%d faction=%d",
                     len(self._ally_dmm), len(self._relation_dmm), len(self._factionrel_dmm))
        except Exception as e:
            QMessageBox.critical(self, tr("Extract Failed"),
                                 f"Could not extract ally/relation/faction pabgbs:\n{e}")
            return False
        return True

    def _parse_ally_index(self) -> list[tuple[int, int]]:
        s = self._allygroup_schema
        count = int.from_bytes(s[0:2], 'little')
        kw = (len(s) - 2 - count * 4) // count
        out = []
        p = 2
        for _ in range(count):
            key = int.from_bytes(s[p:p + kw], "little")
            off = int.from_bytes(s[p + kw:p + kw + 4], "little")
            out.append((key, off))
            p += kw + 4
        return out

    def _parse_relation_index(self) -> list[tuple[int, int]]:
        s = self._relationinfo_schema
        count = int.from_bytes(s[0:2], 'little')
        kw = (len(s) - 2 - count * 4) // count
        out = []
        p = 2
        for _ in range(count):
            key = int.from_bytes(s[p:p + kw], "little")
            off = int.from_bytes(s[p + kw:p + kw + 4], "little")
            out.append((key, off))
            p += kw + 4
        return out

    def _field_edit_all_hostile(self):
        if not self._ally_ensure_loaded():
            return
        reply = QMessageBox.question(
            self, tr("Path A — All NPCs Hostile"),
            "Set RelationInfo.order = 99 on all entries?\n\n"
            "Everyone becomes max-hostile to everyone. Your mounts will\n"
            "damage guards. Side effect: town NPCs may attack each other.\n\n"
            "Fully reversible via Restore. Continue?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply != QMessageBox.Yes:
            return
        flipped = 0
        for it in self._relation_dmm:
            if it.get('order', 0) != 99:
                it['order'] = 99
                flipped += 1
        import dmm_parser
        self._relationinfo_data = bytearray(dmm_parser.serialize_table(
            'relation_info', self._relation_dmm))
        log.info("path_a (dmm_parser): set order=99 on %d entries", flipped)
        self._field_edit_status.setText(
            f"Path A: set order=99 on {flipped} relation entries. Click Apply.")

    def _field_edit_wipe_ally_lists(self):
        if not self._ally_ensure_loaded():
            return
        reply = QMessageBox.question(
            self, tr("Path B — Wipe Ally Lists"),
            "Clear add_on_ally_group_list on all ally groups?\n\n"
            "Effect: no faction is allied with any other. Guards still\n"
            "function (combat rules intact) but mutual defense is gone.\n\n"
            "Fully reversible via Restore. Continue?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply != QMessageBox.Yes:
            return
        zeroed = 0
        for it in self._ally_dmm:
            lst = it.get('add_on_ally_group_list', [])
            if lst:
                zeroed += len(lst)
                it['add_on_ally_group_list'] = []
        import dmm_parser
        self._allygroup_data = bytearray(dmm_parser.serialize_table(
            'ally_group_info', self._ally_dmm))
        log.info("path_b (dmm_parser): cleared %d ally hashes", zeroed)
        self._field_edit_status.setText(
            f"Path B: cleared {zeroed} ally-group hashes. Click Apply.")

    def _field_edit_set_ally_flag(self, slot: int):
        if slot < 0 or slot > 4:
            return
        if not self._ally_ensure_loaded():
            return
        flag_names = {
            0: 'is_intruder', 1: 'is_main_ally_group', 2: 'is_wild',
            3: 'apply_reporting', 4: 'interesting_condition',
        }
        fname = flag_names.get(slot, f'flag_{slot}')
        reply = QMessageBox.question(
            self, tr(f"Path C — {fname}"),
            f"Set {fname} = 1 on all AllyGroup entries?\n\n"
            f"Fully reversible via Restore. Continue?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply != QMessageBox.Yes:
            return
        set_count = 0
        for it in self._ally_dmm:
            if it.get(fname, 0) != 1:
                it[fname] = 1
                set_count += 1
        import dmm_parser
        self._allygroup_data = bytearray(dmm_parser.serialize_table(
            'ally_group_info', self._ally_dmm))
        log.info("path_c (dmm_parser): set %s=1 on %d entries", fname, set_count)
        self._field_edit_status.setText(
            f"Path C: set {fname}=1 on {set_count} ally groups. Click Apply.")

    def _parse_factionrelgrp_index(self) -> list[tuple[int, int]]:
        s = self._factionrelgrp_schema
        count = int.from_bytes(s[0:2], 'little')
        kw = (len(s) - 2 - count * 4) // count
        out = []
        p = 2
        for _ in range(count):
            key = int.from_bytes(s[p:p + kw], "little")
            off = int.from_bytes(s[p + kw:p + kw + 4], "little")
            out.append((key, off))
            p += kw + 4
        return out

    def _field_edit_wipe_faction_relation_allies(self, all_lists: bool = False):
        if not self._ally_ensure_loaded():
            return
        label = "ALL 4 LISTS" if all_lists else "rel_0 (allied)"
        reply = QMessageBox.question(
            self, tr(f"Path D — Wipe FactionRelationGroup {label}"),
            f"Clear {label} across all FactionRelationGroup entries?\n\n"
            f"Target: the top-level groups (Civilian/Guard/Bandit/Player/...).\n"
            f"This is the LEVEL ABOVE allygroupinfo — where guard immunity lives.\n\n"
            f"Fully reversible via Restore. Continue?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply != QMessageBox.Yes:
            return
        zeroed_entries = 0
        zeroed_hashes = 0
        list_fields = ['rel_0', 'rel_1', 'rel_2', 'rel_3'] if all_lists else ['rel_0']
        for it in self._factionrel_dmm:
            for lf in list_fields:
                lst = it.get(lf, [])
                if lst:
                    zeroed_hashes += len(lst)
                    it[lf] = []
            zeroed_entries += 1
        import dmm_parser
        self._factionrelgrp_data = bytearray(dmm_parser.serialize_table(
            'faction_relation_group_info', self._factionrel_dmm))
        log.info("path_d (dmm_parser): wiped %s on %d entries, %d hashes cleared",
                 label, zeroed_entries, zeroed_hashes)
        self._field_edit_status.setText(
            f"Path D: wiped {label} on {zeroed_entries} entries "
               f"({zeroed_hashes} hashes zeroed). Click Apply.")

    def _field_edit_invincible_mounts(self):
        """Make all rideable mounts invincible (four_flags.flag_a = 1).

        The old implementation used _find_bool_block heuristic which landed
        4 bytes too early — in the alive_skill_info_list count field instead
        of flag_a (_invincibility). This version mutates _charinfo_dmm_items
        directly (same dict objects the export diff watches) so changes are
        picked up by Export Field JSON v3 without needing a re-serialize.
        """
        if not self._charinfo_data or not self._charinfo_schema:
            QMessageBox.information(self, tr("Invincible Mounts"),
                tr("Load game data first (click Load FieldInfo)."))
            return

        dmm_items = getattr(self, '_charinfo_dmm_items', None)
        if not dmm_items:
            QMessageBox.warning(self, tr("Invincible Mounts"),
                "characterinfo DMM items not loaded.\n"
                "Click 'Load FieldInfo' first.")
            return

        count = 0
        for it in dmm_items:
            sk = it.get('string_key', '') or ''
            vehicle = it.get('vehicle_info', 0) or 0
            # Mount entries: have a vehicle_info reference OR start with Riding_
            if vehicle == 0 and not sk.startswith('Riding_'):
                continue
            # four_flags is a nested dict; flag_a = _invincibility
            four_flags = it.get('four_flags')
            if isinstance(four_flags, dict) and four_flags.get('flag_a', 0) == 0:
                four_flags['flag_a'] = 1
                count += 1

        if count == 0:
            QMessageBox.information(self, tr("Invincible Mounts"),
                tr("All mounts are already invincible."))
            return

        # Re-serialize _charinfo_data so Apply to Game also picks up the change
        try:
            from characterinfo_full_parser import serialize_all_dmm
            new_bytes = serialize_all_dmm(dmm_items)
            if new_bytes:
                self._charinfo_data[:] = new_bytes
        except Exception as e:
            log.warning("invincible_mounts: re-serialize failed: %s", e)

        self._field_edit_modified = True

        # Refresh mount table display
        from characterinfo_full_parser import parse_all_entries as ci_parse_all
        try:
            self._charinfo_mount_entries = [
                e for e in ci_parse_all(bytes(self._charinfo_data), self._charinfo_schema)
                if e.get('_vehicleInfo', 0) != 0 or (e.get('name', '') or '').startswith('Riding_')
            ]
            self._mount_populate()
        except Exception:
            pass

        log.info("invincible_mounts: patched=%d via dmm_items", count)
        self._field_edit_status.setText(f"Made {count} mounts invincible")

    def _field_edit_enable_mounts(self):
        log.info("=== enable_mounts: starting ===")
        if not self._vehicle_data:
            log.warning("enable_mounts: _vehicle_data is None — can't patch vehicleinfo")
        if not self._regioninfo_data:
            log.warning("enable_mounts: _regioninfo_data is None — can't patch regioninfo "
                        "(regioninfo_parser import probably failed)")
        if not self._regioninfo_entries:
            log.warning("enable_mounts: _regioninfo_entries is EMPTY (parser failed or file missing)")
        if not self._charinfo_data:
            log.warning("enable_mounts: _charinfo_data is None — can't patch characterinfo")
        if not self._vehicle_data and not self._regioninfo_data and not self._charinfo_data:
            QMessageBox.information(self, tr("FieldEdit"), tr("Load FieldInfo first."))
            return
        v_count = 0
        vi_dmm = getattr(self, '_vehicleinfo_dmm_items', None)
        if vi_dmm:
            for vit in vi_dmm:
                if vit.get('send_damage_to', 1) == 0:
                    vit['send_damage_to'] = 1
                    v_count += 1
            self._vehicleinfo_dmm_dirty = True
            self._vehicle_populate()
        elif self._vehicle_data and self._vehicle_entries:
            for e in self._vehicle_entries:
                off = e.get('can_call_safe_zone_offset', -1)
                if off >= 0 and e.get('can_call_safe_zone', 0) == 0:
                    self._vehicle_data[off] = 1
                    e['can_call_safe_zone'] = 1
                    v_count += 1
            self._vehicle_populate()
        ri_count = 0
        ri_dmm = getattr(self, '_regioninfo_dmm_items', None)
        if ri_dmm:
            for rit in ri_dmm:
                if rit.get('is_wild', 0):
                    rit['is_wild'] = 0
                    ri_count += 1
                if rit.get('is_town', 0):
                    rit['is_town'] = 0
                    ri_count += 1
                if rit.get('limit_vehicle_run', 0):
                    rit['limit_vehicle_run'] = 0
                    ri_count += 1
            if ri_count:
                self._regioninfo_dmm_dirty = True
            if self._ri_filter_combo.currentData() != 'all':
                self._ri_filter_combo.blockSignals(True)
                self._ri_filter_combo.setCurrentIndex(1)
                self._ri_filter_combo.blockSignals(False)
            self._regioninfo_populate()
        elif self._regioninfo_entries:
            for e in self._regioninfo_entries:
                dmm_r = e.get('_dmm_ref')
                if e.get('_limitVehicleRun', 0):
                    if dmm_r:
                        dmm_r['limit_vehicle_run'] = 0
                    e['_limitVehicleRun'] = 0
                    ri_count += 1
                if e.get('_isTown', 0):
                    if dmm_r:
                        dmm_r['is_town'] = 0
                    e['_isTown'] = 0
                    ri_count += 1
            if ri_count:
                self._regioninfo_dmm_dirty = True
                # Build _regioninfo_dmm_items from _dmm_ref if not already set,
                # then re-serialize so byte diff + struct diff both work
                try:
                    import dmm_parser as _dmp_em
                    if not getattr(self, '_regioninfo_dmm_items', None):
                        # Collect all _dmm_ref objects from entries to form the items list
                        refs = [e['_dmm_ref'] for e in self._regioninfo_entries if '_dmm_ref' in e]
                        if refs:
                            self._regioninfo_dmm_items = refs
                            if not getattr(self, '_regioninfo_dmm_vanilla', None):
                                self._regioninfo_dmm_vanilla = [dict(r) for r in refs]
                    if getattr(self, '_regioninfo_dmm_items', None):
                        _ser = _dmp_em.serialize_table('region_info', self._regioninfo_dmm_items)
                        self._regioninfo_data = bytearray(_ser)
                except Exception as _se:
                    log.warning("enable_mounts: region re-serialize failed: %s", _se)
            if self._ri_filter_combo.currentData() != 'all':
                self._ri_filter_combo.blockSignals(True)
                self._ri_filter_combo.setCurrentIndex(1)
                self._ri_filter_combo.blockSignals(False)
            self._regioninfo_populate()
        mount_count = 0
        patched_names: list[str] = []
        candidates = 0
        dmm_items = getattr(self, '_charinfo_dmm_items', None)
        if dmm_items:
            for it in dmm_items:
                if it.get('vehicle_info', 0) == 0:
                    continue
                dur = it.get('call_mercenary_spawn_duration', 0)
                cool = it.get('call_mercenary_cool_time', 0)
                if dur > 0 or cool > 0:
                    candidates += 1
                hit = False
                if dur > 0:
                    it['call_mercenary_spawn_duration'] = 604800  # 7 days — large enough to be permanent, avoids i32/i64 overflow on engine cast
                    mount_count += 1; hit = True
                if cool > 0:
                    it['call_mercenary_cool_time'] = 0
                    mount_count += 1; hit = True
                if hit:
                    patched_names.append(it.get('string_key', '?'))
            self._charinfo_dmm_dirty = True
            self._mount_populate()
        self._field_edit_modified = True
        log.info("enable_mounts: vehicle=%d region=%d mount=%d edits applied "
                 "(%d/%d mount-entries had dur>0 or cool>0; patched: %s)",
                 v_count, ri_count, mount_count,
                 candidates, len(self._charinfo_mount_entries or []),
                 ", ".join(patched_names) if patched_names else "(none)")
        self._field_edit_status.setText(
            f"Enabled: {v_count} vehicle flags + {ri_count} region dismounts + {mount_count} mount limits")


    def _field_edit_open_mesh_swap(self) -> None:
        if not self._charinfo_data:
            QMessageBox.information(
                self, tr("Mesh Swap"),
                "Click 'Load FieldInfo' first — we need characterinfo.pabgb "
                "loaded into memory before we can queue mesh swaps.")
            return

        catalog: list = []
        for base in [os.path.dirname(os.path.abspath(__file__)),
                     getattr(sys, '_MEIPASS', '') or '',
                     os.getcwd()]:
            path = os.path.join(base, 'character_catalog.json')
            if os.path.isfile(path):
                try:
                    with open(path, 'r', encoding='utf-8') as f:
                        catalog = json.load(f).get('characters', []) or []
                    break
                except Exception:
                    continue
        if not catalog:
            try:
                from data_db import get_connection
                conn = get_connection()
                rows = conn.execute(
                    "SELECT character_key, internal_name, display_name, category "
                    "FROM characters"
                ).fetchall()
                for r in rows:
                    catalog.append({
                        'character_key': int(r['character_key']),
                        'internal_name': r['internal_name'] or '',
                        'display_name': r['display_name'] or '',
                        'category': r['category'] or 'Other',
                    })
            except Exception as e:
                log.debug("characters SQLite fallback failed: %s", e)

        if not catalog:
            QMessageBox.warning(
                self, tr("Mesh Swap"),
                "character_catalog.json / characters DB not found. Run _dump_characters.py to "
                "generate it (6872 characters expected).")
            return

        # Load the authoritative pet catalog (derived from Ghost Hunter's
        # PetRenamerMod v1.2.0 — 40 named pets/mounts × 216 char_keys).
        pet_keys: dict[int, str] = {}  # char_key -> group ('Horse'/'Dog'/'Cat'/'Special Mount')
        pet_display: dict[int, str] = {}  # char_key -> pretty name
        for base in [os.path.dirname(os.path.abspath(__file__)),
                     os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data'),
                     os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                     os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'data'),
                     os.path.join(getattr(sys, '_MEIPASS', ''), 'data'),
                     getattr(sys, '_MEIPASS', '') or '',
                     os.path.join(os.getcwd(), 'data'),
                     os.getcwd()]:
            ppath = os.path.join(base, 'pet_catalog.json')
            if os.path.isfile(ppath):
                try:
                    with open(ppath, 'r', encoding='utf-8') as pf:
                        pdata = json.load(pf)
                    for group, names in (pdata.get('groups') or {}).items():
                        for pname, keys in names.items():
                            for k in (keys or []):
                                pet_keys[int(k)] = group
                                pet_display[int(k)] = pname
                    log.info("Loaded pet_catalog: %d pet keys across %d groups",
                             len(pet_keys), len(pdata.get('groups') or {}))
                    break
                except Exception as e:
                    log.warning("pet_catalog load failed: %s", e)

        # Re-categorize with friendlier buckets so Pet / Mount / Animal stand
        # out from the 6000+ generic "Other" characters. Authoritative pet
        # catalog takes priority; name-heuristic is fallback for unlisted.
        PET_WORDS = ('_cat', 'cat_', 'dog', 'puppy', 'kitten', 'chicken',
                     'rabbit', 'bunny', 'fox', 'squirrel', 'mouse_',
                     'lizard', 'turtle', 'parrot', 'frog', 'sheep_lamb',
                     'ducks', 'duckling', 'hamster')
        ANIMAL_WORDS = ('wolf', 'bear', 'tiger', 'lion', 'boar', 'deer',
                        'moose', 'elk', 'goat', 'sheep', 'cow', 'donkey',
                        'camel', 'snake', 'eagle', 'hawk', 'owl', 'wyvern',
                        'horse', 'hound', 'serpent', 'spider', 'scorpion',
                        'crow_', 'raven')
        HERO_NAMES = ('kliff', 'damian', 'oongka', 'macduff', 'ziane',
                      'macgyver')
        for c in catalog:
            existing = c.get('category', 'Other')
            ck = int(c.get('character_key', 0))
            internal = (c.get('internal_name') or '').lower()
            display = (c.get('display_name') or '').lower()
            name_mash = internal + ' ' + display

            # Authoritative pet catalog takes priority
            if ck in pet_keys:
                group = pet_keys[ck]
                if group == 'Horse':
                    c['category'] = 'Mount (Horse)'
                elif group == 'Dog':
                    c['category'] = 'Pet (Dog)'
                elif group == 'Cat':
                    c['category'] = 'Pet (Cat)'
                elif group == 'Special Mount':
                    c['category'] = 'Mount (Special)'
                else:
                    c['category'] = 'Pet'
                # Override display to the friendly pet name for clarity
                pretty = pet_display.get(ck)
                if pretty and not (c.get('display_name') or '').strip():
                    c['display_name'] = pretty
                continue

            if existing in ('Boss', 'MiddleBoss'):
                c['category'] = 'Boss'
            elif existing == 'Dragon':
                c['category'] = 'Dragon'
            elif existing == 'Mount' or internal.startswith('riding_'):
                c['category'] = 'Mount'
            elif any(h == display or h == internal for h in HERO_NAMES):
                c['category'] = 'Hero'
            elif any(w in name_mash for w in PET_WORDS):
                c['category'] = 'Pet'
            elif any(w in name_mash for w in ANIMAL_WORDS):
                c['category'] = 'Animal'
            elif existing == 'NPC':
                c['category'] = 'NPC'
            # else keep 'Other'

        saved = self._config.get('mesh_swap_queue') or []
        if not self._mesh_swap_queue and saved:
            self._mesh_swap_queue = [
                {'src': int(s['src']), 'tgt': int(s['tgt']),
                 'scale': float(s.get('scale', 1.0) or 1.0),
                 'rideable': bool(s.get('rideable', False)),
                 'rider_y': float(s.get('rider_y', 8.0) or 8.0)}
                for s in saved if isinstance(s, dict) and 'src' in s and 'tgt' in s
            ]

        dlg = QDialog(self)
        from PySide6.QtWidgets import QScrollArea
        dlg.setWindowTitle(tr("Mesh Swap — Character Visual Transmog"))
        dlg.resize(1100, 860)
        dlg.setSizeGripEnabled(True)
        _dl_outer = QVBoxLayout(dlg)
        _dl_outer.setContentsMargins(0, 0, 0, 0)
        _scroll = QScrollArea(dlg)
        _scroll.setWidgetResizable(True)
        _scroll.setFrameShape(QScrollArea.NoFrame)
        _scroll_widget = QWidget()
        _dl_outer.addWidget(_scroll)
        _scroll.setWidget(_scroll_widget)
        dlg_layout = QVBoxLayout(_scroll_widget)

        tutorial = QLabel(
            "<b>How to use Mesh Swap (Visual Transmog)</b><br>"
            "<b>1.</b> Pick what you want to change on the <b>LEFT</b> (Target) "
            "— e.g. your horse, your pet cat, an NPC.<br>"
            "<b>2.</b> Pick what you want it to <b>look like</b> on the <b>RIGHT</b> "
            "(Source) — e.g. a wolf, a dragon, another NPC.<br>"
            "<b>3.</b> Click <b>Add Swap</b>. Repeat for as many swaps as you want.<br>"
            "<b>4.</b> Close this dialog and click <b>Apply to Game</b> in the Field Edit tab "
            "(or <b>Export Field JSON v3</b>).<br>"
            "<b>Tip:</b> Use the <b>Category</b> dropdown to narrow down to Pets, Mounts, "
            "Animals, Heroes, or Bosses. Use the search boxes to find a specific character by name.")
        tutorial.setWordWrap(True)
        tutorial.setTextFormat(Qt.RichText)
        tutorial.setStyleSheet(
            f"color: {COLORS['text']}; background-color: {COLORS['panel']}; "
            f"padding: 10px; border: 2px solid {COLORS['accent']}; "
            f"border-radius: 6px; font-size: 12px;")
        dlg_layout.addWidget(tutorial)

        info = QLabel(
            "Swap one character's visual mesh with another's. Same 0039/ overlay "
            "as Field Edit — applied on Apply-to-Game / Export.")
        info.setWordWrap(True)
        info.setStyleSheet(
            f"color: {COLORS['text_dim']}; padding: 4px; font-size: 10px;")
        dlg_layout.addWidget(info)

        CATEGORY_ORDER = ['All',
                          'Pet (Cat)', 'Pet (Dog)', 'Pet',
                          'Animal',
                          'Mount (Horse)', 'Mount (Special)', 'Mount',
                          'Hero', 'Dragon', 'Boss', 'NPC', 'Other']
        cats_present = {c.get('category', 'Other') for c in catalog}
        def _make_cat_combo():
            cb = QComboBox()
            for key in CATEGORY_ORDER:
                if key == 'All' or key in cats_present:
                    cb.addItem(key)
            cb.setToolTip(
                "Pet = cats, dogs, bunnies, chickens (small companions).\n"
                "Animal = wolves, bears, tigers (wild / swap sources).\n"
                "Mount = rideable (horses, camels, wolves, wagons).\n"
                "Hero = playable main characters.\n"
                "Dragon / Boss / NPC / Other — self-explanatory.\n\n"
                "Target and Source filters are INDEPENDENT — filter the left\n"
                "to Pet (Cat) and the right to Dragon to make cats look like "
                "wyverns.")
            return cb
        tgt_cat_combo = _make_cat_combo()
        src_cat_combo = _make_cat_combo()

        # ─── Quick-Swap Presets row ────────────────────────────────────────
        presets_row = QHBoxLayout()
        presets_lbl = QLabel("Quick Swap Helpers:")
        presets_lbl.setStyleSheet(f"color: {COLORS['accent']}; font-weight: bold;")
        presets_row.addWidget(presets_lbl)

        def _show_filter(target_cat: str, source_cat: str):
            # Set Target panel to the narrow filter, Source panel to the wider
            # suggestion (user can pick any source they want).
            ti = tgt_cat_combo.findText(target_cat)
            if ti >= 0:
                tgt_cat_combo.setCurrentIndex(ti)
            si = src_cat_combo.findText(source_cat)
            if si >= 0:
                src_cat_combo.setCurrentIndex(si)
            QMessageBox.information(
                dlg, "Quick Swap Tip",
                f"Target filter set to <b>{target_cat}</b> (left panel).<br>"
                f"Source filter set to <b>{source_cat}</b> (right panel) — "
                f"change it on the right if you want something different.<br><br>"
                f"1. Pick the specific character on the LEFT.<br>"
                f"2. Pick what to look like on the RIGHT "
                f"(use the source search or change its category to 'All' for full freedom).<br>"
                f"3. Click <b>Add Swap</b>.")

        cat_btn = QPushButton("🐱 Cats →")
        cat_btn.setToolTip(
            "Guided helper: filter Target to Pet (Cat) — 11 named cat breeds "
            "from Ghost Hunter's catalog. Then pick what you want them to look "
            "like (wolves, bears, dragons...) on the RIGHT.")
        cat_btn.clicked.connect(lambda: _show_filter('Pet (Cat)', 'Animal'))
        presets_row.addWidget(cat_btn)

        dog_btn = QPushButton("🐶 Dogs →")
        dog_btn.setToolTip(
            "Guided helper: filter Target to Pet (Dog) — 12 named dog breeds "
            "(Beagle, Boarhound, Husky, etc). Then pick what you want them "
            "to look like on the RIGHT.")
        dog_btn.clicked.connect(lambda: _show_filter('Pet (Dog)', 'Animal'))
        presets_row.addWidget(dog_btn)

        horse_btn = QPushButton("🐴 Horses →")
        horse_btn.setToolTip(
            "Guided helper: filter Target to Mount (Horse) — named horse "
            "breeds (Priden, Royler, Camora, etc). Then pick what you want "
            "them to look like — Dragon / Special Mount / anything.")
        horse_btn.clicked.connect(lambda: _show_filter('Mount (Horse)', 'Dragon'))
        presets_row.addWidget(horse_btn)

        special_btn = QPushButton("✨ Special Mounts →")
        special_btn.setToolTip(
            "Guided helper: filter Target to Mount (Special) — the 5 unique "
            "mounts (Silver Fang, White Bear, Snowwhite Deer, Clawed Bear, "
            "Kuku Bird Hatchling). Swap their appearance to anything else.")
        special_btn.clicked.connect(lambda: _show_filter('Mount (Special)', 'Animal'))
        presets_row.addWidget(special_btn)

        presets_row.addStretch()
        dlg_layout.addLayout(presets_row)

        vsplitter = QSplitter(Qt.Vertical)
        dlg_layout.addWidget(vsplitter, 1)

        picker_wrap = QWidget()
        picker_wrap_layout = QVBoxLayout(picker_wrap)
        picker_wrap_layout.setContentsMargins(0, 0, 0, 0)
        cols = QHBoxLayout()

        def make_column(title: str, placeholder: str, cat_combo):
            box = QVBoxLayout()
            lbl = QLabel(title)
            lbl.setStyleSheet(f"color: {COLORS['accent']}; font-weight: bold;")
            box.addWidget(lbl)
            cat_row = QHBoxLayout()
            cat_row.addWidget(QLabel(tr("Filter:")))
            cat_row.addWidget(cat_combo, 1)
            box.addLayout(cat_row)
            search = QLineEdit()
            search.setPlaceholderText(placeholder)
            box.addWidget(search)
            lst = QListWidget()
            lst.setMinimumHeight(350)
            box.addWidget(lst, 1)
            return box, search, lst

        tgt_col, tgt_search, tgt_list = make_column(
            tr("Target (the character to re-skin)"),
            tr("Search name, internal name, or character key (e.g. 1000318)"),
            tgt_cat_combo)
        src_col, src_search, src_list = make_column(
            tr("Source (visual to copy — pick ANY character, any category)"),
            tr("Search name, internal name, or character key (e.g. 30507). Set filter to 'All' to browse everything."),
            src_cat_combo)
        cols.addLayout(tgt_col, 1)
        cols.addLayout(src_col, 1)
        picker_wrap_layout.addLayout(cols, 1)
        vsplitter.addWidget(picker_wrap)

        persistent_sel_style = (
            "QListWidget::item:selected:!active { "
            f"background-color: {COLORS['accent']}; color: black; }} "
            "QListWidget::item:selected:active { "
            f"background-color: {COLORS['accent']}; color: black; }}"
        )
        tgt_list.setStyleSheet(persistent_sel_style)
        src_list.setStyleSheet(persistent_sel_style)

        def populate(lst: QListWidget, search_text: str, cat_filter: str):
            prev_key = None
            cur = lst.currentItem()
            if cur is not None:
                prev_key = cur.data(Qt.UserRole)
            lst.clear()
            restore_row = -1
            q = (search_text or '').strip().lower()
            q_as_int = None
            if q.isdigit():
                try:
                    q_as_int = int(q)
                except ValueError:
                    pass
            for c in catalog:
                if cat_filter != 'All' and c.get('category') != cat_filter:
                    continue
                ck = int(c.get('character_key', 0))
                internal = (c.get('internal_name') or '').lower()
                display = (c.get('display_name') or '').lower()
                if q:
                    hit = False
                    if q_as_int is not None and q in str(ck):
                        hit = True
                    if not hit and (q in internal or q in display):
                        hit = True
                    if not hit:
                        continue
                disp = c.get('display_name') or ''
                internal_show = c.get('internal_name') or ''
                if disp and disp != internal_show:
                    label = (f"[{c.get('category', 'Other')}] {disp}"
                             f"  — {internal_show}  ({ck})")
                else:
                    label = f"[{c.get('category', 'Other')}] {internal_show}  ({ck})"
                it = QListWidgetItem(label)
                it.setData(Qt.UserRole, ck)
                lst.addItem(it)
                if prev_key is not None and ck == prev_key:
                    restore_row = lst.count() - 1
            if restore_row >= 0:
                lst.setCurrentRow(restore_row)

        tgt_search.textChanged.connect(
            lambda _: populate(tgt_list, tgt_search.text(), tgt_cat_combo.currentText()))
        src_search.textChanged.connect(
            lambda _: populate(src_list, src_search.text(), src_cat_combo.currentText()))
        tgt_cat_combo.currentTextChanged.connect(
            lambda _: populate(tgt_list, tgt_search.text(), tgt_cat_combo.currentText()))
        src_cat_combo.currentTextChanged.connect(
            lambda _: populate(src_list, src_search.text(), src_cat_combo.currentText()))
        populate(tgt_list, '', 'All')
        populate(src_list, '', 'All')

        queue_wrap = QWidget()
        queue_wrap_layout = QVBoxLayout(queue_wrap)
        queue_wrap_layout.setContentsMargins(0, 0, 0, 0)
        queue_lbl = QLabel(tr("Queued swaps (applied at Export/Apply time — drag divider above to resize):"))
        queue_lbl.setStyleSheet(f"color: {COLORS['accent']}; font-weight: bold;")
        queue_wrap_layout.addWidget(queue_lbl)
        queue_list = QListWidget()
        queue_list.setMinimumHeight(80)
        queue_wrap_layout.addWidget(queue_list, 1)
        vsplitter.addWidget(queue_wrap)
        vsplitter.setSizes([520, 200])
        vsplitter.setStretchFactor(0, 3)
        vsplitter.setStretchFactor(1, 2)

        def _name_for(ck: int) -> str:
            for c in catalog:
                if c.get('character_key') == ck:
                    disp = c.get('display_name') or ''
                    internal = c.get('internal_name') or ''
                    if disp and disp != internal:
                        return f"{disp} ({internal})"
                    return internal or f"char {ck}"
            return f"char {ck}"

        def refresh_queue():
            queue_list.clear()
            for sw in self._mesh_swap_queue:
                sc = float(sw.get('scale') or 1.0)
                tk, sk = sw['tgt'], sw['src']
                tags = []
                if sc != 1.0:
                    tags.append(f"scale {sc:g}")
                if sw.get('rideable'):
                    tags.append(f"rideable Y={float(sw.get('rider_y', 8.0)):g}")
                tag_str = "   [" + ", ".join(tags) + "]" if tags else ""
                if tk == sk and sc != 1.0 and not sw.get('rideable'):
                    xp = sw.get('xml_path', '')
                    xml_short = xp.split('/')[-1] if xp else 'auto'
                    line = f"{_name_for(tk)}   ->   [SCALE {sc:g}, xml: {xml_short}]"
                else:
                    line = (f"{_name_for(tk)}   ->   now looks like   ->   "
                            f"{_name_for(sk)}{tag_str}")
                queue_list.addItem(line)
            try:
                self._config['mesh_swap_queue'] = list(self._mesh_swap_queue)
                self.config_save_requested.emit()
            except Exception:
                pass

        refresh_queue()

        btn_row = QHBoxLayout()
        add_btn = QPushButton(tr("Add Swap"))
        add_btn.setObjectName("accentBtn")
        remove_btn = QPushButton(tr("Remove Selected"))
        clear_btn = QPushButton(tr("Clear All"))
        change_all_btn = QPushButton(tr("Change All (visible targets)"))
        change_all_btn.setToolTip(
            "Replace the appearance of EVERY character currently visible in "
            "the Target panel with the selected Source's appearance. "
            "Respects current category/search filter.")
        scale_spin = QDoubleSpinBox()
        scale_spin.setRange(0.01, 10.0)
        scale_spin.setSingleStep(0.01)
        scale_spin.setDecimals(3)
        scale_spin.setValue(1.0)
        scale_spin.setToolTip("Character scale (1.0 = default size, <1.0 = shrink, >1.0 = enlarge)")
        scale_only_btn = QPushButton(tr("Scale Only"))
        export_field_btn = QPushButton(tr("Export Field JSON v3"))
        export_field_btn.setToolTip(
            "Export queued mesh swaps as a Format 3 field JSON mod.\n"
            "Sets lookup_22 (_appearanceName, visual mesh) and appearance_name\n"
            "(_upperActionChartPackageGroupName, animation chart) on each target\n"
            "to match the source character. Targets characterinfo.pabgb.\n"
            "Compatible with Stacker Tool and DMM mod loader.")
        export_field_btn.setStyleSheet(
            "QPushButton { background-color: #1565C0; color: white; font-weight: bold; }")
        btn_row.addWidget(add_btn)
        btn_row.addWidget(change_all_btn)
        btn_row.addWidget(remove_btn)
        btn_row.addWidget(clear_btn)
        btn_row.addWidget(export_field_btn)
        btn_row.addStretch()
        dlg_layout.addLayout(btn_row)

        # ── Mount options row: scale + rideable ──
        mount_row = QHBoxLayout()
        mount_row.addWidget(QLabel("Scale (1.0=default):"))
        mount_row.addWidget(scale_spin)
        mount_row.addWidget(scale_only_btn)
        mount_row.addSpacing(20)
        rideable_cb = QCheckBox("Make Rideable")
        rideable_cb.setToolTip(
            "Inject B_Rider_01 bone into the source skeleton so you can ride it.\n"
            "Only confirmed working for flying dragon-type mounts\n"
            "(e.g. Golden Star swapped onto Blackstar).")
        def _on_rideable_toggled(checked):
            rider_y_spin.setEnabled(checked)
            if checked:
                QMessageBox.information(
                    dlg, "Make Rideable — Limitation",
                    "This feature currently only works reliably for "
                    "flying dragon-type mounts.\n\n"
                    "Example: swap Golden Star onto Blackstar (both dragons, "
                    "same mount controller).\n\n"
                    "Cross-family swaps (spider on horse, human on mount, etc.) "
                    "will NOT produce rideable results — the mount movement "
                    "controller is tied to the target character type, not the "
                    "visual mesh.\n\n"
                    "Scale overrides work for any mount regardless.")
        rideable_cb.toggled.connect(_on_rideable_toggled)
        mount_row.addWidget(rideable_cb)
        mount_row.addWidget(QLabel("Rider Height:"))
        rider_y_spin = QDoubleSpinBox()
        rider_y_spin.setRange(0.0, 20.0)
        rider_y_spin.setSingleStep(0.5)
        rider_y_spin.setDecimals(1)
        rider_y_spin.setValue(8.0)
        rider_y_spin.setEnabled(False)
        rider_y_spin.setToolTip(
            "Y offset for rider seat position (relative to Spine1 bone).\n"
            "Higher = rider sits higher above the mount's body.\n"
            "Golden Star: ~8.0, Wyvern: ~4.0, Wolf: ~2.0")
        mount_row.addWidget(rider_y_spin)
        mount_row.addStretch()
        dlg_layout.addLayout(mount_row)

        cfg_row = QHBoxLayout()
        export_btn = QPushButton(tr("Export Config"))
        import_btn = QPushButton(tr("Import Config"))
        close_btn = QPushButton(tr("Close"))
        cfg_row.addWidget(export_btn)
        cfg_row.addWidget(import_btn)
        cfg_row.addStretch()
        cfg_row.addWidget(close_btn)
        dlg_layout.addLayout(cfg_row)

        def on_add():
            ti = tgt_list.currentItem()
            si = src_list.currentItem()
            if not ti or not si:
                QMessageBox.information(dlg, tr("Mesh Swap"),
                    "Pick one TARGET (left panel) and one SOURCE (right panel) before adding.")
                return
            tk = ti.data(Qt.UserRole)
            sk = si.data(Qt.UserRole)
            scale = float(scale_spin.value())
            rideable = rideable_cb.isChecked()
            rider_y = float(rider_y_spin.value())
            if tk == sk and scale <= 0 and not rideable:
                QMessageBox.information(dlg, tr("Mesh Swap"),
                    "Target and source are the same and no scale override or rideable is set. "
                    "Either pick a different source, or raise the Scale value above 0 "
                    "to queue a scale-only entry (alternatively use 'Scale Only' button).")
                return
            self._mesh_swap_queue[:] = [s for s in self._mesh_swap_queue if s['tgt'] != tk]
            self._mesh_swap_queue.append(
                {'src': int(sk), 'tgt': int(tk), 'scale': scale,
                 'rideable': rideable, 'rider_y': rider_y})
            refresh_queue()

        def on_scale_only():
            scale = float(scale_spin.value())
            if scale == 1.0:
                QMessageBox.information(dlg, tr("Scale Only"),
                    "Change the Scale value from 1.0 first.\n"
                    "1.0 = default size. Use <1.0 to shrink, >1.0 to enlarge.")
                return
            ti = tgt_list.currentItem()
            si = src_list.currentItem()
            pick = ti or si
            if not pick:
                QMessageBox.information(dlg, tr("Scale Only"),
                    "Select a character in either the TARGET or SOURCE panel first.")
                return
            ck = int(pick.data(Qt.UserRole))

            from character_mesh_swap import _load_appearance_paths
            catalog = _load_appearance_paths() or []
            if not catalog:
                QMessageBox.warning(dlg, tr("Scale Only"),
                    "appearance_paths.json not found — cannot look up XML paths.")
                return

            from PySide6.QtWidgets import QInputDialog
            pick_name = pick.text().split('[')[0].strip()
            search_dlg = QDialog(dlg)
            search_dlg.setWindowTitle(f"Pick appearance XML for: {pick_name}")
            search_dlg.resize(600, 400)
            sl = QVBoxLayout(search_dlg)
            sf = QLineEdit()
            sf.setPlaceholderText("Search by name (e.g. horse, wolf, blackstar)...")
            sl.addWidget(sf)
            from PySide6.QtWidgets import QListWidget
            slist = QListWidget()
            sl.addWidget(slist, 1)
            from PySide6.QtWidgets import QDialogButtonBox
            sbb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
            sbb.accepted.connect(search_dlg.accept)
            sbb.rejected.connect(search_dlg.reject)
            sl.addWidget(sbb)

            def _filter_xml(text):
                slist.clear()
                q = text.strip().lower()
                for e in catalog:
                    p = e.get('path', '')
                    if q and q not in p.lower():
                        continue
                    item = QListWidgetItem(p)
                    item.setData(Qt.UserRole, p)
                    slist.addItem(item)
                    if slist.count() > 200:
                        break

            sf.textChanged.connect(_filter_xml)
            _filter_xml("")

            if search_dlg.exec() != QDialog.Accepted:
                return
            sel = slist.currentItem()
            if not sel:
                return
            xml_path = sel.data(Qt.UserRole)

            self._mesh_swap_queue[:] = [s for s in self._mesh_swap_queue if s['tgt'] != ck]
            self._mesh_swap_queue.append(
                {'src': ck, 'tgt': ck, 'scale': scale,
                 'xml_path': xml_path,
                 'rideable': False, 'rider_y': 8.0})
            refresh_queue()

        def on_change_all():
            si = src_list.currentItem()
            if not si:
                QMessageBox.information(dlg, tr("Mesh Swap"),
                    "Pick one SOURCE (right panel) first. Change All will add "
                    "a swap entry for every character currently visible in the "
                    "Target panel (respects your category/search filter).")
                return
            sk = int(si.data(Qt.UserRole))
            target_keys = []
            for i in range(tgt_list.count()):
                it = tgt_list.item(i)
                if it is None:
                    continue
                tk = int(it.data(Qt.UserRole))
                if tk == sk:
                    continue
                target_keys.append(tk)
            if not target_keys:
                QMessageBox.information(dlg, tr("Mesh Swap"),
                    "No targets currently visible. Clear or adjust filters first.")
                return
            scale = float(scale_spin.value())
            scale_note = f" (scale {scale:g})" if scale > 0 else ""
            ans = QMessageBox.question(
                dlg, tr("Change All"),
                f"Replace the appearance of ALL {len(target_keys)} visible target(s) "
                f"with '{_name_for(sk)}'{scale_note}?\n\n"
                "This overwrites existing queue entries for those targets.",
                QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
            if ans != QMessageBox.Yes:
                return
            existing_by_tgt = {s['tgt']: s for s in self._mesh_swap_queue}
            added = updated = 0
            for tk in target_keys:
                if tk in existing_by_tgt:
                    updated += 1
                else:
                    added += 1
                existing_by_tgt[tk] = {'src': sk, 'tgt': tk, 'scale': scale,
                                       'rideable': rideable_cb.isChecked(),
                                       'rider_y': float(rider_y_spin.value())}
            self._mesh_swap_queue[:] = list(existing_by_tgt.values())
            refresh_queue()
            QMessageBox.information(dlg, tr("Change All"),
                f"Added {added}, updated {updated}. Queue now has "
                f"{len(self._mesh_swap_queue)} swap(s).")

        def on_remove():
            row = queue_list.currentRow()
            if 0 <= row < len(self._mesh_swap_queue):
                del self._mesh_swap_queue[row]
                refresh_queue()

        def on_clear():
            self._mesh_swap_queue.clear()
            refresh_queue()

        def on_export():
            path, _ = QFileDialog.getSaveFileName(
                dlg, tr("Export Mesh Swap Config"), "mesh_swap_config.json", "JSON (*.json)")
            if not path:
                return
            out = {
                'version': 2,
                'kind': 'character_mesh_swap',
                'swaps': [
                    {
                        'target_key': s['tgt'],
                        'target_name': _name_for(s['tgt']),
                        'source_key': s['src'],
                        'source_name': _name_for(s['src']),
                        'scale': float(s.get('scale') or 0),
                        'rideable': bool(s.get('rideable', False)),
                        'rider_y': float(s.get('rider_y', 8.0) or 8.0),
                    }
                    for s in self._mesh_swap_queue
                ],
            }
            try:
                with open(path, 'w', encoding='utf-8') as f:
                    json.dump(out, f, indent=2, ensure_ascii=False)
                QMessageBox.information(dlg, tr("Export"),
                    f"Wrote {len(self._mesh_swap_queue)} swap(s) to:\n{path}")
            except Exception as e:
                QMessageBox.critical(dlg, tr("Export Failed"), str(e))

        def on_import():
            path, _ = QFileDialog.getOpenFileName(
                dlg, tr("Import Mesh Swap Config"), "", "JSON (*.json)")
            if not path:
                return
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    cfg = json.load(f)
            except Exception as e:
                QMessageBox.critical(dlg, tr("Import Failed"), str(e))
                return
            if cfg.get('kind') not in ('character_mesh_swap', None):
                QMessageBox.warning(dlg, tr("Import"),
                    f"File is '{cfg.get('kind', 'unknown')}' — expected 'character_mesh_swap'.")
                return
            entries = cfg.get('swaps') or []
            if not entries:
                QMessageBox.information(dlg, tr("Import"), tr("Config contains no swaps."))
                return
            if self._mesh_swap_queue:
                btn = QMessageBox.question(
                    dlg, tr("Import"),
                    f"Current queue has {len(self._mesh_swap_queue)} swap(s).\n\n"
                    f"Yes = Replace queue with {len(entries)} imported\n"
                    f"No  = Append imported to current queue\n"
                    f"Cancel",
                    QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
                    QMessageBox.No)
                if btn == QMessageBox.Cancel:
                    return
                if btn == QMessageBox.Yes:
                    self._mesh_swap_queue.clear()
            by_key = {c['character_key']: c for c in catalog}
            added = missed = 0
            for s in entries:
                tk = s.get('target_key')
                sk = s.get('source_key')
                sc = float(s.get('scale') or 0)
                if tk is None or sk is None or tk not in by_key or sk not in by_key:
                    missed += 1
                    continue
                self._mesh_swap_queue[:] = [x for x in self._mesh_swap_queue if x['tgt'] != tk]
                self._mesh_swap_queue.append(
                    {'src': int(sk), 'tgt': int(tk), 'scale': sc,
                     'rideable': bool(s.get('rideable', False)),
                     'rider_y': float(s.get('rider_y', 8.0) or 8.0)})
                added += 1
            refresh_queue()
            QMessageBox.information(dlg, tr("Import"),
                f"Imported {added} swap(s). {missed} skipped (character not in catalog).")

        def on_export_field_json():
            queue = self._mesh_swap_queue or []
            if not queue:
                QMessageBox.information(dlg, tr("Export Field JSON v3"),
                    "No swaps queued.")
                return
            game_path = self._config.get("game_install_path", "") or ""
            if not game_path:
                QMessageBox.warning(dlg, tr("Export Field JSON v3"),
                    "Set the game install path first.")
                return

            # Build character_key -> internal_name from the same catalog
            # that populated the dialog.
            _ck_to_name: dict[int, str] = {
                int(c.get("character_key", 0)): c.get("internal_name", "")
                for c in catalog if c.get("internal_name")
            }

            # Extract and parse characterinfo
            try:
                import crimson_rs as _cr_efj
                _dp_efj = "gamedata/binary__/client/bin"
                _pabgb_efj = bytes(_cr_efj.extract_file(
                    game_path, "0008", _dp_efj, "characterinfo.pabgb"))
                _pabgh_efj = bytes(_cr_efj.extract_file(
                    game_path, "0008", _dp_efj, "characterinfo.pabgh"))
            except Exception as _e_efj:
                QMessageBox.critical(dlg, tr("Export Field JSON v3"),
                    f"Could not extract characterinfo from game:\n{_e_efj}")
                return
            try:
                import dmm_parser as _dmp_efj
                _entries_efj = _dmp_efj.parse_table(
                    "character_info", _pabgb_efj, _pabgh_efj)
            except Exception as _e2_efj:
                QMessageBox.critical(dlg, tr("Export Field JSON v3"),
                    f"Could not parse characterinfo with dmm_parser:\n{_e2_efj}")
                return

            # Build lookup by string_key (internal_name) — this matches the
            # catalog internal_name which the swap queue character_keys map to.
            # The numeric key in dmm_parser output differs from characterinfo_full_parser
            # entry_key, so we must match via name.
            _by_skey_efj: dict[str, dict] = {
                e.get("string_key", ""): e
                for e in _entries_efj if e.get("string_key")
            }
            # Also keep numeric key lookup as secondary fallback
            _by_nkey_efj: dict[int, dict] = {
                int(e.get("key", 0)): e for e in _entries_efj
            }

            # ── Compatibility pre-check ──────────────────────────────────────
            # Check all swaps for skeleton mismatches and cross-type issues
            # before building intents. Collect warnings so user can abort.
            _compat_warnings: list[str] = []
            for _sw in queue:
                try:
                    _tc = int(_sw["tgt"]); _sc = int(_sw["src"])
                except Exception:
                    continue
                if _tc == _sc:
                    continue
                _tn = _ck_to_name.get(_tc, "")
                _sn = _ck_to_name.get(_sc, "")
                _te = _by_skey_efj.get(_tn) or _by_nkey_efj.get(_tc)
                _se = _by_skey_efj.get(_sn) or _by_nkey_efj.get(_sc)
                if not _te or not _se:
                    continue
                # Skeleton mismatch — animations will be broken / T-pose
                _t_skel = _te.get("skeleton_name")
                _s_skel = _se.get("skeleton_name")
                if _t_skel and _s_skel and _t_skel != _s_skel:
                    _compat_warnings.append(
                        f"⚠ Skeleton mismatch: '{_tn or _tc}' → '{_sn or _sc}'\n"
                        f"   Target skeleton: {_t_skel}\n"
                        f"   Source skeleton: {_s_skel}\n"
                        f"   Animations may be broken or T-pose in-game."
                    )
                # Cross-type check via flag_c (1=male human, 2=female human,
                # other values = mount/animal/NPC).  Warn if neither or only
                # one side is a human-playable type.
                _t_fc = _te.get("flag_c")
                _s_fc = _se.get("flag_c")
                _human_flags = {1, 2}
                if (_t_fc is not None and _s_fc is not None
                        and bool(_t_fc in _human_flags) != bool(_s_fc in _human_flags)):
                    _type_tgt = "human/NPC" if _t_fc in _human_flags else "mount/animal/other"
                    _type_src = "human/NPC" if _s_fc in _human_flags else "mount/animal/other"
                    _compat_warnings.append(
                        f"⚠ Cross-type swap: '{_tn or _tc}' ({_type_tgt}) → '{_sn or _sc}' ({_type_src})\n"
                        f"   Swapping a {_type_tgt} with a {_type_src} is likely incompatible.\n"
                        f"   The result may crash, be invisible, or behave incorrectly."
                    )

            if _compat_warnings:
                _warn_text = "\n\n".join(_compat_warnings)
                _warn_text += (
                    "\n\n─────────────────────────────────────\n"
                    "Export anyway? (Not recommended unless you know the skeletons are compatible)"
                )
                _ans = QMessageBox.warning(
                    dlg, tr("Compatibility Warning — Mesh Swap"),
                    _warn_text,
                    QMessageBox.Ok | QMessageBox.Cancel,
                    QMessageBox.Cancel,
                )
                if _ans != QMessageBox.Ok:
                    return

            # ── Build intents ────────────────────────────────────────────────
            intents = []
            skipped = []
            for sw in queue:
                try:
                    tgt_ck = int(sw["tgt"])
                    src_ck = int(sw["src"])
                except (KeyError, TypeError, ValueError):
                    skipped.append(str(sw))
                    continue
                if tgt_ck == src_ck:
                    continue

                tgt_name = _ck_to_name.get(tgt_ck, "")
                src_name = _ck_to_name.get(src_ck, "")

                tgt_e = (_by_skey_efj.get(tgt_name)
                         or _by_nkey_efj.get(tgt_ck))
                src_e = (_by_skey_efj.get(src_name)
                         or _by_nkey_efj.get(src_ck))

                if not tgt_e or not src_e:
                    skipped.append(tgt_name or f"key {tgt_ck}")
                    continue

                src_vis   = src_e.get("lookup_22")        # _appearanceName — visual mesh
                tgt_vis   = tgt_e.get("lookup_22")
                src_app   = src_e.get("appearance_name")  # _upperActionChartPackageGroupName — anim chart
                tgt_app   = tgt_e.get("appearance_name")
                src_skel  = src_e.get("skeleton_name")    # _skeletonName — skeleton rig
                tgt_skel  = tgt_e.get("skeleton_name")
                src_mdl   = src_e.get("lookup_24")        # model path .pab
                tgt_mdl   = tgt_e.get("lookup_24")
                src_skelv = src_e.get("lookup_25")        # skeleton variation .pabc — bone variation data
                tgt_skelv = tgt_e.get("lookup_25")

                vis_differs   = src_vis   is not None and tgt_vis   != src_vis
                anim_differs  = src_app   is not None and tgt_app   != src_app
                skel_differs  = src_skel  is not None and tgt_skel  != src_skel
                mdl_differs   = src_mdl   is not None and tgt_mdl   != src_mdl
                skelv_differs = src_skelv is not None and tgt_skelv != src_skelv

                # Nothing to change for this pair
                if not any([vis_differs, anim_differs, skel_differs,
                            mdl_differs, skelv_differs]):
                    continue

                if all(v is None for v in [src_vis, src_app, src_skel,
                                           src_mdl, src_skelv]):
                    skipped.append(tgt_e.get("string_key", str(tgt_ck)))
                    continue

                entry_name = tgt_e.get("string_key", tgt_name)
                entry_key  = int(tgt_e.get("key", tgt_ck))

                # Intent 1: visual mesh (_appearanceName = lookup_22)
                if vis_differs:
                    intents.append({
                        "entry": entry_name,
                        "key": entry_key,
                        "field": "lookup_22",
                        "op": "set",
                        "new": src_vis,
                        "_comment": f"mesh swap: visual mesh (_appearanceName) from {src_name or src_ck}",
                    })

                # Intent 2: animation chart (_upperActionChartPackageGroupName = appearance_name)
                if anim_differs:
                    intents.append({
                        "entry": entry_name,
                        "key": entry_key,
                        "field": "appearance_name",
                        "op": "set",
                        "new": src_app,
                        "_comment": f"mesh swap: anim chart (_upperActionChartPackageGroupName) from {src_name or src_ck}",
                    })

                # Intent 3: skeleton rig (_skeletonName = skeleton_name)
                if skel_differs:
                    intents.append({
                        "entry": entry_name,
                        "key": entry_key,
                        "field": "skeleton_name",
                        "op": "set",
                        "new": src_skel,
                        "_comment": f"mesh swap: skeleton (_skeletonName) from {src_name or src_ck}",
                    })

                # Intent 4: model path .pab (lookup_24)
                if mdl_differs:
                    intents.append({
                        "entry": entry_name,
                        "key": entry_key,
                        "field": "lookup_24",
                        "op": "set",
                        "new": src_mdl,
                        "_comment": f"mesh swap: model path from {src_name or src_ck}",
                    })

                # Intent 5: skeleton variation .pabc (lookup_25)
                # Bone variation data — must match skeleton for animations to play correctly.
                if skelv_differs:
                    intents.append({
                        "entry": entry_name,
                        "key": entry_key,
                        "field": "lookup_25",
                        "op": "set",
                        "new": src_skelv,
                        "_comment": f"mesh swap: skeleton variation (_skeletonVariationName) from {src_name or src_ck}",
                    })

            if not intents:
                msg = "No field-level differences found."
                if skipped:
                    msg += f"\n\nSkipped: {', '.join(skipped)}"
                QMessageBox.warning(dlg, tr("Export Field JSON v3"), msg)
                return
            path, _ = QFileDialog.getSaveFileName(
                dlg, tr("Export Field JSON v3"),
                "mesh_swap.field.json",
                "Field JSON (*.field.json *.json);;All Files (*)")
            if not path:
                return
            import os as _os_efj, json as _json_efj
            doc = {
                "modinfo": {
                    "title": "Mesh Swap Mod",
                    "version": "1.0",
                    "author": "CrimsonGameMods MeshSwap",
                    "description": (
                        f"{len(queue)} swap(s), {len(intents)} intent(s)"
                    ),
                    "note": (
                        "Format 3 — full visual+animation swap: "
                        "lookup_22 (_appearanceName / mesh), "
                        "appearance_name (_upperActionChartPackageGroupName / anim chart), "
                        "skeleton_name (_skeletonName / rig), "
                        "lookup_24 (model path), "
                        "lookup_25 (_skeletonVariationName / bone variation). "
                        "Targets characterinfo.pabgb."
                    ),
                },
                "format": 3,
                "format_minor": 1,
                "targets": [
                    {
                        "file": "characterinfo.pabgb",
                        "intents": intents,
                    }
                ],
            }
            try:
                with open(path, "w", encoding="utf-8") as _fh_efj:
                    _json_efj.dump(doc, _fh_efj, indent=2,
                                  ensure_ascii=False, default=str)
                msg2 = (
                    f"Exported {len(intents)} intent(s) for {len(queue)} swap(s)."
                )
                if skipped:
                    msg2 += f"\n\nSkipped: {', '.join(skipped)}"
                QMessageBox.information(dlg, tr("Export Field JSON v3"),
                    f"{msg2}\n\nFile: {_os_efj.path.basename(path)}")
            except Exception as _e3_efj:
                QMessageBox.critical(dlg, tr("Export Failed"), str(_e3_efj))

        add_btn.clicked.connect(on_add)
        scale_only_btn.clicked.connect(on_scale_only)
        change_all_btn.clicked.connect(on_change_all)
        remove_btn.clicked.connect(on_remove)
        clear_btn.clicked.connect(on_clear)
        export_field_btn.clicked.connect(on_export_field_json)
        export_btn.clicked.connect(on_export)
        import_btn.clicked.connect(on_import)
        close_btn.clicked.connect(dlg.accept)
        queue_list.itemDoubleClicked.connect(lambda _: on_remove())

        dlg.exec()

    def _field_edit_apply_mesh_swaps(self) -> int:
        queue = self._mesh_swap_queue or []
        if not queue:
            return 0
        if not self._charinfo_data:
            log.warning("mesh swap queue has %d entries but _charinfo_data is empty "
                        "— click 'Load FieldInfo' first", len(queue))
            return 0
        try:
            import crimson_rs
            from character_mesh_swap import apply_mesh_swaps
            game_path = self._config.get("game_install_path", "") or ""
            dp = 'gamedata/binary__/client/bin'
            pabgh = crimson_rs.extract_file(game_path, '0008', dp, 'characterinfo.pabgh')
            pre_bytes = bytes(self._charinfo_data)

            if self._charinfo_original:
                pre_diff_vs_vanilla = sum(1 for a, b in zip(pre_bytes, self._charinfo_original)
                                          if a != b)
                log.info("mesh swap PRE-apply: _charinfo_data has %d bytes "
                         "differing from vanilla (mount cooldown/killable/invincible)",
                         pre_diff_vs_vanilla)

            new_data, applied, report = apply_mesh_swaps(
                pre_bytes, bytes(pabgh), list(queue))
            # Mesh swap now uses dmm_parser — re-parse so dmm items stay in sync
            if applied:
                import dmm_parser as _dmp
                import copy as _copy
                self._charinfo_dmm_items = _dmp.parse_table(
                    'character_info', bytes(new_data), bytes(pabgh))
                self._charinfo_dmm_dirty = True

            mesh_diff = sum(1 for a, b in zip(new_data, pre_bytes) if a != b)
            log.info("mesh swap: queue=%d applied=%d diff_bytes=%d",
                     len(queue), applied, mesh_diff)

            if self._charinfo_original:
                post_diff_vs_vanilla = sum(1 for a, b in zip(new_data, self._charinfo_original)
                                           if a != b)
                log.info("mesh swap POST-apply: _charinfo_data has %d bytes "
                         "differing from vanilla (combined: mesh swap + prior edits)",
                         post_diff_vs_vanilla)
                expected_minimum = max(pre_diff_vs_vanilla, mesh_diff)
                if post_diff_vs_vanilla < expected_minimum:
                    log.warning("mesh swap CLOBBER DETECTED: post_diff=%d < expected=%d "
                                "— mesh swap apparently reset some prior bytes!",
                                post_diff_vs_vanilla, expected_minimum)

            if applied:
                self._charinfo_data = bytearray(new_data)
                for r in report:
                    log.info(
                        "mesh swap: %s (%d) <- %s (%d): off=0x%x 0x%08X -> 0x%08X",
                        r['tgt_name'], r['tgt'], r['src_name'], r['src'],
                        r['tgt_offset'], r['old_key'], r['new_key'])
            elif queue:
                log.warning("mesh swap: %d queued swap(s) produced 0 byte changes "
                            "— check character_catalog / parser output", len(queue))

            try:
                needs_overlay = [
                    s for s in queue
                    if float(s.get('scale') or 0) > 0 or s.get('rideable')
                ]
                if needs_overlay:
                    overlay_msg = self._deploy_mount_overlays(game_path, needs_overlay)
                    if overlay_msg:
                        log.info("mount overlay: %s", overlay_msg)
            except Exception:
                log.exception("mount overlay deploy failed")

            return applied
        except Exception:
            log.exception("mesh swap apply failed (queue=%d)", len(queue))
            return 0


    @property
    def _MOUNT_OVERLAY_GROUP(self):
        return f"{self._config.get('mesh_swap_overlay_dir', 62):04d}"

    def _deploy_mount_overlays(self, game_path: str, queue: list[dict]) -> str:
        """Deploy skeleton (rider bone) + appearance (scale) overlays for mesh swaps."""
        import crimson_rs
        import crimson_rs.pack_mod
        from character_mesh_swap import _load_appearance_paths, _guess_xml_path
        from characterinfo_full_parser import parse_all_entries
        from rider_bone_injector import inject_rider_bone, has_rider_bone, \
            _write_scaled_appearance

        gp = Path(game_path)
        catalog = _load_appearance_paths() or []
        dp = 'gamedata/binary__/client/bin'
        pabgh = crimson_rs.extract_file(game_path, '0008', dp, 'characterinfo.pabgh')
        entries = parse_all_entries(bytes(self._charinfo_data), bytes(pabgh))
        # parse_all_entries uses 'name', but _guess_xml_path expects 'internal_name'
        by_key = {}
        for e in entries:
            k = int(e.get('entry_key', 0))
            if 'name' in e and 'internal_name' not in e:
                e['internal_name'] = e['name']
            by_key[k] = e

        files_written = 0
        with tempfile.TemporaryDirectory(prefix='mount_overlay_') as tmp_dir:
            for sw in queue:
                sk = int(sw['src'])
                entry = by_key.get(sk)
                if not entry:
                    log.warning("mount overlay: src=%d not in characterinfo", sk)
                    continue

                # Find the appearance XML path for this character
                xml_path = sw.get('xml_path') or _guess_xml_path(entry, catalog)

                # ── Scale override ──
                scale = float(sw.get('scale') or 1.0)
                if scale != 1.0 and scale > 0 and xml_path:
                    try:
                        _write_scaled_appearance(
                            game_path, xml_path, scale, tmp_dir)
                        files_written += 1
                        log.info("mount overlay: scale %.1f for src=%d xml=%s",
                                 scale, sk, xml_path)
                    except Exception:
                        log.exception("mount overlay: scale failed for src=%d", sk)

                # ── Rider bone injection ──
                if sw.get('rideable'):
                    rider_y = float(sw.get('rider_y', 8.0) or 8.0)
                    # Find the skeleton .pab path from the prefabdata
                    skel_path = self._find_skeleton_path(game_path, entry, catalog)
                    if not skel_path:
                        log.warning("mount overlay: can't find skeleton for src=%d", sk)
                        continue
                    try:
                        skel_dir = '/'.join(skel_path.split('/')[:-1])
                        skel_file = skel_path.split('/')[-1]
                        pab_data = bytes(crimson_rs.extract_file(
                            game_path, '0009', skel_dir, skel_file))
                        if has_rider_bone(pab_data):
                            log.info("mount overlay: src=%d already has B_Rider_01", sk)
                        else:
                            modified = inject_rider_bone(pab_data, rider_y=rider_y)
                            out_dir = os.path.join(tmp_dir, *skel_path.split('/')[:-1])
                            os.makedirs(out_dir, exist_ok=True)
                            with open(os.path.join(out_dir, skel_file), 'wb') as f:
                                f.write(modified)
                            files_written += 1
                            log.info("mount overlay: injected B_Rider_01 Y=%.1f "
                                     "for src=%d skel=%s", rider_y, sk, skel_path)
                    except Exception:
                        log.exception("mount overlay: rider inject failed for src=%d", sk)

            if files_written == 0:
                return "no overlay files needed"

            # Pack and deploy
            pack_out = os.path.join(tmp_dir, 'output')
            os.makedirs(pack_out, exist_ok=True)
            group = self._MOUNT_OVERLAY_GROUP
            crimson_rs.pack_mod.pack_mod(
                game_dir=game_path, mod_folder=tmp_dir,
                output_dir=pack_out, group_name=group)

            papgt = gp / 'meta' / '0.papgt'
            backup = papgt.with_suffix(f'.papgt.mount_{group}_bak')
            if papgt.exists() and not backup.exists():
                shutil.copy2(papgt, backup)

            dest = gp / group
            dest.mkdir(exist_ok=True)
            shutil.copyfile(
                os.path.join(pack_out, group, '0.paz'), dest / '0.paz')
            shutil.copyfile(
                os.path.join(pack_out, group, '0.pamt'), dest / '0.pamt')
            shutil.copyfile(
                os.path.join(pack_out, 'meta', '0.papgt'), papgt)

        return f"deployed {files_written} file(s) to {group}/"

    def _find_skeleton_path(self, game_path: str, entry: dict,
                            catalog: list[dict]) -> str | None:
        """Resolve a characterinfo entry to its skeleton .pab PAZ path.

        Strategy: find the appearance XML path, then search the PAMT for
        a .pab skeleton in a matching model/ directory. The appearance path
        encodes the creature family which maps to the skeleton directory.

        Example:
          appearance: character/appearance/2_mon/cd_m0004_00_dragon/cd_m0004_00_golemdragon_0001/...
          skeleton:   character/model/2_mon/cd_m0004_00_dragon/cd_m0004_00_golemdragon/cd_m0004_00_golemdragon.pab
        """
        try:
            import crimson_rs
            from character_mesh_swap import _guess_xml_path

            xml_path = _guess_xml_path(entry, catalog)
            if not xml_path:
                log.warning("_find_skeleton_path: no xml_path for %s",
                            entry.get('internal_name'))
                return None

            # Extract the creature stem from the appearance path DIRECTORIES (not filename)
            # e.g. ".../cd_m0004_00_golemdragon_0001/file.app.xml" -> "cd_m0004_00_golemdragon"
            parts = xml_path.split('/')
            dirs = parts[:-1]  # exclude filename
            creature_stem = None
            for p in reversed(dirs):
                if p.startswith('cd_') and '_00_' in p:
                    # Strip trailing variant number suffix
                    # "cd_m0004_00_golemdragon_0001" -> "cd_m0004_00_golemdragon"
                    segs = p.split('_')
                    while segs and segs[-1].isdigit():
                        segs.pop()
                    creature_stem = '_'.join(segs)
                    break

            if not creature_stem:
                log.warning("_find_skeleton_path: can't extract creature stem from %s",
                            xml_path)
                return None

            # Search PAMT for a .pab skeleton matching this creature
            pamt = crimson_rs.parse_pamt_file(
                os.path.join(game_path, '0009', '0.pamt'))

            target_pab = creature_stem + '.pab'
            for d in pamt.get('directories', []):
                if '/model/' in d['path']:
                    for f in d.get('files', []):
                        if f['name'] == target_pab:
                            result = d['path'] + '/' + f['name']
                            log.info("_find_skeleton_path: %s -> %s",
                                     entry.get('internal_name'), result)
                            return result

            log.warning("_find_skeleton_path: no .pab named %s in PAMT", target_pab)
        except Exception:
            log.exception("_find_skeleton_path failed")
        return None

    def _write_modified_files(self, mod_dir: str) -> None:
        written: list[str] = []
        if (self._field_edit_data and self._field_edit_original
                and bytes(self._field_edit_data) != self._field_edit_original):
            with open(os.path.join(mod_dir, "fieldinfo.pabgb"), "wb") as f:
                f.write(self._field_edit_data)
            written.append(f"fieldinfo.pabgb ({len(self._field_edit_data)}B)")
        if (self._vehicle_data and self._vehicle_original
                and bytes(self._vehicle_data) != self._vehicle_original):
            with open(os.path.join(mod_dir, "vehicleinfo.pabgb"), "wb") as f:
                f.write(self._vehicle_data)
            written.append(f"vehicleinfo.pabgb ({len(self._vehicle_data)}B)")
        if (self._gptrigger_data and self._gptrigger_original
                and bytes(self._gptrigger_data) != self._gptrigger_original):
            with open(os.path.join(mod_dir, "gameplaytrigger.pabgb"), "wb") as f:
                f.write(self._gptrigger_data)
            written.append(f"gameplaytrigger.pabgb ({len(self._gptrigger_data)}B)")
        if (self._regioninfo_data and self._regioninfo_original
                and bytes(self._regioninfo_data) != self._regioninfo_original):
            with open(os.path.join(mod_dir, "regioninfo.pabgb"), "wb") as f:
                f.write(self._regioninfo_data)
            written.append(f"regioninfo.pabgb ({len(self._regioninfo_data)}B)")
        if (self._charinfo_data and self._charinfo_original
                and bytes(self._charinfo_data) != self._charinfo_original):
            with open(os.path.join(mod_dir, "characterinfo.pabgb"), "wb") as f:
                f.write(self._charinfo_data)
            diff_bytes = sum(1 for a, b in zip(self._charinfo_data, self._charinfo_original)
                             if a != b)
            written.append(f"characterinfo.pabgb ({len(self._charinfo_data)}B, "
                           f"{diff_bytes} bytes diff)")
        if (self._wantedinfo_data and self._wantedinfo_original
                and bytes(self._wantedinfo_data) != self._wantedinfo_original):
            with open(os.path.join(mod_dir, "wantedinfo.pabgb"), "wb") as f:
                f.write(self._wantedinfo_data)
            written.append(f"wantedinfo.pabgb ({len(self._wantedinfo_data)}B)")
        if (self._allygroup_data is not None and self._allygroup_original is not None
                and bytes(self._allygroup_data) != self._allygroup_original):
            with open(os.path.join(mod_dir, "allygroupinfo.pabgb"), "wb") as f:
                f.write(self._allygroup_data)
            written.append(f"allygroupinfo.pabgb ({len(self._allygroup_data)}B)")
        if (self._relationinfo_data is not None and self._relationinfo_original is not None
                and bytes(self._relationinfo_data) != self._relationinfo_original):
            with open(os.path.join(mod_dir, "relationinfo.pabgb"), "wb") as f:
                f.write(self._relationinfo_data)
            written.append(f"relationinfo.pabgb ({len(self._relationinfo_data)}B)")
        if (self._factionrelgrp_data is not None and self._factionrelgrp_original is not None
                and bytes(self._factionrelgrp_data) != self._factionrelgrp_original):
            with open(os.path.join(mod_dir, "factionrelationgroup.pabgb"), "wb") as f:
                f.write(self._factionrelgrp_data)
            written.append(f"factionrelationgroup.pabgb ({len(self._factionrelgrp_data)}B)")
        if written:
            log.info("FieldEdit wrote %d file(s) to mod_dir: %s",
                     len(written), ", ".join(written))
        else:
            log.warning("FieldEdit _write_modified_files: no diffs — no files written")

    def _ally_relation_dirty(self) -> bool:
        ag_dirty = (self._allygroup_data is not None
                    and self._allygroup_original is not None
                    and bytes(self._allygroup_data) != self._allygroup_original)
        ri_dirty = (self._relationinfo_data is not None
                    and self._relationinfo_original is not None
                    and bytes(self._relationinfo_data) != self._relationinfo_original)
        fg_dirty = (self._factionrelgrp_data is not None
                    and self._factionrelgrp_original is not None
                    and bytes(self._factionrelgrp_data) != self._factionrelgrp_original)
        return ag_dirty or ri_dirty or fg_dirty

    def _field_edit_apply(self):
        mesh_queue = self._mesh_swap_queue or []
        ally_dirty = self._ally_relation_dirty()
        if (not self._field_edit_data and not ally_dirty
                and not self._field_edit_modified and not mesh_queue):
            QMessageBox.information(self, tr("FieldEdit"), tr("No modifications to apply."))
            return
        game_path = self._config.get("game_install_path", "")
        if not game_path or not os.path.isdir(game_path):
            QMessageBox.critical(self, tr("Game Path"), tr("Game install path not set."))
            return
        reply = QMessageBox.question(
            self, tr("Apply FieldInfo Changes"),
            "Deploy modified game data to the game?\n\n"
            "TIP: Use 'Export Field JSON v3' to get a DMM-compatible mod file instead.\n\n"
            "IMPORTANT: If you already have a FieldEdit mod applied,\n"
            "click Restore FIRST before applying new changes.\n"
            "Applying over an existing mod will crash the game.\n\n"
            "Creates PAZ overlay. Restart game to take effect.\n"
            "Use Restore to undo.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply != QMessageBox.Yes:
            return

        mesh_applied = self._field_edit_apply_mesh_swaps()
        if mesh_applied:
            self._field_edit_status.setText(f"Applied {mesh_applied} mesh swap(s). Packing...")
        else:
            self._field_edit_status.setText(tr("Packing with pack_mod..."))
        QApplication.processEvents()

        log.info("=== field_edit apply pipeline: dirty-buffer summary ===")
        buffers = [
            ("fieldinfo",     self._field_edit_data,   self._field_edit_original),
            ("vehicleinfo",   self._vehicle_data,      self._vehicle_original),
            ("gameplaytrigger", self._gptrigger_data,  self._gptrigger_original),
            ("regioninfo",    self._regioninfo_data,   self._regioninfo_original),
            ("characterinfo", self._charinfo_data,     self._charinfo_original),
            ("wantedinfo",    self._wantedinfo_data,   self._wantedinfo_original),
            ("allygroupinfo", self._allygroup_data,    self._allygroup_original),
            ("relationinfo",  self._relationinfo_data, self._relationinfo_original),
            ("factionrelationgroup", self._factionrelgrp_data, self._factionrelgrp_original),
        ]
        dirty_names: list[str] = []
        for name, data, orig in buffers:
            if data is None:
                log.info("  %-16s : NOT LOADED", name)
            elif orig is None:
                log.info("  %-16s : no original snapshot (loaded but baseline missing)", name)
            elif bytes(data) == orig:
                log.info("  %-16s : clean (%d B, no changes)", name, len(data))
            else:
                diff = sum(1 for a, b in zip(data, orig) if a != b)
                log.info("  %-16s : DIRTY (%d B, %d bytes diff vs vanilla)",
                         name, len(data), diff)
                dirty_names.append(name)
        if not dirty_names:
            log.warning("field_edit apply: NO buffers are dirty — pack_mod will produce an empty overlay!")
        else:
            log.info("field_edit apply: %d dirty buffer(s) will be packed: %s",
                     len(dirty_names), ", ".join(dirty_names))

        try:
            from gui.utils import resolve_overlay_group
            requested = self._fieldedit_overlay_spin.value()
            group_num = resolve_overlay_group(game_path, requested, "FieldEdit", parent=self)
            if group_num is None:
                return
            if group_num != requested:
                self._fieldedit_overlay_spin.setValue(group_num)

            import crimson_rs
            INTERNAL_DIR = "gamedata/binary__/client/bin"
            gp = Path(game_path)
            base_group = group_num

            dirty_tables = []
            table_defs = [
                ("fieldinfo",     self._field_edit_data,    self._field_edit_original),
                ("gameplaytrigger", self._gptrigger_data,   self._gptrigger_original),
                ("wantedinfo",    self._wantedinfo_data,    self._wantedinfo_original),
                ("allygroupinfo", self._allygroup_data,     self._allygroup_original),
                ("relationinfo",  self._relationinfo_data,  self._relationinfo_original),
                ("factionrelationgroup", self._factionrelgrp_data, self._factionrelgrp_original),
            ]
            for stem, data, orig in table_defs:
                if data is not None and orig is not None and bytes(data) != orig:
                    dirty_tables.append((stem, bytes(data)))

            # vehicleinfo: prefer dmm_parser field-level serialization
            _vi_dmm_dirty = getattr(self, '_vehicleinfo_dmm_dirty', False)
            _vi_dmm_items = getattr(self, '_vehicleinfo_dmm_items', None)
            _vi_byte_dirty = (self._vehicle_data is not None
                              and self._vehicle_original is not None
                              and bytes(self._vehicle_data) != self._vehicle_original)
            if _vi_dmm_dirty and _vi_dmm_items:
                import dmm_parser as _dmp
                vi_pabgb = bytes(_dmp.serialize_table('vehicle_info', _vi_dmm_items))
                dirty_tables.append(("vehicleinfo", vi_pabgb))
                log.info("vehicleinfo: serialized via dmm_parser (%d bytes)", len(vi_pabgb))
            elif _vi_byte_dirty:
                dirty_tables.append(("vehicleinfo", bytes(self._vehicle_data)))
                log.info("vehicleinfo: using byte-patched data")

            # regioninfo: prefer dmm_parser field-level serialization
            _ri_dmm_dirty = getattr(self, '_regioninfo_dmm_dirty', False)
            _ri_dmm_items = getattr(self, '_regioninfo_dmm_items', None)
            _ri_byte_dirty = (self._regioninfo_data is not None
                              and self._regioninfo_original is not None
                              and bytes(self._regioninfo_data) != self._regioninfo_original)
            if _ri_dmm_dirty and _ri_dmm_items:
                import dmm_parser as _dmp
                ri_pabgb = bytes(_dmp.serialize_table('region_info', _ri_dmm_items))
                dirty_tables.append(("regioninfo", ri_pabgb))
                log.info("regioninfo: serialized via dmm_parser (%d bytes)", len(ri_pabgb))
            elif _ri_byte_dirty:
                dirty_tables.append(("regioninfo", bytes(self._regioninfo_data)))
                log.info("regioninfo: using byte-patched data")

            # characterinfo: deployed separately via merge helper to avoid
            # conflicts with ItemBuffs Kliff gun fix overlay
            _dmm_dirty = getattr(self, '_charinfo_dmm_dirty', False)
            _dmm_items = getattr(self, '_charinfo_dmm_items', None)
            _byte_dirty = (self._charinfo_data is not None
                           and self._charinfo_original is not None
                           and bytes(self._charinfo_data) != self._charinfo_original)
            _charinfo_pabgb = None
            if _dmm_dirty and _dmm_items:
                import dmm_parser as _dmp
                _charinfo_pabgb = bytes(_dmp.serialize_table('character_info', _dmm_items))
                log.info("characterinfo: serialized via dmm_parser (%d bytes)", len(_charinfo_pabgb))
            elif _byte_dirty:
                _charinfo_pabgb = bytes(self._charinfo_data)
                log.info("characterinfo: using byte-patched data (%d bytes)", len(_charinfo_pabgb))

            if not dirty_tables:
                log.warning("FieldEdit apply: no dirty tables")

            papgt_path = gp / "meta" / "0.papgt"
            backup_path = papgt_path.with_suffix(".papgt.field_bak")
            if papgt_path.exists() and not backup_path.exists():
                shutil.copy2(papgt_path, backup_path)

            deployed = []
            for i, (stem, pabgb_data) in enumerate(dirty_tables):
                grp = f"{base_group + i:04d}"
                pabgh_data = bytes(crimson_rs.extract_file(
                    game_path, "0008", INTERNAL_DIR, f"{stem}.pabgh"))

                with tempfile.TemporaryDirectory() as tmp_dir:
                    group_dir = os.path.join(tmp_dir, grp)
                    builder = crimson_rs.PackGroupBuilder(
                        group_dir, crimson_rs.Compression.NONE,
                        crimson_rs.Crypto.NONE)
                    builder.add_file(INTERNAL_DIR, f"{stem}.pabgb", pabgb_data)
                    builder.add_file(INTERNAL_DIR, f"{stem}.pabgh", pabgh_data)
                    pamt_bytes = bytes(builder.finish())
                    pamt_checksum = crimson_rs.parse_pamt_bytes(pamt_bytes)["checksum"]

                    dest = gp / grp
                    dest.mkdir(exist_ok=True)
                    for fn in os.listdir(group_dir):
                        shutil.copy2(os.path.join(group_dir, fn), str(dest / fn))

                papgt = crimson_rs.parse_papgt_file(str(papgt_path))
                papgt["entries"] = [e for e in papgt["entries"]
                                    if e.get("group_name") != grp]
                papgt = crimson_rs.add_papgt_entry(
                    papgt, grp, pamt_checksum, 0, 16383)
                crimson_rs.write_papgt_file(papgt, str(papgt_path))
                deployed.append(f"{stem} -> {grp}/")
                log.info("FieldEdit deployed %s to %s/ (%d bytes)",
                         stem, grp, len(pabgb_data))

            # Deploy characterinfo separately via merge helper
            if _charinfo_pabgb:
                from gui.utils import deploy_merged_pabgb
                ci_pabgh = bytes(crimson_rs.extract_file(
                    game_path, "0008", INTERNAL_DIR, "characterinfo.pabgh"))
                ci_grp = f"{self._config.get('charinfo_overlay_dir', 65):04d}"
                deploy_merged_pabgb(
                    game_path, 'character_info', 'characterinfo',
                    _charinfo_pabgb, ci_pabgh, ci_grp,
                    "FieldEdit mounts/mesh", parent=self)
                deployed.append(f"characterinfo -> {ci_grp}/ (merged)")

            summary = "\n".join(f"  {d}" for d in deployed)
            self._field_edit_status.setText(f"Applied {len(deployed)} table(s)")
            QMessageBox.information(self, tr("Applied"),
                f"FieldInfo mod deployed:\n{summary}\n\n"
                f"Restart the game for changes to take effect.")
        except Exception as e:
            log.exception("FieldEdit apply failed")
            self._field_edit_status.setText(f"Apply failed: {e}")
            QMessageBox.critical(self, tr("Apply Failed"), str(e))

    def _field_edit_export_mod(self):
        mesh_queue = self._mesh_swap_queue or []
        ally_dirty = self._ally_relation_dirty()
        if (not self._field_edit_data and not ally_dirty
                and not self._field_edit_modified and not mesh_queue):
            QMessageBox.information(self, tr("Export Field JSON v3"), tr("No modifications to export."))
            return
        self._field_edit_apply_mesh_swaps()

        name, ok = QInputDialog.getText(self, tr("Export Field JSON v3"),
                                        tr("Mod name:"), text="FieldEdit Mod")
        if not ok or not name.strip():
            return
        name = name.strip()

        exe_dir = os.path.dirname(os.path.abspath(sys.argv[0] or "."))
        default_dir = os.path.join(exe_dir, "packs")
        os.makedirs(default_dir, exist_ok=True)
        folder_name = "".join(c if (c.isalnum() or c in "-_ ") else "_" for c in name)
        out_path = os.path.join(default_dir, folder_name)

        try:
            if os.path.isdir(out_path):
                shutil.rmtree(out_path)
            os.makedirs(out_path, exist_ok=True)
            files_dir = os.path.join(out_path, "files", "gamedata", "binary__", "client", "bin")
            os.makedirs(files_dir, exist_ok=True)
            self._write_modified_files(files_dir)

            modinfo = {
                "id": name.lower().replace(" ", "_"),
                "name": name,
                "version": "1.0.0",
                "game_version": "1.00.03",
                "author": "CrimsonSaveEditor",
                "description": f"FieldEdit mod: {name}",
            }
            with open(os.path.join(out_path, "modinfo.json"), "w", encoding="utf-8") as f:
                json.dump(modinfo, f, indent=2)

            written = sorted(os.listdir(files_dir))
            self._field_edit_status.setText(
                f"Exported mod to packs/{folder_name}/ ({len(written)} file(s))")
            QMessageBox.information(self, tr("Mod Exported"),
                f"Mod exported to:\n{out_path}\n\n"
                f"Contents:\n"
                f"  files/gamedata/binary__/client/bin/\n"
                + "".join(f"    {fn}\n" for fn in written)
                + f"  modinfo.json\n\n"
                f"To install: copy '{folder_name}' into your mod loader's\n"
                f"mods/ directory (CD JSON Mod Manager, DMM, or CDUMM).")
        except Exception as e:
            log.exception("FieldEdit raw-mod export failed")
            self._field_edit_status.setText(f"Export failed: {e}")
            QMessageBox.critical(self, tr("Export Failed"), str(e))

    def _field_edit_export(self):
        mesh_queue = self._mesh_swap_queue or []
        ally_dirty = self._ally_relation_dirty()
        if (not self._field_edit_data and not ally_dirty
                and not self._field_edit_modified and not mesh_queue):
            QMessageBox.information(self, tr("FieldEdit"), tr("No modifications to export."))
            return
        self._field_edit_apply_mesh_swaps()

        name, ok = QInputDialog.getText(self, tr("Export Field Mod"),
                                        tr("Mod name:"), text="Mount Everywhere")
        if not ok or not name.strip():
            return
        name = name.strip()

        exe_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
        default_dir = os.path.join(exe_dir, "packs")
        os.makedirs(default_dir, exist_ok=True)
        folder_name = "".join(c if (c.isalnum() or c in "-_ ") else "_" for c in name)
        save_dir = QFileDialog.getExistingDirectory(
            self, f"Choose folder for '{folder_name}' mod", default_dir)
        if not save_dir:
            return
        out_path = os.path.join(save_dir, folder_name)

        self._field_edit_status.setText(tr("Packing..."))
        QApplication.processEvents()

        try:
            import crimson_rs.pack_mod
            game_path = self._config.get("game_install_path", "")
            if os.path.isdir(out_path):
                shutil.rmtree(out_path)
            os.makedirs(out_path, exist_ok=True)

            with tempfile.TemporaryDirectory() as tmp_dir:
                mod_dir = os.path.join(tmp_dir, "gamedata", "binary__", "client", "bin")
                os.makedirs(mod_dir, exist_ok=True)
                self._write_modified_files(mod_dir)

                pack_out = os.path.join(tmp_dir, "output")
                os.makedirs(pack_out, exist_ok=True)
                mod_group = "0036"
                crimson_rs.pack_mod.pack_mod(
                    game_dir=game_path,
                    mod_folder=tmp_dir,
                    output_dir=pack_out,
                    group_name=mod_group,
                )
                paz_dst = os.path.join(out_path, mod_group)
                os.makedirs(paz_dst, exist_ok=True)
                shutil.copy2(os.path.join(pack_out, mod_group, "0.paz"),
                             os.path.join(paz_dst, "0.paz"))
                shutil.copy2(os.path.join(pack_out, mod_group, "0.pamt"),
                             os.path.join(paz_dst, "0.pamt"))
                meta_dst = os.path.join(out_path, "meta")
                os.makedirs(meta_dst, exist_ok=True)
                shutil.copy2(os.path.join(pack_out, "meta", "0.papgt"),
                             os.path.join(meta_dst, "0.papgt"))

            modinfo = {
                "id": name.lower().replace(" ", "_"),
                "name": name,
                "version": "1.0.0",
                "game_version": "1.00.03",
                "author": "CrimsonSaveEditor",
                "description": f"FieldInfo mod: {name}",
            }
            with open(os.path.join(out_path, "modinfo.json"), "w", encoding="utf-8") as f:
                json.dump(modinfo, f, indent=2)

            self._field_edit_status.setText(f"Exported to {folder_name}/{mod_group}/")
            QMessageBox.information(self, tr("Exported"),
                f"Mod exported to:\n{out_path}\n\n"
                f"Contents:\n"
                f"  {mod_group}/0.paz + {mod_group}/0.pamt\n"
                f"  meta/0.papgt\n"
                f"  modinfo.json\n\n"
                f"JMM / CDUMM should show this as 'compiled'.")
        except Exception as e:
            log.exception("FieldEdit export failed")
            self._field_edit_status.setText(f"Export failed: {e}")
            QMessageBox.critical(self, tr("Export Failed"), str(e))

    def _field_edit_export_json(self):
        self._field_edit_apply_mesh_swaps()
        mesh_queue = self._mesh_swap_queue or []
        if not self._field_edit_modified and not mesh_queue:
            QMessageBox.information(self, tr("Export JSON"), tr("No modifications to export."))
            return

        name, ok = QInputDialog.getText(self, tr("Export JSON Mod"),
                                        tr("Mod name:"), text="Mount Everywhere")
        if not ok or not name.strip():
            return
        name = name.strip()

        def _diff_bytes(data, original, label_fn=None):
            changes = []
            i = 0
            while i < len(data):
                if data[i] != original[i]:
                    start = i
                    while i < len(data) and data[i] != original[i]:
                        i += 1
                    label = label_fn(start, i - start) if label_fn else f"offset_{start}"
                    changes.append({
                        "offset": start,
                        "label": label,
                        "original": bytes(original[start:i]).hex().upper(),
                        "patched": bytes(data[start:i]).hex().upper(),
                    })
                else:
                    i += 1
            return changes

        patches = []

        if self._field_edit_data and self._field_edit_original:
            def _fi_label(off, _n):
                for e in self._field_edit_entries:
                    if e.get('can_call_vehicle_offset') == off:
                        return f"{e.get('name', 'Zone_' + str(e['key']))}: _canCallVehicle ({e.get('can_call_vehicle', '?')})"
                    if e.get('always_call_vehicle_dev_offset') == off:
                        return f"{e.get('name', 'Zone_' + str(e['key']))}: _alwaysCallVehicle_dev ({e.get('always_call_vehicle_dev', '?')})"
                return f"fieldinfo offset {off}"
            ch = _diff_bytes(self._field_edit_data, self._field_edit_original, _fi_label)
            if ch:
                patches.append({"game_file": "gamedata/fieldinfo.pabgb", "changes": ch})

        if self._vehicle_data and self._vehicle_original:
            def _vi_label(off, _n):
                for e in self._vehicle_entries:
                    if e.get('mount_call_type_offset') == off:
                        return f"{e['name']}: _mountCallType ({e.get('mount_call_type', '?')})"
                    if e.get('can_call_safe_zone_offset') == off:
                        return f"{e['name']}: _canCallSafeZone ({e.get('can_call_safe_zone', '?')})"
                    ao = e.get('altitude_cap_offset', -1)
                    if ao <= off < ao + 4:
                        return f"{e['name']}: _altitudeCap"
                return f"vehicleinfo offset {off}"
            ch = _diff_bytes(self._vehicle_data, self._vehicle_original, _vi_label)
            if ch:
                patches.append({"game_file": "gamedata/vehicleinfo.pabgb", "changes": ch})

        if self._gptrigger_data and self._gptrigger_original:
            def _gt_label(off, _n):
                for e in self._gptrigger_entries:
                    if e.get('safe_zone_type_offset') == off:
                        return f"{e['name']}: _safeZoneType ({e.get('safe_zone_type', '?')})"
                return f"gameplaytrigger offset {off}"
            ch = _diff_bytes(self._gptrigger_data, self._gptrigger_original, _gt_label)
            if ch:
                patches.append({"game_file": "gamedata/gameplaytrigger.pabgb", "changes": ch})

        if self._regioninfo_entries and hasattr(self, '_regioninfo_dmm_items') and self._regioninfo_dmm_items:
            import dmm_parser as _dmp_ri_exp
            cur_ri = _dmp_ri_exp.serialize_table('region_info', self._regioninfo_dmm_items)
            van_ri = _dmp_ri_exp.serialize_table('region_info', self._regioninfo_dmm_vanilla)
            if cur_ri != van_ri:
                def _ri_label(off, _n):
                    return f"regioninfo offset {off}"
                ch = _diff_bytes(cur_ri, van_ri, _ri_label)
                if ch:
                    patches.append({"game_file": "gamedata/regioninfo.pabgb", "changes": ch})

        if self._charinfo_data and self._charinfo_original:
            ci_field_map = {}
            from characterinfo_full_parser import parse_all_entries as _ci_all
            _all_ci = _ci_all(bytes(self._charinfo_data), self._charinfo_schema)
            for e in _all_ci:
                dur_off = e.get('_callMercenarySpawnDuration_offset', -1)
                cool_off = e.get('_callMercenaryCoolTime_offset', -1)
                att_off = e.get('_isAttackable_offset', -1)
                inv_off = e.get('_invincibility_offset', -1)
                nm = e.get('name', '?')
                if dur_off >= 0:
                    ci_field_map[dur_off] = f"{nm}: _callMercenarySpawnDuration"
                if cool_off >= 0:
                    ci_field_map[cool_off] = f"{nm}: _callMercenaryCoolTime"
                if att_off >= 0:
                    ci_field_map[att_off] = f"{nm}: _isAttackable"
                if inv_off >= 0:
                    ci_field_map[inv_off] = f"{nm}: _invincibility"
            def _ci_label(off, _n):
                for foff, lbl in ci_field_map.items():
                    if foff <= off < foff + 8:
                        return lbl
                return f"characterinfo offset {off}"
            ch = _diff_bytes(self._charinfo_data, self._charinfo_original, _ci_label)
            if ch:
                patches.append({"game_file": "gamedata/characterinfo.pabgb", "changes": ch})

        if self._wantedinfo_data and self._wantedinfo_original:
            from wantedinfo_parser import parse_all_entries as wi_parse, FACTION_NAMES, CRIME_TIERS
            wi_entries = wi_parse(bytes(self._wantedinfo_data), self._wantedinfo_schema)
            wi_field_map = {}
            for e in wi_entries:
                faction = FACTION_NAMES.get(e['_faction'], f"Faction_{e['_faction']}")
                tier = CRIME_TIERS.get(e['_crimeTier'], f"Tier_{e['_crimeTier']}")
                off = e.get('_isBlocked_offset', -1)
                if off >= 0:
                    wi_field_map[off] = f"{faction}_{tier}: _isBlocked"
                price_off = e.get('_increasePrice_offset', -1)
                if price_off >= 0:
                    for b in range(8):
                        wi_field_map[price_off + b] = f"{faction}_{tier}: _increasePrice"
            def _wi_label(off, _n):
                return wi_field_map.get(off, f"wantedinfo offset {off}")
            ch = _diff_bytes(self._wantedinfo_data, self._wantedinfo_original, _wi_label)
            if ch:
                patches.append({"game_file": "gamedata/wantedinfo.pabgb", "changes": ch})

        if not patches:
            QMessageBox.information(self, tr("Export JSON"), tr("No byte-level changes detected."))
            return

        total_changes = sum(len(p['changes']) for p in patches)
        export = {
            "name": name,
            "version": "1.0.0",
            "author": "CrimsonSaveEditor",
            "description": f"{name} — {total_changes} changes across {len(patches)} game files.",
            "patches": patches,
        }

        default_name = "".join(c if (c.isalnum() or c in "-_ ") else "_" for c in name) + ".json"
        path, _ = QFileDialog.getSaveFileName(
            self, tr("Export JSON Mod"), default_name, "JSON Files (*.json)")
        if not path:
            return
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(export, f, indent=2, ensure_ascii=False)

        self._field_edit_status.setText(f"Exported {total_changes} changes to {os.path.basename(path)}")
        QMessageBox.information(self, tr("Exported"),
            f"Saved {total_changes} changes across {len(patches)} files to:\n{path}")

    def _field_edit_import_field_json_v3(self) -> None:
        """Import a Format 3 field JSON mod and apply intents to in-memory buffers."""
        if not self._field_edit_data and not self._vehicle_data:
            QMessageBox.warning(self, tr("Import Field JSON v3"),
                "Load FieldInfo first (click 'Load FieldInfo').")
            return
        path, _ = QFileDialog.getOpenFileName(
            self, tr("Import Field JSON v3"), "",
            "Field JSON (*.field.json *.json);;All Files (*)")
        if not path:
            return
        try:
            import json as _jifj
            with open(path, encoding='utf-8') as _fh:
                doc = _jifj.load(_fh)
        except Exception as _e:
            QMessageBox.critical(self, tr("Import Field JSON v3"),
                f"Could not read file:\n{_e}")
            return
        if doc.get('format') != 3:
            QMessageBox.warning(self, tr("Import Field JSON v3"),
                "Not a Format 3 field JSON file.")
            return

        _TABLE_MAP = {
            'fieldinfo':              ('_field_edit_data', '_field_edit_original'),
            'vehicleinfo':            ('_vehicle_data',    '_vehicle_original'),
            'gameplaytrigger':        ('_gptrigger_data',  '_gptrigger_original'),
            'gameplaytriggerinfo':    ('_gptrigger_data',  '_gptrigger_original'),
            'regioninfo':             ('_regioninfo_data', '_regioninfo_original'),
            'characterinfo':          ('_charinfo_data',   '_charinfo_original'),
            'wantedinfo':             ('_wantedinfo_data', '_wantedinfo_original'),
        }
        _DMM_TABLE = {
            'fieldinfo':           'field_info',
            'vehicleinfo':         'vehicle_info',
            'gameplaytrigger':     'game_play_trigger_info',
            'gameplaytriggerinfo': 'game_play_trigger_info',
            'regioninfo':          'region_info',
            'characterinfo':       'character_info',
            'wantedinfo':          'wanted_info',
        }
        _PHYS_NAME = {
            'fieldinfo':           'fieldinfo',
            'vehicleinfo':         'vehicleinfo',
            'gameplaytrigger':     'gameplaytrigger',
            'gameplaytriggerinfo': 'gameplaytrigger',
            'regioninfo':          'regioninfo',
            'characterinfo':       'characterinfo',
            'wantedinfo':          'wantedinfo',
        }

        targets = doc.get('targets', [])
        if not targets:
            tgt_file = doc.get('target', doc.get('file', ''))
            if tgt_file:
                targets = [{'file': tgt_file, 'intents': doc.get('intents', [])}]

        game_path = self._config.get('game_install_path', '') or ''
        total_applied = total_skipped = 0
        modified_tables = []

        for tgt in targets:
            fname_compact = tgt.get('file', '').replace('.pabgb','').replace('_','').lower()
            table_key = next((k for k in _TABLE_MAP
                              if k.replace('_','').lower() == fname_compact), None)
            if table_key is None:
                total_skipped += len(tgt.get('intents', [])); continue

            data_attr, orig_attr = _TABLE_MAP[table_key]
            cur_buf = getattr(self, data_attr, None)
            orig_buf = getattr(self, orig_attr, None)
            if cur_buf is None:
                total_skipped += len(tgt.get('intents', [])); continue

            intents = tgt.get('intents', [])
            applied = skipped = 0
            dmm_ok = False

            # ── Classify intents ──
            # Hex-string new values (e.g. '00000001') are raw byte patches
            # regardless of field name. Apply them via _offset.
            # _camelCase fields are PA canonical names from v3.1 exports —
            # also raw byte patches with _offset.
            raw_byte_intents = []
            named_field_intents = []
            for _intent in intents:
                _field = _intent.get('field', '')
                _new = _intent.get('new')
                _is_hex = (isinstance(_new, str) and
                           all(c in '0123456789abcdefABCDEF' for c in _new) and
                           len(_new) % 2 == 0 and len(_new) > 0)
                if (_field == 'raw_bytes' or _is_hex or
                        (_field.startswith('_') and '_offset' in _intent)):
                    raw_byte_intents.append(_intent)
                else:
                    named_field_intents.append(_intent)

            # ── dmm_parser field-level apply (snake_case named fields) ──
            if named_field_intents and game_path and _DMM_TABLE.get(table_key):
                try:
                    import dmm_parser as _dmp_ifj
                    import crimson_rs as _cr_ifj
                    _dp = 'gamedata/binary__/client/bin'
                    _phys = _PHYS_NAME[table_key]
                    _pabgh = bytes(_cr_ifj.extract_file(game_path, '0008', _dp, f'{_phys}.pabgh'))
                    _dname = _DMM_TABLE[table_key]
                    _recs = list(_dmp_ifj.parse_table(_dname, bytes(cur_buf), _pabgh))
                    _by_key  = {int(r.get('key', 0)): r for r in _recs}
                    _by_skey = {r.get('string_key', ''): r for r in _recs}
                    _dmp_applied = 0
                    for intent in named_field_intents:
                        if intent.get('op') != 'set': skipped += 1; continue
                        field = intent.get('field', '')
                        new_val = intent.get('new')
                        if new_val is None: skipped += 1; continue
                        rec = _by_skey.get(intent.get('entry', ''))
                        if rec is None:
                            rk = intent.get('key')
                            if rk is not None: rec = _by_key.get(int(rk))
                        if rec is None: skipped += 1; continue
                        parts = field.split('.')
                        td = rec
                        for part in parts[:-1]:
                            td = td.get(part) if isinstance(td, dict) else None
                            if td is None: break
                        if td is None: skipped += 1; continue
                        leaf = parts[-1]
                        ex = td.get(leaf)
                        if isinstance(ex, dict) and isinstance(new_val, (int, float)):
                            td[leaf] = {k: type(v)(new_val) for k, v in ex.items()}
                        else:
                            td[leaf] = new_val
                        _dmp_applied += 1
                    if _dmp_applied:
                        new_bytes = _dmp_ifj.serialize_table(_dname, _recs, _pabgh)
                        setattr(self, data_attr, bytearray(new_bytes))
                        applied += _dmp_applied
                        dmm_ok = True
                except Exception:
                    # Fall through — treat named intents as raw byte too
                    raw_byte_intents = intents

            # ── Raw byte apply (raw_bytes field, hex new values, _camelCase fields) ──
            if raw_byte_intents:
                buf = bytearray(getattr(self, data_attr))
                for intent in raw_byte_intents:
                    if intent.get('op') != 'set': skipped += 1; continue
                    off = intent.get('_offset')
                    if off is None:
                        off = intent.get('key', 0)
                    try:
                        off = int(off)
                        raw = bytes.fromhex(str(intent['new']))
                        if 0 <= off <= len(buf) - len(raw):
                            buf[off:off + len(raw)] = raw
                            applied += 1
                        else:
                            skipped += 1
                    except Exception:
                        skipped += 1
                setattr(self, data_attr, buf)

            if applied:
                modified_tables.append(table_key)
            total_applied += applied
            total_skipped += skipped

        if not total_applied:
            QMessageBox.warning(self, tr("Import Field JSON v3"),
                f"No intents applied ({total_skipped} skipped).\n\n"
                "Make sure FieldInfo is loaded and the mod targets a supported table.")
            return

        self._field_edit_modified = True
        # Re-parse entries from modified buffers, then refresh UI tables.
        # _vehicle_populate() reads self._vehicle_entries (structs), not bytes —
        # so we must re-parse the modified byte buffer first.
        if 'vehicleinfo' in modified_tables and self._vehicle_data and self._vehicle_schema:
            try:
                from vehicleinfo_parser import parse_pabgh_index_u16, parse_entry as _vparse
                _vidx = parse_pabgh_index_u16(self._vehicle_schema)
                _vsorted = sorted(set(_vidx.values()))
                _ventries = []
                for _vk, _vo in sorted(_vidx.items()):
                    _vbi = _vsorted.index(_vo)
                    _vend = _vsorted[_vbi + 1] if _vbi + 1 < len(_vsorted) else len(self._vehicle_data)
                    _ve = _vparse(bytes(self._vehicle_data), _vo, _vend)
                    if _ve:
                        _ventries.append(_ve)
                self._vehicle_entries = _ventries
                self._vehicle_populate()
            except Exception:
                pass

        if 'regioninfo' in modified_tables and self._regioninfo_data:
            try:
                self._regioninfo_populate()
            except Exception:
                pass

        if 'fieldinfo' in modified_tables and self._field_edit_data:
            try:
                # Re-parse field entries from modified bytes
                from fieldinfo_parser import parse_pabgh_index, parse_entry as _fparse
                _fidx = parse_pabgh_index(getattr(self, '_field_edit_schema', None) or b'')
                _fentries = []
                for _fk, _fo in sorted(_fidx.items()):
                    _fe = _fparse(bytes(self._field_edit_data), _fo)
                    if _fe:
                        _fentries.append(_fe)
                if _fentries:
                    self._field_edit_entries = _fentries
            except Exception:
                pass

        import os as _os_ifj
        QMessageBox.information(self, tr("Import Field JSON v3"),
            f"Imported '{_os_ifj.path.basename(path)}':\n\n"
            f"  {total_applied} intent(s) applied\n"
            f"  {total_skipped} skipped\n\n"
            f"Modified: {', '.join(set(modified_tables))}\n\n"
            f"Use Export Field JSON v3 or")

    def _field_edit_export_field_json_v3(self) -> None:
        """Export all FieldEdit modifications as Format 3.1 multi-target field JSON.

        Uses dmm_parser struct-level diff for all tables — produces semantic intents
        (entry name + key + field name + new value) instead of raw byte offsets.
        Compatible with DMM 1.3.6b+ for all supported tables.
        """
        def _has_diff(cur, orig):
            return cur is not None and orig is not None and bytes(cur) != bytes(orig)

        # Bake any queued mesh swaps into characterinfo BEFORE diffing, so they
        # are captured in the export. Every other export path (_export_mod/
        # _export/_export_json) already does this; without it the main-tab
        # "Export Field JSON v3" button silently drops queued mesh swaps.
        self._field_edit_apply_mesh_swaps()

        _any_change = (
            self._field_edit_modified
            or bool(self._mesh_swap_queue)
            or getattr(self, '_regioninfo_dmm_dirty', False)
            or getattr(self, '_vehicleinfo_dmm_dirty', False)
            or getattr(self, '_charinfo_dmm_dirty', False)
            or _has_diff(self._charinfo_data, self._charinfo_original)
            or _has_diff(self._field_edit_data, self._field_edit_original)
            or _has_diff(self._vehicle_data, self._vehicle_original)
            or _has_diff(self._gptrigger_data, self._gptrigger_original)
            or _has_diff(self._regioninfo_data, self._regioninfo_original)
            or _has_diff(self._wantedinfo_data, self._wantedinfo_original)
        )
        if not _any_change:
            QMessageBox.information(self, tr("Export Field JSON v3"),
                tr("No modifications to export. Make some changes first."))
            return

        import dmm_parser as _dmp_efj

        def _struct_diff(current_recs, vanilla_recs):
            """Diff two dmm_parser record lists, return Format 3 intents.

            For nested dict fields (e.g. four_flags), emits sub-field intents
            using dot-path syntax (e.g. 'four_flags.flag_a') so DMM only sets
            the specific sub-field that changed rather than replacing the whole
            nested object.
            """
            van_by_key = {r['key']: r for r in vanilla_recs}
            intents = []
            for rec in current_recs:
                rkey = rec.get('key')
                rskey = rec.get('string_key', f'entry_{rkey}')
                van = van_by_key.get(rkey)
                if van is None:
                    continue
                for field in rec:
                    if field in ('key', 'string_key'):
                        continue
                    cur_val = rec[field]
                    van_val = van.get(field)
                    if cur_val == van_val:
                        continue
                    # For nested dicts, emit one intent per changed sub-field
                    # using dot-path syntax (e.g. 'four_flags.flag_a')
                    if isinstance(cur_val, dict) and isinstance(van_val, dict):
                        for sub_field, sub_val in cur_val.items():
                            if sub_val != van_val.get(sub_field):
                                intents.append({
                                    'entry': rskey,
                                    'key':   rkey,
                                    'field': f'{field}.{sub_field}',
                                    'op':    'set',
                                    'new':   sub_val,
                                })
                    else:
                        intents.append({
                            'entry': rskey,
                            'key':   rkey,
                            'field': field,
                            'op':    'set',
                            'new':   cur_val,
                        })
            return intents

        def _dmm_diff(table_name, cur_bytes, van_bytes, schema_bytes):
            """Parse both buffers via dmm_parser and struct-diff them."""
            try:
                cur_recs = list(_dmp_efj.parse_table(table_name, bytes(cur_bytes), schema_bytes))
                van_recs = list(_dmp_efj.parse_table(table_name, bytes(van_bytes), schema_bytes))
                return _struct_diff(cur_recs, van_recs)
            except Exception as _e:
                log.warning("FieldEdit v3 dmm_diff %s failed: %s", table_name, _e)
                return []

        targets = []

        # ── fieldinfo ──────────────────────────────────────────────────────
        if _has_diff(self._field_edit_data, self._field_edit_original):
            its = _dmm_diff('field_info', self._field_edit_data,
                            self._field_edit_original, self._field_edit_schema)
            if its:
                targets.append({'file': 'fieldinfo.pabgb', 'intents': its})

        # ── vehicleinfo ─────────────────────────────────────────────────────
        vi_cur = getattr(self, '_vehicleinfo_dmm_items', None)
        vi_van = getattr(self, '_vehicleinfo_dmm_vanilla', None)
        if vi_cur and vi_van:
            its = _struct_diff(vi_cur, vi_van)
            if its:
                targets.append({'file': 'vehicleinfo.pabgb', 'intents': its})
        elif _has_diff(self._vehicle_data, self._vehicle_original):
            its = _dmm_diff('vehicle_info', self._vehicle_data,
                            self._vehicle_original, self._vehicle_schema)
            if its:
                targets.append({'file': 'vehicleinfo.pabgb', 'intents': its})

        # ── gameplaytrigger ─────────────────────────────────────────────────
        if _has_diff(self._gptrigger_data, self._gptrigger_original):
            its = _dmm_diff('gameplay_trigger_info', self._gptrigger_data,
                            self._gptrigger_original, self._gptrigger_schema)
            if its:
                targets.append({'file': 'gameplaytrigger.pabgb', 'intents': its})

        # ── regioninfo ──────────────────────────────────────────────────────
        ri_cur = getattr(self, '_regioninfo_dmm_items', None)
        ri_van = getattr(self, '_regioninfo_dmm_vanilla', None)
        if ri_cur and ri_van:
            its = _struct_diff(ri_cur, ri_van)
            if its:
                targets.append({'file': 'regioninfo.pabgb', 'intents': its})
        elif _has_diff(self._regioninfo_data, self._regioninfo_original):
            its = _dmm_diff('region_info', self._regioninfo_data,
                            self._regioninfo_original, self._regioninfo_schema)
            if its:
                targets.append({'file': 'regioninfo.pabgb', 'intents': its})

        # ── characterinfo ───────────────────────────────────────────────────
        ci_cur = getattr(self, '_charinfo_dmm_items', None)
        ci_van = getattr(self, '_charinfo_dmm_vanilla', None)
        if ci_cur and ci_van:
            its = _struct_diff(ci_cur, ci_van)
            if its:
                targets.append({'file': 'characterinfo.pabgb', 'intents': its})
        elif _has_diff(self._charinfo_data, self._charinfo_original):
            its = _dmm_diff('character_info', self._charinfo_data,
                            self._charinfo_original, self._charinfo_schema)
            if its:
                targets.append({'file': 'characterinfo.pabgb', 'intents': its})

        # ── wantedinfo ──────────────────────────────────────────────────────
        if _has_diff(self._wantedinfo_data, self._wantedinfo_original):
            its = _dmm_diff('wanted_info', self._wantedinfo_data,
                            self._wantedinfo_original, self._wantedinfo_schema)
            if its:
                targets.append({'file': 'wantedinfo.pabgb', 'intents': its})

        if not targets:
            QMessageBox.information(self, tr("Export Field JSON v3"),
                tr("No field-level changes detected.\n\nMake changes then try again."))
            return

        total   = sum(len(t['intents']) for t in targets)
        summary = ', '.join(f"{len(t['intents'])} {t['file'].split('.')[0]}"
                            for t in targets)

        path, _ = QFileDialog.getSaveFileName(
            self, tr("Export Field JSON v3"), "FieldEdit.field.json",
            "Field JSON (*.field.json *.json);;All Files (*)")
        if not path:
            return

        doc = {
            'modinfo': {
                'title': 'FieldEdit Mod', 'version': '1.0',
                'author': 'CrimsonGameMods FieldEdit',
                'description': f'{total} field-level intent(s) across {len(targets)} target(s) — {summary}',
                'note': ('Format 3.1 multi-target field JSON. '
                         'Structured intents via dmm_parser — update-proof, '
                         'compatible with DMM 1.3.6b+.'),
            },
            'format': 3, 'format_minor': 1,
            'targets': targets,
        }

        import json as _json
        with open(path, 'w', encoding='utf-8') as f:
            _json.dump(doc, f, indent=2, ensure_ascii=False, default=str)

        self._field_edit_status.setText(f"Exported {total} intents to {os.path.basename(path)}")
        QMessageBox.information(self, tr("Export Field JSON v3"),
            f"Exported {total} structured field intent(s) across {len(targets)} target(s):\n"
            + "\n".join(f"  \u2022 {t['file']}: {len(t['intents'])} intent(s)" for t in targets)
            + f"\n\nFile: {path}")

    def _field_edit_export_mesh_json(self):
        queue = self._mesh_swap_queue or []
        if not queue:
            QMessageBox.information(
                self, tr("Export Mesh Swap as JSON Mod"),
                tr("Mesh swap queue is empty — open Mesh Swap and add some swaps first."))
            return

        if not self._charinfo_data or not self._charinfo_schema:
            game_path = self._config.get("game_install_path", "")
            if not game_path or not os.path.isdir(game_path):
                QMessageBox.warning(
                    self, tr("Game Path"),
                    tr("Game path not set — either click 'Load FieldInfo' first or "
                       "configure the game install path."))
                return
            try:
                import crimson_rs
                dp = "gamedata/binary__/client/bin"
                ci_body = crimson_rs.extract_file(game_path, "0008", dp, "characterinfo.pabgb")
                ci_gh = crimson_rs.extract_file(game_path, "0008", dp, "characterinfo.pabgh")
                ci_pabgb = bytes(ci_body)
                ci_pabgh = bytes(ci_gh)
            except Exception as e:
                log.exception("characterinfo extract for JMM export failed")
                QMessageBox.critical(self, tr("Extract Failed"), str(e))
                return
        else:
            ci_pabgb = bytes(self._charinfo_data)
            ci_pabgh = bytes(self._charinfo_schema)

        try:
            from characterinfo_full_parser import parse_all_entries, parse_pabgh_index
            idx = parse_pabgh_index(ci_pabgh)
            parsed = parse_all_entries(ci_pabgb, ci_pabgh)
        except Exception as e:
            log.exception("characterinfo parse for JMM export failed")
            QMessageBox.critical(self, tr("Parse Failed"), str(e))
            return

        by_key = {}
        for e in parsed:
            ek = e.get('entry_key')
            if ek is not None:
                by_key[int(ek)] = e

        changes = []
        skipped_missing = []
        skipped_no_appearance = []
        for sw in queue:
            try:
                tk = int(sw['tgt'])
                sk = int(sw['src'])
            except (KeyError, TypeError, ValueError):
                continue
            tgt = by_key.get(tk)
            src = by_key.get(sk)
            if tgt is None or src is None:
                skipped_missing.append((tk, sk))
                continue
            tgt_appear_off = tgt.get('_appearanceName_stream_offset')
            tgt_appear_key = tgt.get('_appearanceName_key')
            src_appear_key = src.get('_appearanceName_key')
            if (tgt_appear_off is None or tgt_appear_key is None
                    or src_appear_key is None):
                skipped_no_appearance.append((tk, sk))
                continue
            blob_start = idx.get(tk)
            if blob_start is None:
                skipped_missing.append((tk, sk))
                continue
            rel_offset = tgt_appear_off - blob_start
            entry_name = tgt.get('name') or f"char_{tk}"
            src_name = src.get('name') or f"char_{sk}"
            original_hex = struct.pack('<I', int(tgt_appear_key) & 0xFFFFFFFF).hex()
            patched_hex = struct.pack('<I', int(src_appear_key) & 0xFFFFFFFF).hex()
            changes.append({
                "entry": entry_name,
                "rel_offset": int(rel_offset),
                "original": original_hex,
                "patched": patched_hex,
                "label": (f"Mesh Swap: {entry_name} (target key {tk}) "
                          f"-> looks like {src_name} (source key {sk})"),
            })

        if not changes:
            msg = "No exportable swaps — every queued entry failed a pre-flight check."
            if skipped_missing:
                msg += f"\n\n{len(skipped_missing)} missing from characterinfo.pabgb."
            if skipped_no_appearance:
                msg += f"\n{len(skipped_no_appearance)} missing _appearanceName field."
            QMessageBox.warning(self, tr("Export Mesh Swap as JSON Mod"), msg)
            return

        title, ok = QInputDialog.getText(
            self, tr("Export Mesh Swap as JSON Mod"),
            tr("Mod name:"), text="Mesh Swap Pack")
        if not ok or not title.strip():
            return
        title = title.strip()

        desc_default = f"{len(changes)} character mesh swap(s) via _appearanceName patching."
        description, ok = QInputDialog.getText(
            self, tr("Export Mesh Swap as JSON Mod"),
            tr("Description (optional):"), text=desc_default)
        if not ok:
            return
        description = description.strip() or desc_default

        author, ok = QInputDialog.getText(
            self, tr("Export Mesh Swap as JSON Mod"),
            tr("Author (optional):"), text="CrimsonSaveEditor")
        if not ok:
            return
        author = author.strip() or "CrimsonSaveEditor"

        jmm_mod = {
            "modinfo": {
                "title": title,
                "version": "1.0",
                "description": description,
                "author": author,
            },
            "format": 2,
            "patches": [{
                "game_file": "gamedata/characterinfo.pabgb",
                "changes": changes,
            }],
        }

        exe_dir = os.path.dirname(os.path.abspath(sys.argv[0] or "."))
        default_dir = os.path.join(exe_dir, "packs")
        os.makedirs(default_dir, exist_ok=True)
        safe_name = "".join(c if (c.isalnum() or c in "-_ ") else "_" for c in title)
        default_path = os.path.join(default_dir, safe_name + ".json")

        path, _ = QFileDialog.getSaveFileName(
            self, tr("Export Mesh Swap as JSON Mod"),
            default_path, "JSON Files (*.json)")
        if not path:
            return

        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(jmm_mod, f, indent=2, ensure_ascii=False)
        except Exception as e:
            log.exception("JMM mesh swap export write failed")
            QMessageBox.critical(self, tr("Export Failed"), str(e))
            return

        warn_suffix = ""
        if skipped_missing or skipped_no_appearance:
            warn_suffix = (f"\n\n{len(skipped_missing)} skipped (not in characterinfo), "
                           f"{len(skipped_no_appearance)} skipped (no _appearanceName).")
        self._field_edit_status.setText(
            f"Exported {len(changes)} mesh swap(s) to {os.path.basename(path)}")
        QMessageBox.information(
            self, tr("Exported"),
            f"Wrote {len(changes)} mesh swap patch(es) to:\n{path}\n\n"
            f"Drop this .json file into JMM's mods/ folder to install."
            f"{warn_suffix}")

    def _field_edit_restore(self):
        game_path = self._config.get("game_install_path", "")
        if not game_path or not os.path.isdir(game_path):
            QMessageBox.warning(self, tr("Game Path"), tr("Game path not set."))
            return
        mod_group = f"{self._fieldedit_overlay_spin.value():04d}"
        mount_group = self._MOUNT_OVERLAY_GROUP
        game_mod = os.path.join(game_path, mod_group)
        mount_mod = os.path.join(game_path, mount_group)
        has_field = os.path.isdir(game_mod)
        has_mount = os.path.isdir(mount_mod)
        if not has_field and not has_mount:
            # No disk overlay — offer to reset in-memory buffers to vanilla
            has_memory = any([
                self._field_edit_data and self._field_edit_original and
                    bytes(self._field_edit_data) != self._field_edit_original,
                self._vehicle_data and self._vehicle_original and
                    bytes(self._vehicle_data) != self._vehicle_original,
                self._gptrigger_data and self._gptrigger_original and
                    bytes(self._gptrigger_data) != self._gptrigger_original,
                self._regioninfo_data and self._regioninfo_original and
                    bytes(self._regioninfo_data) != self._regioninfo_original,
                self._charinfo_data and self._charinfo_original and
                    bytes(self._charinfo_data) != self._charinfo_original,
                self._wantedinfo_data and self._wantedinfo_original and
                    bytes(self._wantedinfo_data) != self._wantedinfo_original,
            ])
            if has_memory:
                reply = QMessageBox.question(
                    self, tr("Restore"),
                    "No deployed overlay found on disk.\n\n"
                    "You have unsaved in-memory changes (e.g. from Import Field JSON).\n"
                    "Reset all in-memory edits back to vanilla?",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
                if reply == QMessageBox.Yes:
                    # Reset all buffers to their original snapshots
                    if self._field_edit_original:
                        self._field_edit_data = bytearray(self._field_edit_original)
                    if self._vehicle_original:
                        self._vehicle_data = bytearray(self._vehicle_original)
                    if self._gptrigger_original:
                        self._gptrigger_data = bytearray(self._gptrigger_original)
                    if self._regioninfo_original:
                        self._regioninfo_data = bytearray(self._regioninfo_original)
                    if self._charinfo_original:
                        self._charinfo_data = bytearray(self._charinfo_original)
                    if self._wantedinfo_original:
                        self._wantedinfo_data = bytearray(self._wantedinfo_original)
                    self._field_edit_modified = False
                    # Reload UI tables
                    # Re-parse entries from restored buffers before refreshing UI
                    if self._vehicle_data and self._vehicle_schema:
                        try:
                            from vehicleinfo_parser import parse_pabgh_index_u16, parse_entry as _vpr
                            _vi = parse_pabgh_index_u16(self._vehicle_schema)
                            _vs = sorted(set(_vi.values()))
                            _ves = []
                            for _vk, _vo in sorted(_vi.items()):
                                _vbi = _vs.index(_vo)
                                _vend = _vs[_vbi+1] if _vbi+1 < len(_vs) else len(self._vehicle_data)
                                _ve = _vpr(bytes(self._vehicle_data), _vo, _vend)
                                if _ve: _ves.append(_ve)
                            self._vehicle_entries = _ves
                        except Exception: pass
                    for fn in ('_vehicle_populate', '_regioninfo_populate'):
                        if hasattr(self, fn):
                            try: getattr(self, fn)()
                            except Exception: pass
                    self._field_edit_status.setText(tr("Reset to vanilla (in-memory only)"))
                    QMessageBox.information(self, tr("Restore"),
                        "In-memory edits reset to vanilla.\n\n"
                        "No game files were modified — nothing to deploy.")
            else:
                QMessageBox.information(self, tr("Restore"),
                    f"No {mod_group}/ or {mount_group}/ overlay found on disk "
                    "and no in-memory changes detected.")
            return
        parts = []
        if has_field:
            parts.append(f"{mod_group}/ (field edits + mesh swap)")
        if has_mount:
            parts.append(f"{mount_group}/ (skeleton + scale overlays)")
        reply = QMessageBox.question(
            self, tr("Restore Vanilla FieldInfo"),
            f"Remove overlays and restore vanilla?\n\n" + "\n".join(parts),
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply != QMessageBox.Yes:
            return
        try:
            msgs = []
            if has_field:
                if self._rebuild_papgt_fn:
                    msgs.append(self._rebuild_papgt_fn(game_path, mod_group))
                shutil.rmtree(game_mod)
            if has_mount:
                if self._rebuild_papgt_fn:
                    msgs.append(self._rebuild_papgt_fn(game_path, mount_group))
                shutil.rmtree(mount_mod)
            self._field_edit_status.setText(tr("Restored vanilla fieldinfo"))
            QMessageBox.information(self, tr("Restored"),
                f"Removed overlays.\n" + "\n".join(msgs) + "\n"
                f"Restart the game for changes to take effect.")
        except Exception as e:
            QMessageBox.critical(self, tr("Restore Failed"), str(e))
