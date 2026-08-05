"""CrimsonGameMods Simple — lightweight one-click mod launcher."""
from __future__ import annotations

import copy
import json
import logging
import os
import shutil
import string
import struct
import subprocess
import sys
import tempfile
from typing import Optional

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QFont, QColor
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFileDialog, QMessageBox, QFrame, QScrollArea,
)

log = logging.getLogger(__name__)

APP_VERSION = "1.1.9"

# ─── Palette ──────────────────────────────────────────────────────────
BG        = "#1a1510"
PANEL     = "#272018"
HEADER    = "#3d2e1a"
ACCENT    = "#daa850"
TEXT      = "#f0e6d4"
TEXT_DIM  = "#b0a088"
BORDER    = "#554430"
INPUT_BG  = "#1e1610"
SUCCESS   = "#9cc470"
ERROR     = "#d44f40"
CARD_BG   = "#221c14"
CARD_HOVER = "#2e2518"
ON_COLOR  = "#9cc470"
OFF_COLOR = "#665840"
RESET_CLR = "#d44f40"
COMBO_BG  = "#2a2218"
SECTION_CLR = "#886830"

STYLESHEET = f"""
QWidget {{ background-color: {BG}; color: {TEXT};
    font-family: 'Segoe UI', Consolas, sans-serif; font-size: 13px; }}
QScrollArea {{ border: none; }}
"""

VANILLA_GROUPS = set(f"{g:04d}" for g in range(36))
INTERNAL = "gamedata/binary__/client/bin"

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(sys.executable if getattr(sys, 'frozen', False) else __file__)),
    "simple_launcher_config.json")


# ─── Mod Definitions ─────────────────────────────────────────────────
# Combo members
QOL_MEMBERS = {"no_cooldown", "max_charges", "max_stacks", "inf_durability"}
EVERYTHING_MEMBERS = QOL_MEMBERS | {
    "make_dyeable", "five_sockets", "unlock_abyss", "universal_prof"}

# All mods that write to the iteminfo overlay (0058+0066). universal_prof
# additionally drives the 0059 equipslotinfo overlay (weapon component) via
# _rebuild_equipslotinfo().
ITEMINFO_MODS = {
    "no_cooldown", "max_charges", "max_stacks", "inf_durability",
    "make_dyeable", "five_sockets", "unlock_abyss", "universal_prof"}

# Mods that write to the store overlay (0060)
STORE_MODS = {"all_items_shop"}

# All-Items Shop: the Hernandel grocer (Delkin) sells every SAFE item in the game.
# npcinfo + storeinfo only (no characterinfo/scene) so it stacks with every other
# mod. A fresh dedicated store (nobody else references it) keeps real vendors intact.
AIS_NPCINFO_GROUP = "0067"          # own group — no other mod touches npcinfo
AIS_DELKIN_NPCINFO = 4294967219     # Delkin(451)'s npc_info key (used only by him)
# Fill Store_Dev (999) IN PLACE using its OWN stock entry as the template — this is
# what the proven build does. Each stock entry carries lookup_a = its store key; if
# you clone another store's entry the UI mis-resolves items (repeated icons/names).
# 999 is dev-only (no real vendor references it), so filling it has no collateral.
AIS_STORE_KEY = 999
AIS_STOCK_QTY = 999                 # per-item purchase count

# Mutual exclusion pairs
MUTEX_PAIRS = [("drop_5x", "drop_max"), ("bagspace_240", "bagspace_700")]

ALL_MODS_MEMBERS = EVERYTHING_MEMBERS | {
    "all_items_shop",
    "drop_max", "merc_max", "speed_3x", "hard_2x_hp",
    "quest_unlock", "bagspace_700"}

COMBO_DEFS = {
    "enable_all_mods": ALL_MODS_MEMBERS,
    "enable_everything": EVERYTHING_MEMBERS,
    "enable_all_qol": QOL_MEMBERS,
}

MOD_DEFS = [
    # ── All-Items Shop (headline) ──
    {"id": "all_items_shop", "title": "All-Items Grocer Shop",
     "desc": "The Hernandel grocer sells every buyable item in the game "
             "(~6,455, crash-unsafe items filtered out). No other vendor is changed.",
     "section": "All-Items Shop"},
    # ── Combo Badges ──
    {"id": "enable_everything", "title": "Enable Everything",
     "desc": "QoL + Dyeable + 5 Sockets + Abyss Unlock + Universal Proficiency",
     "section": "Combos", "combo": True},
    {"id": "enable_all_qol", "title": "Enable All QoL",
     "desc": "Max Stacks + Max Charges + Inf Durability + No Cooldown",
     "section": "Combos", "combo": True},
    # ── Item Mods ──
    {"id": "no_cooldown", "title": "No Cooldown",
     "desc": "Item use cooldown to 1s on every item", "section": "Item Mods"},
    {"id": "max_charges", "title": "Max Charges",
     "desc": "Max charges to 99 on all charged items", "section": "Item Mods"},
    {"id": "max_stacks", "title": "Max Stacks",
     "desc": "Max stack count to 999,999", "section": "Item Mods"},
    {"id": "inf_durability", "title": "Infinity Durability",
     "desc": "Max endurance to 65,535 — items never break", "section": "Item Mods"},
    {"id": "make_dyeable", "title": "Make All Dyeable",
     "desc": "Every equipment piece becomes dyeable", "section": "Item Mods"},
    {"id": "five_sockets", "title": "All Items 5 Sockets",
     "desc": "Extend/force-enable 5 gem sockets on everything", "section": "Item Mods"},
    {"id": "unlock_abyss", "title": "Unlock Abyss Gear",
     "desc": "Remove abyss gate restriction (equipable_hash=0)", "section": "Item Mods"},
    {"id": "universal_prof", "title": "Universal Proficiency v3",
     "desc": "All outfits + weapons usable by Kliff / Damiane / Oongka", "section": "Item Mods"},
    # ── Skills ──
    {"id": "infinite_stamina", "title": "Infinite Stamina",
     "desc": "Zero all skill stamina/spirit resource costs", "section": "Skills"},
    # ── World / Field Mods ──
    # kliff_gun_fix removed — Kliff uses gun natively in 1.10+
    # ── Drop Mods ──
    {"id": "drop_5x", "title": "5x Drop Rates",
     "desc": "Multiply all drop rates by 5 (capped at 100%)", "section": "Drops"},
    {"id": "drop_max", "title": "Max Drop Rates",
     "desc": "Set all drop rates to 100%", "section": "Drops"},
    # ── Other ──
    {"id": "merc_max", "title": "Max Merc/Pet Cap",
     "desc": "Unlimited mercenary and pet deployment", "section": "Other"},
    # ── Player Speed ──
    {"id": "speed_3x", "title": "3x Player Speed",
     "desc": "Triple attack speed and move speed (stat_level_data max)", "section": "Player Stats"},
    # ── Difficulty ──
    {"id": "hard_2x_hp", "title": "Hard Mode x2 Enemy HP",
     "desc": "Double enemy HP in Difficulty/Boss buff levels", "section": "Difficulty"},
    {"id": "quest_unlock", "title": "Unlock All Quest Characters",
     "desc": "Remove character restrictions on all quests / missions / stages", "section": "Other"},
    {"id": "bagspace_240", "title": "Bag Space 240 / 700",
     "desc": "Character inventory=240, warehouse=700", "section": "Bag Space"},
    {"id": "bagspace_700", "title": "Bag Space 700 / 700",
     "desc": "Character inventory=700, warehouse=700", "section": "Bag Space"},
]


# ─── Utilities ────────────────────────────────────────────────────────
def find_game_path() -> str:
    candidates = []
    for letter in string.ascii_uppercase:
        candidates.append(f"{letter}:\\SteamLibrary\\steamapps\\common\\Crimson Desert")
    candidates.extend([
        r"C:\Program Files (x86)\Steam\steamapps\common\Crimson Desert",
        r"C:\Program Files\Steam\steamapps\common\Crimson Desert",
        r"C:\Program Files\Epic Games\CrimsonDesert",
    ])
    for p in candidates:
        if os.path.isfile(os.path.join(p, "0008", "0.paz")):
            return p
    return ""


def is_game_running() -> bool:
    try:
        out = subprocess.check_output(
            ["tasklist", "/FI", "IMAGENAME eq CrimsonDesert.exe", "/FO", "CSV", "/NH"],
            stderr=subprocess.DEVNULL, text=True, timeout=3,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return "CrimsonDesert.exe" in out
    except Exception:
        return False


def can_write_game_dir(gp: str) -> bool:
    try:
        t = os.path.join(gp, ".se_write_test")
        with open(t, "w") as f:
            f.write("t")
        os.remove(t)
        return True
    except Exception:
        return False


def _safe_iv(v, default=0):
    if v is None:
        return default
    if isinstance(v, (int, float, bool)):
        return int(v)
    if isinstance(v, dict):
        for k in ('a', 'value', '_v', 'v', 'val', 'n', 'data'):
            if k in v:
                return int(v[k])
        vals = [x for x in v.values() if isinstance(x, (int, float))]
        return int(vals[0]) if vals else default
    return default


def load_config() -> dict:
    try:
        with open(CONFIG_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg: dict):
    try:
        with open(CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass


def _ensure_papgt_backup(gp: str):
    papgt = os.path.join(gp, "meta", "0.papgt")
    bak = papgt + ".vanilla"
    if not os.path.isfile(bak) and os.path.isfile(papgt):
        shutil.copy2(papgt, bak)


def _papgt_sync(gp: str):
    """Rebuild PAPGT to match what's on disk.

    Uses the .vanilla or .backup PAPGT as a clean base (has correct vanilla
    entries with proper string table offsets), then adds mod overlays via
    add_papgt_entry which handles the binary format correctly.

    Finally re-syncs all checksums from the actual PAMT files on disk.
    """
    import crimson_rs
    papgt_path = os.path.join(gp, "meta", "0.papgt")

    # Start from cleanest available base
    base_path = papgt_path + ".vanilla"
    if not os.path.isfile(base_path):
        base_path = papgt_path + ".backup"
    if not os.path.isfile(base_path):
        base_path = papgt_path
    papgt = crimson_rs.parse_papgt_file(base_path)

    # Remove any non-vanilla entries from the base (it may be stale)
    papgt["entries"] = [
        e for e in papgt["entries"]
        if e.get("group_name", "").isdigit() and int(e["group_name"]) < 36
    ]

    # Discover mod overlay folders on disk and add them
    for d in sorted(os.listdir(gp)):
        full = os.path.join(gp, d)
        pamt_file = os.path.join(full, "0.pamt")
        if not os.path.isdir(full) or not os.path.isfile(pamt_file):
            continue
        if not d.isdigit() or int(d) < 36:
            continue
        try:
            ck = crimson_rs.parse_pamt_file(pamt_file)["checksum"]
            papgt = crimson_rs.add_papgt_entry(papgt, d, ck, 0, 0x3FFF)
        except Exception:
            pass

    # Re-sync ALL checksums (vanilla + mod) from actual PAMT files
    for e in papgt["entries"]:
        gn = e["group_name"]
        pamt_file = os.path.join(gp, gn, "0.pamt")
        if os.path.isfile(pamt_file):
            try:
                e["pack_meta_checksum"] = crimson_rs.parse_pamt_file(pamt_file)["checksum"]
            except Exception:
                pass

    crimson_rs.write_papgt_file(papgt, papgt_path)


def _deploy_overlay(gp: str, group: str, files: list[tuple[str, bytes]],
                    compression=None):
    import crimson_rs
    _ensure_papgt_backup(gp)
    comp = compression or crimson_rs.Compression.NONE
    with tempfile.TemporaryDirectory() as tmp:
        gdir = os.path.join(tmp, group)
        b = crimson_rs.PackGroupBuilder(gdir, comp, crimson_rs.Crypto.NONE)
        for fname, data in files:
            b.add_file(INTERNAL, fname, data)
        pamt = bytes(b.finish())
        ck = crimson_rs.parse_pamt_bytes(pamt)["checksum"]

        dst = os.path.join(gp, group)
        if os.path.isdir(dst):
            shutil.rmtree(dst)
        os.makedirs(dst, exist_ok=True)
        for f in os.listdir(gdir):
            shutil.copy2(os.path.join(gdir, f), os.path.join(dst, f))

    _papgt_sync(gp)


def _remove_overlay(gp: str, group: str):
    d = os.path.join(gp, group)
    if os.path.isdir(d):
        shutil.rmtree(d)
    _papgt_sync(gp)
    try:
        from shared_state import remove_overlay
        remove_overlay(gp, group)
    except Exception:
        pass


def _parse_table(name, gp, filename):
    import crimson_rs
    try:
        import dmm_parser
        pt, st = dmm_parser.parse_table, dmm_parser.serialize_table
    except ImportError:
        pt, st = crimson_rs.parse_table, crimson_rs.serialize_table
    b = bytes(crimson_rs.extract_file(gp, '0008', INTERNAL, f'{filename}.pabgb'))
    h = bytes(crimson_rs.extract_file(gp, '0008', INTERNAL, f'{filename}.pabgh'))
    items = pt(name, b, h)
    return items, h, st


def _ais_shop_unsafe(it) -> bool:
    """Item that crashes the Buy panel: no price, no icon, or a currency (Money_*)."""
    icons = it.get("item_icon_list") or []
    no_icon = (not icons) or all(not e.get("icon_path") for e in icons)
    no_price = not (it.get("price_list") or [])
    currency = it.get("money_type_define") not in (None, 0)
    return no_icon or no_price or currency


# ── item-category buckets (from iteminfo.category_info — the game's own taxonomy)
# Lets the user stock only a few categories instead of everything. First matching
# predicate wins; "Other" is the catch-all. Mirrors custom_npc/store_stock_builder.
_AIS_CATEGORY_BUCKETS = [
    ("Weapons",              lambda ci: 100 <= ci < 300),
    ("Armor",                lambda ci: 400 <= ci < 500),
    ("Accessories",          lambda ci: 500 <= ci < 600),
    ("Ammo",                 lambda ci: 2400 <= ci < 2500),
    ("Gems / Enhancement",   lambda ci: 2500 <= ci < 2600),
    ("Tools / Throwables",   lambda ci: 3000 <= ci < 3100),
    ("Quest",                lambda ci: 4000 <= ci < 4100),
    ("Materials / Gathering",
        lambda ci: ci == 3201 or 3300 <= ci < 4000 or 7000 <= ci < 7100 or 9000 <= ci < 9100),
    ("Food / Consumables",
        lambda ci: 3100 <= ci < 3200 or 10000 <= ci < 13000 or 28000 <= ci < 29000),
]
AIS_CATEGORY_NAMES = [n for n, _ in _AIS_CATEGORY_BUCKETS] + ["Other"]


def _ais_bucket_for(category_info) -> str:
    ci = category_info if isinstance(category_info, int) else -1
    for name, pred in _AIS_CATEGORY_BUCKETS:
        if pred(ci):
            return name
    return "Other"


def _ais_parse_items(gp: str) -> list:
    import crimson_rs
    return crimson_rs.parse_table(
        "iteminfo",
        bytes(crimson_rs.extract_file(gp, '0008', INTERNAL, 'iteminfo.pabgb')),
        bytes(crimson_rs.extract_file(gp, '0008', INTERNAL, 'iteminfo.pabgh')))


def _ais_safe_item_keys(gp: str, categories=None) -> list:
    """Buyable item keys minus the crash-unsafe ones (see _ais_shop_unsafe).
    If `categories` is given (a set/list of AIS_CATEGORY_NAMES), keep only items in
    those buckets; None/empty-falsey means ALL categories."""
    wanted = set(categories) if categories else None
    out = []
    for it in _ais_parse_items(gp):
        if _ais_shop_unsafe(it):
            continue
        if wanted is not None and _ais_bucket_for(it.get("category_info")) not in wanted:
            continue
        out.append(it["key"])
    return out


def _ais_category_counts(gp: str) -> dict:
    """{bucket: safe-item-count} for the picker's live totals."""
    counts = {n: 0 for n in AIS_CATEGORY_NAMES}
    for it in _ais_parse_items(gp):
        if _ais_shop_unsafe(it):
            continue
        counts[_ais_bucket_for(it.get("category_info"))] += 1
    return counts


def _ais_stock_entry(template: dict, item_key: int, index: int, qty: int) -> dict:
    """One stock_data_list entry for item_key, cloned from a real template entry."""
    e = copy.deepcopy(template)
    e["value"]["payload"]["body"] = item_key
    e["value"]["raw_q"] = item_key
    e["raw_d"] = index
    e["raw_e"] = index
    e["raw_c"] = qty
    e["sub_data"] = None
    e["effect_list"] = []
    return e


# ─── Worker ───────────────────────────────────────────────────────────
class ModWorker(QThread):
    progress = Signal(str)
    finished = Signal(bool, str)

    def __init__(self, game_path: str, mod_id: str, active_mods: set):
        super().__init__()
        self.gp = game_path
        self.mod_id = mod_id
        self.active = set(active_mods)

    def run(self):
        try:
            mid = self.mod_id
            is_all = mid == "enable_all_mods"
            # Determine which rebuilds are needed
            if is_all or mid in ITEMINFO_MODS or mid in COMBO_DEFS:
                self._rebuild_iteminfo()
                self._rebuild_equipslotinfo()
            if is_all or mid in ("drop_5x", "drop_max"):
                self._rebuild_drops()
            if is_all or mid == "merc_max":
                self._handle_merc()
            if is_all or mid == "speed_3x":
                self._handle_speed()
            if is_all or mid == "hard_2x_hp":
                self._handle_difficulty()
            if is_all or mid == "infinite_stamina":
                self._handle_stamina()
            if is_all or mid in STORE_MODS:
                self._rebuild_stores()
            if is_all or mid == "all_items_shop":
                self._rebuild_all_items_npcinfo()
            if is_all or mid == "quest_unlock":
                self._handle_quest()
            if is_all or mid in ("bagspace_240", "bagspace_700"):
                self._handle_bagspace()
            if mid == "reset_vanilla":
                self._reset_vanilla()
            self.finished.emit(True, "")
        except Exception as e:
            log.exception("ModWorker failed: %s", self.mod_id)
            self.finished.emit(False, str(e))

    # ── Iteminfo Group (0058 + 0066 + 0059) ──
    def _rebuild_iteminfo(self):
        active_ii = self.active & ITEMINFO_MODS
        if not active_ii:
            self.progress.emit("Removing iteminfo overlays...")
            for g in ("0058", "0066", "0059"):
                _remove_overlay(self.gp, g)
            return

        import crimson_rs

        self.progress.emit("Extracting iteminfo...")
        raw = bytes(crimson_rs.extract_file(self.gp, '0008', INTERNAL, 'iteminfo.pabgb'))
        import dmm_parser
        items = dmm_parser.parse_iteminfo_from_bytes(raw)
        unparsed = []

        self.progress.emit(f"Applying {len(active_ii)} item mutations to {len(items)} items...")
        COSTS = [500, 1000, 2000, 3000, 4000, 5000, 6000, 7000]
        for it in items:
            if "no_cooldown" in active_ii:
                # 1.12: cooltime is a 3xi64 struct {a,b,c}, not a scalar. The 3
                # values are the cooldown + 2 post fields (was unk_post_cooltime_a/b).
                ct = it.get('cooltime')
                if isinstance(ct, dict) and any(_safe_iv(ct.get(k, 0)) > 1 for k in ('a', 'b', 'c')):
                    it['cooltime'] = {'a': 1, 'b': 1, 'c': 1}
                elif _safe_iv(ct) > 1:  # pre-1.12 scalar fallback
                    it['cooltime'] = 1
                    it['unk_post_cooltime_a'] = 1
                    it['unk_post_cooltime_b'] = 1
            if "max_charges" in active_ii:
                # 1.12: max_charged_useable_count is also a {a,b,c} struct.
                mc = it.get('max_charged_useable_count')
                if isinstance(mc, dict):
                    if _safe_iv(it.get('item_charge_type', 0)) == 0 and \
                            any(_safe_iv(mc.get(k, 0)) > 0 for k in ('a', 'b', 'c')):
                        it['max_charged_useable_count'] = {'a': 99, 'b': 99, 'c': 99}
                elif _safe_iv(it.get('item_charge_type', 0)) == 0 and _safe_iv(mc) > 0:
                    it['max_charged_useable_count'] = 99
                    it['unk_post_max_charged_a'] = 99
                    it['unk_post_max_charged_b'] = 99
            if "max_stacks" in active_ii and _safe_iv(it.get('max_stack_count', 0)) > 1:
                it['max_stack_count'] = 999999
            if "inf_durability" in active_ii and _safe_iv(it.get('max_endurance', 0)) > 0:
                it['max_endurance'] = 65535
                it['is_destroy_when_broken'] = 0
            if "make_dyeable" in active_ii:
                if _safe_iv(it.get('equip_type_info', 0)) and not _safe_iv(it.get('is_dyeable', 0)):
                    it['is_dyeable'] = 1
                    it['is_editable_grime'] = 1
            if "unlock_abyss" in active_ii and _safe_iv(it.get('equipable_hash', 0)) != 0:
                it['equipable_hash'] = 0
            if "universal_prof" in active_ii and it.get('equip_type_info'):
                for pd in (it.get('prefab_data_list') or []):
                    if pd.get('tribe_gender_list'):
                        pd['tribe_gender_list'] = []
            if "five_sockets" in active_ii:
                ddd = it.get('drop_default_data')
                if ddd:
                    cur = ddd.get('add_socket_material_item_list') or []
                    if ddd.get('use_socket', 0) and cur and len(cur) < 5:
                        while len(cur) < 5:
                            cur.append({'item': 1, 'value': COSTS[len(cur)] if len(cur) < len(COSTS) else 5000})
                        ddd['add_socket_material_item_list'] = cur
                        ddd['socket_valid_count'] = 5
                    elif it.get('equip_type_info'):
                        ddd['use_socket'] = 1
                        ddd['add_socket_material_item_list'] = [
                            {'item': 1, 'value': COSTS[i] if i < len(COSTS) else 5000} for i in range(5)]
                        ddd['socket_valid_count'] = 5

        self.progress.emit("Rebuilding iteminfo in vanilla order...")
        van_raw = bytes(crimson_rs.extract_file(self.gp, '0008', INTERNAL, 'iteminfo.pabgb'))
        van_gh = bytes(crimson_rs.extract_file(self.gp, '0008', INTERNAL, 'iteminfo.pabgh'))
        gh_count = struct.unpack_from('<H', van_gh, 0)[0]
        gh_rs = (len(van_gh) - 2) // gh_count if gh_count else 8

        order = []
        for i in range(gh_count):
            rec = 2 + i * gh_rs
            if rec + gh_rs > len(van_gh):
                break
            off = struct.unpack_from('<I', van_gh, rec + (gh_rs - 4))[0]
            nxt = len(van_raw)
            if i + 1 < gh_count:
                nrec = 2 + (i + 1) * gh_rs
                if nrec + gh_rs <= len(van_gh):
                    nxt = struct.unpack_from('<I', van_gh, nrec + (gh_rs - 4))[0]
            if off + 4 <= len(van_raw):
                key = struct.unpack_from('<I', van_raw, off)[0]
                order.append((key, off, nxt - off))

        parsed_ser = {}
        for it in items:
            parsed_ser[int(it['key'])] = dmm_parser.serialize_iteminfo([it])
        unparsed_map = {}
        for raw_chunk in unparsed:
            if len(raw_chunk) >= 4:
                uk = struct.unpack_from('<I', raw_chunk, 0)[0]
                unparsed_map[uk] = raw_chunk

        final = bytearray()
        new_entries = []
        for key, voff, vsize in order:
            new_entries.append((key, len(final)))
            if key in parsed_ser:
                final.extend(parsed_ser[key])
            elif key in unparsed_map:
                final.extend(unparsed_map[key])
            else:
                final.extend(van_raw[voff:voff + vsize])

        pabgh_out = bytearray(struct.pack('<H', len(new_entries)))
        for key, off in new_entries:
            pabgh_out.extend(struct.pack('<II', key, off))
        pabgh_out = bytes(pabgh_out)

        self.progress.emit("Deploying iteminfo overlays...")
        _ensure_papgt_backup(self.gp)
        papgt_path = os.path.join(self.gp, "meta", "0.papgt")
        groups = {
            "0058": (crimson_rs.Compression.LZ4, [("iteminfo.pabgb", bytes(final))]),
            "0066": (crimson_rs.Compression.NONE, [("iteminfo.pabgh", pabgh_out)]),
        }

        # equipslotinfo (the Universal Proficiency *weapon* component) is
        # handled separately in _rebuild_equipslotinfo(), which owns the 0059
        # overlay. The outfit component (clearing tribe_gender_list above)
        # stays in this iteminfo overlay.

        with tempfile.TemporaryDirectory() as tmp:
            for grp, (comp, files) in groups.items():
                gdir = os.path.join(tmp, grp)
                b = crimson_rs.PackGroupBuilder(gdir, comp, crimson_rs.Crypto.NONE)
                for fn, data in files:
                    b.add_file(INTERNAL, fn, data)
                bytes(b.finish())
                dst = os.path.join(self.gp, grp)
                if os.path.isdir(dst):
                    shutil.rmtree(dst)
                os.makedirs(dst, exist_ok=True)
                for f in os.listdir(gdir):
                    shutil.copy2(os.path.join(gdir, f), os.path.join(dst, f))
            _papgt_sync(self.gp)
            with open(os.path.join(self.gp, "0058", ".se_itembuffs"), "w") as f:
                f.write("CrimsonGameMods Simple\n")

    # ── Equipslotinfo Group (0059) — Universal Proficiency weapon component ──
    def _rebuild_equipslotinfo(self):
        """Expand equip slots on the 3 player characters (Kliff=1, Damiane=4,
        Oongka=6) so weapons/armor become cross-equippable.

        1.12 note: equipslotinfo IS editable via crimson_rs.parse_table(
        'equipslotinfo', ...) + serialize_table('equip_slot_info', ...). The
        earlier assumption that the 1.12 table was unparseable (and the
        Python equipslotinfo_parser, which still expects the 1.10 shape) is
        obsolete — that path is what made Universal Proficiency outfits-only.
        """
        if "universal_prof" not in self.active:
            _remove_overlay(self.gp, "0059")
            return

        import crimson_rs
        self.progress.emit("Expanding equip slots (weapons)...")

        es_pabgb = bytes(crimson_rs.extract_file(
            self.gp, '0008', INTERNAL, 'equipslotinfo.pabgb'))
        es_pabgh = bytes(crimson_rs.extract_file(
            self.gp, '0008', INTERNAL, 'equipslotinfo.pabgh'))

        table = crimson_rs.parse_table('equipslotinfo', es_pabgb, es_pabgh)
        es_records = sorted(table, key=lambda e: e['key'])

        PLAYER_KEYS = {1, 4, 6}  # Kliff, Damiane, Oongka
        player_records = [r for r in es_records if r['key'] in PLAYER_KEYS]
        # 1.12+ removed category_a/category_b from EquipInfoData. The 3 player
        # records are structurally identical (22 entries, matching slot_index
        # 0..21), differing only in each entry's etl_hashes — so slot_index is
        # the correct grouping key: union the accepted equip-type hashes for
        # each slot across all 3 chars, then grant every char the full union.
        # (name_hash is NOT usable here — it repeats across slots 0/13/14, which
        # hold very different etl sets, so grouping by it would over-permit.)
        category_hashes: dict[int, set[int]] = {}
        for rec in player_records:
            for e in rec['entries']:
                k = e['slot_index']
                category_hashes.setdefault(k, set()).update(e['etl_hashes'])

        added = 0
        for rec in es_records:
            if rec['key'] not in PLAYER_KEYS:
                continue
            for e in rec['entries']:
                k = e['slot_index']
                to_add = sorted(category_hashes.get(k, set()) - set(e['etl_hashes']))
                if to_add:
                    e['etl_hashes'].extend(to_add)
                    added += len(to_add)

        # serialize_table needs the canonical name 'equip_slot_info' and the
        # original pabgh so offsets regenerate after etl_hashes growth.
        # Returns (pabgb, pabgh) — NOT the reverse.
        new_pabgb, new_pabgh = crimson_rs.serialize_table(
            'equip_slot_info', es_records, None, es_pabgh)

        self.progress.emit(f"Deploying equipslotinfo overlay (+{added} slot hashes)...")
        _deploy_overlay(self.gp, "0059", [
            ("equipslotinfo.pabgb", bytes(new_pabgb)),
            ("equipslotinfo.pabgh", bytes(new_pabgh))])
        log.info("equipslotinfo (UP weapons): +%d slot hashes deployed to 0059", added)

    # ── Characterinfo Group (0065) ──
    # ── Drop Rates (0042) ──
    def _rebuild_drops(self):
        active_drops = self.active & {"drop_5x", "drop_max"}
        if not active_drops:
            _remove_overlay(self.gp, "0042")
            return

        self.progress.emit("Patching drop rates...")
        items, pabgh, serialize = _parse_table('drop_set_info', self.gp, 'dropsetinfo')
        multiplier = 0 if "drop_max" in active_drops else 5
        for d in items:
            for item in d.get('list', []):
                old = item.get('raw_16', 0)
                if 100 <= old <= 100000 and old % 100 == 0:
                    new_val = 10000 if multiplier == 0 else min(old * multiplier, 10000)
                    if new_val != old:
                        item['raw_16'] = new_val
        new_pabgb = bytes(serialize('drop_set_info', items))
        _deploy_overlay(self.gp, "0042", [
            ("dropsetinfo.pabgb", new_pabgb),
            ("dropsetinfo.pabgh", pabgh)])

    # ── Merc/Pet Max (0090) ──
    def _handle_merc(self):
        if "merc_max" not in self.active:
            _remove_overlay(self.gp, "0090")
            return

        self.progress.emit("Patching merc/pet caps...")
        items, pabgh, serialize = _parse_table('mercenary_info', self.gp, 'mercenaryinfo')
        for rec in items:
            sc = rec.get('default_summon_count', 0)
            if sc and sc > 0 and sc != -1:
                rec['default_summon_count'] = max(9999, sc * 10)
            hc = rec.get('default_hire_count', 0)
            if hc and hc > 0 and hc != -1:
                rec['default_hire_count'] = max(99999, hc * 100)
            rec['max_hire_count'] = -1
        new_pabgb = bytes(serialize('mercenary_info', items))
        _deploy_overlay(self.gp, "0090", [
            ("mercenaryinfo.pabgb", new_pabgb),
            ("mercenaryinfo.pabgh", pabgh)])

    # ── Stores (0060) ──
    def _rebuild_stores(self):
        active_st = self.active & STORE_MODS
        if not active_st:
            _remove_overlay(self.gp, "0060")
            return

        self.progress.emit("Patching stores...")
        items, pabgh, serialize = _parse_table('store_info', self.gp, 'storeinfo')
        if "all_items_shop" in active_st:
            self.progress.emit("Filling all-items store...")
            by = {s["key"]: s for s in items}
            store = by.get(AIS_STORE_KEY)
            if store is None or not store.get("stock_data_list"):
                raise RuntimeError(f"store {AIS_STORE_KEY} not found / has no template entry")
            keys = _ais_safe_item_keys(self.gp)
            # clone the store's OWN first entry -> its lookup_a/flag_a already match
            tmpl_entry = store["stock_data_list"][0]
            store["stock_data_list"] = [
                _ais_stock_entry(tmpl_entry, k, i, AIS_STOCK_QTY)
                for i, k in enumerate(keys)]
            store["buyable_stock_count"] = len(keys)
            self._log(f"  all-items store {AIS_STORE_KEY}: {len(keys)} items")
        # size-changing edit (new store) needs the pabgh regenerated from original
        new_pabgb, new_pabgh = serialize('store_info', items, None, pabgh)
        _deploy_overlay(self.gp, "0060", [
            ("storeinfo.pabgb", bytes(new_pabgb)),
            ("storeinfo.pabgh", bytes(new_pabgh))])

    # ── All-Items Shop npcinfo (0067) — point Delkin at the all-items store ──
    def _rebuild_all_items_npcinfo(self):
        if "all_items_shop" not in self.active:
            _remove_overlay(self.gp, AIS_NPCINFO_GROUP)
            return
        self.progress.emit("Pointing grocer at all-items store...")
        items, pabgh, serialize = _parse_table('npc_info', self.gp, 'npcinfo')
        hit = False
        for n in items:
            if n.get("key") == AIS_DELKIN_NPCINFO:
                n["store_info"] = AIS_STORE_KEY
                hit = True
                break
        if not hit:
            raise RuntimeError(f"Delkin npc_info {AIS_DELKIN_NPCINFO} not found")
        new_pabgb, new_pabgh = serialize('npc_info', items, None, pabgh)
        _deploy_overlay(self.gp, AIS_NPCINFO_GROUP, [
            ("npcinfo.pabgb", bytes(new_pabgb)),
            ("npcinfo.pabgh", bytes(new_pabgh))])

    def _log(self, msg):
        try:
            self.progress.emit(msg)
        except Exception:
            pass

    # ── Player Speed (0071) ──
    def _handle_speed(self):
        if "speed_3x" not in self.active:
            _remove_overlay(self.gp, "0071")
            return

        self.progress.emit("Patching player speed...")
        items, pabgh, serialize = _parse_table('status_info', self.gp, 'statusinfo')
        for it in items:
            sk = it.get('string_key', '')
            if sk in ('AttackSpeedRate', 'MoveSpeedRate'):
                sdl = it.get('stat_level_data', [])
                if sdl and len(sdl) >= 16:
                    for i in range(len(sdl)):
                        if sdl[i] > 0:
                            sdl[i] = sdl[i] * 3
        new_pabgb = bytes(serialize('status_info', items))
        _deploy_overlay(self.gp, "0071", [
            ("statusinfo.pabgb", new_pabgb),
            ("statusinfo.pabgh", pabgh)])

    # ── Difficulty (0072) ──
    def _handle_difficulty(self):
        if "hard_2x_hp" not in self.active:
            _remove_overlay(self.gp, "0072")
            return

        self.progress.emit("Patching difficulty buffs...")
        items, pabgh, serialize = _parse_table('buff_info', self.gp, 'buffinfo')
        for it in items:
            sk = it.get('string_key', '')
            if sk not in ('BuffLevel_Difficulty', 'BuffLevel_Difficulty_Boss'):
                continue
            for bd in it.get('buff_data_list', []):
                data = bd.get('data', {})
                variant = data.get('variant', {})
                body = variant.get('body', {})
                if not body:
                    continue
                f00 = body.get('f00', -1)
                if f00 == 0:
                    val = body.get('f01', 0)
                    if isinstance(val, int) and 0 < val < 2**31:
                        body['f01'] = val * 2
        new_pabgb = bytes(serialize('buff_info', items))
        _deploy_overlay(self.gp, "0072", [
            ("buffinfo.pabgb", new_pabgb),
            ("buffinfo.pabgh", pabgh)])

    # ── Infinite Stamina (0073) ──
    # NOTE: must NOT reuse 0064 — _handle_quest deploys missioninfo.pabgb to
    # 0064, so the two overlays would clobber each other (last writer wins).
    _STAMINA_HASH = 1000026

    def _handle_stamina(self):
        if "infinite_stamina" not in self.active:
            _remove_overlay(self.gp, "0073")
            return

        self.progress.emit("Patching skill stamina costs...")
        items, pabgh, serialize = _parse_table('skill_info', self.gp, 'skill')

        for it in items:
            # Zero ALL resource d values — both cost (positive) and drain
            # (negative stored as large unsigned). Matches the working
            # skill_mod.field.json which sets d=0 on all 385 entries.
            for list_key in ('use_resource_stat_list', 'use_driver_resource_stat_list'):
                for r in (it.get(list_key) or []):
                    if isinstance(r, dict) and r.get('d', 0) != 0:
                        r['d'] = 0

        new_pabgb = bytes(serialize('skill_info', items))
        _deploy_overlay(self.gp, "0073", [
            ("skill.pabgb", new_pabgb),
            ("skill.pabgh", pabgh)])

    # ── Quest Unlock (0070) ──
    # Quest overlay groups — each table gets its own, matching the main app.
    # stageinfo uses 0070 instead of main app's 0066 to avoid conflict with
    # iteminfo pabgh index which also uses 0066.
    _QUEST_GROUPS = {
        "questinfo":   "0063",
        "missioninfo": "0064",
        "stageinfo":   "0070",
    }

    def _handle_quest(self):
        quest_groups = list(self._QUEST_GROUPS.values())
        if "quest_unlock" not in self.active:
            for g in quest_groups:
                _remove_overlay(self.gp, g)
            return

        self.progress.emit("Unlocking quest characters...")
        for stem, table in [("questinfo", "quest_info"),
                            ("missioninfo", "mission_info"),
                            ("stageinfo", "stage_info")]:
            grp = self._QUEST_GROUPS[stem]
            items, pabgh, serialize = _parse_table(table, self.gp, stem)
            for it in items:
                pl = it.get('start_player_list')
                if pl is not None and sorted(pl) != [1, 4, 6]:
                    it['start_player_list'] = [1, 4, 6]
                if it.get('forbidden_character_list'):
                    it['forbidden_character_list'] = []
                if it.get('hide_mercenary_group_info_list'):
                    it['hide_mercenary_group_info_list'] = []
            new_pabgb = bytes(serialize(table, items))
            _deploy_overlay(self.gp, grp, [
                (f"{stem}.pabgb", new_pabgb),
                (f"{stem}.pabgh", pabgh)])

    # ── Bag Space (0061) ──
    def _handle_bagspace(self):
        active_bs = self.active & {"bagspace_240", "bagspace_700"}
        if not active_bs:
            _remove_overlay(self.gp, "0061")
            return

        default_slots = 700 if "bagspace_700" in active_bs else 240
        max_slots = 700
        self.progress.emit(f"Patching bag space to {default_slots}/{max_slots}...")

        import crimson_rs
        from inventory_parser_108 import parse_inventory_entries, modify_slots

        pabgb = bytes(crimson_rs.extract_file(
            self.gp, '0008', INTERNAL, 'inventory.pabgb'))
        pabgh = bytes(crimson_rs.extract_file(
            self.gp, '0008', INTERNAL, 'inventory.pabgh'))

        entries = parse_inventory_entries(pabgb, pabgh)
        modified = bytearray(pabgb)

        log.info("Bag space: found %d inventory entries", len(entries))
        for name, dslot, mslot in [
            ('Character', default_slots, max_slots),
            ('CampWareHouse', 700, 700),
            ('WareHouse', 700, 700),
            ('Bank', 700, 700),
        ]:
            ok = modify_slots(modified, entries, name, dslot, mslot)
            log.info("  %s -> %d/%d: %s", name, dslot, mslot, "OK" if ok else "NOT FOUND")

        if modified == pabgb:
            log.warning("Bag space: no bytes changed! Parser may have failed.")

        _deploy_overlay(self.gp, "0061", [
            ("inventory.pabgb", bytes(modified)),
            ("inventory.pabgh", pabgh)])

    # ── Reset to Vanilla ──
    def _reset_vanilla(self):
        import crimson_rs
        gp = self.gp
        papgt_path = os.path.join(gp, "meta", "0.papgt")

        # 1. Restore 0008/ PAZ and PAMT from backups
        self.progress.emit("Restoring PAZ backups...")
        for bak_name, orig_name in [
            ("0.paz.backup", "0.paz"), ("0.pamt.backup", "0.pamt")
        ]:
            bak = os.path.join(gp, "0008", bak_name)
            if os.path.isfile(bak):
                shutil.copy2(bak, os.path.join(gp, "0008", orig_name))
                os.remove(bak)

        # 2. Restore .vanilla PAPGT if it exists
        papgt_vanilla = papgt_path + ".vanilla"
        if os.path.isfile(papgt_vanilla):
            shutil.copy2(papgt_vanilla, papgt_path)

        # 3. Remove all non-vanilla overlay directories
        self.progress.emit("Removing overlay directories...")
        for entry in os.listdir(gp):
            full = os.path.join(gp, entry)
            if not os.path.isdir(full):
                continue
            if entry.isdigit() and entry not in VANILLA_GROUPS:
                if os.path.isfile(os.path.join(full, "0.paz")) or \
                   os.path.isfile(os.path.join(full, "0.pamt")):
                    shutil.rmtree(full)
            elif entry.startswith(("dmmsa", "dmmgen", "dmmequ", "dmmlang")):
                shutil.rmtree(full)

        # 4. Rebuild PAPGT from what's actually on disk.
        #    _papgt_sync scans all dirs with 0.pamt, reads real checksums,
        #    sorts by group number, and writes a consistent PAPGT.
        self.progress.emit("Rebuilding PAPGT from disk...")
        _papgt_sync(gp)

        # 6. Clean up backup files
        for bak in [papgt_vanilla, papgt_path + ".sebak"]:
            if os.path.isfile(bak):
                os.remove(bak)

        # 7. Clear shared state
        try:
            from shared_state import save_state, ModdingState
            save_state(gp, ModdingState())
        except Exception:
            sf = os.path.join(gp, "crimson_modding_state.json")
            if os.path.isfile(sf):
                os.remove(sf)


# ─── Mod Card Widget ──────────────────────────────────────────────────
class ModCard(QFrame):
    toggled = Signal(str, bool)

    def __init__(self, mod_id: str, title: str, desc: str,
                 is_combo: bool = False, parent=None):
        super().__init__(parent)
        self.mod_id = mod_id
        self.title = title
        self._enabled = False
        self._busy = False
        self._is_combo = is_combo

        self.setFixedHeight(72 if not is_combo else 78)
        self.setCursor(Qt.PointingHandCursor)
        self._update_style()

        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 8, 14, 8)
        lay.setSpacing(10)

        self._dot = QLabel()
        self._dot.setFixedSize(10, 10)
        self._update_dot()
        lay.addWidget(self._dot, 0, Qt.AlignVCenter)

        col = QVBoxLayout()
        col.setSpacing(1)
        self._title_lbl = QLabel(title)
        sz = 13 if is_combo else 12
        self._title_lbl.setFont(QFont("Segoe UI", sz, QFont.Bold))
        self._title_lbl.setStyleSheet(f"color: {TEXT}; background: transparent;")
        col.addWidget(self._title_lbl)
        self._desc_lbl = QLabel(desc)
        self._desc_lbl.setStyleSheet(
            f"color: {TEXT_DIM}; background: transparent; font-size: 10px;")
        self._desc_lbl.setWordWrap(True)
        col.addWidget(self._desc_lbl)
        lay.addLayout(col, 1)

        self._badge = QLabel("OFF")
        self._badge.setFixedWidth(50)
        self._badge.setAlignment(Qt.AlignCenter)
        self._badge.setFont(QFont("Segoe UI", 9, QFont.Bold))
        self._update_badge()
        lay.addWidget(self._badge, 0, Qt.AlignVCenter)

    def _update_style(self):
        bg = COMBO_BG if self._is_combo else CARD_BG
        border = ON_COLOR if self._enabled else BORDER
        self.setStyleSheet(
            f"ModCard {{ background-color: {bg}; border: 2px solid {border}; "
            f"border-radius: 6px; }}"
            f"ModCard:hover {{ background-color: {CARD_HOVER}; }}")

    def _update_dot(self):
        c = ON_COLOR if self._enabled else OFF_COLOR
        self._dot.setStyleSheet(
            f"background-color: {c}; border-radius: 5px; border: none;")

    def _update_badge(self):
        if self._enabled:
            self._badge.setStyleSheet(
                f"background-color: {ON_COLOR}; color: #1a1510; "
                f"border-radius: 4px; padding: 2px 6px; border: none;")
            self._badge.setText("ON")
        else:
            self._badge.setStyleSheet(
                f"background-color: {OFF_COLOR}; color: {TEXT_DIM}; "
                f"border-radius: 4px; padding: 2px 6px; border: none;")
            self._badge.setText("OFF")

    def set_enabled(self, on: bool):
        self._enabled = on
        self._update_style()
        self._update_dot()
        self._update_badge()

    def set_busy(self, busy: bool):
        self._busy = busy
        self.setCursor(Qt.WaitCursor if busy else Qt.PointingHandCursor)

    def mousePressEvent(self, ev):
        if not self._busy:
            self.toggled.emit(self.mod_id, not self._enabled)
        super().mousePressEvent(ev)


# ─── Main Window ──────────────────────────────────────────────────────
class SimpleWindow(QWidget):

    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"CrimsonGameMods Simple v{APP_VERSION}")
        self.setMinimumSize(680, 600)
        self.resize(720, 780)
        self.setStyleSheet(STYLESHEET)

        self._config = load_config()
        self._game_path = self._config.get("game_path", "") or find_game_path()
        self._active: set[str] = set(self._config.get("active_mods", []))
        self._worker: Optional[ModWorker] = None
        self._cards: dict[str, ModCard] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 12, 18, 12)
        root.setSpacing(8)

        hdr = QLabel("CrimsonGameMods Simple")
        hdr.setFont(QFont("Segoe UI", 17, QFont.Bold))
        hdr.setStyleSheet(f"color: {ACCENT};")
        hdr.setAlignment(Qt.AlignCenter)
        root.addWidget(hdr)

        # Game path
        pr = QHBoxLayout()
        pr.setSpacing(6)
        pl = QLabel("Game:")
        pl.setStyleSheet(f"color: {TEXT_DIM};")
        pl.setFixedWidth(40)
        pr.addWidget(pl)
        self._path_lbl = QLabel(self._game_path or "Not found — click Browse")
        self._path_lbl.setStyleSheet(
            f"color: {TEXT}; background: {INPUT_BG}; border: 1px solid {BORDER}; "
            f"border-radius: 4px; padding: 5px 7px;")
        self._path_lbl.setWordWrap(True)
        pr.addWidget(self._path_lbl, 1)
        bb = QPushButton("Browse")
        bb.setFixedWidth(65)
        bb.setStyleSheet(
            f"QPushButton {{ background: {HEADER}; color: {TEXT}; border: 1px solid {BORDER}; "
            f"border-radius: 4px; padding: 5px; }}"
            f"QPushButton:hover {{ background: {ACCENT}; color: #1a1510; }}")
        bb.clicked.connect(self._browse)
        pr.addWidget(bb)
        root.addLayout(pr)

        # Main content: nav sidebar + card scroll area side by side
        content = QHBoxLayout()
        content.setSpacing(6)

        # Left nav list — vertical, fixed width
        nav_widget = QWidget()
        nav_widget.setFixedWidth(140)
        nav_layout = QVBoxLayout(nav_widget)
        nav_layout.setContentsMargins(0, 4, 0, 4)
        nav_layout.setSpacing(2)

        sections = []
        for md in MOD_DEFS:
            s = md.get("section", "")
            if s and s not in sections:
                sections.append(s)

        for sec in sections:
            lbl = QPushButton(sec)
            lbl.setCursor(Qt.PointingHandCursor)
            lbl.setFont(QFont("Segoe UI", 9))
            lbl.setFixedHeight(26)
            lbl.setStyleSheet(
                f"QPushButton {{ color: {ACCENT}; background: {HEADER}; "
                f"border: 1px solid {BORDER}; border-radius: 3px; "
                f"text-align: left; padding: 2px 8px; }}"
                f"QPushButton:hover {{ background: {ACCENT}; color: #1a1510; }}")
            lbl.clicked.connect(lambda _, s=sec: self._scroll_to_section(s))
            nav_layout.addWidget(lbl)
        nav_layout.addStretch(1)
        content.addWidget(nav_widget)

        # Right: scrollable cards
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        inner = QWidget()
        self._cl = QVBoxLayout(inner)
        self._cl.setContentsMargins(0, 0, 0, 0)
        self._cl.setSpacing(5)

        self._section_widgets: dict[str, QLabel] = {}
        last_section = None
        for md in MOD_DEFS:
            sec = md.get("section", "")
            if sec != last_section:
                sl = QLabel(sec)
                sl.setFont(QFont("Segoe UI", 10, QFont.Bold))
                sl.setStyleSheet(
                    f"color: {SECTION_CLR}; padding: 6px 0 2px 4px;")
                self._cl.addWidget(sl)
                self._section_widgets[sec] = sl
                last_section = sec
            card = ModCard(md["id"], md["title"], md["desc"],
                           is_combo=md.get("combo", False))
            card.toggled.connect(self._on_toggle)
            self._cards[md["id"]] = card
            self._cl.addWidget(card)

        self._cl.addSpacing(400)
        self._scroll.setWidget(inner)
        content.addWidget(self._scroll, 1)
        root.addLayout(content, 1)

        # Bottom buttons
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet(f"color: {BORDER};")
        root.addWidget(sep)

        br = QHBoxLayout()
        br.setSpacing(8)
        rbtn = QPushButton("Restore to Vanilla")
        rbtn.setFixedHeight(36)
        rbtn.setStyleSheet(
            f"QPushButton {{ background: {RESET_CLR}; color: white; font-weight: bold; "
            f"font-size: 12px; border: none; border-radius: 6px; padding: 7px; }}"
            f"QPushButton:hover {{ background: #b03830; }}")
        rbtn.clicked.connect(self._on_reset)
        br.addWidget(rbtn)
        sbtn = QPushButton("Start Game")
        sbtn.setFixedHeight(36)
        sbtn.setStyleSheet(
            f"QPushButton {{ background: {SUCCESS}; color: #1a1510; font-weight: bold; "
            f"font-size: 12px; border: none; border-radius: 6px; padding: 7px; }}"
            f"QPushButton:hover {{ background: #b8d890; }}")
        sbtn.clicked.connect(self._start_game)
        br.addWidget(sbtn)
        root.addLayout(br)

        self._status = QLabel("Ready")
        self._status.setStyleSheet(f"color: {TEXT_DIM}; font-size: 11px;")
        self._status.setAlignment(Qt.AlignCenter)
        root.addWidget(self._status)

        self._sync_badges()

    # ── State Management ──
    def _sync_badges(self):
        """Refresh all badge states from self._active."""
        for mid, card in self._cards.items():
            if mid in COMBO_DEFS:
                card.set_enabled(COMBO_DEFS[mid].issubset(self._active))
            else:
                card.set_enabled(mid in self._active)

    def _apply_mutex(self, toggled_id: str):
        """Enforce mutual exclusion pairs."""
        for a, b in MUTEX_PAIRS:
            if toggled_id == a and a in self._active:
                self._active.discard(b)
            elif toggled_id == b and b in self._active:
                self._active.discard(a)

    def _save_active(self):
        self._config["active_mods"] = sorted(self._active)
        save_config(self._config)

    # ── Actions ──
    def _scroll_to_section(self, section: str):
        w = self._section_widgets.get(section)
        if w:
            y = w.mapTo(self._scroll.widget(), w.rect().topLeft()).y()
            self._scroll.verticalScrollBar().setValue(y)

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "Select Crimson Desert install folder")
        if d:
            if not os.path.isfile(os.path.join(d, "0008", "0.paz")):
                QMessageBox.warning(self, "Invalid", "No 0008/0.paz found.")
                return
            self._game_path = d
            self._path_lbl.setText(d)
            self._config["game_path"] = d
            save_config(self._config)

    def _preflight(self) -> bool:
        if not self._game_path:
            QMessageBox.warning(self, "No Game Path", "Set game folder first.")
            return False
        if is_game_running():
            QMessageBox.warning(self, "Game Running", "Close the game first.")
            return False
        if not can_write_game_dir(self._game_path):
            QMessageBox.warning(self, "No Write Access",
                f"Cannot write to:\n{self._game_path}\n\nRun as Administrator.")
            return False
        return True

    _SOCKET_ABYSS_WARNING_MODS = {"five_sockets", "unlock_abyss", "enable_everything"}

    def _on_toggle(self, mod_id: str, want_on: bool):
        if self._worker and self._worker.isRunning():
            QMessageBox.warning(self, "Busy", "Wait for current operation.")
            return
        if not self._preflight():
            return

        # Warn about socket/abyss crash risk
        if want_on and mod_id in self._SOCKET_ABYSS_WARNING_MODS:
            reply = QMessageBox.warning(
                self, "Possible Loading Issue",
                "5 Sockets + Unlock Abyss Gear may cause infinite loading "
                "or crashes on saves with many socketed abyss gems.\n\n"
                "The game has a ~23 buff line limit across all equipped gear. "
                "If exceeded, the save will not load.\n\n"
                "If this happens, use 'Restore to Vanilla' to fix it.\n\n"
                "Continue?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
            if reply != QMessageBox.Yes:
                return

        # Handle combo toggles
        if mod_id in COMBO_DEFS:
            members = COMBO_DEFS[mod_id]
            if want_on:
                self._active |= members
            else:
                self._active -= members
        else:
            if want_on:
                self._active.add(mod_id)
            else:
                self._active.discard(mod_id)

        self._apply_mutex(mod_id)
        self._sync_badges()
        self._save_active()

        # Figure out which worker mod_id to dispatch
        dispatch_id = mod_id
        if mod_id in COMBO_DEFS:
            # Combo touches multiple groups — pick the primary
            dispatch_id = mod_id  # worker handles combo by checking active set
        self._run_worker(dispatch_id)

    def _on_reset(self):
        if self._worker and self._worker.isRunning():
            QMessageBox.warning(self, "Busy", "Wait for current operation.")
            return
        if not self._preflight():
            return
        reply = QMessageBox.warning(
            self, "Restore to Vanilla",
            "Remove ALL mods and restore game to vanilla?\n\n"
            "Deletes overlay dirs, restores 0008/0.paz, cleans PAPGT.\n"
            "Cannot be undone.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply != QMessageBox.Yes:
            return
        self._active.clear()
        self._save_active()
        self._sync_badges()
        self._run_worker("reset_vanilla")

    def _start_game(self):
        if not self._game_path:
            QMessageBox.warning(self, "No Game Path", "Set game folder first.")
            return
        exe = os.path.join(self._game_path, "bin64", "CrimsonDesert.exe")
        if not os.path.isfile(exe):
            exe = os.path.join(self._game_path, "CrimsonDesert.exe")
        if not os.path.isfile(exe):
            exe = os.path.join(self._game_path, "start_protected_game.exe")
        if not os.path.isfile(exe):
            for root, dirs, files in os.walk(self._game_path):
                for f in files:
                    if f.lower() == "crimsondesert.exe":
                        exe = os.path.join(root, f)
                        break
                if os.path.isfile(exe):
                    break
        if not os.path.isfile(exe):
            try:
                os.startfile("steam://rungameid/2467650")
                self._status.setText("Launching via Steam...")
                self._status.setStyleSheet(f"color: {SUCCESS}; font-size: 11px;")
            except Exception:
                QMessageBox.warning(self, "Launch Failed", "Could not find game exe or Steam.")
            return
        try:
            subprocess.Popen([exe], cwd=self._game_path)
            self._status.setText("Game launched")
            self._status.setStyleSheet(f"color: {SUCCESS}; font-size: 11px;")
        except Exception as e:
            QMessageBox.warning(self, "Launch Failed", str(e))

    def _run_worker(self, mod_id: str):
        for c in self._cards.values():
            c.set_busy(True)
        title = self._cards[mod_id].title if mod_id in self._cards else "Restore to Vanilla"
        self._status.setText(f"Applying: {title}...")
        self._status.setStyleSheet(f"color: {ACCENT}; font-size: 11px;")

        self._worker = ModWorker(self._game_path, mod_id, self._active)
        self._worker.progress.connect(lambda m: self._status.setText(m))
        self._worker.finished.connect(
            lambda ok, err, mid=mod_id: self._on_done(mid, ok, err))
        self._worker.start()

    def _on_done(self, mod_id: str, ok: bool, error: str):
        for c in self._cards.values():
            c.set_busy(False)
        if ok:
            self._status.setText("Done")
            self._status.setStyleSheet(f"color: {SUCCESS}; font-size: 11px;")
        else:
            self._status.setText(f"Failed: {error}")
            self._status.setStyleSheet(f"color: {ERROR}; font-size: 11px;")
            QMessageBox.critical(self, "Error", f"Failed:\n\n{error}")
        self._sync_badges()


def main():
    logging.basicConfig(level=logging.INFO)
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    w = SimpleWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
