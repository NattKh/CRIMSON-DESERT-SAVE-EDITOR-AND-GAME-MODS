"""
Stacker Tool — multi-mod iteminfo.pabgb merger.

Drop N mods (compiled PAZ folder / loose pabgb / legacy JMM format:2 JSON)
into the window, click Install Stack, get ONE merged overlay in the game.

Field-level merge, not byte-level:
- Two mods touching different fields of the same entry both land.
- Two mods touching the SAME field with different values → shown in
  conflict table, user picks or last-writer wins.
- Mods that add entries just extend the merged dict list; no offset math.

Everything runs on primitives already in the codebase:
- crimson_rs.parse_iteminfo_from_bytes  (→ dict items)
- paz_patcher.ItemBuffPatcher.find_items (→ entry→BlobStart for legacy JSON resolve)
- crimson_rs.serialize_iteminfo (→ merged bytes)
- buffs_v319._buff_export_cdumm_mod / _buff_apply_to_game (→ overlay write + install)

No byte-level truncation. No MergePabgb pad/truncate. Output goes
through crimson_rs.serialize_iteminfo which rebuilds sizes from the
dicts, so Super MOD's entries keep their full shape.
"""
from __future__ import annotations
import os, sys, json, struct, copy, zipfile, tempfile, shutil, logging, time
from dataclasses import dataclass, field  # field: default_factory for mutable defaults
from pathlib import Path
from typing import Optional

from . import iteminfo_inspector
from PySide6.QtCore import Qt, Signal, QSize
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QFontMetrics, QColor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QTableWidget, QTableWidgetItem, QTextEdit, QLineEdit, QFileDialog,
    QMessageBox, QGroupBox, QHeaderView, QAbstractItemView, QSplitter,
    QApplication, QSizePolicy, QFrame, QListWidget, QListWidgetItem,
    QCheckBox, QToolButton,
)


def _size_button(btn: QPushButton, extra_px: int = 28, primary: bool = False) -> QPushButton:
    """Size a button so its label never clips, regardless of system font.

    Measures the text width at the widget's current font, adds padding for
    the border + internal margin, and fixes that as minimumWidth. Keeps a
    fixed vertical size policy (buttons never stretch tall) and an
    expanding-or-preferred horizontal policy so rows of buttons share
    width evenly in tight layouts.
    """
    fm = QFontMetrics(btn.font())
    w = fm.horizontalAdvance(btn.text()) + extra_px
    btn.setMinimumWidth(w)
    btn.setMinimumHeight(30 if not primary else 34)
    btn.setSizePolicy(
        QSizePolicy.Expanding if primary else QSizePolicy.Preferred,
        QSizePolicy.Fixed,
    )
    return btn

log = logging.getLogger(__name__)

INTERNAL_DIR = "gamedata/binary__/client/bin"
ITEMINFO_PABGB = "iteminfo.pabgb"
ITEMINFO_PABGH = "iteminfo.pabgh"

# Non-numeric group name, deliberate. JMM's CleanupStaleOverlayGroups
# (see _ilspy_dump_mod_manager/source_v10/CDModManager/ModManager.cs:2485)
# deletes any overlay directory whose name passes `All(char.IsDigit)` and
# is >= 36, unless it's in JMM's own papgt backup. A non-numeric name
# like "stk1" is invisible to that predicate — our overlay survives
# subsequent JMM Apply runs. The game's PAPGT loader does not require
# numeric group names; that convention is JMM's alone.
# Apply Stack uses numeric group names because the game's PAPGT loader
# silently rejects non-numeric group names like "stk1" -- the PAZ never
# loads even though PAPGT registration looks correct on disk.
# Confirmed empirically 2026-04-21.
#
# Tradeoff: JMM's CleanupStaleOverlayGroups pass wipes all-numeric group
# dirs it doesn't recognize. Users who run JMM after Apply Stack must
# re-apply. Non-numeric would have survived JMM but the game doesn't
# load them -- functionality beats compatibility.
OVERLAY_GROUP_DEFAULT = "0062"
# Equipslotinfo MUST ship in a separate overlay group from iteminfo.
# Bundling them together in one group empirically breaks UP v2
# (guns-on-Kliff etc.) -- confirmed 2026-04-21 against v1.0.3 split
# deployment that works.
OVERLAY_EQUIP_GROUP_DEFAULT = "0063"


# ============================================================
#   Field JSON v3 export — extensible target registry
# ============================================================
#
# Each entry describes one pabgb table the Stacker can produce intents for.
# Adding support for a new target = adding one entry to this list and a
# matching `_diff_<table>_to_intents()` helper above. The exporter
# (_export_field_json) iterates this registry to assemble the multi-target
# `targets[]` array, falling back to legacy single-target shape only when
# nothing but iteminfo is present.
#
# Schema per entry:
#   - name           v3 target string (matches `targets[].file` in output JSON)
#   - enabled        gate flag — set False for scaffolds awaiting prerequisites
#   - merged_attr    name of the StackerTab attribute that holds merged bytes
#                    as `{filename: bytes}` dict (set in _run after the merge)
#   - pabgb_filename / pabgh_filename
#                    keys to pull out of the merged_attr dict
#   - vanilla_group  PAZ group string for crimson_rs.extract_file (almost
#                    always "0008" for game data tables)
#   - vanilla_dir    INTERNAL_DIR for game data; constants up top
#   - diff_fn        callable(vanilla_pabgh, vanilla_pabgb,
#                             mod_pabgh, mod_pabgb) -> list[intent]
#   - label          human-readable, used in mount log lines
#   - todo           one-liner explaining why an entry is disabled (None if
#                    enabled)
#
# Iteminfo is intentionally NOT in this registry — it uses the dict-list
# diff path (vanilla_items vs merged_items) which is structurally different
# from the bytes-diff approach used here. The exporter handles it inline.
_FIELD_JSON_TARGET_REGISTRY: list[dict] = [
    {
        'name':            'equipslotinfo.pabgb',
        'enabled':         True,
        'merged_attr':     '_merged_equip_files',
        'pabgb_filename':  'equipslotinfo.pabgb',
        'pabgh_filename':  'equipslotinfo.pabgh',
        'vanilla_group':   '0008',
        'vanilla_dir':     INTERNAL_DIR,
        'diff_fn':         None,  # set after _diff_equipslot_to_intents is defined
        'label':           'equipslotinfo (universal weapon proficiency)',
        'todo':            None,
    },
    {
        'name':            'skill.pabgb',
        'enabled':         False,
        'merged_attr':     '_merged_skill_files',
        'pabgb_filename':  'skill.pabgb',
        'pabgh_filename':  'skill.pabgh',
        'vanilla_group':   '0008',
        'vanilla_dir':     INTERNAL_DIR,
        'diff_fn':         None,  # set after _diff_skill_to_intents is defined
        'label':           'skill (cooltimes, learn levels, etc.)',
        'todo':            ('field name mismatch between skillinfo_parser.py '
                            '(IDA names) and DMM dmm_parser::tables::skill_info '
                            '(Mac symbols). Build a name map, then flip enabled.'),
    },
    # When a generic blob-table fallback target is needed (e.g. enabling a
    # "block this buff" / "disable this condition" workflow), copy this
    # template and set name + merged_attr accordingly. The Stacker doesn't
    # currently merge non-{item,skill,equipslot}info tables; that's the
    # blocker, NOT the export pipeline. Wire merge first, then enable here.
    # Example for a future buff-blocker:
    # {
    #     'name':           'buff_info.pabgb',
    #     'enabled':        False,
    #     'merged_attr':    '_merged_other_files',
    #     'pabgb_filename': 'buff_info.pabgb',
    #     'pabgh_filename': 'buff_info.pabgh',
    #     'vanilla_group':  '0008',
    #     'vanilla_dir':    INTERNAL_DIR,
    #     'diff_fn':        lambda vh, vb, mh, mb: _diff_blob_table_to_intents(vh, vb, mh, mb, 'buff_info.pabgb'),
    #     'label':          'buff_info (blob fallback)',
    #     'todo':           'Stacker merge-side support for buff_info pending',
    # },
]


# ============================================================
#   Per-entry parse fallback (handles housing items)
# ============================================================

def _parse_with_fallback(crimson_rs, game_path: str,
                         raw: bytes) -> tuple[list, list]:
    """Parse iteminfo using PABGH boundaries when bulk parse fails."""
    pabgh = bytes(crimson_rs.extract_file(
        game_path, '0008', INTERNAL_DIR, ITEMINFO_PABGH))
    count = struct.unpack_from('<H', pabgh, 0)[0]
    rs = (len(pabgh) - 2) // count if count else 8
    entries = []
    for i in range(count):
        rec = 2 + i * rs
        if rec + rs > len(pabgh):
            break
        soff = struct.unpack_from('<I', pabgh, rec + (rs - 4))[0]
        if soff + 8 <= len(raw):
            entries.append(soff)
    entries.sort()
    items = []
    unparsed = []
    for idx, soff in enumerate(entries):
        nxt = entries[idx + 1] if idx + 1 < len(entries) else len(raw)
        try:
            parsed = crimson_rs.parse_iteminfo_from_bytes(raw[soff:nxt])
            if parsed:
                items.append(parsed[0])
            else:
                unparsed.append(bytes(raw[soff:nxt]))
        except Exception:
            unparsed.append(bytes(raw[soff:nxt]))
    return items, unparsed


# ============================================================
#   Mod classification / ingestion
# ============================================================

@dataclass
class ModEntry:
    """A single source in the stack. Four kinds, four state buckets.

    Bucket A (dict-level edits) → parsed_items
    Bucket B (checkbox toggles) → apply_stacks, apply_inf_dura
    Bucket C (post-serialize byte patches) → cd_patches, transmog_swaps, vfx_*
    Bucket D (sibling files bundled in same overlay) → staged_skill_files,
        staged_equip_files

    External mod sources (folder_paz / loose_pabgb / legacy_json) can
    populate A and D. ItemBuffs pulls populate all four buckets since
    ItemBuffs applies all four when its own Apply-to-Game button runs
    — see buffs_v319.py:9770.
    """
    name: str
    path: str
    kind: str                # "folder_paz" | "loose_pabgb" | "legacy_json" | "itembuffs_edits"
    group: str = ""          # folder_paz only: "0036" etc.
    ok: bool = True
    note: str = ""
    enabled: bool = True     # user toggle — disabled sources skip the merge
    effective_pabgb: Optional[bytes] = None   # filled during install prep (external mods only)
    parsed_items: Optional[list] = None       # Bucket A — dict list
    apply_stats: str = ""    # "411 applied, 0 skipped" etc.
    detected_features: dict = field(default_factory=dict)   # feature → count
    # Bucket B — checkbox toggles. None/False means "don't apply".
    apply_stacks: Optional[int] = None        # target stack value, e.g. 9999
    apply_inf_dura: bool = False
    # Bucket C — post-serialize byte patches. All lists/dicts captured
    # by deep-copy at Pull time so subsequent ItemBuffs edits don't
    # mutate what we captured.
    cd_patches: dict = field(default_factory=dict)
    transmog_swaps: list = field(default_factory=list)
    vfx_size_changes: list = field(default_factory=list)
    vfx_swaps: list = field(default_factory=list)
    vfx_anim_swaps: list = field(default_factory=list)
    vfx_attach_changes: list = field(default_factory=list)
    # Bucket D — sibling files to add alongside iteminfo in the overlay PAZ.
    # Keys are "skill.pabgb", "skill.pabgh", "equipslotinfo.pabgb",
    # "equipslotinfo.pabgh". Values are raw bytes.
    staged_skill_files: dict = field(default_factory=dict)
    staged_equip_files: dict = field(default_factory=dict)
    # Catch-all bucket for every other .pabgb/.pabgh pair the source mod
    # ships (buff_info, condition_info, gimmick_info, store_info, etc.).
    # Pre-1.1.5 these were dropped at folder_paz ingestion (only the four
    # equipslot+skill names were extracted), making any mod that touched
    # other tables silently broken in the Stacker pipeline. The Field
    # JSON exporter consumes this via _merged_other_files.
    staged_other_files: dict = field(default_factory=dict)
    # Legacy-JSON-only: per-patch field attribution + merge strategy. See
    # iteminfo_inspector.py. `merge_mode` is "strict" (default — apply
    # bytes to vanilla then re-parse) or "semantic" (skip byte apply,
    # convert each patch to a field-level edit that survives offset
    # drift from other mods). `inspections` is populated during
    # _run_inner; the Details pane reads it to show per-patch field
    # names and any stale/missing reasons.
    merge_mode: str = "strict"
    inspections: list = field(default_factory=list)
    # Populated when merge_mode == "reparse_diff". A
    # iteminfo_inspector.ReparseDiffReport or None.
    reparse_report: object = None


# Kliff's 11 tribe_gender hashes — if ALL of them are present in an
# item's prefab tribe_gender_list, it's an unambiguous signal that
# Universal Proficiency v2 was applied (vanilla gear never has them
# unioned in). Kept in sync with buffs_v319.py:_CHAR_TRIBE_HASHES[1].
_UP_V2_KLIFF_HASHES = frozenset({
    0x13FB2B6E, 0x26BE971F, 0x87D08287, 0x8BF46446,
    0xABFCD791, 0xBFA1F64B, 0xD0A2E1EF, 0xF96C1DD4,
    0xFC66D914, 0xFE7169E2, 0xFF16A579,
})


def _detect_dict_features(items: list) -> dict:
    """Scan parsed iteminfo dict list, count ItemBuffs feature signatures.

    Used at Pull time so the user can *see* that UP v2, Make Dyeable,
    Socket Extend, No Cooldown, etc. were actually captured — not just
    "6024 entries" with the real content hidden inside. Also flags
    missing-equipslotinfo when tribe edits are detected so we can run an
    auto-stage fallback.

    Detection uses only unambiguous field values (e.g. `max_endurance ==
    65535` = Inf Dura, not a vanilla coincidence). Ambiguous signals
    (modified enchant stats, added buffs) are skipped — they need a
    vanilla baseline to detect correctly.
    """
    feats = {
        'max_stacks': 0, 'max_stacks_target': 0,
        'no_cooldown': 0,
        'inf_durability': 0,
        'max_charges': 0,
        'dyeable_flipped': 0,
        'socket_forced_on': 0,
        'socket_5_extended': 0,
        'up_v2_tribe_unioned': 0,
        'abyss_unlocked': 0,
        'abyss_total': 0,
        'passives_added': 0,
        'extra_enchant_levels': 0,
    }
    for it in items:
        stack = it.get('max_stack_count') or 0
        if stack >= 1000:
            feats['max_stacks'] += 1
            if stack > feats['max_stacks_target']:
                feats['max_stacks_target'] = stack

        if it.get('cooltime') == 1:
            feats['no_cooldown'] += 1

        if it.get('max_endurance') == 65535:
            feats['inf_durability'] += 1

        if (it.get('max_charged_useable_count') or 0) >= 99:
            feats['max_charges'] += 1

        if it.get('equip_type_info') and it.get('is_dyeable') == 1:
            feats['dyeable_flipped'] += 1

        ddd = it.get('drop_default_data') or {}
        if ddd.get('use_socket') == 1:
            sock_list = ddd.get('add_socket_material_item_list') or []
            if len(sock_list) >= 5:
                feats['socket_5_extended'] += 1
            # "Forced on" heuristic: accessories/cloaks vanilla ship with
            # use_socket=0; if it's 1 AND has exactly 5 entries at the
            # DennyBro default cost pattern (500, 1000, 2000, 3000, 4000)
            # → our force-enable likely did this.
            if len(sock_list) == 5:
                costs = [e.get('value') for e in sock_list]
                if costs == [500, 1000, 2000, 3000, 4000]:
                    feats['socket_forced_on'] += 1

        if 'AbyssGear' in (it.get('string_key') or ''):
            feats['abyss_total'] += 1
            if it.get('equipable_hash', -1) == 0:
                feats['abyss_unlocked'] += 1

        if it.get('equip_passive_skill_list'):
            feats['passives_added'] += 1

        for pd in (it.get('prefab_data_list') or []):
            tg = pd.get('tribe_gender_list') or []
            if tg and _UP_V2_KLIFF_HASHES.issubset(set(tg)):
                feats['up_v2_tribe_unioned'] += 1
                break

        enchants = it.get('enchant_data_list') or []
        if len(enchants) >= 12:
            feats['extra_enchant_levels'] += 1

    return feats


def _features_summary_lines(feats: dict) -> list:
    """Render detected features as human-readable lines. Empty list
    means no unambiguous feature fingerprints were found."""
    lines = []
    if feats.get('max_stacks'):
        lines.append(f"  Max Stacks            : {feats['max_stacks']:>5} items "
                     f"(up to {feats['max_stacks_target']})")
    if feats.get('no_cooldown'):
        lines.append(f"  No Cooldown (=1)      : {feats['no_cooldown']:>5} items")
    if feats.get('inf_durability'):
        lines.append(f"  Inf Durability        : {feats['inf_durability']:>5} items")
    if feats.get('max_charges'):
        lines.append(f"  Max Charges (=99)     : {feats['max_charges']:>5} items")
    if feats.get('dyeable_flipped'):
        lines.append(f"  Made Dyeable          : {feats['dyeable_flipped']:>5} items")
    if feats.get('socket_5_extended'):
        lines.append(f"  Sockets (5 slots)     : {feats['socket_5_extended']:>5} items")
    if feats.get('socket_forced_on'):
        lines.append(f"    └ force-enabled     : {feats['socket_forced_on']:>5} accessories/cloaks (DennyBro pattern)")
    if feats.get('abyss_unlocked') and feats['abyss_unlocked'] == feats.get('abyss_total', 0):
        lines.append(f"  Abyss Gear Unlocked   : {feats['abyss_unlocked']:>5}/{feats['abyss_total']} (all unrestricted)")
    elif feats.get('abyss_unlocked'):
        lines.append(f"  Abyss Gear Unlocked   : {feats['abyss_unlocked']:>5}/{feats['abyss_total']} items")
    if feats.get('up_v2_tribe_unioned'):
        lines.append(f"  UP v2 tribe unioned   : {feats['up_v2_tribe_unioned']:>5} items")
    if feats.get('passives_added'):
        lines.append(f"  Passives attached     : {feats['passives_added']:>5} items")
    if feats.get('extra_enchant_levels'):
        lines.append(f"  Extended enchant sets : {feats['extra_enchant_levels']:>5} items (≥12 levels)")
    return lines


def _compact_features_summary(feats: dict) -> str:
    """One-line summary for the sources-table note column."""
    bits = []
    if feats.get('max_stacks'):
        bits.append(f"stacks({feats['max_stacks']})")
    if feats.get('no_cooldown'):
        bits.append(f"no-cd({feats['no_cooldown']})")
    if feats.get('inf_durability'):
        bits.append(f"inf-dura({feats['inf_durability']})")
    if feats.get('max_charges'):
        bits.append(f"max-charge({feats['max_charges']})")
    if feats.get('dyeable_flipped'):
        bits.append(f"dyeable({feats['dyeable_flipped']})")
    if feats.get('socket_5_extended'):
        bits.append(f"sock-5({feats['socket_5_extended']})")
    if feats.get('abyss_unlocked'):
        bits.append(f"abyss({feats['abyss_unlocked']}/{feats.get('abyss_total',0)})")
    if feats.get('up_v2_tribe_unioned'):
        bits.append(f"UP-v2({feats['up_v2_tribe_unioned']})")
    if feats.get('passives_added'):
        bits.append(f"passives({feats['passives_added']})")
    return "; ".join(bits)


def _find_loose_iteminfo(path: str) -> Optional[str]:
    if os.path.isfile(path) and os.path.basename(path).lower() == ITEMINFO_PABGB:
        return path
    if os.path.isdir(path):
        for root, _, files in os.walk(path):
            if ITEMINFO_PABGB in files:
                return os.path.join(root, ITEMINFO_PABGB)
    return None


def _find_folder_paz_group(path: str) -> Optional[str]:
    """Return group name (e.g. '0036') if the folder has NNNN/0.paz AND
    iteminfo.pabgb is inside that PAZ. Else None."""
    if not os.path.isdir(path):
        return None
    try:
        import crimson_rs
    except Exception:
        return None
    for name in sorted(os.listdir(path)):
        sub = os.path.join(path, name)
        if not (os.path.isdir(sub) and len(name) == 4 and name.isdigit()
                and os.path.isfile(os.path.join(sub, "0.paz"))
                and os.path.isfile(os.path.join(sub, "0.pamt"))):
            continue
        try:
            crimson_rs.extract_file(path, name, INTERNAL_DIR, ITEMINFO_PABGB)
            return name
        except Exception:
            continue
    return None


def _classify(path: str) -> ModEntry:
    display = Path(path).stem or path
    pabgb = _find_loose_iteminfo(path)
    if pabgb:
        return ModEntry(name=display, path=pabgb, kind="loose_pabgb",
                        note="loose iteminfo.pabgb")
    grp = _find_folder_paz_group(path)
    if grp:
        return ModEntry(name=display, path=path, kind="folder_paz",
                        group=grp, note=f"compiled PAZ mod (group {grp})")
    if os.path.isfile(path) and path.lower().endswith(".json"):
        try:
            with open(path, encoding="utf-8") as f:
                doc = json.load(f)
            if doc.get("format") == 3 and doc.get("intents"):
                n = len(doc["intents"])
                title = (doc.get("modinfo") or {}).get("title", display)
                return ModEntry(name=title, path=path, kind="field_json",
                                note=f"Format 3 field JSON ({n} intents)")
            patches = doc.get("patches") or []
            if any("iteminfo.pabgb" in (pt.get("game_file") or "").lower()
                   for pt in patches):
                n = sum(len(pt.get("changes") or []) for pt in patches)
                return ModEntry(name=display, path=path, kind="legacy_json",
                                note=f"legacy JMM JSON ({n} byte patches)")
            return ModEntry(name=display, path=path, kind="",
                            ok=False, note="JSON does not target iteminfo.pabgb")
        except Exception as e:
            return ModEntry(name=display, path=path, kind="",
                            ok=False, note=f"JSON parse failed: {e}")
    if os.path.isdir(path):
        return ModEntry(name=display, path=path, kind="", ok=False,
                        note="folder has no iteminfo.pabgb (not an iteminfo mod)")
    return ModEntry(name=display, path=path, kind="", ok=False,
                    note="not an iteminfo mod")


# ============================================================
#   Legacy JSON apply (resolves entry+rel_offset via BlobStart map)
# ============================================================

def _apply_one_legacy_patch(patched: bytearray, vanilla: bytes,
                            change: dict) -> int:
    """Apply a single legacy JSON v2 patch to a bytearray. Returns 1 if applied."""
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
            abs_off = int(off_val, 16) if isinstance(off_val, str) else off_val
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
            abs_off = int(off_val, 16) if isinstance(off_val, str) else off_val
        else:
            return 0
        insert_bytes = bytes.fromhex(change.get('bytes', ''))
        if not insert_bytes:
            return 0
        patched[abs_off:abs_off] = insert_bytes
        return 1
    return 0


def _apply_legacy_json(vanilla_bytes: bytes, mod_json: dict,
                      entry_blob_start: dict
                      ) -> tuple[bytes, int, int, list[bool]]:
    """Apply legacy byte patches to a copy of vanilla bytes. Resolves
    entry+rel_offset via the BlobStart map (BuffPatcher.find_items output).

    Returns (modded_bytes, applied, skipped, per_change_mask). The mask
    is parallel to the doc's flattened `changes` list (in the order
    produced by `iteminfo_inspector.collect_iteminfo_patches`) and is
    True when that specific patch landed. The Inspector uses the mask to
    tag patches as PATCH_STALE vs PATCH_APPLIED. Skipped = patches whose
    'original' didn't match current bytes — stale mod for different game
    version — plus unsupported change types and malformed hex.
    """
    buf = bytearray(vanilla_bytes)
    applied = 0
    skipped = 0
    mask: list[bool] = []
    for patch in mod_json.get("patches", []):
        gf = (patch.get("game_file") or "").lower()
        if "iteminfo.pabgb" not in gf:
            continue
        for change in patch.get("changes", []):
            ctype = change.get("type", "replace")
            if ctype != "replace":
                skipped += 1
                mask.append(False)
                continue
            def _parse_off(v):
                # JMM format:2 JSON writes offsets as hex strings without a
                # 0x prefix (e.g. "3EEF91"). Older tools sometimes wrote
                # decimal ints. Accept both — try hex first for strings,
                # fall back to decimal.
                if isinstance(v, int):
                    return v
                s = str(v).strip()
                if s.lower().startswith("0x"):
                    return int(s, 16)
                try:
                    return int(s, 16)
                except ValueError:
                    return int(s)

            if "offset" in change:
                off = _parse_off(change["offset"])
            elif "entry" in change and "rel_offset" in change:
                base = entry_blob_start.get(change["entry"])
                if base is None:
                    skipped += 1
                    mask.append(False)
                    continue
                off = base + _parse_off(change["rel_offset"])
            else:
                skipped += 1
                mask.append(False)
                continue
            orig_hex = change.get("original", "")
            new_hex = change.get("patched", "")
            if not new_hex:
                skipped += 1
                mask.append(False)
                continue
            try:
                orig = bytes.fromhex(orig_hex)
                new = bytes.fromhex(new_hex)
            except ValueError:
                skipped += 1
                mask.append(False)
                continue
            if orig and buf[off:off+len(orig)] != orig:
                skipped += 1
                mask.append(False)
                continue
            buf[off:off+len(new)] = new
            applied += 1
            mask.append(True)
    return bytes(buf), applied, skipped, mask


# ============================================================
#   Dict-level merge (field by field)
# ============================================================

def _walk_leaves(d, prefix=""):
    out = []
    if isinstance(d, dict):
        for k, v in d.items():
            p = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict):
                out.extend(_walk_leaves(v, p))
            else:
                out.append((p, v))
    return out

def _get_path(d, path):
    cur = d
    for part in path.split("."):
        if cur is None or not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur

def _set_path(d, path, value):
    parts = path.split(".")
    cur = d
    for p in parts[:-1]:
        if p not in cur or not isinstance(cur[p], dict):
            cur[p] = {}
        cur = cur[p]
    cur[parts[-1]] = value


@dataclass
class FieldConflict:
    entry_key: str
    field_path: str
    winner_mod: str
    loser_mod: str
    winner_value: object
    loser_value: object
    # Optional per-side patch index, populated when the mod is a legacy
    # JSON whose inspector resolved an individual byte patch to this
    # field. Lets the log say "patch #7 in mod A beat patch #3 in mod B
    # for Item_X.cooltime" instead of just naming the mods.
    winner_patch_index: Optional[int] = None
    loser_patch_index: Optional[int] = None


def _merge_entries(vanilla_entry: dict, mod_entries: list[tuple[str, dict]]
                   ) -> tuple[dict, list[FieldConflict]]:
    merged = json.loads(json.dumps(vanilla_entry))
    conflicts: list[FieldConflict] = []
    field_author: dict[str, str] = {}
    for mod_name, mod_entry in mod_entries:
        if mod_entry is None:
            continue
        for path, mod_val in _walk_leaves(mod_entry):
            van_val = _get_path(vanilla_entry, path)
            if mod_val == van_val:
                continue
            prev = field_author.get(path)
            if prev and _get_path(merged, path) != mod_val:
                conflicts.append(FieldConflict(
                    entry_key=str(vanilla_entry.get("string_key") or vanilla_entry.get("key") or "?"),
                    field_path=path, winner_mod=mod_name, loser_mod=prev,
                    winner_value=mod_val,
                    loser_value=_get_path(merged, path),
                ))
            _set_path(merged, path, mod_val)
            field_author[path] = mod_name
    return merged, conflicts


def _attach_patch_provenance(conflicts: list[FieldConflict],
                              mods: list) -> None:
    """Post-merge: look up each conflict's winner/loser mod and, if that
    mod is a legacy JSON with resolved inspections, find the patch index
    that targeted this field. Mutates conflicts in place.

    Handles multiple patches to the same field within one JSON by
    returning the *last* such index (matches last-writer-wins semantics
    of the byte-apply). None if no match."""
    by_name = {m.name: m for m in mods}

    def lookup(mod_name: str, entry_key: str, field_path: str) -> Optional[int]:
        m = by_name.get(mod_name)
        if m is None or m.kind != "legacy_json" or not m.inspections:
            return None
        last_hit: Optional[int] = None
        for insp in m.inspections:
            if (insp.entry == entry_key
                    and insp.field_path == field_path):
                last_hit = insp.index
        return last_hit

    for c in conflicts:
        c.winner_patch_index = lookup(c.winner_mod, c.entry_key, c.field_path)
        c.loser_patch_index = lookup(c.loser_mod, c.entry_key, c.field_path)


def _apply_field_set(target: dict, field_path: str, value) -> bool:
    """Navigate a dot/bracket path and set the value on the target dict."""
    import re
    parts = re.split(r'\.(?![^\[]*\])', field_path)
    obj = target
    for i, part in enumerate(parts[:-1]):
        m = re.match(r'^(.+?)\[(\d+)\]$', part)
        if m:
            key, idx = m.group(1), int(m.group(2))
            obj = obj.get(key, [])
            if not isinstance(obj, list) or idx >= len(obj):
                return False
            obj = obj[idx]
        else:
            obj = obj.get(part)
            if obj is None:
                return False
    last = parts[-1]
    m = re.match(r'^(.+?)\[(\d+)\]$', last)
    if m:
        key, idx = m.group(1), int(m.group(2))
        lst = obj.get(key)
        if isinstance(lst, list):
            while len(lst) <= idx:
                lst.append(None)
            lst[idx] = value
        return True
    if isinstance(obj, dict):
        obj[last] = value
        return True
    return False


def _strip_meta(d: dict) -> dict:
    """Remove internal keys from an item dict for export."""
    return {k: v for k, v in d.items() if k not in ('key', 'string_key')}


def _deep_diff_to_intents(entry: str, key: int, a: dict, b: dict,
                          prefix: str = '') -> list[dict]:
    """Recursively diff two parsed item dicts and emit Format 3 intents."""
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
            intents.extend(_deep_diff_to_intents(entry, key, va, vb, path))
        elif isinstance(va, list) and isinstance(vb, list):
            if len(va) != len(vb) or va != vb:
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


def _diff_skill_to_intents(vanilla_pabgh: bytes, vanilla_pabgb: bytes,
                            modded_pabgh: bytes, modded_pabgb: bytes
                            ) -> list[dict]:
    """Diff modded skill.pabgb against vanilla, emit Field JSON v3 intents.

    SCAFFOLDING — currently DISABLED in _FIELD_JSON_TARGET_REGISTRY.

    Why: the Stacker's local `skillinfo_parser.py` was reverse-engineered from
    IDA and uses raw symbol names (`field_12`, `field_16`, `field_88`, etc.)
    for its struct fields. DMM's apply pipeline reads skill.pabgb via
    `dmm_parser::tables::skill_info::parse_skill_to_json`, which uses the
    Mac-binary-derived clean names (`cooltime`, `learn_level`, `apply_type`,
    `dev_skill_name`, etc.). Intents emitted with `field_12` paths won't
    resolve in DMM.

    To enable:
      1. Map skillinfo_parser.py field names → dmm_parser names (one-time
         work, ~30 fields). Build a `_SKILL_FIELD_NAME_MAP` dict.
      2. In the diff loop below, translate every emitted `field` path
         through the map; skip fields with no mapping.
      3. Flip `enabled: True` in the registry entry.
      4. Round-trip-test against a real skill mod (e.g. one from
         https://www.nexusmods.com/crimsondesert tagged "skill").

    Until that's done, skill mods continue shipping as folder overlays
    (Bucket D of the merger captures skill.pabgb/.pabgh fine; only the
    JSON export path is missing).
    """
    # Stub returns no intents so any accidental call is a no-op rather
    # than a corrupt-export hazard.
    return []


def _diff_blob_table_to_intents(vanilla_pabgh: bytes, vanilla_pabgb: bytes,
                                 modded_pabgh: bytes, modded_pabgb: bytes,
                                 target_label: str) -> list[dict]:
    """Diff a generic pabgh_blob_table file against vanilla.

    BACKEND ONLY — currently DISABLED in _FIELD_JSON_TARGET_REGISTRY because
    the Stacker doesn't merge non-{item,skill,equipslot}info tables today.
    Once merge-side support lands for an additional table, register a new
    entry pointing at this helper and the existing dispatcher handles it.

    The wire format is universal: every pabgh_blob_table-formatted record
    is `[key:u32][string_key:CString][is_blocked:u8][blob:rest]`. The diff
    emits intents for `key` / `string_key` / `is_blocked` changes and a
    base64 `_blob_b64` clone for any record whose blob bytes changed.

    Field paths produced here match exactly what DMM 1.3.3+'s blob fallback
    (apply_v3_to_blob_table_body) consumes — see
    `dmm_parser::tables::blob_runtime::BlobTableRecord::to_json_value`.
    """
    import base64
    import struct

    def _parse_blob_table(pabgh: bytes, pabgb: bytes):
        """Walk a pabgh_blob_table file. Returns {key: (string_key, is_blocked, blob)}."""
        # pabgh: u16 count + N×(u32 key, u32 offset). Sister file boundaries
        # determine record sizes.
        if len(pabgh) < 2:
            return {}
        count = struct.unpack_from('<H', pabgh, 0)[0]
        # Detect 8-byte vs 6-byte entry layout (small minority of tables use u16 keys).
        if 2 + count * 8 == len(pabgh):
            entry_size, key_size, key_fmt = 8, 4, '<I'
        elif 2 + count * 6 == len(pabgh):
            entry_size, key_size, key_fmt = 6, 2, '<H'
        else:
            # Unknown layout. Bail to caller, who'll surface it.
            raise ValueError(f"unknown pabgh layout: count={count}, file={len(pabgh)}B")

        offsets = []
        for i in range(count):
            base = 2 + i * entry_size
            key = struct.unpack_from(key_fmt, pabgh, base)[0]
            off = struct.unpack_from('<I', pabgh, base + key_size)[0]
            offsets.append((key, off))
        offsets.sort(key=lambda kv: kv[1])

        records = {}
        for i, (key, off) in enumerate(offsets):
            end = offsets[i + 1][1] if i + 1 < len(offsets) else len(pabgb)
            # [key u32][string_key cstring][is_blocked u8][blob ...]
            p = off
            _k = struct.unpack_from('<I', pabgb, p)[0]; p += 4
            slen = struct.unpack_from('<I', pabgb, p)[0]; p += 4
            sk = pabgb[p:p + slen].decode('utf-8', errors='replace'); p += slen
            ib = pabgb[p]; p += 1
            blob = pabgb[p:end]
            records[key] = (sk, ib, blob)
        return records

    vanilla = _parse_blob_table(vanilla_pabgh, vanilla_pabgb)
    modded  = _parse_blob_table(modded_pabgh, modded_pabgb)

    intents: list[dict] = []
    for key in sorted(set(vanilla) | set(modded)):
        v = vanilla.get(key)
        m = modded.get(key)
        if not v or not m:
            # Add/remove not yet supported by Field JSON v3 spec.
            continue
        v_sk, v_ib, v_blob = v
        m_sk, m_ib, m_blob = m
        if v_sk != m_sk:
            intents.append({
                'entry': v_sk, 'key': int(key),
                'field': 'string_key', 'op': 'set', 'new': m_sk,
            })
        if v_ib != m_ib:
            intents.append({
                'entry': v_sk, 'key': int(key),
                'field': 'is_blocked', 'op': 'set', 'new': int(m_ib),
            })
        if v_blob != m_blob:
            intents.append({
                'entry': v_sk, 'key': int(key),
                'field': '_blob_b64', 'op': 'set',
                'new': base64.b64encode(m_blob).decode('ascii'),
            })

    return intents


def _diff_table_field_level(vanilla_pabgh: bytes, vanilla_pabgb: bytes,
                            modded_pabgh: bytes, modded_pabgb: bytes,
                            target_label: str) -> tuple[list[dict], str]:
    """Field-level diff using dmm_parser.parse_table (122 supported tables).

    Returns (intents, source) where source is "field" if dmm_parser handled
    the table, "blob" if it fell back to the blob-level diff, or "" if
    nothing could be done.

    For tables that dmm_parser knows the typed schema for (gimmick_info,
    condition_info, drop_set_info, character_info, buff_info, etc.), this
    walks both vanilla and modded record dicts and emits one intent per
    changed field — much finer-grained than `_diff_blob_table_to_intents`
    which replaces entire records via `_blob_b64`.

    The output intent shape matches Field JSON v3:
        {"entry": str, "key": int, "field": "...", "op": "set", "new": ...}

    Field paths use dot notation for nested dicts and `[N]` for list
    indices, matching what DMM 1.3.4+ field-resolver consumes.

    Falls back gracefully:
      - dmm_parser not installed → caller should use blob diff
      - unknown table_name in dmm_parser → caller should use blob diff
      - parse fails on either side → caller should use blob diff

    The caller is `_export_field_json`'s catchall loop (~line 3852).
    """
    try:
        import dmm_parser  # type: ignore[import-not-found]
    except ImportError:
        return ([], "")

    # Strip ".pabgb" to get table_name. dmm_parser.parse_table expects
    # snake_case names matching `src/tables/<name>/`.
    if target_label.endswith(".pabgb"):
        table_name = target_label[:-len(".pabgb")]
    else:
        table_name = target_label

    try:
        vanilla_items = dmm_parser.parse_table(table_name, vanilla_pabgb, vanilla_pabgh)
        modded_items  = dmm_parser.parse_table(table_name, modded_pabgb, modded_pabgh)
    except (ValueError, RuntimeError):
        # Unknown table or parse failure — caller falls back to blob diff.
        return ([], "")

    # Index by entry identity. Most tables key by string_key (entry name);
    # numeric `key` is the stable cross-update fallback. Some tables only
    # have `key` (no string_key), so prefer key as the identity.
    def _identity(item: dict) -> tuple:
        sk = item.get("string_key", "") or ""
        k  = item.get("key")
        return (sk, k)

    v_by_id = {_identity(it): it for it in vanilla_items}
    m_by_id = {_identity(it): it for it in modded_items}

    intents: list[dict] = []
    # Walk only entries present on both sides — record add/remove not
    # supported by Field JSON v3 set op (would need add_entry op).
    for ident in sorted(set(v_by_id) & set(m_by_id), key=lambda x: (x[0] or "", x[1] or 0)):
        v = v_by_id[ident]
        m = m_by_id[ident]
        sk, k = ident
        for path, new_value in _walk_dict_diff(v, m):
            # Skip the identity fields themselves; they're how we found
            # the entry in the first place. Editing string_key/key would
            # change the identity which the v3 resolver can't follow.
            if path in ("string_key", "key"):
                continue
            intents.append({
                "entry": sk or "",
                "key":   int(k) if k is not None else 0,
                "field": path,
                "op":    "set",
                "new":   new_value,
            })

    return (intents, "field")


def _walk_dict_diff(vanilla: dict, modded: dict, prefix: str = ""):
    """Yield (field_path, new_value) for every leaf that differs.

    Path syntax:
      - dotted for nested dicts:           "drop_default_data.use_socket"
      - bracket+index for list elements:   "enchant_data_list[3].level"

    For dict-shaped lists (each element is a dict), recurses element-wise
    when both lists have the same length. If lengths differ, emits one
    intent for the whole list (set to the new full list) — caller just
    replaces the entire list, matching set-op semantics.
    """
    if isinstance(vanilla, dict) and isinstance(modded, dict):
        # Union of keys; vanilla-only or modded-only keys mean field
        # add/remove which the v3 set op handles by replacing the value.
        for key in sorted(set(vanilla) | set(modded)):
            v_val = vanilla.get(key)
            m_val = modded.get(key)
            sub = f"{prefix}.{key}" if prefix else key
            if v_val == m_val:
                continue
            yield from _walk_dict_diff(v_val, m_val, sub)
    elif isinstance(vanilla, list) and isinstance(modded, list):
        if len(vanilla) != len(modded) or not all(
            isinstance(x, (dict, list)) for x in modded
        ):
            # Length change OR list of primitives → emit whole-list set.
            if vanilla != modded:
                yield (prefix, modded)
        else:
            # Equal length, recurse element-wise. Useful for fixed-shape
            # lists like enchant_data_list where each level is its own
            # dict and we want per-level field intents.
            for i, (v_el, m_el) in enumerate(zip(vanilla, modded)):
                if v_el == m_el:
                    continue
                sub = f"{prefix}[{i}]"
                yield from _walk_dict_diff(v_el, m_el, sub)
    else:
        # Leaf — primitive or differing types. Emit a set intent.
        if vanilla != modded:
            yield (prefix, modded)


def _diff_equipslot_to_intents(vanilla_pabgh: bytes, vanilla_pabgb: bytes,
                                modded_pabgh: bytes, modded_pabgb: bytes
                                ) -> list[dict]:
    """Diff modded equipslotinfo against vanilla, emit Field JSON v3 intents.

    Each EquipSlotRecord is keyed by class id (1, 4, 6, 7, 201, 203, 211,
    701, 708, 712, 730, 750, 801 in vanilla 1.04). Per record, walk every
    EquipInfoData entry and compare every editable field. Most universal-
    proficiency-style mods only modify `etl_hashes` (the equip_type_info
    list), but other field changes (category, slot_index, blob, etc.) are
    captured the same way for completeness.

    Requires DMM 1.3.3+ on the consumer side -- that release added the
    equipslotinfo.pabgb v3 target. Earlier DMM versions silently ignore
    these intents (mounted but no effect).
    """
    import equipslotinfo_parser as esp

    vanilla = esp.parse_all(vanilla_pabgh, vanilla_pabgb)
    modded  = esp.parse_all(modded_pabgh, modded_pabgb)
    v_by_key = {r.key: r for r in vanilla}
    m_by_key = {r.key: r for r in modded}

    intents: list[dict] = []
    for key in sorted(set(v_by_key) | set(m_by_key)):
        v = v_by_key.get(key)
        m = m_by_key.get(key)
        # Skip records present in only one set -- record add/remove not
        # supported by Field JSON v3 spec yet (would need add_entry op).
        if not v or not m:
            continue
        # Skip records where entries[] grew/shrank -- spec is set-only,
        # no append/remove. Such mods need to ship as folder overlays.
        if len(v.entries) != len(m.entries):
            continue

        for i, (ve, me) in enumerate(zip(v.entries, m.entries)):
            # etl_hashes -- the unlock vector (universal proficiency etc.)
            if list(ve.etl_hashes) != list(me.etl_hashes):
                intents.append({
                    'entry': '', 'key': int(key),
                    'field': f'entries[{i}].etl_hashes',
                    'op': 'set',
                    'new': [int(h) for h in me.etl_hashes],
                })
            # Scalar fields -- only emitted when value changed so we don't
            # bloat the export with no-op intents.
            for fname in ('category_a', 'category_b', 'name_hash',
                          'slot_index', 'field_u64', 'name_hash_2',
                          'complex_u8', 'complex_u64'):
                vv = getattr(ve, fname)
                mv = getattr(me, fname)
                if vv != mv:
                    intents.append({
                        'entry': '', 'key': int(key),
                        'field': f'entries[{i}].{fname}',
                        'op': 'set',
                        'new': int(mv),
                    })
            # fields_u32 -- fixed [u32; 4] array
            if list(ve.fields_u32) != list(me.fields_u32):
                intents.append({
                    'entry': '', 'key': int(key),
                    'field': f'entries[{i}].fields_u32',
                    'op': 'set',
                    'new': [int(x) for x in me.fields_u32],
                })

    return intents


# ── Late-bind diff_fn references in the registry now that the helpers exist.
# (The registry constant is declared near the top of the module for visibility,
# but Python doesn't allow forward-referencing names there. This binds the
# functions in once they're defined.)
for _entry in _FIELD_JSON_TARGET_REGISTRY:
    if _entry['name'] == 'equipslotinfo.pabgb':
        _entry['diff_fn'] = _diff_equipslot_to_intents
    elif _entry['name'] == 'skill.pabgb':
        _entry['diff_fn'] = _diff_skill_to_intents
del _entry


# ============================================================
#   Texture mod export — folder-mod backend
# ============================================================
#
# TODO(frontend): wire this up to a "📦 EXPORT TEXTURE MOD" button in the
# Stacker GUI. The dialog should let the user:
#   1. Drag .dds files into a list (or pick from a folder)
#   2. Per file: edit the destination vpath (auto-suggest from PATHC lookup
#      against vanilla_dumps/)
#   3. Edit mod metadata (title / author / version / description)
#   4. Click "Export" → call _build_texture_mod_folder() → show success
#      dialog with "Open in Explorer" + "Copy to DMM mods folder" actions
#
# The output is a DMM-compatible folder mod (manifest.json + files/<group>/
# <vpath>). DMM's existing browser/folder-mod pipeline picks it up with no
# additional changes — the texture injection path (DDS → PAZ overlay via
# meta/0.pathc lookup) is already production-grade.
#
# This backend is intentionally GUI-agnostic so it can also be called from:
#   - A future DDS batch converter that auto-resizes / reformats textures
#   - A bulk import-and-export pipeline ("convert this folder of PNGs to DDS
#     and export as a mod")
#   - Programmatic callers (CLI, scripted bulk operations)


def _build_texture_mod_folder(
    out_dir: str,
    mod_name: str,
    textures: list[tuple[str, str]],
    *,
    title: str = "",
    author: str = "CrimsonGameMods Stacker",
    version: str = "1.0",
    description: str = "",
) -> str:
    """Produce a DMM-compatible folder texture mod.

    Args:
        out_dir:   parent directory the mod folder is created under
        mod_name:  folder name to create (also default title if `title` empty)
        textures:  list of (source_dds_path, vpath) tuples. `vpath` is the
                   slash-separated path inside the game's PAZ archive
                   STARTING with the PAZ group number. Examples:
                       "0012/ui/texture/cd_icon_common_01.dds"
                       "0009/character/texture/macduff/diffuse.dds"
        title:     optional manifest title (defaults to mod_name)
        author:    manifest author
        version:   manifest version (SemVer-ish)
        description: free-form description shown in DMM's mod list

    Returns:
        Absolute path to the created mod folder.

    Raises:
        ValueError: if `textures` is empty or any source file is missing
        OSError:    on filesystem errors during write

    DMM consumption path:
        manifest-free folder mods are auto-detected by DMM as long as the
        `files/` tree is present and the first sub-folder is a 4-digit PAZ
        group number. Including manifest.json gives DMM nicer mod-list
        metadata (title, version, author, description); leaving it out is
        also fine. We ALWAYS write the manifest here so authors get a
        polished mod-list entry without thinking about it.

    Output structure:
        <out_dir>/<mod_name>/
            manifest.json
            files/
                <vpath of texture #1>
                <vpath of texture #2>
                ...

    File writing strategy: copy-not-symlink so the mod folder is
    self-contained and can be zipped + shipped without follow-up.
    """
    import shutil

    if not textures:
        raise ValueError("textures list is empty — nothing to export")

    # Validate every source up front; partial mods are confusing.
    for i, (src, vpath) in enumerate(textures):
        if not os.path.isfile(src):
            raise ValueError(
                f"texture[{i}] source file not found: {src}")
        if not vpath or not vpath.lower().endswith('.dds'):
            raise ValueError(
                f"texture[{i}] vpath must end in .dds (got: {vpath!r})")
        # Quick PAZ-group-prefix sanity. The first segment of vpath should
        # be a 4-digit numeric group like "0009" / "0012".
        first_seg = vpath.replace('\\', '/').split('/', 1)[0]
        if not (first_seg.isdigit() and len(first_seg) == 4):
            raise ValueError(
                f"texture[{i}] vpath should START with a 4-digit PAZ group "
                f"(got first segment {first_seg!r}). Common groups: "
                f"0009 (character), 0012 (UI), 0014 (level data)")

    mod_dir = os.path.join(out_dir, mod_name)
    files_dir = os.path.join(mod_dir, "files")
    os.makedirs(files_dir, exist_ok=True)

    # Write each texture into files/<vpath>
    written: list[str] = []
    for src, vpath in textures:
        norm_vpath = vpath.replace('\\', '/').lstrip('/')
        dst = os.path.join(files_dir, *norm_vpath.split('/'))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        written.append(norm_vpath)

    # Manifest. DMM reads `id`/`name`/`version`/`author`/`description` for
    # the mod-list display; other fields are optional.
    manifest = {
        "id": f"com.crimsongamemods.texture.{mod_name.lower().replace(' ', '_')}",
        "name": title or mod_name,
        "version": version,
        "author": author,
        "description": description or (
            f"{len(written)} texture replacement(s) — built by "
            f"CrimsonGameMods Stacker"),
        "format": "folder_mod",
        "_built_by": "CrimsonGameMods Stacker (texture export backend)",
        "_texture_count": len(written),
    }
    with open(os.path.join(mod_dir, "manifest.json"), 'w',
              encoding='utf-8') as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    return mod_dir


def _merge_all(vanilla_items: list[dict],
               mod_lists: list[tuple[str, list[dict]]]
               ) -> tuple[list[dict], list[FieldConflict]]:
    def idx(items): return {it["key"]: it for it in items if "key" in it}
    v_idx = idx(vanilla_items)
    mod_idxs = [(n, idx(items)) for n, items in mod_lists]
    all_keys = set(v_idx.keys())
    for _, m in mod_idxs:
        all_keys.update(m.keys())

    merged_items: list[dict] = []
    conflicts: list[FieldConflict] = []
    for key in sorted(all_keys):
        v = v_idx.get(key)
        mod_entries = [(n, m.get(key)) for n, m in mod_idxs]
        present = [(n, e) for n, e in mod_entries if e is not None]
        if v is None and len(present) == 1:
            merged_items.append(present[0][1])
            continue
        if v is None:
            merged_items.append(present[0][1])
            continue
        merged, confs = _merge_entries(v, mod_entries)
        merged_items.append(merged)
        conflicts.extend(confs)
    return merged_items, conflicts


# ============================================================
#   Drop zone
# ============================================================

class DropZone(QLabel):
    def __init__(self, on_files):
        super().__init__(
            "\n  Drag iteminfo mods here  \n"
            "  (folders with NNNN/0.paz, .pabgb files, or legacy .json mods)  \n")
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumHeight(110)
        self.setStyleSheet("""
            QLabel {
                border: 2px dashed #888; border-radius: 8px;
                font-size: 13px; color: #ccc; background: #2a2a2a;
            }
            QLabel:hover { border-color: #4080FF; color: #fff; }
        """)
        self.setAcceptDrops(True)
        self._cb = on_files

    def dragEnterEvent(self, e: QDragEnterEvent) -> None:
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e: QDropEvent) -> None:
        paths = [u.toLocalFile() for u in e.mimeData().urls()]
        self._cb(paths)


# ============================================================
#   StackerTab
# ============================================================

class StackerTab(QWidget):
    status_message = Signal(str)
    config_save_requested = Signal()

    def __init__(self, name_db=None, icon_cache=None, config=None,
                 show_guide_fn=None, buffs_tab=None, parent=None):
        super().__init__(parent)
        self._name_db = name_db
        self._icon_cache = icon_cache
        self._config = config if config is not None else {}
        self._show_guide_fn = show_guide_fn
        self._buffs_tab = buffs_tab  # for shared state + Apply to Game
        self._mod_tabs: dict = {}  # populated by main_window after all tabs created
        self._game_path: str = self._config.get("game_install_path", "")
        self._mods: list[ModEntry] = []
        self._merged_items: list = []
        # Per-target merged-bytes captures populated during _run. Each holds
        # `{filename: bytes}` for the table's pabgb + pabgh sister files.
        # Read by _export_field_json via the names declared in the
        # _FIELD_JSON_TARGET_REGISTRY entries' `merged_attr` fields.
        #
        # _merged_equip_files: ENABLED in registry (1.1.4+) — universal
        #     weapon proficiency / Universal Proficiency v2 / tribe edits.
        # _merged_skill_files: SCAFFOLDED in registry (disabled). Captured
        #     here so the diff helper has data the moment its registry
        #     entry flips to enabled (after the field-name map is built).
        # _merged_other_files: catch-all for future non-{item,equip,skill}
        #     blob-table targets. Currently empty — Stacker doesn't merge
        #     non-{iteminfo, skill, equipslot} tables yet.
        self._merged_equip_files: dict = {}
        self._merged_skill_files: dict = {}
        self._merged_other_files: dict = {}
        self._conflicts: list[FieldConflict] = []
        self._build_ui()

    def set_game_path(self, path: str) -> None:
        self._game_path = path or ""
        if hasattr(self, "_game_edit"):
            self._game_edit.setText(self._game_path)

    # ------------------------------------------------------------
    # Styling palette — matches JMM's dark look (MainWindow.xaml).
    # Colours are hardcoded so the tab stays consistent across themes
    # and does not inherit whatever the rest of the app is rendering in.
    _BG        = "#111218"
    _PANEL_BG  = "#1A1D24"
    _PANEL_HDR = "#1A1D28"
    _BORDER    = "#2A3040"
    _ROW_ALT   = "#16181F"
    _TXT       = "#C8CCD8"
    _TXT_DIM   = "#8C91A0"
    _TXT_MUTED = "#5A6070"
    _ACCENT_G  = "#32B450"   # green — active / success
    _ACCENT_R  = "#C83232"   # red   — destructive
    _ACCENT_B  = "#3C78DC"   # blue  — info / export
    _ACCENT_O  = "#DC961E"   # orange — warn / filter

    def _build_ui(self):
        """3-column layout, JMM-style.

        Column 1 — SOURCES: list of all dropped mods + the ItemBuffs
        snapshot. Each row has a toggle (enable/disable), name, type tag,
        and a delete button. Disabled rows are skipped in the merge but
        stay visible.

        Column 2 — DETAILS: info about the currently-selected source
        (kind, path, entry count, staged sibling files, conflict list
        after Preview/Apply). Replaces the old standalone conflicts
        table — conflicts now show inline with the source that won.

        Column 3 — LOG: monospace progress log, unchanged semantics.

        Top strip: header + game path + dropzone.
        Bottom strip: primary action buttons (Preview / Apply / Export)
        and secondary actions (Pull / Remove Stack / Hand off).
        """
        self.setMinimumWidth(900)
        self.setMinimumHeight(560)
        self.setStyleSheet(self._build_qss())

        v = QVBoxLayout(self)
        v.setContentsMargins(10, 10, 10, 10)
        v.setSpacing(8)

        # --- Top strip: title --------------------------------------------
        title_row = QHBoxLayout()
        title = QLabel("STACKER TOOL")
        title.setObjectName("stackerTitle")
        subtitle = QLabel("Merge N iteminfo.pabgb mods into ONE clean overlay")
        subtitle.setObjectName("stackerSubtitle")
        title_row.addWidget(title)
        title_row.addSpacing(10)
        title_row.addWidget(subtitle)
        title_row.addStretch(1)
        v.addLayout(title_row)

        # --- Game dir row (flat bar, JMM-style) ---------------------------
        game_bar = QFrame()
        game_bar.setObjectName("gameBar")
        gbl = QHBoxLayout(game_bar)
        gbl.setContentsMargins(10, 5, 5, 5)
        gbl.setSpacing(6)
        gp_lbl = QLabel("GAME DIR")
        gp_lbl.setObjectName("gameLbl")
        gbl.addWidget(gp_lbl)
        self._game_edit = QLineEdit(self._game_path)
        self._game_edit.setPlaceholderText(
            r"C:\Program Files (x86)\Steam\steamapps\common\Crimson Desert")
        self._game_edit.setObjectName("gameEdit")
        self._game_edit.setMinimumHeight(26)
        gbl.addWidget(self._game_edit, stretch=1)
        browse = _size_button(QPushButton("Browse"))
        browse.setObjectName("flatBtn")
        browse.clicked.connect(self._pick_game_dir)
        gbl.addWidget(browse)
        v.addWidget(game_bar)

        # --- Drop zone --------------------------------------------------
        self._drop = DropZone(self._add_files)
        v.addWidget(self._drop)

        # --- Main 3-column splitter ------------------------------------
        main = QSplitter(Qt.Horizontal)
        main.setChildrenCollapsible(False)
        main.setHandleWidth(6)
        main.setObjectName("mainSplitter")

        # Column 1: Sources
        col1 = self._build_sources_panel()
        main.addWidget(col1)

        # Column 2: Details / Conflicts
        col2 = self._build_details_panel()
        main.addWidget(col2)

        # Column 3: Log
        col3 = self._build_log_panel()
        main.addWidget(col3)

        main.setStretchFactor(0, 3)
        main.setStretchFactor(1, 3)
        main.setStretchFactor(2, 2)
        main.setSizes([360, 360, 260])
        v.addWidget(main, stretch=1)

        # --- Bottom action bar (JMM-style rounded pills) ---------------
        bottom = QFrame()
        bottom.setObjectName("bottomBar")
        bl = QHBoxLayout(bottom)
        bl.setContentsMargins(8, 6, 8, 6)
        bl.setSpacing(8)

        self._preview_btn = _size_button(QPushButton("PREVIEW"))
        self._preview_btn.setObjectName("btnNeutral")
        self._preview_btn.setToolTip(
            "Merge all enabled sources in memory, show conflicts, write nothing.")
        self._preview_btn.clicked.connect(lambda: self._run(install=False))
        bl.addWidget(self._preview_btn)

        self._install_btn = _size_button(QPushButton("✔ APPLY STACK"))
        self._install_btn.setObjectName("btnPrimary")
        self._install_btn.setToolTip(
            f"Merge all enabled sources and write one overlay at "
            f"<game>/{OVERLAY_GROUP_DEFAULT}/.")
        self._install_btn.clicked.connect(lambda: self._run(install=True))
        bl.addWidget(self._install_btn)

        self._install_single_btn = _size_button(QPushButton("✔ APPLY SINGLE"))
        self._install_single_btn.setObjectName("btnPrimary")
        self._install_single_btn.setToolTip(
            "Merge all sources and write to a specific overlay folder #.\n"
            "Use this to deploy directly into an existing overlay slot\n"
            "(e.g. 0058) so it replaces instead of conflicting.")
        self._install_single_btn.clicked.connect(self._apply_single_stack)
        bl.addWidget(self._install_single_btn)

        self._export_btn = _size_button(QPushButton("📦 EXPORT MOD"))
        self._export_btn.setObjectName("btnExport")
        self._export_btn.setToolTip(
            "Merge all enabled sources and write a standard compiled folder "
            "mod to a path you pick. Does NOT touch the game folder.")
        self._export_btn.clicked.connect(lambda: self._run(install=False, export=True))
        bl.addWidget(self._export_btn)

        self._export_field_btn = _size_button(QPushButton("📄 EXPORT FIELD JSON"))
        self._export_field_btn.setObjectName("btnExport")
        self._export_field_btn.setToolTip(
            "Export the merged result as a Format 3 semantic JSON file.\n"
            "Uses field names instead of byte offsets — survives game updates.\n"
            "Run PREVIEW first, then click this to export the diff.")
        self._export_field_btn.clicked.connect(self._export_field_json)
        bl.addWidget(self._export_field_btn)

        self._export_legacy_btn = _size_button(QPushButton("📄 EXPORT LEGACY JSON"))
        self._export_legacy_btn.setObjectName("btnExport")
        self._export_legacy_btn.setToolTip(
            "Export the merged result as a Format 2 byte-diff JSON.\n"
            "Compatible with legacy mod loaders (CrimsonWings style).\n"
            "Run PREVIEW first, then click this to export the diff.")
        self._export_legacy_btn.clicked.connect(self._export_legacy_json)
        bl.addWidget(self._export_legacy_btn)

        bl.addStretch(1)

        self._uninstall_btn = _size_button(QPushButton("✖ REMOVE STACK"))
        self._uninstall_btn.setObjectName("btnDestructive")
        self._uninstall_btn.setToolTip(
            f"Delete <game>/{OVERLAY_GROUP_DEFAULT}/ and drop its PAPGT entry. "
            "Does not touch JMM/ItemBuffs overlays.")
        self._uninstall_btn.clicked.connect(self._uninstall_stack)
        bl.addWidget(self._uninstall_btn)

        self._send_buffs_btn = _size_button(QPushButton("→ Push to ItemBuff"))
        self._send_buffs_btn.setObjectName("flatBtn")
        self._send_buffs_btn.setStyleSheet(
            "QPushButton { background-color: #2E7D32; color: white; font-weight: bold; }")
        self._send_buffs_btn.setToolTip(
            "Load the merged dict list into ItemBuffs for hand-tuning.")
        self._send_buffs_btn.clicked.connect(self._send_to_buffs)
        bl.addWidget(self._send_buffs_btn)

        v.addWidget(bottom)

    # ------------------------------------------------------------
    def _build_qss(self) -> str:
        """Inline stylesheet — matches JMM's dark look. Scoped via object
        names so it doesn't leak to sibling tabs."""
        return f"""
        StackerTab {{
            background: {self._BG};
            color: {self._TXT};
        }}
        QLabel#stackerTitle {{
            color: {self._ACCENT_R};
            font-family: "Segoe UI";
            font-size: 16px;
            font-weight: bold;
            padding: 2px 4px;
        }}
        QLabel#stackerSubtitle {{
            color: {self._TXT_DIM};
            font-size: 11px;
            padding: 2px 4px;
        }}
        QFrame#gameBar, QFrame#bottomBar {{
            background: {self._PANEL_HDR};
            border: 1px solid {self._BORDER};
            border-radius: 5px;
        }}
        QLabel#gameLbl {{
            color: {self._ACCENT_O};
            font-size: 10px;
            font-weight: bold;
            padding-right: 4px;
        }}
        QLineEdit#gameEdit {{
            background: {self._BG};
            color: {self._ACCENT_G};
            border: 1px solid {self._BORDER};
            border-radius: 4px;
            padding: 4px 8px;
            selection-background-color: #2A3050;
        }}
        QSplitter#mainSplitter::handle {{
            background: {self._BORDER};
        }}
        QFrame#sourcesPanel, QFrame#detailsPanel, QFrame#logPanel {{
            background: {self._PANEL_BG};
            border: 1px solid {self._BORDER};
            border-radius: 6px;
        }}
        QLabel[role="panelHeader"] {{
            background: {self._PANEL_HDR};
            color: {self._TXT_DIM};
            font-size: 11px;
            font-weight: bold;
            padding: 7px 10px;
            border: none;
            border-top-left-radius: 6px;
            border-top-right-radius: 6px;
        }}
        QLabel#patchesHeader {{
            color: {self._ACCENT_O};
        }}
        QLabel#logHeader {{
            color: {self._TXT_MUTED};
        }}
        QListWidget#sourcesList {{
            background: {self._PANEL_BG};
            color: {self._TXT};
            border: none;
            outline: 0;
            font-family: "Segoe UI";
            font-size: 13px;
        }}
        QListWidget#sourcesList::item {{
            border-bottom: 1px solid #252838;
            padding: 0;
        }}
        QListWidget#sourcesList::item:selected {{
            background: #242838;
            border-left: 3px solid {self._ACCENT_B};
        }}
        QListWidget#sourcesList::item:hover {{
            background: #1E2230;
        }}
        QTextEdit#detailsText, QTextEdit#logText {{
            background: #14161E;
            color: {self._TXT_DIM};
            border: none;
            font-family: "Consolas", "Cascadia Code", monospace;
            font-size: 12px;
            padding: 6px;
        }}
        QPushButton {{
            font-family: "Segoe UI";
            font-size: 12px;
            font-weight: 600;
            border-radius: 4px;
            padding: 6px 14px;
            background: #28303E;
            color: {self._TXT};
            border: 1px solid #30384A;
        }}
        QPushButton:hover {{ background: #343C4E; }}
        QPushButton:pressed {{ background: #1E2430; }}
        QPushButton:disabled {{ background: #202430; color: #555; border-color: #2A3040; }}
        QPushButton#flatBtn {{
            background: #1A1D2A;
            color: {self._TXT_DIM};
            border: 1px solid #2A3040;
        }}
        QPushButton#flatBtn:hover {{ background: #2C3040; color: {self._TXT}; }}
        QPushButton#btnNeutral {{
            background: #464B5A;
            color: #FFFFFF;
            border: 1px solid #565B6A;
            padding: 7px 16px;
        }}
        QPushButton#btnPrimary {{
            background: {self._ACCENT_G};
            color: #0E1018;
            border: 1px solid #42C460;
            font-weight: bold;
            padding: 7px 18px;
        }}
        QPushButton#btnPrimary:hover {{ background: #42C460; }}
        QPushButton#btnExport {{
            background: {self._ACCENT_B};
            color: #FFFFFF;
            border: 1px solid #4C88EC;
            font-weight: bold;
            padding: 7px 18px;
        }}
        QPushButton#btnExport:hover {{ background: #4C88EC; }}
        QPushButton#btnDestructive {{
            background: {self._ACCENT_R};
            color: #FFFFFF;
            border: 1px solid #D84242;
            padding: 7px 14px;
        }}
        QPushButton#btnDestructive:hover {{ background: #D84242; }}
        QToolButton#rowDelete {{
            background: #2A2030;
            color: {self._TXT_DIM};
            border: 1px solid #3A3E52;
            border-radius: 3px;
            padding: 2px 8px;
            font-weight: bold;
        }}
        QToolButton#rowDelete:hover {{
            background: #6A2030;
            color: #FF6060;
        }}
        QCheckBox {{ color: {self._TXT}; }}
        QCheckBox::indicator {{
            width: 16px; height: 16px;
        }}
        """

    # ------------------------------------------------------------
    def _panel_header(self, text: str, obj_name: str = "") -> QLabel:
        """A JMM-style panel header row — flat, bold small-caps."""
        lbl = QLabel(text)
        lbl.setProperty("role", "panelHeader")
        if obj_name:
            lbl.setObjectName(obj_name)
        return lbl

    # ------------------------------------------------------------
    def _build_sources_panel(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("sourcesPanel")
        frame.setMinimumWidth(280)
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        header = self._panel_header("SOURCES  —  drag mods above or use Add")
        lay.addWidget(header)

        self._mods_list = QListWidget()
        self._mods_list.setObjectName("sourcesList")
        self._mods_list.setSelectionMode(QAbstractItemView.SingleSelection)
        self._mods_list.setAlternatingRowColors(False)
        self._mods_list.setUniformItemSizes(False)
        self._mods_list.currentRowChanged.connect(self._on_source_selected)
        lay.addWidget(self._mods_list, stretch=1)

        # Toolbar at the bottom of the sources panel
        bar = QFrame()
        bar.setStyleSheet(
            f"QFrame {{ background: {self._PANEL_HDR}; "
            f"border-bottom-left-radius: 6px; border-bottom-right-radius: 6px; }}")
        blay = QHBoxLayout(bar)
        blay.setContentsMargins(6, 6, 6, 6)
        blay.setSpacing(4)

        add_btn = _size_button(QPushButton("+ Add"), extra_px=44)
        add_btn.setStyleSheet(
            "QPushButton { background-color: #2E7D32; color: white; "
            "font-weight: bold; font-size: 14px; padding: 6px 18px; }")
        add_btn.clicked.connect(self._pick_files)
        blay.addWidget(add_btn)

        pull_btn = _size_button(QPushButton("⇅ Pull ItemBuffs Edit"))
        pull_btn.setObjectName("flatBtn")
        pull_btn.setStyleSheet(
            "QPushButton { background-color: #B71C1C; color: white; font-weight: bold; }")
        pull_btn.setToolTip(
            "Snapshot the current ItemBuffs tab edits (dict edits, Max Stacks, "
            "Inf Durability, cooldowns, transmog, VFX, staged skill/equipslot "
            "files) as a stack source. Clicking again replaces the prior snapshot.")
        pull_btn.clicked.connect(self._pull_from_itembuffs)
        blay.addWidget(pull_btn)

        pull_all_btn = _size_button(QPushButton("⇅ Pull All Edits"))
        pull_all_btn.setObjectName("flatBtn")
        pull_all_btn.setStyleSheet(
            "QPushButton { background-color: #00695C; color: white; font-weight: bold; }")
        pull_all_btn.setToolTip(
            "Pull edits from ALL tabs: ItemBuffs + MercPets + SkillTree + "
            "BagSpace + ReserveSlot + FieldEdit. Creates one mod entry per "
            "tab that has modifications.")
        pull_all_btn.clicked.connect(self._pull_all_edits)
        blay.addWidget(pull_all_btn)

        pull_dmm_btn = _size_button(QPushButton("⇅ Pull DMM"))
        pull_dmm_btn.setObjectName("flatBtn")
        pull_dmm_btn.setToolTip(
            "Import all enabled DMM mods that target iteminfo.pabgb. "
            "Runs the Stacker Inspector to convert byte patches to "
            "field-level edits, then adds them as stack sources for "
            "unified merging.")
        pull_dmm_btn.clicked.connect(self._pull_from_dmm)
        pull_dmm_btn.setVisible(False)

        blay.addStretch(1)

        rm_btn = _size_button(QPushButton("Remove"))
        rm_btn.setObjectName("flatBtn")
        rm_btn.clicked.connect(self._remove_selected)
        blay.addWidget(rm_btn)

        clr_btn = _size_button(QPushButton("Clear"))
        clr_btn.setObjectName("flatBtn")
        clr_btn.clicked.connect(self._clear_all)
        blay.addWidget(clr_btn)

        lay.addWidget(bar)
        return frame

    # ------------------------------------------------------------
    def _build_details_panel(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("detailsPanel")
        frame.setMinimumWidth(260)
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        lay.addWidget(self._panel_header(
            "DETAILS  —  selected source + merge conflicts",
            obj_name="patchesHeader"))

        self._details = QTextEdit()
        self._details.setObjectName("detailsText")
        self._details.setReadOnly(True)
        self._details.setLineWrapMode(QTextEdit.WidgetWidth)
        self._details.setPlainText(
            "Select a source in the left panel to see its details here.\n\n"
            "After clicking Preview or Apply Stack, this panel shows any\n"
            "field-level conflicts between sources and which one won.")
        lay.addWidget(self._details, stretch=1)
        return frame

    # ------------------------------------------------------------
    def _build_log_panel(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("logPanel")
        frame.setMinimumWidth(220)
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        lay.addWidget(self._panel_header("LOG", obj_name="logHeader"))

        self._log = QTextEdit()
        self._log.setObjectName("logText")
        self._log.setReadOnly(True)
        self._log.setLineWrapMode(QTextEdit.NoWrap)
        lay.addWidget(self._log, stretch=1)
        return frame

    # ------------------------------------------------------------
    def _pick_game_dir(self):
        d = QFileDialog.getExistingDirectory(
            self, "Pick Crimson Desert game folder", self._game_edit.text() or "")
        if d:
            self._game_edit.setText(d)
            self._config["game_install_path"] = d
            self.config_save_requested.emit()

    def _pick_files(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Pick mod files",
            "", "Mod files (*.json *.pabgb);;All files (*.*)")
        if paths:
            self._add_files(paths)

    def _add_files(self, paths: list[str]):
        for p in paths:
            entry = _classify(p)
            self._mods.append(entry)
            self._append_mod_row(entry)

    def _pull_from_itembuffs(self):
        """Snapshot the full ItemBuffs tab state — all four buckets.

        A: _buff_rust_items (dict-level edits)
        B: _stack_check state + _stack_spin value, _inf_dura_check state
        C: _cd_patches, _transmog_swaps, _vfx_* lists (post-serialize byte patches)
        D: _staged_skill_files, _staged_equip_files (sibling files for the
           same overlay PAZ — skill.pabgb/pabgh + equipslotinfo.pabgb/pabgh)

        Replaces any prior itembuffs snapshot so repeated clicks refresh
        rather than accumulate.
        """
        if self._buffs_tab is None:
            QMessageBox.warning(self, "Stacker Tool",
                "ItemBuffs tab not available.")
            return
        items = getattr(self._buffs_tab, "_buff_rust_items", None)
        if not items:
            QMessageBox.information(self, "Stacker Tool",
                "ItemBuffs has no items loaded yet. Open ItemBuffs, click "
                "Extract Iteminfo, make your edits, then come back and click "
                "Pull again.")
            return

        bt = self._buffs_tab

        # Drop any previous itembuffs snapshot so we don't double-count
        self._mods = [m for m in self._mods if m.kind != "itembuffs_edits"]

        # Bucket A — deep copy the parsed dict list so later ItemBuffs
        # edits don't mutate our capture.
        snap_items = copy.deepcopy(items)

        # Bucket B — checkbox toggles. These are only applied to
        # _buff_rust_items at the moment ItemBuffs's Apply runs. We
        # capture them separately so our merge can apply them before
        # serialize, regardless of whether ItemBuffs itself has run Apply.
        apply_stacks_val: Optional[int] = None
        if (hasattr(bt, "_stack_check") and bt._stack_check is not None
                and bt._stack_check.isChecked()
                and hasattr(bt, "_stack_spin") and bt._stack_spin is not None):
            apply_stacks_val = bt._stack_spin.value()

        apply_inf_dura_val: bool = (
            hasattr(bt, "_inf_dura_check") and bt._inf_dura_check is not None
            and bt._inf_dura_check.isChecked())

        # Bucket C — post-serialize byte patches. Deep-copy to decouple.
        cd_snap = copy.deepcopy(getattr(bt, "_cd_patches", {}) or {})
        transmog_snap = copy.deepcopy(getattr(bt, "_transmog_swaps", []) or [])
        vfx_size_snap = copy.deepcopy(getattr(bt, "_vfx_size_changes", []) or [])
        vfx_swap_snap = copy.deepcopy(getattr(bt, "_vfx_swaps", []) or [])
        vfx_anim_snap = copy.deepcopy(getattr(bt, "_vfx_anim_swaps", []) or [])
        vfx_attach_snap = copy.deepcopy(getattr(bt, "_vfx_attach_changes", []) or [])

        # Bucket D — sibling file bytes. These are already raw bytes in
        # ItemBuffs's state, so a shallow copy of the dict is enough;
        # the bytes objects themselves are immutable.
        staged_skill_snap = dict(getattr(bt, "_staged_skill_files", None) or {})
        staged_equip_snap = dict(getattr(bt, "_staged_equip_files", None) or {})

        # Scan the dict so we can tell the user *what* is in the 6024
        # entries — not just "6024 entries". Surfaces UP v2 tribe edits,
        # Make Dyeable, No Cooldown (dict-level, not _cd_patches), etc.
        feats = _detect_dict_features(snap_items)

        # ------------------------------------------------------------
        # UP v2 equipslotinfo auto-stage fallback.
        #
        # _eb_enable_everything_oneclick and _eb_universal_proficiency_v2
        # both stage equipslotinfo into _staged_equip_files — BUT their
        # equipslotinfo serializer is wrapped in a try/except that
        # silently fails if crimson_rs.extract_file can't find 0008
        # (wrong game path, missing install, read error). Users who hit
        # that path get tribe_gender edits in the dict but no
        # equipslotinfo bundle, which means the game rejects the UP v2
        # feature in-game even though the dict *looks* right.
        #
        # At Pull time: if the dict has UP v2 tribe edits AND staged
        # equipslotinfo is missing, re-run the expansion here from the
        # Stacker's game path. Best-effort — logged but not fatal.
        if feats.get('up_v2_tribe_unioned') and (
                'equipslotinfo.pabgb' not in staged_equip_snap
                or 'equipslotinfo.pabgh' not in staged_equip_snap):
            try:
                generated = self._regenerate_equipslotinfo_for_up_v2()
                if generated:
                    staged_equip_snap.update(generated)
                    self._log_line(
                        f"  ℹ UP v2 tribe edits detected without equipslotinfo "
                        f"bundle — re-staged inline ({len(generated)} files).")
            except Exception as e:
                self._log_line(
                    f"  ⚠ UP v2 tribe edits detected but equipslotinfo stage "
                    f"failed: {e}. Fix game path and click Extract + "
                    f"UP v2 in ItemBuffs, then re-Pull.")

        # Build a readable summary for the Sources table so the user can
        # verify Pull captured what they expect.
        summary_bits = [f"{len(snap_items)} entries"]
        if apply_stacks_val is not None:
            summary_bits.append(f"stacks→{apply_stacks_val}")
        if apply_inf_dura_val:
            summary_bits.append("inf-dura toggle")
        feat_summary = _compact_features_summary(feats)
        if feat_summary:
            summary_bits.append(feat_summary)
        if cd_snap:
            summary_bits.append(f"{len(cd_snap)} cooldown byte-patches")
        if transmog_snap:
            summary_bits.append(f"{len(transmog_snap)} transmog swaps")
        vfx_total = (len(vfx_size_snap) + len(vfx_swap_snap)
                     + len(vfx_anim_snap) + len(vfx_attach_snap))
        if vfx_total:
            summary_bits.append(f"{vfx_total} VFX changes")
        if staged_skill_snap:
            summary_bits.append(
                f"skill bundle ({len(staged_skill_snap)} "
                f"file{'s' if len(staged_skill_snap) != 1 else ''})")
        if staged_equip_snap:
            summary_bits.append(
                f"equipslotinfo bundle ({len(staged_equip_snap)} "
                f"file{'s' if len(staged_equip_snap) != 1 else ''})")
        summary = "; ".join(summary_bits)

        entry = ModEntry(
            name="ItemBuffs tab (current edits)",
            path="<in-memory>",
            kind="itembuffs_edits",
            ok=True,
            note=summary,
            parsed_items=snap_items,
            apply_stats="snapshot captured",
            detected_features=feats,
            apply_stacks=apply_stacks_val,
            apply_inf_dura=apply_inf_dura_val,
            cd_patches=cd_snap,
            transmog_swaps=transmog_snap,
            vfx_size_changes=vfx_size_snap,
            vfx_swaps=vfx_swap_snap,
            vfx_anim_swaps=vfx_anim_snap,
            vfx_attach_changes=vfx_attach_snap,
            staged_skill_files=staged_skill_snap,
            staged_equip_files=staged_equip_snap,
        )
        self._mods.append(entry)
        self._refresh_mod_list()
        self._log_line(f"✓ Pulled from ItemBuffs: {summary}.")

    # ------------------------------------------------------------
    def _pull_all_edits(self):
        """Pull edits from ALL tabs that have modifications."""
        pulled = []

        if self._buffs_tab is not None:
            has_items = bool(getattr(self._buffs_tab, "_buff_rust_items", None))
            if has_items:
                self._pull_from_itembuffs()
                pulled.append("ItemBuffs")

        _TAB_LABELS = {
            "mercpets": "MercPets",
            "bagspace": "BagSpace",
            "skilltree": "SkillTree",
            "reserveslot": "ReserveSlot",
            "fieldedit": "FieldEdit",
        }

        for tab_key, tab_obj in self._mod_tabs.items():
            if tab_obj is None:
                continue
            get_fn = getattr(tab_obj, "get_staged_files", None)
            if get_fn is None:
                continue
            try:
                staged = get_fn()
            except Exception as e:
                log.warning("Pull all: %s.get_staged_files() failed: %s", tab_key, e)
                continue
            if not staged:
                continue

            label = _TAB_LABELS.get(tab_key, tab_key)
            file_names = sorted(staged.keys())
            summary = f"{label}: {', '.join(file_names)}"

            self._mods = [
                m for m in self._mods
                if not (m.kind == "companion_files"
                        and m.name == f"{label} tab (staged files)")
            ]

            entry = ModEntry(
                name=f"{label} tab (staged files)",
                path="<in-memory>",
                kind="companion_files",
                ok=True,
                note=summary,
            )
            entry.staged_companion_files = staged
            self._mods.append(entry)
            pulled.append(label)
            self._log_line(f"  ✓ {summary}")

        self._refresh_mod_list()
        if pulled:
            self._log_line(f"✓ Pull All: {', '.join(pulled)}")
            QMessageBox.information(
                self, "Pull All Edits",
                f"Pulled edits from: {', '.join(pulled)}\n\n"
                f"Run PREVIEW then Export Field JSON to build multi-target mod.")
        else:
            QMessageBox.information(
                self, "Pull All Edits",
                "No tabs have modifications to pull.\n\n"
                "Load and edit data in ItemBuffs, MercPets, SkillTree, etc. first.")

    # ------------------------------------------------------------
    def _regenerate_equipslotinfo_for_up_v2(self) -> dict:
        """Re-run the equipslotinfo expansion logic from
        _eb_universal_proficiency_v2 in-process, using the Stacker's
        game path. Returns {filename: bytes} for the two equipslotinfo
        files, or empty dict on failure.

        Called from _pull_from_itembuffs when UP v2 tribe edits were
        detected in the dict but the staged files dict is empty —
        meaning ItemBuffs's equipslotinfo serializer silently failed.
        """
        game = self._game_edit.text().strip() if hasattr(self, "_game_edit") else ""
        if not game or not os.path.isdir(game):
            return {}
        try:
            import crimson_rs
            import equipslotinfo_parser as esp
        except Exception:
            return {}

        try:
            pabgh = bytes(crimson_rs.extract_file(
                game, "0008", INTERNAL_DIR, "equipslotinfo.pabgh"))
            pabgb = bytes(crimson_rs.extract_file(
                game, "0008", INTERNAL_DIR, "equipslotinfo.pabgb"))
        except Exception:
            return {}

        try:
            records = esp.parse_all(pabgh, pabgb)
        except Exception:
            return {}

        # Kliff/Damiane/Oongka player char keys — same constants as the
        # buffs tab (_PLAYER_CHAR_KEYS = {1, 4, 6}).
        player_keys = {1, 4, 6}
        pool: dict = {}
        for rec in records:
            if rec.key not in player_keys:
                continue
            for e in rec.entries:
                key = (e.category_a, e.category_b)
                pool.setdefault(key, set()).update(e.etl_hashes)

        added = 0
        for rec in records:
            if rec.key not in player_keys:
                continue
            for e in rec.entries:
                have = set(e.etl_hashes)
                extra = sorted(pool.get((e.category_a, e.category_b), set()) - have)
                if extra:
                    e.etl_hashes.extend(extra)
                    added += len(extra)

        if added == 0:
            # Already unioned — nothing to bundle (would re-serialize vanilla).
            return {}

        try:
            new_pabgh, new_pabgb = esp.serialize_all(records)
        except Exception:
            return {}

        return {
            "equipslotinfo.pabgh": bytes(new_pabgh),
            "equipslotinfo.pabgb": bytes(new_pabgb),
        }

    # ------------------------------------------------------------
    def _pull_from_dmm(self):
        """Import all enabled DMM mods that target iteminfo.pabgb.

        Reads DMM's config.json, finds its mods folder, loads each active
        JSON mod that patches iteminfo, and adds them as Stacker sources.
        For legacy byte-patch mods, runs the Inspector for field attribution
        so semantic merge is possible.
        """
        dmm_exe = self._config.get("dmm_exe_path", "")
        if not dmm_exe or not os.path.isfile(dmm_exe):
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(
                self, "DMM Not Found",
                "DMM is not configured. Go to the Mod Loader tab and "
                "set the DMM path first."
            )
            return

        from shared_state import get_dmm_iteminfo_mods
        game = self._game_edit.text().strip() if hasattr(self, "_game_edit") else ""
        mods = get_dmm_iteminfo_mods(game, dmm_exe)
        if not mods:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.information(
                self, "No ItemInfo Mods",
                "No active DMM mods target iteminfo.pabgb."
            )
            return

        # Remove any prior DMM-pulled entries to avoid duplicates
        self._mods = [m for m in self._mods if not m.name.startswith("[DMM] ")]

        added = 0
        for file_name, mods_path, doc in mods:
            json_path = os.path.join(mods_path, file_name)
            info = doc.get("modinfo") or doc
            title = info.get("title") or info.get("name") or file_name

            # Classify: if it has format:3 intents, load as field_json;
            # otherwise as legacy_json
            if doc.get("format") == 3 and doc.get("intents"):
                entry = ModEntry(
                    name=f"[DMM] {title}",
                    path=json_path,
                    kind="field_json",
                    note=f"DMM Format 3 ({len(doc['intents'])} intents)",
                )
            else:
                patches = doc.get("patches", [])
                n = sum(len(p.get("changes", [])) for p in patches)
                entry = ModEntry(
                    name=f"[DMM] {title}",
                    path=json_path,
                    kind="legacy_json",
                    note=f"DMM legacy JSON ({n} byte patches)",
                )

            self._mods.append(entry)
            added += 1
            self._log_line(f"  + [DMM] {title} ({entry.kind})")

        self._refresh_mod_list()
        self._log_line(
            f"✓ Pulled {added} iteminfo mod(s) from DMM. "
            f"Click Preview to inspect, then Install Stack to merge."
        )

    # ------------------------------------------------------------
    # Type-tag styling for the Sources list row. Keeps the badge visually
    # distinct at a glance: green for itembuffs, blue for folder mods,
    # amber for legacy JSON, grey for loose pabgb.
    _TYPE_META = {
        "itembuffs_edits": ("ITEMBUFFS", "#1E2A1E", "#4CAF50"),
        "folder_paz":      ("PAZ MOD",   "#1A2030", "#6A9CE0"),
        "loose_pabgb":     ("PABGB",     "#1F2128", "#9EA4B8"),
        "legacy_json":     ("JSON v2",   "#2A2418", "#DC961E"),
        "field_json":      ("FIELD v3",  "#1A2A2A", "#26C6DA"),
        "dmm_json":        ("DMM",       "#1A1A2A", "#7B68EE"),
    }

    def _append_mod_row(self, m: ModEntry):
        """Create a list row with toggle, name, type tag, and delete."""
        row_widget = self._build_source_row_widget(m)

        item = QListWidgetItem()
        item.setSizeHint(QSize(0, 44))
        # Keep a back-reference to the ModEntry via Qt.UserRole so we
        # can match list positions to the self._mods index even after
        # user-driven reorders.
        item.setData(Qt.UserRole, id(m))
        self._mods_list.addItem(item)
        self._mods_list.setItemWidget(item, row_widget)

    def _build_source_row_widget(self, m: ModEntry) -> QWidget:
        w = QWidget()
        w.setStyleSheet("QWidget { background: transparent; }")
        h = QHBoxLayout(w)
        h.setContentsMargins(8, 4, 6, 4)
        h.setSpacing(8)

        # Toggle — drives m.enabled. Disabled-but-kept rows go semi-dim.
        toggle = QCheckBox()
        toggle.setChecked(bool(m.enabled))
        toggle.setFixedWidth(20)
        toggle.stateChanged.connect(lambda st, me=m: self._set_enabled(me, bool(st)))
        h.addWidget(toggle)

        # Name + status text (two-line label) — stretches to fill.
        name_lbl = QLabel(m.name)
        name_lbl.setStyleSheet(
            f"color: {self._TXT if m.enabled else self._TXT_MUTED}; "
            "font-weight: 600;")
        name_lbl.setToolTip(m.path)
        h.addWidget(name_lbl, stretch=1)

        # Error/status hint — small, right-aligned, shown only if the
        # source failed classification.
        if not m.ok:
            err = QLabel("✘ skipped")
            err.setStyleSheet(f"color: #E06060; font-size: 10px;")
            err.setToolTip(m.note)
            h.addWidget(err)

        # Type tag badge — identifies what kind of source this is.
        kind_txt, bg, fg = self._TYPE_META.get(
            m.kind, ("UNKNOWN", "#2A2A2A", "#888"))
        tag = QLabel(kind_txt)
        tag.setStyleSheet(
            f"background: {bg}; color: {fg}; "
            "font-size: 9px; font-weight: bold; "
            "border: 1px solid #2E3245; border-radius: 3px; "
            "padding: 2px 6px;")
        h.addWidget(tag)

        # Legacy JSON — three-way merge mode cycle. Clicking the button
        # rotates Strict → Semantic → Reparse-Diff → Strict.
        # Strict: byte-apply only (original must match vanilla).
        # Semantic: per-patch field resolution via inspector; stale
        #   patches are skipped but applied ones survive offset drift.
        # Reparse-Diff: splice all patches, reparse, diff. Recovers
        #   stale patches, insert patches, and absolute-offset patches
        #   as a single uniform set of FieldEdits.
        if m.kind == "legacy_json":
            mode_btn = QToolButton()
            mode_btn.setObjectName("rowModeToggle")
            mode_btn.setCursor(Qt.PointingHandCursor)
            mode_btn.setProperty("modeValue", m.merge_mode)
            self._style_mode_btn(mode_btn, m.merge_mode)
            mode_btn.clicked.connect(
                lambda _=False, me=m, btn=mode_btn:
                    self._cycle_merge_mode(me, btn))
            h.addWidget(mode_btn)

        # Per-row delete button (JMM uses ✕)
        del_btn = QToolButton()
        del_btn.setText("✕")
        del_btn.setObjectName("rowDelete")
        del_btn.setCursor(Qt.PointingHandCursor)
        del_btn.setToolTip("Remove this source from the stack")
        del_btn.clicked.connect(lambda _=False, me=m: self._remove_by_ref(me))
        h.addWidget(del_btn)
        return w

    # Mapping used by the three-way toggle. Each entry is
    #   mode_value -> (button_text, bg_color, fg_color, border_color)
    _MODE_META = {
        "strict":       ("STR", "#1F2430", "#9EA4B8", "#2E3245"),
        "semantic":     ("SEM", "#1E3524", "#4CAF50", "#2E8B3E"),
        "reparse_diff": ("RPD", "#2A1F3A", "#C08FE8", "#503E73"),
    }
    _MODE_CYCLE = ["strict", "semantic", "reparse_diff"]

    def _style_mode_btn(self, btn, mode: str) -> None:
        """Set button text + colors to match its current mode value."""
        text, bg, fg, border = self._MODE_META.get(
            mode, self._MODE_META["strict"])
        btn.setText(text)
        btn.setToolTip(
            "Click to cycle merge mode.\n"
            "STR = Strict byte-apply (original must match vanilla).\n"
            "SEM = Semantic field-apply (per-patch field resolution;\n"
            "      survives offset drift from other mods).\n"
            "RPD = Reparse-Diff (splice all, reparse, diff; recovers\n"
            "      stale, insert, and absolute-offset patches uniformly).\n"
            "Requires Preview Merge to re-run.")
        btn.setStyleSheet(
            "QToolButton#rowModeToggle { "
            f"  background: {bg}; color: {fg}; "
            "  font-size: 9px; font-weight: bold; "
            f"  border: 1px solid {border}; border-radius: 3px; "
            "  padding: 2px 6px; }")

    def _cycle_merge_mode(self, m: ModEntry, btn) -> None:
        """Rotate the mode to the next option. Clear cached state so the
        user re-runs Preview — avoids a stale-data pitfall where the
        mode flips but results still reflect the prior mode."""
        try:
            i = self._MODE_CYCLE.index(m.merge_mode)
        except ValueError:
            i = -1
        m.merge_mode = self._MODE_CYCLE[(i + 1) % len(self._MODE_CYCLE)]
        m.inspections = []
        m.reparse_report = None
        m.parsed_items = None
        m.apply_stats = ""
        self._style_mode_btn(btn, m.merge_mode)
        self._refresh_mod_list()

    def _set_enabled(self, m: ModEntry, state: bool) -> None:
        m.enabled = state
        self._refresh_mod_list()

    def _remove_by_ref(self, m: ModEntry) -> None:
        try:
            self._mods.remove(m)
        except ValueError:
            return
        self._refresh_mod_list()

    def _on_source_selected(self, row: int) -> None:
        """Update the middle Details pane when selection changes."""
        if row < 0 or row >= len(self._mods):
            self._refresh_details()
            return
        m = self._mods[row]
        self._refresh_details(selected=m)

    def _remove_selected(self):
        row = self._mods_list.currentRow()
        if row < 0 or row >= len(self._mods):
            return
        self._mods.pop(row)
        self._refresh_mod_list()

    def _clear_all(self):
        self._mods.clear()
        self._mods_list.clear()
        self._merged_items = []
        self._vanilla_items = []
        self._merged_equip_files = {}
        self._merged_skill_files = {}
        self._merged_other_files = {}
        self._conflicts = []
        self._refresh_details()

    # ------------------------------------------------------------
    def _log_line(self, s: str):
        self._log.append(s)
        QApplication.processEvents()   # flush UI so user sees progress live

    def _run(self, install: bool, export: bool = False):
        game = self._game_edit.text().strip()
        if not game or not os.path.isfile(os.path.join(game, "meta", "0.papgt")):
            QMessageBox.warning(self, "Stacker Tool",
                "Pick a valid Crimson Desert install folder first.\n\n"
                "(Looking for <game>/meta/0.papgt — we still need vanilla to "
                "read from, even when exporting as a standalone mod.)")
            return

        ok_mods = [m for m in self._mods if m.ok and m.kind and m.enabled]
        if not ok_mods:
            any_disabled = any(m.ok and m.kind and not m.enabled for m in self._mods)
            msg = ("Add at least one iteminfo mod first." if not any_disabled else
                   "All sources are currently disabled. Toggle at least one on "
                   "in the Sources panel to include it in the merge.")
            QMessageBox.information(self, "Stacker Tool", msg)
            return

        # Pre-apply conflict check — warn if other tools have iteminfo overlays
        if install and not export:
            try:
                from overlay_coordinator import check_iteminfo_conflicts_before_apply
                our_group = f"{self._overlay_spin.value():04d}" if hasattr(self, "_overlay_spin") else "0062"
                warning = check_iteminfo_conflicts_before_apply(
                    game, our_group, self._config)
                if warning:
                    reply = QMessageBox.warning(
                        self, "ItemInfo Conflict Detected", warning,
                        QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
                    if reply != QMessageBox.Yes:
                        return
            except Exception:
                pass

        # If exporting, ask the user where + what name BEFORE doing any work.
        export_target = None
        export_name = None
        if export:
            export_target, export_name = self._ask_export_target()
            if not export_target:
                return

        # Lock buttons during the run so double-clicks don't stack up
        try:
            for b in (self._preview_btn, self._install_btn,
                      self._install_single_btn, self._uninstall_btn,
                      self._export_btn, self._export_field_btn, self._export_legacy_btn):
                b.setEnabled(False)
            if export:
                self.status_message.emit("Stacker: building folder mod…")
            elif install:
                self.status_message.emit("Stacker: applying…")
            else:
                self.status_message.emit("Stacker: previewing merge…")
            self._log.clear()
            self._log_line(f"Game: {game}")
            self._log_line(f"Sources: {len(ok_mods)}")
            return self._run_inner(install, game, ok_mods,
                                   export_target=export_target,
                                   export_name=export_name)
        finally:
            for b in (self._preview_btn, self._install_btn,
                      self._install_single_btn, self._uninstall_btn,
                      self._export_btn, self._export_field_btn, self._export_legacy_btn):
                b.setEnabled(True)

    def _run_inner(self, install: bool, game: str, ok_mods: list,
                   export_target: Optional[str] = None,
                   export_name: Optional[str] = None):

        try:
            import crimson_rs
            from paz_patcher import ItemBuffPatcher
        except Exception as e:
            self._log_line(f"✘ crimson_rs/paz_patcher import failed: {e}")
            return

        try:
            vanilla_bytes = bytes(crimson_rs.extract_file(
                game, "0008", INTERNAL_DIR, ITEMINFO_PABGB))
        except Exception as e:
            self._log_line(f"✘ Could not extract vanilla iteminfo: {e}")
            return
        self._log_line(f"  vanilla iteminfo.pabgb: {len(vanilla_bytes):,} bytes")

        # Check if another tool (DMM) has an active iteminfo overlay.
        # If so, warn the user — merging from vanilla may conflict with
        # what DMM already applied.
        try:
            from overlay_coordinator import get_active_iteminfo_overlay, load_state
            state = load_state(game)
            dmm_overlays = [
                (g, e) for g, e in state.overlays.items()
                if e.owner != "CrimsonGameMods"
                and any("iteminfo" in f.lower() for f in e.files)
            ]
            if dmm_overlays:
                groups_str = ", ".join(f"{g} ({e.owner})" for g, e in dmm_overlays)
                self._log_line(
                    f"  ⚠ Other tool(s) have iteminfo overlays active: {groups_str}")
                self._log_line(
                    f"    Stacker merges from vanilla (0008/) — the final overlay "
                    f"will include your mods' changes but NOT the other tool's changes.")
                self._log_line(
                    f"    If you want both, use 'Pull DMM' to import their mods "
                    f"into this merge.")
        except Exception:
            pass

        try:
            vanilla_items = crimson_rs.parse_iteminfo_from_bytes(vanilla_bytes)
            self._stacker_unparsed_raw = []
        except Exception:
            vanilla_items, self._stacker_unparsed_raw = (
                _parse_with_fallback(crimson_rs, game, vanilla_bytes))
            if not vanilla_items:
                self._log_line("✘ Could not parse vanilla iteminfo")
                return
        self._vanilla_items = vanilla_items
        self._log_line(f"  {len(vanilla_items)} entries parsed"
                       + (f" ({len(self._stacker_unparsed_raw)} raw)"
                          if self._stacker_unparsed_raw else ""))

        # Entry BlobStart map for legacy JSON resolve
        entry_blob_start: dict[str, int] = {}
        try:
            patcher = ItemBuffPatcher(game_path=game)
            patcher._original_data = vanilla_bytes
            for rec in patcher.find_items(vanilla_bytes):
                entry_blob_start[rec.name] = rec.data_offset
        except Exception as e:
            self._log_line(f"  (warn) BlobStart map unavailable: {e}")

        # Get per-source parsed items. External mods get extracted-and-parsed;
        # the ItemBuffs snapshot source is already parsed dicts.
        per_mod_parsed: list[tuple[str, list]] = []
        for m in ok_mods:
            try:
                if m.kind == "itembuffs_edits":
                    # Already a list[dict] captured at Pull time. Keep
                    # apply_stats short since the detailed bucket
                    # summary is already in m.note from Pull.
                    items = m.parsed_items or []
                    m.apply_stats = "parsed"
                elif m.kind == "folder_paz":
                    raw = bytes(crimson_rs.extract_file(
                        m.path, m.group, INTERNAL_DIR, ITEMINFO_PABGB))
                    m.effective_pabgb = raw
                    try:
                        items = crimson_rs.parse_iteminfo_from_bytes(raw)
                    except Exception:
                        items, _ = _parse_with_fallback(crimson_rs, game, raw)
                    m.parsed_items = items
                    # Pull every .pabgb/.pabgh sibling this mod ships.
                    # Pre-1.1.5 only the four hardcoded equipslotinfo +
                    # skill names made it through here; mods that
                    # touched buff_info / condition_info / gimmick_info
                    # / store_info / etc. silently lost those bytes at
                    # this layer. Now we enumerate the PAMT directly so
                    # any pabgb table the modder ships gets picked up.
                    #
                    # Routing:
                    #   equipslotinfo.* → staged_equip_files (registry)
                    #   skill.*         → staged_skill_files (registry, disabled)
                    #   anything else   → staged_other_files (catch-all blob diff)
                    companions = []
                    discovered: list[str] = []
                    try:
                        pamt_path = os.path.join(m.path, m.group, "0.pamt")
                        with open(pamt_path, 'rb') as _pf:
                            pamt_data = _pf.read()
                        pamt = crimson_rs.parse_pamt_bytes(pamt_data)
                        for d in pamt.get('directories', []):
                            if d.get('path') != INTERNAL_DIR:
                                continue
                            for f in d.get('files', []):
                                fname = f.get('name', '')
                                # Iteminfo flows through the dict-list
                                # merge path, not Bucket D.
                                if fname == ITEMINFO_PABGB or fname == ITEMINFO_PABGH:
                                    continue
                                if fname.endswith('.pabgb') or fname.endswith('.pabgh'):
                                    discovered.append(fname)
                    except Exception:
                        # PAMT parse failed — fall back to the legacy
                        # four hardcoded names so equipslotinfo + skill
                        # mods still work even if the PAMT decoder hits
                        # an edge case.
                        discovered = [
                            "equipslotinfo.pabgb", "equipslotinfo.pabgh",
                            "skill.pabgb", "skill.pabgh",
                        ]

                    for fname in discovered:
                        try:
                            comp_raw = bytes(crimson_rs.extract_file(
                                m.path, m.group, INTERNAL_DIR, fname))
                        except Exception:
                            continue
                        if not comp_raw:
                            continue
                        if fname.startswith("equipslotinfo."):
                            m.staged_equip_files[fname] = comp_raw
                        elif fname.startswith("skill."):
                            m.staged_skill_files[fname] = comp_raw
                        else:
                            m.staged_other_files[fname] = comp_raw
                        companions.append(fname)
                    m.apply_stats = (
                        f"compiled PAZ unpacked"
                        + (f" (+ {len(companions)} companion"
                           f"{'s' if len(companions) != 1 else ''}: "
                           f"{', '.join(companions)})"
                           if companions else ""))
                elif m.kind == "loose_pabgb":
                    with open(m.path, "rb") as f:
                        m.effective_pabgb = f.read()
                    try:
                        items = crimson_rs.parse_iteminfo_from_bytes(m.effective_pabgb)
                    except Exception:
                        items, _ = _parse_with_fallback(
                            crimson_rs, game, m.effective_pabgb)
                    m.parsed_items = items
                    m.apply_stats = "loose pabgb loaded"
                elif m.kind == "legacy_json":
                    with open(m.path, encoding="utf-8") as f:
                        doc = json.load(f)
                    # Inspector pass — field attribution for every patch.
                    # Runs BEFORE byte-apply so we have field paths to
                    # surface even for patches that fail bytes-mismatch.
                    changes = iteminfo_inspector.collect_iteminfo_patches(doc)
                    insps = iteminfo_inspector.inspect_patches(
                        vanilla_bytes, changes)
                    m.inspections = insps

                    # Surface corruption-risk signals to the log during
                    # the preview so authors see this even if they don't
                    # open the Details pane. The worst offenders are
                    # __count__ / __len__ / __tag__ writes and split-
                    # primitive patches — all of which byte-patching
                    # cannot do safely across game versions.
                    has_inserts = any(
                        c.get("type") == "insert" for c in changes)
                    danger = iteminfo_inspector.count_dangerous_patches(insps)
                    total_prefix = (danger["count_prefix"]
                                    + danger["len_prefix"]
                                    + danger["tag_prefix"])
                    if (total_prefix or danger["split_primitive"]
                            or has_inserts) and m.merge_mode == "strict":
                        bits = []
                        if danger["count_prefix"]:
                            bits.append(
                                f"{danger['count_prefix']} CArray-count writes")
                        if danger["len_prefix"]:
                            bits.append(
                                f"{danger['len_prefix']} CString-len writes")
                        if danger["tag_prefix"]:
                            bits.append(
                                f"{danger['tag_prefix']} COptional-tag writes")
                        if danger["split_primitive"]:
                            bits.append(
                                f"{danger['split_primitive']} split-primitive patches")
                        if has_inserts:
                            bits.append("uses `insert` patches")
                        self._log_line(
                            f"  ⚠ {m.name}: corruption-risk patterns detected "
                            f"({', '.join(bits)}). "
                            "Switch to Reparse-Diff mode (click STR → RPD on the "
                            "source row) to translate safely.")

                    if m.merge_mode == "reparse_diff":
                        # Splice every patch into vanilla, reparse, diff.
                        # This is the most forgiving mode — it pulls
                        # intent out of stale + insert + absolute-offset
                        # patches uniformly, producing a FieldEdit list
                        # that apply_field_edits writes to the dict tree.
                        items = copy.deepcopy(vanilla_items)
                        report = iteminfo_inspector.reparse_diff_patches(
                            vanilla_bytes, doc, entry_blob_start)
                        m.reparse_report = report
                        r_applied, r_skipped, r_reasons = (
                            iteminfo_inspector.apply_field_edits(
                                items, report.edits))
                        m.parsed_items = items
                        m.effective_pabgb = vanilla_bytes
                        unapplied = len(report.unapplied_patches)
                        parse_break = len(report.parse_break_patches)
                        noop = len(report.no_op_patches)
                        m.apply_stats = (
                            f"reparse-diff ({report.mode}): "
                            f"{r_applied} edits applied, "
                            f"{r_skipped} dict-skip, "
                            f"{unapplied} unapplied, "
                            f"{parse_break} parse-break, "
                            f"{noop} no-op")
                        if parse_break:
                            self._log_line(
                                f"  ⚠ {m.name}: {parse_break} patch(es) "
                                "broke parsing in isolation — those specific "
                                "patches contribute no field edits.")
                    elif m.merge_mode == "semantic":
                        # Per-patch inspector resolution; stale patches
                        # dropped. Survives offset drift for the applied
                        # patches but misses stale ones (unlike
                        # reparse_diff).
                        items = copy.deepcopy(vanilla_items)
                        s_applied, s_skipped, s_reasons = (
                            iteminfo_inspector.apply_semantic(items, insps))
                        m.parsed_items = items
                        m.effective_pabgb = vanilla_bytes
                        m.apply_stats = (
                            f"semantic: {s_applied} field edits, "
                            f"{s_skipped} skipped")
                        if s_skipped and s_reasons:
                            self._log_line(
                                f"  ⚠ {m.name}: {s_skipped} patch(es) "
                                "skipped in semantic mode — see Details.")
                    else:
                        # Strict: legacy byte-apply path. Byte splice +
                        # re-parse of the whole file.
                        modded, applied, skipped, mask = _apply_legacy_json(
                            vanilla_bytes, doc, entry_blob_start)
                        iteminfo_inspector.mark_stale_status(insps, mask)
                        m.effective_pabgb = modded
                        try:
                            items = crimson_rs.parse_iteminfo_from_bytes(modded)
                        except Exception:
                            items, _ = _parse_with_fallback(
                                crimson_rs, game, modded)
                        m.parsed_items = items
                        m.apply_stats = f"{applied} applied, {skipped} skipped"
                        if skipped and not applied:
                            self._log_line(
                                f"  ⚠ {m.name}: 0 applied, {skipped} skipped — "
                                f"all patches bytes-mismatch (stale mod for "
                                f"different game version); contributes no edits. "
                                f"Try Reparse-Diff mode — recovers intent from "
                                f"stale patches uniformly.")
                elif m.kind == "field_json":
                    with open(m.path, encoding="utf-8") as f:
                        doc = json.load(f)
                    intents = doc.get("intents", [])
                    items = copy.deepcopy(vanilla_items)
                    items_by_key = {it['string_key']: it for it in items}
                    applied_count = 0
                    skipped_count = 0
                    verified_count = 0
                    broken_count = 0
                    broken_entries = []
                    for intent in intents:
                        entry = intent.get('entry', '')
                        target = items_by_key.get(entry)
                        if not target:
                            skipped_count += 1
                            continue
                        op = intent.get('op', 'set')
                        field = intent.get('field', '')
                        if op == 'set' and field:
                            _apply_field_set(target, field, intent.get('new'))
                            applied_count += 1
                        elif op == 'add_entry':
                            skipped_count += 1
                        else:
                            skipped_count += 1
                    # Validate: try serialize→reparse each modified item.
                    # If an item breaks, revert it to vanilla so it doesn't
                    # corrupt the output.
                    touched = {i.get('entry') for i in intents}
                    van_by_key = {it['string_key']: it
                                  for it in vanilla_items}
                    for idx, it in enumerate(items):
                        skey = it.get('string_key', '')
                        if skey not in touched:
                            continue
                        try:
                            rt = crimson_rs.serialize_iteminfo([it])
                            rp = crimson_rs.parse_iteminfo_from_bytes(rt)
                            if not rp:
                                raise ValueError("empty reparse")
                            verified_count += 1
                        except Exception as _ve:
                            broken_count += 1
                            broken_entries.append(skey)
                            van = van_by_key.get(skey)
                            if van:
                                items[idx] = copy.deepcopy(van)
                            self._log_line(
                                f"  ⚠ {skey}: intent broke serialization "
                                f"({_ve}) — reverted to vanilla")
                    m.parsed_items = items
                    m.effective_pabgb = vanilla_bytes
                    m.apply_stats = (
                        f"field JSON: {applied_count} applied, "
                        f"{skipped_count} skipped, "
                        f"{verified_count} verified, "
                        f"{broken_count} reverted")
                else:
                    continue
                # Attach dict-feature detection to every source so the
                # Details pane shows what's inside regardless of kind.
                # ItemBuffs already had this attached at Pull time; this
                # catches external mods too (useful when the user drops
                # a buff-pack PAZ mod and wants to see what it changes
                # before merging).
                m.detected_features = _detect_dict_features(items)
                per_mod_parsed.append((m.name, items))
                self._log_line(f"  ✓ {m.name}: {m.apply_stats} "
                               f"({len(items)} entries)")
            except Exception as e:
                m.apply_stats = f"error: {e}"
                self._log_line(f"  ✘ {m.name}: {e}")

        # Refresh source rows so updated apply_stats text renders
        self._refresh_mod_list()

        if not per_mod_parsed:
            self._log_line("✘ No mods produced parseable iteminfo.")
            return

        # Dict-level merge
        self._log_line("Merging dict-level…")
        try:
            merged_items, conflicts = _merge_all(vanilla_items, per_mod_parsed)
        except Exception as e:
            self._log_line(f"✘ merge failed: {e}")
            QMessageBox.critical(self, "Stacker Tool",
                f"Merge failed:\n{e}\n\n"
                "One of the sources has an unexpected shape. Try removing "
                "sources one at a time to find which.")
            return
        # For any conflict whose mods are legacy JSONs, try to attribute
        # each side to a specific patch index within that JSON. Lets the
        # conflict log read "patch #7 of A beat patch #3 of B".
        _attach_patch_provenance(conflicts, ok_mods)

        self._merged_items = merged_items
        self._conflicts = conflicts
        self._log_line(f"  merged: {len(merged_items)} entries; "
                       f"{len(conflicts)} field conflict(s)")
        self._refresh_details()

        # Preview-only path — no serialize, no write.
        if not install and not export_target:
            self._log_line("Preview complete. Click Apply Stack to Game, or "
                           "Export as Folder Mod.")
            self.status_message.emit(
                f"Stacker: preview ready ({len(merged_items)} entries, "
                f"{len(conflicts)} conflicts)")
            return

        # ─── Bucket B — apply checkbox toggles to merged dict BEFORE serialize ───
        # Mirrors buffs_v319.py:9821-9842 (_buff_apply_to_game). Any
        # itembuffs_edits source with apply_stacks / apply_inf_dura set
        # mutates the merged dict here. If two sources both have
        # apply_stacks with different targets, the highest wins (user
        # probably wants the more-permissive of the two).
        bucket_b_stacks_target: Optional[int] = None
        bucket_b_inf_dura = False
        for m in ok_mods:
            if m.kind != "itembuffs_edits":
                continue
            if m.apply_stacks is not None:
                if bucket_b_stacks_target is None or m.apply_stacks > bucket_b_stacks_target:
                    bucket_b_stacks_target = m.apply_stacks
            if m.apply_inf_dura:
                bucket_b_inf_dura = True

        if bucket_b_stacks_target is not None:
            n = 0
            for it in merged_items:
                if it.get("max_stack_count", 1) > 1:
                    it["max_stack_count"] = bucket_b_stacks_target
                    n += 1
            self._log_line(f"  Max Stacks: set max_stack_count={bucket_b_stacks_target} on {n} items")
        if bucket_b_inf_dura:
            n = 0
            for it in merged_items:
                endurance = it.get("max_endurance", 0)
                if endurance > 0 and endurance != 65535:
                    it["max_endurance"] = 65535
                    it["is_destroy_when_broken"] = 0
                    n += 1
            self._log_line(f"  Infinite Durability: max_endurance=65535 on {n} items")

        # Serialize using vanilla-ordered rebuild (same as ItemBuffs).
        # This ensures every item lands at the right position with a
        # matching pabgh, and unparsed housing items stay in place.
        self._log_line("Serializing merged iteminfo…")
        try:
            van_pabgh = bytes(crimson_rs.extract_file(
                game, '0008', INTERNAL_DIR, ITEMINFO_PABGH))
            van_count = struct.unpack_from('<H', van_pabgh, 0)[0]
            van_rs = (len(van_pabgh) - 2) // van_count if van_count else 8

            order = []
            for _pi in range(van_count):
                _prec = 2 + _pi * van_rs
                if _prec + van_rs > len(van_pabgh):
                    break
                _psoff = struct.unpack_from('<I', van_pabgh,
                                           _prec + (van_rs - 4))[0]
                if _psoff + 4 <= len(vanilla_bytes):
                    _pk = struct.unpack_from('<I', vanilla_bytes, _psoff)[0]
                    _pnxt = len(vanilla_bytes)
                    if _pi + 1 < van_count:
                        _nrec = 2 + (_pi + 1) * van_rs
                        if _nrec + van_rs <= len(van_pabgh):
                            _pnxt = struct.unpack_from(
                                '<I', van_pabgh, _nrec + (van_rs - 4))[0]
                    order.append((_pk, _psoff, _pnxt - _psoff))

            for it in merged_items:
                dcd = it.get('docking_child_data')
                if isinstance(dcd, dict):
                    dcd.setdefault('inherit_summoner', 0)
                    dcd.setdefault('summon_tag_name_hash', [0, 0, 0, 0])
            merged_by_key = {it['key']: it for it in merged_items}
            unparsed = getattr(self, '_stacker_unparsed_raw', []) or []
            unparsed_map = {}
            for raw in unparsed:
                uk = struct.unpack_from('<I', raw, 0)[0]
                unparsed_map[uk] = raw

            final = bytearray()
            new_entries = []
            for pk, psoff, psize in order:
                new_entries.append((pk, len(final)))
                if pk in merged_by_key:
                    final.extend(crimson_rs.serialize_iteminfo(
                        [merged_by_key[pk]]))
                elif pk in unparsed_map:
                    final.extend(unparsed_map[pk])
                else:
                    final.extend(vanilla_bytes[psoff:psoff + psize])

            rebuilt_pabgh = struct.pack('<H', len(new_entries))
            for pk, poff in new_entries:
                rebuilt_pabgh += struct.pack('<II', pk, poff)
            self._rebuilt_pabgh = rebuilt_pabgh
            merged_bytes = bytes(final)
        except Exception as e:
            self._log_line(f"✘ serialize failed: {e}")
            import traceback; traceback.print_exc()
            QMessageBox.critical(self, "Stacker Tool",
                f"Serialize failed:\n{e}\n\n"
                "This usually means one of the merged entries has an invalid "
                "field type. Nothing was written.")
            return
        self._log_line(f"  {len(merged_bytes):,} bytes "
                       f"({len(new_entries)} entries in pabgh)")

        # ─── Bucket C — apply post-serialize byte patches ───
        # Mirrors buffs_v319.py:9845-9879 ordering: VFX → cooldowns → transmog.
        # Each itembuffs_edits source contributes independently; per-item
        # conflicts are resolved last-writer-wins (install order).
        merged_bytes = self._apply_bucket_c(merged_bytes, ok_mods)

        # Sibling files (Bucket D) — collected here, passed to pack step.
        sibling_files = self._collect_bucket_d(ok_mods)

        # Split equipslotinfo OUT of the main overlay bundle -- UP v2 only
        # works when equipslotinfo ships as a separate overlay group from
        # iteminfo. See OVERLAY_EQUIP_GROUP_DEFAULT docstring for why.
        equip_files = {}
        for _k in list(sibling_files.keys()):
            if _k.startswith("equipslotinfo."):
                equip_files[_k] = sibling_files.pop(_k)
        if equip_files:
            self._log_line(
                f"  Equipslotinfo split into its own overlay group: "
                f"{{{', '.join(sorted(equip_files.keys()))}}}")

        # Persist the merged equipslotinfo bytes so the field-JSON exporter
        # can diff them against vanilla and emit equipslotinfo intents in
        # multi-target shape. Without this hand-off, JSON exports silently
        # drop the unlock-all-weapons data even when the source mod stack
        # included it (the bug behind every "SuperMegaMod (1).json doesn't
        # unlock weapons" report through 1.1.x).
        self._merged_equip_files = dict(equip_files) if equip_files else {}

        # Same persistence pattern for skill.pabgb/.pabgh siblings — split
        # them out of the remaining sibling_files so the (currently
        # disabled) skill registry entry has data ready when its diff helper
        # gets enabled. Doesn't change deployment behavior because skill
        # files continue flowing through `sibling_files` to the same
        # overlay group as before this branch ran.
        skill_snap = {
            k: v for k, v in sibling_files.items()
            if k.startswith("skill.")
        }
        self._merged_skill_files = dict(skill_snap) if skill_snap else {}

        # Anything else in Bucket D (rare today; future blob-table additions
        # land here). Captured for the planned generic-blob registry path.
        self._merged_other_files = {
            k: v for k, v in sibling_files.items()
            if not k.startswith("skill.")
            and not k.startswith("equipslotinfo.")
        }

        # Branch: export as standalone folder mod, OR install directly.
        if export_target:
            try:
                out_dir = self._pack_as_folder_mod(
                    export_target, export_name, merged_bytes,
                    source_count=len(per_mod_parsed),
                    conflict_count=len(conflicts),
                    sibling_files=sibling_files,
                    equip_files=equip_files)
            except Exception as e:
                self._log_line(f"✘ export failed: {e}")
                QMessageBox.critical(self, "Stacker Tool",
                    f"Export failed:\n{e}")
                return
            self._log_line(f"✔ Exported folder mod to: {out_dir}")
            self.status_message.emit(
                f"Stacker: exported {export_name} "
                f"({len(per_mod_parsed)} sources merged)")
            reply = QMessageBox.information(
                self, "Stacker Tool",
                f"✔ Built folder mod at:\n\n{out_dir}\n\n"
                f"  {len(per_mod_parsed)} source(s) merged.\n"
                f"  {len(conflicts)} field conflict(s) auto-resolved.\n\n"
                "Install it with any loader (JMM, CDUMM, etc.) the same way "
                "you'd install any other folder mod, OR zip it and share it "
                "on Nexus as a standalone merged pack.\n\n"
                "Open the folder now?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
            if reply == QMessageBox.Yes:
                try:
                    os.startfile(out_dir)
                except Exception:
                    pass
            return

        # Direct-install path
        single_grp = getattr(self, '_single_stack_group', None)
        group = single_grp or OVERLAY_GROUP_DEFAULT
        equip_group = (f"{int(group)+1:04d}"
                       if single_grp
                       else OVERLAY_EQUIP_GROUP_DEFAULT)
        try:
            self._pack_and_install_overlay(game, group, merged_bytes,
                                            sibling_files=sibling_files)
        except Exception as e:
            self._log_line(f"✘ overlay install failed: {e}")
            QMessageBox.critical(self, "Stacker Tool", f"Install failed:\n{e}")
            return

        # If equipslotinfo edits are present, deploy them to the
        # secondary overlay group -- MUST be separate from iteminfo
        # for UP v2 to actually work in-game.
        if equip_files:
            try:
                self._pack_and_install_equipslot_overlay(
                    game, equip_group, equip_files)
            except Exception as e:
                self._log_line(f"✘ equipslotinfo overlay install failed: {e}")
                QMessageBox.critical(self, "Stacker Tool",
                    f"Equipslotinfo overlay install failed:\n{e}")
                return

        self._log_line(f"✔ Installed to {group}/. Launch Crimson Desert.")
        self.status_message.emit(
            f"Stacker: installed ({len(per_mod_parsed)} sources merged, "
            f"{len(conflicts)} conflicts auto-resolved)")
        QMessageBox.information(self, "Stacker Tool",
            f"✔ Installed {len(per_mod_parsed)} source(s) as a single overlay at "
            f"<game>/{group}/.\n"
            f"{len(conflicts)} field conflict(s) auto-resolved (install order wins).\n\n"
            "Launch the game to verify. If you see issues, click Remove Stack "
            "to revert and try a smaller subset.")

    # ------------------------------------------------------------
    def _apply_bucket_c(self, merged_bytes: bytes, ok_mods: list) -> bytes:
        """Apply post-serialize byte patches from itembuffs_edits sources.

        Order (matches buffs_v319.py:9845-9879):
          1. VFX changes — _apply_vfx_changes on the ItemBuffsTab instance,
             with our captured state swapped in temporarily so the method
             reads from our snapshot instead of current ItemBuffs state.
          2. Cooldown patches — iterate cd_patches per source; use
             ItemBuffsTab._cd_detect to find the offset in the merged
             bytes (detection has to run against the merged bytes, not
             the snapshot bytes, because merge may have shifted layout).
          3. Transmog swaps — same temp-state-swap pattern as VFX.

        Multi-source conflicts: per-item last-writer-wins. A cooldown
        patch from itembuffs source #1 gets overwritten if source #2
        also patches the same item_key's cooldown — logged once.
        """
        bt = self._buffs_tab
        itembuffs_sources = [m for m in ok_mods if m.kind == "itembuffs_edits"]
        if not itembuffs_sources:
            return merged_bytes

        data = bytearray(merged_bytes)

        # --- VFX changes (size, vfx swaps, anim swaps, attach changes) ---
        any_vfx = False
        # Collapse across sources: later source overrides earlier on same item.
        # Since _apply_vfx_changes just iterates lists and writes bytes, we
        # concatenate all sources' lists and let last-writer-wins happen
        # naturally via sequential writes.
        if bt is not None and hasattr(bt, "_apply_vfx_changes"):
            combined_size: list = []
            combined_swaps: list = []
            combined_anim: list = []
            combined_attach: list = []
            for m in itembuffs_sources:
                combined_size += m.vfx_size_changes
                combined_swaps += m.vfx_swaps
                combined_anim += m.vfx_anim_swaps
                combined_attach += m.vfx_attach_changes
            if combined_size or combined_swaps or combined_anim or combined_attach:
                # Swap ItemBuffs state temporarily so _apply_vfx_changes
                # reads from our merged lists. Also force
                # _experimental_mode=True for the duration of the call —
                # _apply_vfx_changes silently returns False if experimental
                # mode is off (buffs_v319.py:3209). If the user pulled VFX
                # changes, we want them applied regardless of the tab's
                # current experimental-mode state.
                orig = (
                    getattr(bt, "_vfx_size_changes", None),
                    getattr(bt, "_vfx_swaps", None),
                    getattr(bt, "_vfx_anim_swaps", None),
                    getattr(bt, "_vfx_attach_changes", None),
                    getattr(bt, "_experimental_mode", False),
                )
                try:
                    bt._vfx_size_changes = combined_size
                    bt._vfx_swaps = combined_swaps
                    bt._vfx_anim_swaps = combined_anim
                    bt._vfx_attach_changes = combined_attach
                    bt._experimental_mode = True
                    if bt._apply_vfx_changes(data):
                        any_vfx = True
                        self._log_line(
                            f"  VFX: {len(combined_size)} size, "
                            f"{len(combined_swaps)} vfx/trails, "
                            f"{len(combined_anim)} anims, "
                            f"{len(combined_attach)} attach")
                finally:
                    (bt._vfx_size_changes, bt._vfx_swaps,
                     bt._vfx_anim_swaps, bt._vfx_attach_changes,
                     bt._experimental_mode) = orig

        # --- Cooldown byte patches ---
        # cd_patches dict: { item_key: (original_off, original_val, new_val) }
        # We coalesce across sources keyed by item_key — last source wins.
        all_cd: dict = {}
        cd_losers: list[tuple] = []
        for m in itembuffs_sources:
            for key, patch in m.cd_patches.items():
                if key in all_cd and all_cd[key] != patch:
                    cd_losers.append((key, all_cd[key], patch, m.name))
                all_cd[key] = patch

        if all_cd and bt is not None and hasattr(bt, "_cd_detect"):
            cd_hit = 0
            for item_key, patch in all_cd.items():
                new_val = patch[2] if isinstance(patch, (tuple, list)) and len(patch) >= 3 else None
                if new_val is None:
                    continue
                try:
                    cd_off, _ = bt._cd_detect(item_key, bytes(data))
                except Exception:
                    cd_off = None
                if cd_off is not None:
                    data[cd_off:cd_off + 4] = struct.pack("<I", new_val)
                    cd_hit += 1
            self._log_line(f"  Cooldown patches: {cd_hit}/{len(all_cd)} applied")
            for key, old, new, winner in cd_losers:
                self._log_line(f"    [CONFLICT] item_key={key}: {winner} overrode earlier cooldown patch")

        # --- Transmog swaps ---
        if bt is not None and hasattr(bt, "_apply_transmog_swaps"):
            combined_transmog: list = []
            for m in itembuffs_sources:
                combined_transmog += m.transmog_swaps
            if combined_transmog:
                orig_transmog = getattr(bt, "_transmog_swaps", None)
                try:
                    bt._transmog_swaps = combined_transmog
                    applied = bt._apply_transmog_swaps(data)
                    self._log_line(
                        f"  Transmog: {applied} byte patches for "
                        f"{len(combined_transmog)} swap(s)")
                finally:
                    bt._transmog_swaps = orig_transmog

        return bytes(data)

    def _collect_bucket_d(self, ok_mods: list) -> dict:
        """Gather staged sibling files from all sources.

        Returns a dict of {filename_within_INTERNAL_DIR: bytes}. As of
        1.1.5 every .pabgb/.pabgh pair the source mod ships is in
        scope — pre-1.1.5 only skill.* and equipslotinfo.* were
        recognized. Last source wins if two contribute the same
        filename.

        Sources contributing sibling files:
        - itembuffs_edits (Pull from ItemBuffs): staged by UP v2 /
          passive-skill injection / imbue whitelisting.
        - folder_paz (external mods): every pabgb pair in the mod's
          PAZ now flows through. Equipslotinfo + skill go to their
          dedicated buckets; everything else lands in
          staged_other_files for the catch-all blob diff.
        """
        collected: dict = {}
        conflicts: list[tuple] = []
        for m in ok_mods:
            # itembuffs_edits + folder_paz can both carry companion
            # files now; loose_pabgb / legacy_json never do.
            if m.kind not in ("itembuffs_edits", "folder_paz"):
                continue
            for bucket in (m.staged_skill_files,
                           m.staged_equip_files,
                           m.staged_other_files):
                for fname, data in bucket.items():
                    if fname in collected and collected[fname] != data:
                        conflicts.append((fname, m.name))
                    collected[fname] = data
        if collected:
            self._log_line(
                f"  Sibling files for overlay: "
                f"{', '.join(sorted(collected.keys()))}")
        for fname, winner in conflicts:
            self._log_line(f"    [CONFLICT] {fname}: {winner}'s version wins")
        return collected

    # ------------------------------------------------------------
    def _ask_export_target(self) -> tuple[Optional[str], Optional[str]]:
        """Prompt user for output location + mod name. Returns
        (parent_dir, mod_name) or (None, None) if cancelled."""
        from PySide6.QtWidgets import QInputDialog

        parent = QFileDialog.getExistingDirectory(
            self, "Pick where to save the merged folder mod",
            self._config.get("stacker_export_dir", os.path.expanduser("~")))
        if not parent:
            return None, None
        self._config["stacker_export_dir"] = parent
        self.config_save_requested.emit()

        default_name = f"CrimsonStack-{time.strftime('%Y%m%d-%H%M%S')}"
        name, ok = QInputDialog.getText(
            self, "Mod name",
            "Folder mod name (a folder with this name will be created):",
            text=default_name)
        if not ok or not name.strip():
            return None, None
        safe = "".join(c if (c.isalnum() or c in "-_ .") else "_"
                       for c in name.strip())
        return parent, safe

    def _pack_as_folder_mod(self, parent_dir: str, mod_name: str,
                            iteminfo_bytes: bytes,
                            source_count: int,
                            conflict_count: int,
                            sibling_files: Optional[dict] = None,
                            equip_files: Optional[dict] = None) -> str:
        """Write a standard compiled folder mod — the same shape every
        Nexus folder-PAZ mod ships as — so any loader (JMM, CDUMM, or
        manual drop) can install it without touching the game folder.

        Output layout:
          <parent_dir>/<mod_name>/
            modinfo.json
            0036/
              0.paz    (contains iteminfo.pabgb + optional sibling files)
              0.pamt

        sibling_files lets the caller bundle skill.pabgb/skill.pabgh
        and equipslotinfo.pabgb/equipslotinfo.pabgh alongside iteminfo.
        Used when a pulled ItemBuffs source staged those files (e.g.
        Universal Proficiency v2 stages equipslotinfo).

        No meta/0.papgt is written — the consuming loader rebuilds
        PAPGT when it installs this mod. We're not an installer here.
        """
        import crimson_rs

        sibling_files = sibling_files or {}
        equip_files = equip_files or {}
        out_dir = os.path.join(parent_dir, mod_name)
        if os.path.isdir(out_dir) and os.listdir(out_dir):
            raise RuntimeError(
                f"Output folder '{out_dir}' already exists and is not empty. "
                "Pick a new name or delete it first.")
        os.makedirs(out_dir, exist_ok=True)

        # Standard mod slot — JMM expects numeric group dirs ≥ 36. 0036
        # is the canonical first-mod slot; JMM will remap to any free
        # overlay slot at install time, so there's no conflict with
        # whatever else the user has installed.
        group = "0036"
        group_dir_tmp = None
        with tempfile.TemporaryDirectory() as tmp:
            group_dir_tmp = os.path.join(tmp, group)
            builder = crimson_rs.PackGroupBuilder(
                group_dir_tmp, crimson_rs.Compression.NONE, crimson_rs.Crypto.NONE)
            builder.add_file(INTERNAL_DIR, ITEMINFO_PABGB, iteminfo_bytes)
            _pabgh = getattr(self, '_rebuilt_pabgh', None)
            if not _pabgh:
                try:
                    from item_creator import build_iteminfo_pabgh
                    _pabgh = build_iteminfo_pabgh(iteminfo_bytes)
                except Exception as _e:
                    self._log_line(f"  WARN pabgh regen failed ({_e})")
            if _pabgh:
                builder.add_file(INTERNAL_DIR, "iteminfo.pabgh", _pabgh)
            for fname, fdata in sibling_files.items():
                builder.add_file(INTERNAL_DIR, fname, fdata)
            # finish() writes 0.paz into group_dir_tmp AND returns pamt bytes
            pamt_bytes = bytes(builder.finish())

            # Copy everything (0.paz + 0.pamt etc.) to out_dir/<group>/
            out_group = os.path.join(out_dir, group)
            os.makedirs(out_group, exist_ok=True)
            for fname in os.listdir(group_dir_tmp):
                shutil.copy2(os.path.join(group_dir_tmp, fname),
                             os.path.join(out_group, fname))
            # builder.finish() may not have written 0.pamt to disk — write
            # it explicitly from the bytes it returned so any loader that
            # reads the file (not just the bytes API) finds it.
            pamt_on_disk = os.path.join(out_group, "0.pamt")
            if not os.path.isfile(pamt_on_disk):
                with open(pamt_on_disk, "wb") as f:
                    f.write(pamt_bytes)

        # Equipslotinfo ships as a SEPARATE overlay group (0037/) --
        # required for UP v2 to work in-game when bundled with iteminfo
        # edits.
        equip_group = "0037"
        equip_pamt = None
        if equip_files:
            with tempfile.TemporaryDirectory() as tmp2:
                equip_build_tmp = os.path.join(tmp2, equip_group)
                eb = crimson_rs.PackGroupBuilder(
                    equip_build_tmp, crimson_rs.Compression.NONE,
                    crimson_rs.Crypto.NONE)
                for fname, fdata in equip_files.items():
                    eb.add_file(INTERNAL_DIR, fname, fdata)
                equip_pamt = bytes(eb.finish())
                out_equip = os.path.join(out_dir, equip_group)
                os.makedirs(out_equip, exist_ok=True)
                for fname in os.listdir(equip_build_tmp):
                    shutil.copy2(os.path.join(equip_build_tmp, fname),
                                 os.path.join(out_equip, fname))
                equip_pamt_on_disk = os.path.join(out_equip, "0.pamt")
                if not os.path.isfile(equip_pamt_on_disk):
                    with open(equip_pamt_on_disk, "wb") as f:
                        f.write(equip_pamt)

        # Build meta/0.papgt so the export folder is drop-in ready:
        # copy the current game's PAPGT, add entries for 0036/ (and
        # 0037/ if equipslotinfo shipped), and ship it alongside. User
        # drops the folder contents into their game dir and everything
        # loads without needing JMM/CDUMM/manual PAPGT surgery.
        game_papgt_path = os.path.join(
            self._game_edit.text().strip(), "meta", "0.papgt")
        papgt_shipped = False
        if os.path.isfile(game_papgt_path):
            try:
                papgt = crimson_rs.parse_papgt_file(game_papgt_path)
                # Drop any existing entries for our groups so we don't dupe
                papgt["entries"] = [
                    e for e in papgt["entries"]
                    if e.get("group_name") not in ("0036", "0037")]
                # Add iteminfo group entry with correct checksum
                iteminfo_checksum = crimson_rs.parse_pamt_bytes(
                    pamt_bytes)["checksum"]
                papgt = crimson_rs.add_papgt_entry(
                    papgt, "0036", iteminfo_checksum, 0, 0x3FFF)
                # Add equipslotinfo group entry if we shipped one
                if equip_pamt is not None:
                    equip_checksum = crimson_rs.parse_pamt_bytes(
                        equip_pamt)["checksum"]
                    papgt = crimson_rs.add_papgt_entry(
                        papgt, "0037", equip_checksum, 0, 0x3FFF)
                meta_dir = os.path.join(out_dir, "meta")
                os.makedirs(meta_dir, exist_ok=True)
                crimson_rs.write_papgt_file(
                    papgt, os.path.join(meta_dir, "0.papgt"))
                papgt_shipped = True
                self._log_line(
                    f"  wrote meta/0.papgt (drop-in; "
                    f"{len(papgt['entries'])} total entries including ours)")
            except Exception as e:
                self._log_line(
                    f"  WARN meta/0.papgt generation failed ({e}) -- "
                    f"export will need JMM/CDUMM to install")

        # modinfo.json — the standard shape JMM / CDUMM look for.
        bundled_files_list = sorted(["iteminfo.pabgb"] + list(sibling_files.keys())
                                    + list(equip_files.keys()))
        overlay_groups = ["0036"] + (["0037"] if equip_files else [])
        # meta/ is added to the folder below (after modinfo init) if we
        # managed to write a PAPGT snapshot. Tracked in papgt_shipped.
        modinfo = {
            "id": mod_name.lower().replace(" ", "_"),
            "name": mod_name,
            "version": "1.0.0",
            "author": "CrimsonGameMods Stacker Tool",
            "description": (
                f"Merged pack built from {source_count} source mod(s). "
                f"{conflict_count} field conflict(s) auto-resolved at merge time. "
                f"Bundles: {', '.join(bundled_files_list)}. "
                "Install this folder with any mod loader that accepts compiled "
                "folder mods (JMM, CDUMM, or manual drop)."),
            "source_count": source_count,
            "conflict_count": conflict_count,
            "bundled_files": bundled_files_list,
            "built_with": "CrimsonGameMods Stacker Tool",
            "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        with open(os.path.join(out_dir, "modinfo.json"), "w", encoding="utf-8") as f:
            json.dump(modinfo, f, indent=2, ensure_ascii=False)

        # README.txt — so users who download the zip understand what it is
        contents_section = ""
        if sibling_files:
            file_list = sorted(sibling_files.keys())
            contents_section = (
                f"\nCONTENTS\n"
                f"--------\n"
                f"This pack bundles the following files into 0036/0.paz:\n\n"
                f"  - iteminfo.pabgb  (merged from all sources)\n"
                + "".join(f"  - {fn}\n" for fn in file_list)
                + f"\nThe extra files above come from ItemBuffs-tab features that\n"
                f"require sibling-file edits alongside iteminfo — for example,\n"
                f"Universal Proficiency needs equipslotinfo to open equip-slot\n"
                f"gates, and passive-skill injections need skill.pabgb/pabgh.\n"
                f"Install the folder as one unit; don't cherry-pick individual\n"
                f"files out of 0036/.\n")

        meta_section = ""
        if papgt_shipped:
            meta_section = (
                "\n"
                "The meta/0.papgt shipped in this folder is a snapshot of YOUR game's\n"
                "PAPGT at export time with our group entries added. Dropping it in\n"
                "will register 0036/ and 0037/ for the game to load.\n"
                "\n"
                "BACKUP your existing meta/0.papgt before replacing -- if you have\n"
                "installed other overlays SINCE this export was built, they will be\n"
                "lost when you overwrite. If in doubt, install via a mod loader.\n")
        readme = (
            f"{mod_name}\n"
            f"{'=' * len(mod_name)}\n\n"
            f"Merged mod pack built from {source_count} source mod(s).\n"
            f"{conflict_count} field-level conflict(s) were auto-resolved at merge time.\n"
            f"{contents_section}\n"
            f"HOW TO INSTALL\n"
            f"--------------\n"
            f"Drop-in (simplest -- only if you trust the meta/0.papgt snapshot):\n"
            f"  Copy the 0036/, 0037/, and meta/ folders into your Crimson Desert\n"
            f"  install directory, replacing meta/0.papgt. Done.\n"
            f"{meta_section}\n"
            f"Via mod loader (safer -- preserves other overlays):\n"
            f"  JMM (CD JSON Mod Manager): drag this folder into mods/_enabled/, Apply.\n"
            f"  CDUMM: import this folder as a mod, enable it, Apply.\n"
            f"\n"
            f"Built by CrimsonGameMods Stacker Tool on {modinfo['built_at']}.\n"
            f"If something doesn't apply correctly, it's most likely because one of\n"
            f"the source mods was built for a different game version.\n")
        with open(os.path.join(out_dir, "README.txt"), "w", encoding="utf-8") as f:
            f.write(readme)

        return out_dir

    # ------------------------------------------------------------
    def _pack_and_install_overlay(self, game: str, group: str,
                                  iteminfo_bytes: bytes,
                                  sibling_files: Optional[dict] = None) -> None:
        """Mirror _buff_export_cdumm_mod + _buff_apply_to_game, but for
        the merged iteminfo we computed. Direct write into the game dir.

        sibling_files: optional dict of filename → bytes. These are
        bundled into the same overlay PAZ alongside iteminfo. Used for
        skill.pabgb/pabgh + equipslotinfo.pabgb/pabgh when an
        ItemBuffs source staged them (UP v2, passive skill mods).
        """
        import crimson_rs

        sibling_files = sibling_files or {}

        # Build overlay group PAZ + PAMT
        with tempfile.TemporaryDirectory() as tmp:
            build_dir = os.path.join(tmp, group)
            builder = crimson_rs.PackGroupBuilder(
                build_dir, crimson_rs.Compression.NONE, crimson_rs.Crypto.NONE)
            builder.add_file(INTERNAL_DIR, ITEMINFO_PABGB, iteminfo_bytes)
            _pabgh = getattr(self, '_rebuilt_pabgh', None)
            if not _pabgh:
                try:
                    from item_creator import build_iteminfo_pabgh
                    _pabgh = build_iteminfo_pabgh(iteminfo_bytes)
                except Exception as _e:
                    self._log_line(f"  WARN pabgh regen failed ({_e})")
            if _pabgh:
                builder.add_file(INTERNAL_DIR, "iteminfo.pabgh", _pabgh)
            for fname, fdata in sibling_files.items():
                builder.add_file(INTERNAL_DIR, fname, fdata)
            pamt_bytes = bytes(builder.finish())
            pamt_checksum = crimson_rs.parse_pamt_bytes(pamt_bytes)["checksum"]

            # Copy group files into <game>/NNNN/
            dst_group = os.path.join(game, group)
            if os.path.isdir(dst_group):
                shutil.rmtree(dst_group)
            os.makedirs(dst_group, exist_ok=True)
            for fname in os.listdir(build_dir):
                shutil.copy2(os.path.join(build_dir, fname),
                             os.path.join(dst_group, fname))

        # Update PAPGT: drop any existing entry for this group, add ours
        papgt_path = os.path.join(game, "meta", "0.papgt")
        cur = crimson_rs.parse_papgt_file(papgt_path)
        cur["entries"] = [e for e in cur["entries"]
                          if e.get("group_name") != group]
        cur = crimson_rs.add_papgt_entry(
            cur, group, pamt_checksum, is_optional=0, language=0x3FFF)
        crimson_rs.write_papgt_file(cur, papgt_path)
        bundled_note = ""
        if sibling_files:
            bundled_note = f" (+ {len(sibling_files)} sibling file{'s' if len(sibling_files) != 1 else ''}: {', '.join(sorted(sibling_files.keys()))})"
        self._log_line(f"  wrote overlay {group}/ + updated meta/0.papgt{bundled_note}")
        try:
            from shared_state import record_overlay
            files = ["iteminfo.pabgb", "iteminfo.pabgh"]
            if sibling_files:
                files.extend(sorted(sibling_files.keys()))
            record_overlay(game, group, "Stacker merge", files)
        except Exception:
            pass

    # ------------------------------------------------------------
    def _pack_and_install_equipslot_overlay(self, game: str, group: str,
                                             equip_files: dict) -> None:
        """Write equipslotinfo.pabgb + .pabgh to a SEPARATE overlay group.

        UP v2 requires split deployment -- bundling equipslotinfo with
        iteminfo in a single overlay group breaks cross-character equips
        (guns on Kliff, blasters on other chars, etc.) even though both
        files individually contain correct data. Split into its own group
        and it works. Confirmed empirically 2026-04-21 vs v1.0.3.
        """
        import crimson_rs

        with tempfile.TemporaryDirectory() as tmp:
            build_dir = os.path.join(tmp, group)
            builder = crimson_rs.PackGroupBuilder(
                build_dir, crimson_rs.Compression.NONE, crimson_rs.Crypto.NONE)
            for fname, fdata in equip_files.items():
                builder.add_file(INTERNAL_DIR, fname, fdata)
            pamt_bytes = bytes(builder.finish())
            pamt_checksum = crimson_rs.parse_pamt_bytes(pamt_bytes)["checksum"]

            dst_group = os.path.join(game, group)
            if os.path.isdir(dst_group):
                shutil.rmtree(dst_group)
            os.makedirs(dst_group, exist_ok=True)
            for fname in os.listdir(build_dir):
                shutil.copy2(os.path.join(build_dir, fname),
                             os.path.join(dst_group, fname))

        papgt_path = os.path.join(game, "meta", "0.papgt")
        cur = crimson_rs.parse_papgt_file(papgt_path)
        cur["entries"] = [e for e in cur["entries"]
                          if e.get("group_name") != group]
        cur = crimson_rs.add_papgt_entry(
            cur, group, pamt_checksum, is_optional=0, language=0x3FFF)
        crimson_rs.write_papgt_file(cur, papgt_path)
        self._log_line(
            f"  wrote equipslotinfo overlay {group}/ + updated meta/0.papgt "
            f"({', '.join(sorted(equip_files.keys()))})")
        try:
            from shared_state import record_overlay
            record_overlay(game, group, "Stacker equipslot",
                           sorted(equip_files.keys()))
        except Exception:
            pass

    # ------------------------------------------------------------
    def _refresh_mod_list(self) -> None:
        """Rebuild every row's widget so toggles, names, and tags match
        the current ModEntry state. Row count stays aligned with
        self._mods by construction."""
        sel_row = self._mods_list.currentRow()
        self._mods_list.clear()
        for m in self._mods:
            self._append_mod_row(m)
        if 0 <= sel_row < self._mods_list.count():
            self._mods_list.setCurrentRow(sel_row)
        self._refresh_details()

    def _refresh_details(self, selected: Optional[ModEntry] = None) -> None:
        """Render the middle pane. If a source is selected, show its per-
        source details; always append the merge conflict summary (if any)
        so conflicts are visible without switching selection."""
        lines: list[str] = []

        if selected is not None:
            lines.append(f"● {selected.name}")
            lines.append("─" * 58)
            lines.append(f"Type     : {selected.kind or '(unknown)'}")
            lines.append(f"Enabled  : {'yes' if selected.enabled else 'NO — skipped in merge'}")
            lines.append(f"Path     : {selected.path}")
            if selected.group:
                lines.append(f"Group    : {selected.group}")
            if selected.note:
                lines.append(f"Note     : {selected.note}")
            if selected.apply_stats:
                lines.append(f"Status   : {selected.apply_stats}")
            # ItemBuffs snapshot specifics
            if selected.kind == "itembuffs_edits":
                lines.append("")
                lines.append("BUCKET SUMMARY")
                if selected.parsed_items is not None:
                    lines.append(f"  A — dict entries    : {len(selected.parsed_items)}")
                if selected.apply_stacks is not None:
                    lines.append(f"  B — max stacks      : {selected.apply_stacks}")
                if selected.apply_inf_dura:
                    lines.append(f"  B — inf durability  : yes")
                if selected.cd_patches:
                    lines.append(f"  C — cooldown byte-patches: {len(selected.cd_patches)}")
                if selected.transmog_swaps:
                    lines.append(f"  C — transmog swaps  : {len(selected.transmog_swaps)}")
                vfx_total = (len(selected.vfx_size_changes)
                             + len(selected.vfx_swaps)
                             + len(selected.vfx_anim_swaps)
                             + len(selected.vfx_attach_changes))
                if vfx_total:
                    lines.append(f"  C — VFX changes     : {vfx_total}")
                if selected.staged_skill_files:
                    lines.append(f"  D — skill bundle    : {', '.join(sorted(selected.staged_skill_files))}")
                if selected.staged_equip_files:
                    lines.append(f"  D — equip bundle    : {', '.join(sorted(selected.staged_equip_files))}")

                # Dict-level feature fingerprints — what's *inside* the
                # 6024 entries. Shows the user that UP v2, Make Dyeable,
                # No Cooldown (dict), etc. were all captured even though
                # they don't have dedicated bucket entries.
                feat_lines = _features_summary_lines(selected.detected_features or {})
                if feat_lines:
                    lines.append("")
                    lines.append("DETECTED DICT EDITS (inside Bucket A)")
                    lines.extend(feat_lines)

                # Visible warning if UP v2 tribe edits found but
                # equipslotinfo bundle missing. Auto-fallback in Pull
                # should have caught most cases; this covers the rest.
                if (selected.detected_features.get('up_v2_tribe_unioned')
                        and ('equipslotinfo.pabgb'
                             not in selected.staged_equip_files)):
                    lines.append("")
                    lines.append("⚠ UP v2 tribe edits present but equipslotinfo bundle missing.")
                    lines.append("  Fix: verify the game path is correct, go back to ItemBuffs,")
                    lines.append("  click Extract → Universal Proficiency v2, then re-Pull.")
            # Legacy JSON — inspector readout + mode switch visibility.
            # When a JSON source is selected, surface which patches
            # resolved to which fields, and any stale/missing ones.
            elif selected.kind == "legacy_json":
                lines.append("")
                mode_label = {
                    "strict": "Strict (byte-apply, original must match)",
                    "semantic": "Semantic (per-patch field resolve)",
                    "reparse_diff": "Reparse-Diff (splice-all, reparse, diff)",
                }.get(selected.merge_mode, selected.merge_mode)
                lines.append(f"MERGE MODE : {mode_label}")
                lines.append(
                    "   Click the mode tag (STR/SEM/RPD) in the source row "
                    "to cycle. Requires Preview Merge to re-run.")
                # Inspector readout (present in every mode since it runs
                # up-front before dispatch)
                if selected.inspections:
                    lines.append("")
                    lines.append("INSPECTOR (per-patch field attribution)")
                    lines.extend(
                        iteminfo_inspector.format_inspection_summary(
                            selected.inspections))
                    # Danger warnings — printed BEFORE the per-patch
                    # table so authors see them without scrolling.
                    has_inserts = False
                    if os.path.isfile(selected.path):
                        try:
                            with open(selected.path, encoding="utf-8") as _f:
                                _doc = json.load(_f)
                            has_inserts = any(
                                c.get("type") == "insert"
                                for p in _doc.get("patches") or []
                                for c in p.get("changes") or [])
                        except Exception:
                            pass
                    danger_lines = iteminfo_inspector.format_danger_warnings(
                        selected.inspections, has_inserts=has_inserts)
                    if danger_lines:
                        lines.append("")
                        lines.extend(danger_lines)
                # Reparse-Diff report: only populated when mode ==
                # reparse_diff. Shows the recovered FieldEdits that will
                # flow into the merge.
                rep = getattr(selected, "reparse_report", None)
                if rep is not None:
                    lines.append("")
                    lines.append("REPARSE-DIFF (recovered field edits)")
                    lines.extend(
                        iteminfo_inspector.format_field_edits_summary(rep))
            # External-mod companion bundle surface (folder_paz may
            # carry equipslotinfo / skill in the source PAZ)
            elif selected.kind == "folder_paz":
                if selected.staged_skill_files or selected.staged_equip_files:
                    lines.append("")
                    lines.append("EXTRA FILES BUNDLED FROM MOD")
                    if selected.staged_skill_files:
                        lines.append(f"  skill bundle  : {', '.join(sorted(selected.staged_skill_files))}")
                    if selected.staged_equip_files:
                        lines.append(f"  equip bundle  : {', '.join(sorted(selected.staged_equip_files))}")
            lines.append("")

        # Summary — always shown after selection (if any)
        mods_on = [m for m in self._mods if m.enabled and m.ok and m.kind]
        mods_off = [m for m in self._mods if (not m.enabled) and m.ok and m.kind]
        lines.append(f"STACK — {len(mods_on)} enabled, {len(mods_off)} disabled, "
                     f"{len(self._mods) - len(mods_on) - len(mods_off)} skipped")

        if self._conflicts:
            lines.append("")
            lines.append(f"CONFLICTS — {len(self._conflicts)} field-level "
                         "(install order wins; loser listed beside winner)")
            lines.append("─" * 58)
            # Cap UI to avoid flooding the pane on mega-merges
            for c in self._conflicts[:300]:
                lines.append(f"  {c.entry_key}.{c.field_path}")
                w_tag = (f" (patch #{c.winner_patch_index})"
                         if c.winner_patch_index is not None else "")
                l_tag = (f" (patch #{c.loser_patch_index})"
                         if c.loser_patch_index is not None else "")
                lines.append(f"      ✔ {c.winner_mod}{w_tag} = {c.winner_value!r}")
                lines.append(f"      ✘ {c.loser_mod}{l_tag}  = {c.loser_value!r}")
            if len(self._conflicts) > 300:
                lines.append(f"  … {len(self._conflicts) - 300} more "
                             "(truncated for UI)")
        elif self._merged_items:
            lines.append("")
            lines.append("No field-level conflicts — all sources merged "
                         "without stepping on each other.")

        self._details.setPlainText("\n".join(lines) if lines else
            "Drop mods above, or use + Add / ⇅ Pull Buffs in the Sources panel.")

    # ------------------------------------------------------------
    def _apply_single_stack(self):
        """Apply merged result to a user-chosen overlay folder number."""
        from PySide6.QtWidgets import QInputDialog
        group, ok = QInputDialog.getText(
            self, "Apply Single Stack",
            "Overlay folder number to deploy into:\n\n"
            "e.g. 0058 = ItemBuffs slot, 0062 = Stacker default\n"
            "This REPLACES whatever is in that folder.",
            text="0058")
        if not ok or not group.strip():
            return
        group = group.strip().zfill(4)
        if not group.isdigit() or len(group) != 4:
            QMessageBox.warning(self, "Apply Single Stack",
                f"'{group}' is not a valid 4-digit overlay number.")
            return

        reply = QMessageBox.question(
            self, "Apply Single Stack",
            f"Deploy merged result into <game>/{group}/ ?\n\n"
            f"This will REPLACE whatever is currently in {group}/.\n"
            f"PAPGT will be updated for {group}/.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply != QMessageBox.Yes:
            return

        # Run the normal merge pipeline but override the target group
        self._single_stack_group = group
        try:
            self._run(install=True)
        finally:
            self._single_stack_group = None

    # ------------------------------------------------------------
    def _uninstall_stack(self):
        """Remove Stacker's overlay from the game. Deletes <game>/<group>/
        and drops its entry from meta/0.papgt. Other overlays (JMM's or
        ItemBuffs' direct 0058/) are left alone."""
        game = self._game_edit.text().strip()
        if not game or not os.path.isdir(game):
            QMessageBox.warning(self, "Stacker Tool",
                "Pick a valid Crimson Desert install folder first.")
            return

        groups_to_remove = [OVERLAY_GROUP_DEFAULT, OVERLAY_EQUIP_GROUP_DEFAULT]
        group = OVERLAY_GROUP_DEFAULT  # kept for backward compat in log messages
        group_dirs = [(g, os.path.join(game, g)) for g in groups_to_remove]
        papgt_path = os.path.join(game, "meta", "0.papgt")

        overlays_existing = [g for g, d in group_dirs if os.path.isdir(d)]
        papgt_entries_present: list[str] = []
        try:
            import crimson_rs
            if os.path.isfile(papgt_path):
                cur = crimson_rs.parse_papgt_file(papgt_path)
                existing_names = {e.get("group_name") for e in cur.get("entries", [])}
                papgt_entries_present = [g for g in groups_to_remove
                                         if g in existing_names]
        except Exception as e:
            self._log_line(f"  (warn) couldn't inspect PAPGT: {e}")

        if not overlays_existing and not papgt_entries_present:
            QMessageBox.information(self, "Stacker Tool",
                f"No Stacker overlay installed ({' / '.join(g for g, _ in group_dirs)} "
                "don't exist and PAPGT has no entries).")
            return

        dirs_list = '\n'.join(f"  • Delete {d}" for _, d in group_dirs)
        confirm = QMessageBox.question(
            self, "Stacker Tool",
            f"Remove Stacker's overlay?\n\n"
            f"{dirs_list}\n"
            f"  • Drop PAPGT entries for {', '.join(groups_to_remove)}\n\n"
            "Other overlays (JMM's, ItemBuffs') are not touched.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if confirm != QMessageBox.Yes:
            return

        self._log.clear()
        self._log_line(f"Removing Stacker overlay from {game}…")
        try:
            for g, d in group_dirs:
                if os.path.isdir(d):
                    shutil.rmtree(d)
                    self._log_line(f"  deleted {d}")
            import crimson_rs
            if os.path.isfile(papgt_path):
                cur = crimson_rs.parse_papgt_file(papgt_path)
                before = len(cur["entries"])
                cur["entries"] = [e for e in cur["entries"]
                                  if e.get("group_name") not in groups_to_remove]
                if len(cur["entries"]) != before:
                    crimson_rs.write_papgt_file(cur, papgt_path)
                    self._log_line(
                        f"  dropped {', '.join(groups_to_remove)} from meta/0.papgt "
                        f"({before} → {len(cur['entries'])} entries)")
            self._log_line("✔ Stacker overlay(s) removed.")
            QMessageBox.information(self, "Stacker Tool",
                "Stacker overlay removed. Launch the game to verify.")
        except Exception as e:
            self._log_line(f"✘ Uninstall failed: {e}")
            QMessageBox.critical(self, "Stacker Tool",
                f"Uninstall failed:\n{e}")

    # ------------------------------------------------------------
    def _send_to_buffs(self):
        """Push the merged dict list into ItemBuffs session state so the
        user can hand-tune and re-export through existing buttons."""
        if not self._merged_items:
            QMessageBox.information(self, "Stacker Tool",
                "Run Preview Merge first so there's a merged result to send.")
            return
        if self._buffs_tab is None:
            QMessageBox.warning(self, "Stacker Tool",
                "ItemBuffs tab reference not available.")
            return
        try:
            import crimson_rs
            game = self._game_edit.text().strip()
            vanilla = bytes(crimson_rs.extract_file(
                game, "0008", INTERNAL_DIR, ITEMINFO_PABGB))
            self._buffs_tab._buff_data = bytearray(vanilla)
            self._buffs_tab._buff_rust_items = self._merged_items
            try:
                _van_items = crimson_rs.parse_iteminfo_from_bytes(vanilla)
            except Exception:
                _van_items, _ = _parse_with_fallback(crimson_rs, game, vanilla)
            self._buffs_tab._buff_rust_items_original = copy.deepcopy(_van_items)
            self._buffs_tab._buff_rust_lookup = {
                it["key"]: it for it in self._merged_items if "key" in it}
            self._buffs_tab._buff_use_rust = True
            self._buffs_tab._buff_modified = True
            if hasattr(self._buffs_tab, "_rebuild_index"):
                self._buffs_tab._rebuild_index()
            self._log_line(f"✔ Sent {len(self._merged_items)} merged entries to ItemBuffs.")
            QMessageBox.information(self, "Stacker Tool",
                "Merged state loaded into ItemBuffs tab.\n"
                "Switch to ItemBuffs to hand-tune, then use its\n"
                "'Apply to Game' to export.")
        except Exception as e:
            self._log_line(f"✘ send-to-buffs failed: {e}")

    # ── Export Field JSON ──────────────────────────────────────

    # Field names that changed between parser versions
    _FIELD_RENAMES = {'unk_texture_path': 'default_texture_path'}
    _FIELD_REMOVED = {'usable_alert'}

    def _export_field_json(self):
        """Export as Format 3 semantic JSON (field names, not bytes).

        Two paths:
        A) If PREVIEW has been run and merged_items differ from vanilla,
           diff the merged result.
        B) If legacy JSON sources are present, use the OLD parser + OLD
           vanilla to recover what the mod intended, then emit field-name
           intents compatible with the CURRENT game version.
        """
        # Collect legacy JSON sources from the mod list
        legacy_sources = [
            m for m in self._mods
            if m.enabled and m.kind == "legacy_json"
        ]

        # Path A: diff merged result vs vanilla (for itembuffs_edits, folder_paz, etc.)
        has_merged = bool(self._merged_items and self._vanilla_items)
        # Path B: legacy JSON translation via old parser
        has_legacy = bool(legacy_sources)

        if not has_merged and not has_legacy:
            QMessageBox.information(self, "Export Field JSON",
                "Nothing to export.\n\n"
                "Either run PREVIEW first (for merged edits),\n"
                "or add a legacy JSON mod to translate.")
            return

        intents = []
        mod_title = "Merged Stack"

        # ── Path A: diff merged vs current vanilla (preferred) ──
        # If PREVIEW has been run, the merged result already incorporates
        # ALL source types (legacy_json, folder_paz, itembuffs, etc.)
        # via reparse-diff / semantic / strict modes. Diffing the merged
        # dicts against vanilla gives correct field-name intents regardless
        # of whether the source was legacy byte offsets, inserts, or dicts.
        if has_merged:
            vanilla_lookup = {
                it['string_key']: it for it in self._vanilla_items}
            for merged_item in self._merged_items:
                skey = merged_item.get('string_key', '')
                ikey = merged_item.get('key', 0)
                vanilla = vanilla_lookup.get(skey)
                if not vanilla:
                    intents.append({
                        'entry': skey, 'key': ikey,
                        'op': 'add_entry',
                        'data': _strip_meta(merged_item),
                    })
                    continue
                diffs = _deep_diff_to_intents(skey, ikey, vanilla, merged_item)
                intents.extend(diffs)

        # ── Path B (fallback): legacy JSON → baseline reparse-diff ──
        # Only used when PREVIEW was NOT run (no merged items) but legacy
        # JSON sources are present. Applies byte patches to a matching
        # baseline, reparses, and diffs.
        if not has_merged and has_legacy:
            legacy_intents, title = self._translate_legacy_to_field(
                legacy_sources)
            if legacy_intents is None:
                return
            intents.extend(legacy_intents)
            if title:
                mod_title = title

        # ── Extra targets via the registry (multi-target schema, since 1.1.4) ──
        # Walk every enabled entry in _FIELD_JSON_TARGET_REGISTRY (defined
        # at module top). Each entry knows where its merged bytes live on
        # `self`, which vanilla file to diff against, and which helper
        # produces intents. Adding a new target = one registry entry — the
        # export pipeline below picks it up automatically. Pre-1.1.4 the
        # exporter only emitted iteminfo and silently dropped everything
        # else (the SuperMegaMod-doesn't-unlock-weapons class of bug).
        extra_targets: list[tuple[str, list[dict]]] = []
        try:
            import crimson_rs
        except Exception as _imp_err:
            crimson_rs = None
            self._log_line(
                f"  ⚠ crimson_rs unavailable ({_imp_err}); extra-target "
                f"diff skipped — export will be iteminfo-only")

        if crimson_rs is not None:
            for entry in _FIELD_JSON_TARGET_REGISTRY:
                if not entry.get('enabled'):
                    if entry.get('todo'):
                        self._log_line(
                            f"  ◇ skipping {entry['name']}: {entry['todo']}")
                    continue
                merged_snap = getattr(self, entry['merged_attr'], None) or {}
                pabgb_name = entry['pabgb_filename']
                pabgh_name = entry['pabgh_filename']
                if not (merged_snap.get(pabgb_name) and merged_snap.get(pabgh_name)):
                    # No merged bytes for this target — sources didn't ship
                    # any modifications to it. Not an error, just nothing
                    # to emit.
                    continue
                if not self._game_path:
                    self._log_line(
                        f"  ⚠ {entry['name']} diff skipped: game path not set")
                    continue
                try:
                    vanilla_pabgb = crimson_rs.extract_file(
                        self._game_path, entry['vanilla_group'],
                        entry['vanilla_dir'], pabgb_name)
                    vanilla_pabgh = crimson_rs.extract_file(
                        self._game_path, entry['vanilla_group'],
                        entry['vanilla_dir'], pabgh_name)
                    target_intents = entry['diff_fn'](
                        vanilla_pabgh, vanilla_pabgb,
                        merged_snap[pabgh_name],
                        merged_snap[pabgb_name])
                except Exception as e:
                    # Don't block the export — the iteminfo intents are
                    # still useful even if a side-target diff fails.
                    # Surface the failure in the log so the author knows
                    # which half didn't ship.
                    self._log_line(
                        f"  ⚠ {entry['name']} diff failed: {e} — "
                        f"export will skip this target")
                    continue
                if target_intents:
                    extra_targets.append((entry['name'], target_intents))
                    self._log_line(
                        f"  + {len(target_intents)} {entry['label']} "
                        f"intent(s)")

        # ── Catch-all: every .pabgb/.pabgh pair in _merged_other_files
        # that no registry entry already handled. Uses the generic
        # blob-table diff (key/string_key/is_blocked + per-record
        # _blob_b64). DMM 1.3.3+'s apply_v3_to_blob_table_body consumes
        # this shape directly. Field-level intents for typed-prefix
        # tables would need per-table parser dispatch on the dmm-parser
        # side — that's a follow-up. For now, this unblocks every mod
        # that touches buff_info / condition_info / gimmick_info /
        # store_info / etc. so their edits actually ship.
        if crimson_rs is not None:
            other_snap = getattr(self, "_merged_other_files", None) or {}
            handled_targets = {e['name'] for e in _FIELD_JSON_TARGET_REGISTRY}
            pabgb_names = sorted(
                n for n in other_snap if n.endswith(".pabgb"))
            for pabgb_name in pabgb_names:
                if pabgb_name in handled_targets:
                    # Registry already covered it
                    continue
                pabgh_name = pabgb_name[:-len(".pabgb")] + ".pabgh"
                if pabgh_name not in other_snap:
                    self._log_line(
                        f"  ⚠ {pabgb_name} skipped: no sister "
                        f"{pabgh_name} in mod stack")
                    continue
                if not self._game_path:
                    self._log_line(
                        f"  ⚠ {pabgb_name} catch-all skipped: "
                        f"game path not set")
                    continue
                try:
                    vanilla_pabgb = bytes(crimson_rs.extract_file(
                        self._game_path, '0008',
                        INTERNAL_DIR, pabgb_name))
                    vanilla_pabgh = bytes(crimson_rs.extract_file(
                        self._game_path, '0008',
                        INTERNAL_DIR, pabgh_name))
                    # Try field-level diff first via dmm_parser.parse_table.
                    # 122 tables supported as of dmm-parser commit f054b5e
                    # (gimmick_info, condition_info, drop_set_info,
                    # character_info, buff_info, etc.). Falls back to the
                    # blob-level diff for tables dmm_parser doesn't know
                    # or when dmm_parser isn't installed.
                    t_intents, source = _diff_table_field_level(
                        vanilla_pabgh, vanilla_pabgb,
                        other_snap[pabgh_name],
                        other_snap[pabgb_name],
                        pabgb_name)
                    if not source:
                        # Field-level path declined — use blob-level.
                        t_intents = _diff_blob_table_to_intents(
                            vanilla_pabgh, vanilla_pabgb,
                            other_snap[pabgh_name],
                            other_snap[pabgb_name],
                            pabgb_name)
                        source = "blob"
                except Exception as e:
                    # Don't block the export — iteminfo + registry
                    # targets already gathered are still valid.
                    self._log_line(
                        f"  ⚠ {pabgb_name} catch-all diff failed: {e}"
                        f" — export will skip this target")
                    continue
                if t_intents:
                    extra_targets.append((pabgb_name, t_intents))
                    self._log_line(
                        f"  + {len(t_intents)} {pabgb_name} "
                        f"intent(s) ({source}-level diff)")

        if not intents and not extra_targets:
            QMessageBox.information(self, "Export Field JSON",
                "No field-level changes found. Nothing to export.")
            return

        # Pick save path
        default_name = mod_title.replace(' ', '_') + ".field.json"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Field JSON", default_name,
            "Field JSON (*.field.json *.json);;All Files (*)")
        if not path:
            return

        # Build the doc. Multi-target shape when ANY non-iteminfo target
        # has intents (the 1.1.4 spec extension consumed by DMM 1.3.3+).
        # Single-target legacy shape otherwise — preserves byte-for-byte
        # compatibility with older DMM releases for the common case.
        if extra_targets:
            total = len(intents) + sum(len(it) for _, it in extra_targets)
            target_count = (1 if intents else 0) + len(extra_targets)
            target_summary = ', '.join(
                [f'{len(intents)} iteminfo'] if intents else []
                + [f'{len(it)} {name.split(".")[0]}' for name, it in extra_targets]
            )
            targets_array: list[dict] = []
            if intents:
                targets_array.append({
                    'file': 'iteminfo.pabgb', 'intents': intents,
                })
            for tname, t_intents in extra_targets:
                targets_array.append({'file': tname, 'intents': t_intents})

            doc = {
                'modinfo': {
                    'title': mod_title,
                    'version': '1.0',
                    'author': 'CrimsonGameMods Stacker',
                    'description': (
                        f'{total} field-level intent(s) across '
                        f'{target_count} target(s) — {target_summary}'),
                    'note': ('Field JSON v3.1 (multi-target field patching) '
                             '— uses field names, survives game updates. '
                             'Requires DMM 1.3.4+ for non-iteminfo targets; '
                             'older DMM versions will apply iteminfo intents '
                             'only. See FIELD_JSON_V3_1_SPEC.md.'),
                },
                'format': 3,
                'format_minor': 1,
                'targets': targets_array,
            }
            ui_lines = []
            if intents:
                ui_lines.append(f"  • {len(intents)} iteminfo.pabgb intents")
            for tname, t_intents in extra_targets:
                ui_lines.append(f"  • {len(t_intents)} {tname} intents")
            log_msg = (
                f"✔ Exported {total} field-level intents across "
                f"{target_count} target(s) (multi-target) to {path}")
            ui_msg = (
                f"Exported {total} field-level intents across "
                f"{target_count} targets:\n"
                + '\n'.join(ui_lines)
                + f"\n\nThis file uses field names — survives game updates.\n"
                + f"Requires DMM 1.3.3+ for the non-iteminfo target(s) to apply.\n"
                + f"File: {path}")
        else:
            doc = {
                'modinfo': {
                    'title': mod_title,
                    'version': '1.0',
                    'author': 'CrimsonGameMods Stacker',
                    'description': f'{len(intents)} field-level intent(s)',
                    'note': 'Format 3 — uses field names, survives game updates',
                },
                'format': 3,
                'target': 'iteminfo.pabgb',
                'intents': intents,
            }
            log_msg = f"✔ Exported {len(intents)} intents to {path}"
            ui_msg = (f"Exported {len(intents)} field-level intents.\n\n"
                      f"This file uses field names — it survives game updates.\n"
                      f"File: {path}")

        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(doc, f, indent=2, ensure_ascii=False, default=str)
            self._log_line(log_msg)
            QMessageBox.information(self, "Export Field JSON", ui_msg)
        except Exception as e:
            QMessageBox.critical(self, "Export Failed", str(e))

    def _export_legacy_json(self):
        """Export merged result as Format 2 byte-diff JSON (CrimsonWings style)."""
        if not hasattr(self, '_merged_items') or not self._merged_items:
            QMessageBox.information(self, "Export Legacy JSON",
                "Run PREVIEW first to build the merged result.")
            return
        if not hasattr(self, '_vanilla_items') or not self._vanilla_items:
            QMessageBox.warning(self, "Export Legacy JSON",
                "No vanilla baseline — run PREVIEW first.")
            return

        import crimson_rs

        game = self._game_edit.text().strip()
        try:
            vanilla_bytes = bytes(crimson_rs.extract_file(
                game, "0008", INTERNAL_DIR, ITEMINFO_PABGB))
        except Exception as e:
            QMessageBox.critical(self, "Export Legacy JSON",
                f"Could not extract vanilla iteminfo:\n{e}")
            return

        van_pabgh = bytes(crimson_rs.extract_file(
            game, '0008', INTERNAL_DIR, ITEMINFO_PABGH))

        merged_by_key = {it['key']: it for it in self._merged_items}
        van_by_key = {it['key']: it for it in self._vanilla_items}

        merged_full = self._serialize_merged_iteminfo(
            vanilla_bytes, van_pabgh, self._merged_items)
        if merged_full is None:
            QMessageBox.critical(self, "Export Legacy JSON",
                "Serialization failed.")
            return

        van_count = struct.unpack_from('<H', van_pabgh, 0)[0]
        van_rs = (len(van_pabgh) - 2) // van_count if van_count else 8

        changes = []
        for pi in range(van_count):
            prec = 2 + pi * van_rs
            if prec + van_rs > len(van_pabgh):
                break
            off = struct.unpack_from('<I', van_pabgh, prec + (van_rs - 4))[0]
            nxt = len(vanilla_bytes)
            if pi + 1 < van_count:
                nrec = 2 + (pi + 1) * van_rs
                if nrec + van_rs <= len(van_pabgh):
                    nxt = struct.unpack_from('<I', van_pabgh, nrec + (van_rs - 4))[0]
            size = nxt - off
            if off + size > len(vanilla_bytes) or off + size > len(merged_full):
                continue

            van_chunk = vanilla_bytes[off:off + size]
            mod_chunk = merged_full[off:off + size]
            if van_chunk == mod_chunk:
                continue

            pk = struct.unpack_from('<I', vanilla_bytes, off)[0]
            nl = struct.unpack_from('<I', vanilla_bytes, off + 4)[0]
            entry_name = vanilla_bytes[off + 8:off + 8 + min(nl, 200)].decode(
                'ascii', errors='replace')

            j = 0
            while j < len(van_chunk):
                if j < len(mod_chunk) and van_chunk[j] != mod_chunk[j]:
                    run_start = j
                    while j < min(len(van_chunk), len(mod_chunk)) and van_chunk[j] != mod_chunk[j]:
                        j += 1
                    changes.append({
                        'entry': entry_name,
                        'rel_offset': run_start,
                        'offset': off + run_start,
                        'original': van_chunk[run_start:j].hex(),
                        'patched': mod_chunk[run_start:j].hex(),
                    })
                else:
                    j += 1

        if not changes:
            QMessageBox.information(self, "Export Legacy JSON",
                "No differences from vanilla to export.")
            return

        from PySide6.QtWidgets import QInputDialog
        title, ok = QInputDialog.getText(
            self, "Mod title", "Title for this legacy JSON mod:",
            text="Stacker Merged Mod")
        if not ok or not title.strip():
            return

        path, _ = QFileDialog.getSaveFileName(
            self, "Export Legacy JSON", "stacker_mod.json",
            "JSON Files (*.json);;All Files (*)")
        if not path:
            return

        doc = {
            'modinfo': {
                'title': title.strip(),
                'version': '1.0',
                'author': 'CrimsonGameMods Stacker',
                'description': f'{len(changes)} byte patch(es)',
            },
            'format': 2,
            'patches': [{
                'game_file': 'gamedata/iteminfo.pabgb',
                'source_group': '0008',
                'changes': changes,
            }],
        }

        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(doc, f, indent=2, ensure_ascii=False)
            self._log_line(f"✔ Exported {len(changes)} legacy patches to {path}")
            QMessageBox.information(self, "Export Legacy JSON",
                f"Exported {len(changes)} byte-level patches.\n\nFile: {path}")
        except Exception as e:
            QMessageBox.critical(self, "Export Failed", str(e))

    def _serialize_merged_iteminfo(self, vanilla_bytes, van_pabgh, merged_items):
        """Serialize merged items back into a pabgb blob matching vanilla layout."""
        try:
            import crimson_rs
            van_count = struct.unpack_from('<H', van_pabgh, 0)[0]
            van_rs = (len(van_pabgh) - 2) // van_count if van_count else 8
            merged_by_key = {it['key']: it for it in merged_items}
            unparsed = getattr(self, '_stacker_unparsed_raw', []) or []
            unparsed_map = {}
            for raw in unparsed:
                uk = struct.unpack_from('<I', raw, 0)[0]
                unparsed_map[uk] = raw

            order = []
            for pi in range(van_count):
                prec = 2 + pi * van_rs
                if prec + van_rs > len(van_pabgh):
                    break
                psoff = struct.unpack_from('<I', van_pabgh, prec + (van_rs - 4))[0]
                pnxt = len(vanilla_bytes)
                if pi + 1 < van_count:
                    nrec = 2 + (pi + 1) * van_rs
                    if nrec + van_rs <= len(van_pabgh):
                        pnxt = struct.unpack_from('<I', van_pabgh, nrec + (van_rs - 4))[0]
                pk = struct.unpack_from('<I', vanilla_bytes, psoff)[0]
                order.append((pk, psoff, pnxt - psoff))

            final = bytearray()
            for pk, psoff, psize in order:
                if pk in merged_by_key:
                    final.extend(crimson_rs.serialize_iteminfo([merged_by_key[pk]]))
                elif pk in unparsed_map:
                    final.extend(unparsed_map[pk])
                else:
                    final.extend(vanilla_bytes[psoff:psoff + psize])
            return bytes(final)
        except Exception:
            import traceback; traceback.print_exc()
            return None

    def _translate_legacy_to_field(self, legacy_sources: list
                                   ) -> tuple[list | None, str]:
        """Translate legacy JSON mods to field-name intents.

        Supports mods targeting ANY game version we have a baseline for.
        For each mod, picks the baseline whose vanilla bytes match the
        mod's 'original' hex values, then diffs parsed dicts to emit
        field-name intents.

        Returns (intents_list, mod_title) or (None, '') on failure/cancel.
        """
        import crimson_rs

        # ── 1) Discover all available baselines ──
        baselines: list[tuple[str, str]] = []  # (version, path)
        for base in [os.path.dirname(os.path.abspath(__file__)),
                     getattr(sys, '_MEIPASS', ''), os.getcwd()]:
            for rel in [os.path.join(base, '..', '..', 'game_baselines'),
                        os.path.join(base, 'game_baselines')]:
                bd = os.path.normpath(rel)
                if not os.path.isdir(bd):
                    continue
                for ver in sorted(os.listdir(bd)):
                    candidate = os.path.join(bd, ver, 'iteminfo.pabgb')
                    if os.path.isfile(candidate):
                        baselines.append((ver, candidate))
            if baselines:
                break

        if not baselines:
            path, _ = QFileDialog.getOpenFileName(
                self, "Select vanilla iteminfo.pabgb", "",
                "PABGB files (*.pabgb);;All Files (*)")
            if not path:
                return None, ''
            baselines.append(("unknown", path))

        self._log_line(f"Found {len(baselines)} baseline(s): "
                       f"{', '.join(v for v, _ in baselines)}")

        # ── 2) Load baseline data + parsers ──
        # Each cache entry: (raw_data, parsed_items, lookup_dict, parser_fn)
        # parser_fn is the parse function that successfully parsed this baseline
        baseline_cache: dict[str, tuple[bytes, list, dict, object]] = {}
        failed_vers: list[tuple[str, str]] = []
        for ver, path in baselines:
            try:
                data = open(path, 'rb').read()
                items = crimson_rs.parse_iteminfo_from_bytes(data)
                lookup = {it['string_key']: it for it in items}
                baseline_cache[ver] = (data, items, lookup,
                                       crimson_rs.parse_iteminfo_from_bytes)
                self._log_line(f"  Baseline {ver}: {len(items)} items, "
                               f"{len(data):,} bytes (current parser)")
            except Exception:
                failed_vers.append((ver, path))

        if failed_vers:
            legacy_rs = self._load_legacy_parser()
            if legacy_rs:
                for ver, path in failed_vers:
                    try:
                        data = open(path, 'rb').read()
                        items = legacy_rs.parse_iteminfo_from_bytes(data)
                        lookup = {it['string_key']: it for it in items}
                        baseline_cache[ver] = (data, items, lookup,
                                               legacy_rs.parse_iteminfo_from_bytes)
                        self._log_line(f"  Baseline {ver}: {len(items)} items "
                                       f"(legacy parser)")
                    except Exception as e:
                        self._log_line(f"  Baseline {ver}: FAILED ({e})")
            else:
                for ver, _ in failed_vers:
                    self._log_line(f"  Baseline {ver}: current parser failed, "
                                   f"no legacy parser available")

        if not baseline_cache:
            QMessageBox.critical(self, "Export Field JSON",
                "Could not parse any baseline file.")
            return None, ''

        # ── 3) For each mod, find matching baseline and translate ──
        all_intents = []
        mod_title = None

        for m in legacy_sources:
            try:
                with open(m.path, encoding='utf-8') as f:
                    doc = json.load(f)
            except Exception as e:
                self._log_line(f"  ✘ {m.name}: could not read ({e})")
                continue

            if not mod_title:
                mi = doc.get('modinfo', doc)
                mod_title = mi.get('title', mi.get('name', m.name))

            patches_list = doc.get('patches', [])
            if not patches_list:
                continue
            changes = []
            for p in patches_list:
                changes.extend(p.get('changes', []))
            if not changes:
                self._log_line(f"  ✘ {m.name}: no changes found")
                continue

            # Pick best baseline: the one where most 'original' hex values
            # match the vanilla bytes at the specified offsets
            best_ver = None
            best_score = -1
            best_data = None
            best_lookup = None
            best_parser = None
            for ver, (data, _items, lookup, parser_fn) in baseline_cache.items():
                score = 0
                for c in changes:
                    off = c.get('offset')
                    orig_hex = c.get('original', '')
                    if off is None or not orig_hex:
                        continue
                    try:
                        if isinstance(off, int):
                            pass
                        elif isinstance(off, str):
                            s = off.strip()
                            if s.lower().startswith('0x'):
                                off = int(s, 16)
                            else:
                                try:
                                    off = int(s, 16)
                                except ValueError:
                                    off = int(s)
                        else:
                            off = int(off)
                    except (TypeError, ValueError):
                        continue
                    orig_bytes = bytes.fromhex(orig_hex)
                    if off + len(orig_bytes) <= len(data):
                        if data[off:off + len(orig_bytes)] == orig_bytes:
                            score += 1
                if score > best_score:
                    best_score = score
                    best_ver = ver
                    best_data = data
                    best_lookup = lookup
                    best_parser = parser_fn

            if best_data is None or best_lookup is None:
                self._log_line(f"  ✘ {m.name}: no matching baseline found")
                continue

            match_pct = (best_score / len(changes) * 100) if changes else 0
            self._log_line(f"  {m.name}: matched baseline {best_ver} "
                           f"({best_score}/{len(changes)} = {match_pct:.0f}%)")

            # Apply byte patches to matched baseline.
            # Sort by offset descending so inserts don't shift later offsets.
            def _change_off(c):
                v = c.get('offset', c.get('rel_offset', 0))
                try:
                    return int(v, 16) if isinstance(v, str) else int(v)
                except (TypeError, ValueError):
                    return 0
            sorted_changes = sorted(changes, key=_change_off, reverse=True)
            patched_data = bytearray(best_data)
            applied = 0
            for c in sorted_changes:
                try:
                    applied += _apply_one_legacy_patch(
                        patched_data, best_data, c)
                except Exception:
                    pass

            self._log_line(f"  {m.name}: applied {applied}/{len(changes)} "
                           f"patches")

            # Reparse patched data with the SAME parser that loaded this baseline
            try:
                patched_items = best_parser(bytes(patched_data))
            except Exception as e:
                self._log_line(f"  ✘ {m.name}: reparse failed ({e})")
                continue
            patched_lookup = {it['string_key']: it for it in patched_items}

            # Diff old vs patched → field-name intents
            mod_intents = 0
            for skey, orig in best_lookup.items():
                patched = patched_lookup.get(skey)
                if not patched:
                    continue
                diffs = _deep_diff_to_intents(
                    skey, orig.get('key', 0), orig, patched)
                for intent in diffs:
                    field = intent.get('field', '')
                    for old_name, new_name in self._FIELD_RENAMES.items():
                        if field == old_name or field.startswith(
                                old_name + '.'):
                            intent['field'] = field.replace(
                                old_name, new_name, 1)
                    top_field = field.split('.')[0].split('[')[0]
                    if top_field in self._FIELD_REMOVED:
                        continue
                    all_intents.append(intent)
                    mod_intents += 1

            for skey in patched_lookup:
                if skey not in best_lookup:
                    pi = patched_lookup[skey]
                    all_intents.append({
                        'entry': skey,
                        'key': pi.get('key', 0),
                        'op': 'add_entry',
                        'data': _strip_meta(pi),
                    })
                    mod_intents += 1

            self._log_line(f"  ✓ {m.name}: {mod_intents} field-level intents "
                           f"recovered")

        return all_intents, mod_title or "Translated Legacy Mod"

    def _load_legacy_parser(self):
        """Load the legacy crimson_rs parser from _legacy/ dir. Returns module or None."""
        try:
            import importlib.util
            legacy_pyd = None
            for base in [os.path.dirname(os.path.abspath(__file__)),
                         getattr(sys, '_MEIPASS', ''), os.getcwd()]:
                for rel in [
                    os.path.join(base, '..', '..', 'crimson_rs',
                                 '_legacy', 'crimson_rs.pyd'),
                    os.path.join(base, 'crimson_rs', '_legacy',
                                 'crimson_rs.pyd'),
                ]:
                    p = os.path.normpath(rel)
                    if os.path.isfile(p):
                        legacy_pyd = p
                        break
                if legacy_pyd:
                    break
            if not legacy_pyd:
                return None
            saved_modules = {}
            for k in list(sys.modules):
                if k == 'crimson_rs' or k.startswith('crimson_rs.'):
                    saved_modules[k] = sys.modules.pop(k)
            try:
                spec = importlib.util.spec_from_file_location(
                    "crimson_rs", legacy_pyd)
                legacy_rs = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(legacy_rs)
                return legacy_rs
            finally:
                for k in list(sys.modules):
                    if k == 'crimson_rs' or k.startswith('crimson_rs.'):
                        sys.modules.pop(k, None)
                sys.modules.update(saved_modules)
        except Exception:
            return None
