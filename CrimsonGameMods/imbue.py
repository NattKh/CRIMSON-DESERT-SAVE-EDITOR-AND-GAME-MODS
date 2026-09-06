from __future__ import annotations

import json
import logging
import os
import struct
from typing import Any, Callable, Optional


_MARKER = bytes.fromhex("73 E1 C5 EA 73 E1 C5 EA")

log = logging.getLogger(__name__)

IMBUE_SKILLS: dict[int, tuple[str, str]] = {
    91101: ("Lightning", "Equip_Passive_LightningWeapon"),
    91104: ("Ice",       "Equip_Passive_IceWeapon"),
    91105: ("Fire",      "Equip_Passive_FireWeapon"),
    91109: ("Bismuth",   "Equip_Passive_Bismuth_Spear"),
}


_IMBUE_LOOKUP_CACHE: Optional[dict[int, dict]] = None
_IMBUE_GIMMICK_MAP_CACHE: Optional[dict[int, dict]] = None
_PASSIVE_CATALOG_CACHE: Optional[dict[int, dict]] = None
_VISUAL_CLASS_CACHE: Optional[dict[int, str]] = None


def _data_dir() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')


def _load_json(filename: str) -> Optional[dict]:
    path = os.path.join(_data_dir(), filename)
    if not os.path.isfile(path):
        log.warning("imbue: data file missing: %s", path)
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        log.warning("imbue: failed to load %s: %s", path, e)
        return None


def get_imbue_lookup() -> dict[int, dict]:
    """item_type -> canonical docking fields (hash, socket, flags). Cached."""
    global _IMBUE_LOOKUP_CACHE
    if _IMBUE_LOOKUP_CACHE is not None:
        return _IMBUE_LOOKUP_CACHE
    doc = _load_json('imbue_lookup.json') or {}
    raw = (doc.get('type_to_canonical') or {})
    _IMBUE_LOOKUP_CACHE = {int(k): v for k, v in raw.items()}
    return _IMBUE_LOOKUP_CACHE


def get_imbue_gimmick_map() -> dict[int, dict]:
    """skill_id -> {per_item_type: {item_type_str: {canonical_gimmick_info, ...}}, ...}. Cached."""
    global _IMBUE_GIMMICK_MAP_CACHE
    if _IMBUE_GIMMICK_MAP_CACHE is not None:
        return _IMBUE_GIMMICK_MAP_CACHE
    doc = _load_json('imbue_gimmick_map.json') or {}
    _IMBUE_GIMMICK_MAP_CACHE = {int(k): v for k, v in doc.items()}
    return _IMBUE_GIMMICK_MAP_CACHE


def get_visual_class_map() -> dict[int, str]:
    """skill_id -> visual tier. Cached.

    Tiers:
      'visual'     — at least one vanilla item with this passive has non-zero
                     gimmick_info AND a non-empty attach_parent_socket_name.
                     Applying this imbue produces an attached VFX in-game
                     (Potter's Derictus Spear pattern).
      'functional' — vanilla items exist with a gimmick but socket is empty.
                     The gimmick is invisible (stealth/immunity/faction).
      'stat_only'  — no vanilla item uses this passive with a gimmick at all.
                     Skill filter edits can add stat buffs but no visual.
    """
    global _VISUAL_CLASS_CACHE
    if _VISUAL_CLASS_CACHE is not None:
        return _VISUAL_CLASS_CACHE

    gimmap = _load_json('imbue_gimmick_map.json') or {}
    gcat = _load_json('gimmick_item_catalog.json') or {}
    item_socket: dict[int, str] = {}
    for it in (gcat.get('items') or []):
        k = it.get('item_key')
        if k is None:
            continue
        dcd = it.get('docking_child_data') or {}
        item_socket[int(k)] = dcd.get('attach_parent_socket_name', '') or ''

    out: dict[int, str] = {}
    for sid_str, entry in gimmap.items():
        try:
            sid = int(sid_str)
        except (TypeError, ValueError):
            continue
        samples = entry.get('items_sample') or []
        has_socket = False
        has_any = False
        for s in samples:
            k = s.get('key')
            if k is None:
                continue
            if int(k) in item_socket:
                has_any = True
                if item_socket[int(k)]:
                    has_socket = True
                    break
        if has_socket:
            out[sid] = 'visual'
        elif has_any:
            out[sid] = 'functional'
    _VISUAL_CLASS_CACHE = out
    return out


def get_passive_skill_catalog() -> dict[int, dict]:
    """Full skill_id -> {name, display, pretty_name, description} map.

    Merges passive_skill_catalog.json (internal names) with
    buff_skill_descriptions.json (English pretty names + descriptions) so
    callers can show humans something better than 'Equip_Passive_GadgetGloves'.
    """
    global _PASSIVE_CATALOG_CACHE
    if _PASSIVE_CATALOG_CACHE is not None:
        return _PASSIVE_CATALOG_CACHE
    doc = _load_json('passive_skill_catalog.json') or {}
    descs = _load_json('buff_skill_descriptions.json') or {}
    vclass = get_visual_class_map()
    out: dict[int, dict] = {}
    for e in (doc.get('all_equip_passives') or []):
        sid = e.get('skill_id')
        if sid is None:
            continue
        desc_entry = descs.get(str(sid)) or {}
        pretty = desc_entry.get('name') or e.get('display', '') or e.get('name', '')
        out[int(sid)] = {
            'skill_id': int(sid),
            'name': e.get('name', ''),
            'display': e.get('display', ''),
            'pretty_name': pretty,
            'description': desc_entry.get('description', ''),
            'group': e.get('group', 'other'),
            'weapon': bool(e.get('weapon', False)),
            'visual_class': vclass.get(int(sid), 'stat_only'),
        }
    for sid, (disp, name) in IMBUE_SKILLS.items():
        if sid not in out:
            desc_entry = descs.get(str(sid)) or {}
            out[sid] = {
                'skill_id': sid,
                'name': name,
                'display': disp,
                'pretty_name': desc_entry.get('name') or disp,
                'description': desc_entry.get('description', ''),
                'group': 'imbue',
                'weapon': True,
                'visual_class': vclass.get(sid, 'stat_only'),
            }
    _PASSIVE_CATALOG_CACHE = out
    return out


def build_docking_child_data(target_item: dict, gimmick_info_key: int) -> Optional[dict]:
    """Construct a docking_child_data struct for target_item using vanilla patterns.

    Strategy:
      1. If target already has docking_child_data with populated hash + socket,
         return a COPY of target's own — preserves the correct per-item metadata.
      2. Otherwise, look up canonical fields by target['item_type'] from imbue_lookup.json
         (docking_tag_name_hash, attach_parent_socket_name, and all the boolean flags).
      3. Returns None if item_type is not in the lookup.
    """
    target_dcd = target_item.get('docking_child_data') or {}
    target_hash = (target_dcd.get('docking_tag_name_hash') or [0])[0]
    target_socket = target_dcd.get('attach_parent_socket_name') or ''

    if target_hash and target_socket:
        import copy as _copy
        out = _copy.deepcopy(target_dcd)
        out['gimmick_info_key'] = int(gimmick_info_key)
        return out

    lookup = get_imbue_lookup()
    it_type = target_item.get('item_type')
    if it_type is None:
        return None
    canonical = lookup.get(int(it_type))
    if not canonical:
        return None

    hash_val = canonical.get('canonical_hash') or 0
    socket = canonical.get('canonical_socket') or ''

    return {
        'gimmick_info_key': int(gimmick_info_key),
        'character_key': 0,
        'item_key': 0,
        'attach_parent_socket_name': socket,
        'attach_child_socket_name': '',
        'docking_tag_name_hash': [int(hash_val), 0, 0, 0],
        'docking_equip_slot_no': int(canonical.get('canonical_docking_equip_slot_no', 65535)),
        'spawn_distance_level': int(canonical.get('canonical_spawn_distance_level', 4294967295)),
        'is_item_equip_docking_gimmick': int(canonical.get('canonical_is_item_equip_docking_gimmick', 0)),
        'send_damage_to_parent': 0,
        'is_body_part': int(canonical.get('canonical_is_body_part', 0)),
        'docking_type': int(canonical.get('canonical_docking_type', 0)),
        'is_summoner_team': 0,
        'is_player_only': int(canonical.get('canonical_is_player_only', 0)),
        'is_npc_only': 0,
        'is_sync_break_parent': 0,
        'hit_part': 0,
        'detected_by_npc': 0,
        'is_bag_docking': 0,
        'enable_collision': 0,
        'disable_collision_with_other_gimmick': int(canonical.get('canonical_disable_collision_with_other_gimmick', 1)),
        'docking_slot_key': '',
        'inherit_summoner': 0,
        'summon_tag_name_hash': [0, 0, 0, 0],
        'animation_root_bone_name': '',
    }


def pick_gimmick_info_for(skill_id: int, item_type: int) -> Optional[int]:
    """Find the best gimmick_info value for (skill_id, item_type).

    Priority:
      1. Exact vanilla match — same skill, same item_type.
      2. Same skill, any item_type (vanilla reference).
      3. None.
    """
    gmap = get_imbue_gimmick_map()
    entry = gmap.get(int(skill_id))
    if not entry:
        return None
    per_type = entry.get('per_item_type') or {}

    if str(item_type) in per_type:
        return int(per_type[str(item_type)].get('canonical_gimmick_info') or 0) or None

    best_gimmick = None
    best_count = 0
    for _t, rec in per_type.items():
        all_g = rec.get('all_gimmicks_observed') or {}
        for g_str, cnt in all_g.items():
            if cnt > best_count:
                best_count = cnt
                best_gimmick = int(g_str)
    return best_gimmick


def get_imbue_plan(skill_id: int, target_item: dict) -> dict:
    """Return a complete plan for imbuing target_item with skill_id.

    Output:
      {
        'ok': bool,
        'reason': 'skill_no_vanilla' | 'type_not_supported' | 'preserved_target' | 'applied_canonical',
        'warnings': [str, ...],
        'patches': {
            'gimmick_info': int,
            'cooltime': int,   # guaranteed >=1
            'item_charge_type': int,
            'max_charged_useable_count': int,
            'respawn_time_seconds': int,
            'docking_child_data': {...},
        },
        'skill_filter_class_hash': int,  # for skill.pabgb patching
      }
    """
    warnings: list[str] = []
    it_type = target_item.get('item_type')
    class_hash = target_item.get('equip_type_info') or 0

    gimmick = pick_gimmick_info_for(skill_id, int(it_type) if it_type is not None else -1)
    if gimmick is None:
        gmap = get_imbue_gimmick_map()
        if int(skill_id) not in gmap:
            warnings.append(
                f"skill_id {skill_id} has no vanilla item reference; "
                f"cannot auto-detect gimmick_info. Target will have skill "
                f"filter patched but passive may not activate.")
        else:
            warnings.append(
                f"skill_id {skill_id} exists but no vanilla item of type "
                f"{it_type} uses it; using best cross-type match.")

    existing_gimmick = target_item.get('gimmick_info') or 0
    if existing_gimmick and gimmick is None:
        gimmick = int(existing_gimmick)
        warnings.append(
            f"kept target's existing gimmick_info={existing_gimmick}")
    if gimmick is None:
        gimmick = 0

    dcd = build_docking_child_data(target_item, gimmick) if gimmick else None
    if dcd is None and gimmick:
        warnings.append(
            f"item_type {it_type} not in imbue_lookup — cannot build "
            f"docking_child_data; only passive list + skill filter will be patched.")

    cooltime = target_item.get('cooltime') or 0
    if cooltime < 1:
        cooltime = 1

    item_charge_type = target_item.get('item_charge_type') or 0
    max_charged = target_item.get('max_charged_useable_count') or 100
    respawn = target_item.get('respawn_time_seconds') or 0

    target_dcd = target_item.get('docking_child_data') or {}
    target_hash = (target_dcd.get('docking_tag_name_hash') or [0])[0]
    target_socket = target_dcd.get('attach_parent_socket_name') or ''

    if target_hash and target_socket:
        reason = 'preserved_target'
    elif dcd:
        reason = 'applied_canonical'
    else:
        reason = 'type_not_supported'

    patches = {
        'gimmick_info': int(gimmick or 0),
        'cooltime': int(cooltime),
        'item_charge_type': int(item_charge_type),
        'max_charged_useable_count': int(max_charged),
        'respawn_time_seconds': int(respawn),
    }
    if dcd:
        patches['docking_child_data'] = dcd

    return {
        'ok': bool(dcd) and bool(gimmick),
        'reason': reason,
        'warnings': warnings,
        'patches': patches,
        'skill_filter_class_hash': int(class_hash),
    }


def parse_skill_pabgh(pabgh_blob: bytes, pabgb_size: int) -> list[tuple[int, int, int]]:
    count = struct.unpack_from("<H", pabgh_blob, 0)[0]
    entries: list[tuple[int, int]] = []
    pos = 2
    for _ in range(count):
        key = struct.unpack_from("<I", pabgh_blob, pos)[0]; pos += 4
        off = struct.unpack_from("<I", pabgh_blob, pos)[0]; pos += 4
        entries.append((key, off))

    spatial = sorted(entries, key=lambda e: e[1])
    end_for: dict[tuple[int, int], int] = {}
    for i, (k, o) in enumerate(spatial):
        end_for[(k, o)] = spatial[i + 1][1] if i + 1 < len(spatial) else pabgb_size

    return [(k, o, end_for[(k, o)] - o) for (k, o) in entries]


_CLASS_LIST_MAX = 256


def class_list_in_record(rec: bytes) -> list[tuple[int, list[int]]]:
    """Scan a skill record for class-list blocks anchored by _MARKER.

    Count cap is 256 rather than a tight 64 so records that legitimately
    reference a lot of weapon classes (after bulk imbue) stay detectable.
    False-positive risk is still low — a random 8-byte marker-match would
    need a following u32 in [0,256] to be mistaken for a block.
    """
    results: list[tuple[int, list[int]]] = []
    pos = 0
    while True:
        idx = rec.find(_MARKER, pos)
        if idx < 0:
            break
        list_pos = idx + len(_MARKER)
        if list_pos + 4 > len(rec):
            break
        count = struct.unpack_from("<I", rec, list_pos)[0]
        if 0 <= count <= _CLASS_LIST_MAX and list_pos + 4 + count * 4 <= len(rec):
            hashes = list(struct.unpack_from(f"<{count}I", rec, list_pos + 4))
            results.append((list_pos, hashes))
        pos = idx + 1
    return results


def add_class_to_skill_record(rec: bytes, class_hash: int) -> bytes:
    return add_classes_to_skill_record(rec, [class_hash])


def add_classes_to_skill_record(rec: bytes, class_hashes: list[int]) -> bytes:
    """Add every hash in class_hashes to every non-empty class-list block.

    Single-pass: scans the ORIGINAL record once, then rewrites each block
    with all new hashes appended. Avoids the drift that happens when
    add_class_to_skill_record is called in a loop and each iteration
    re-scans a record whose byte layout has shifted — false positives in
    the permissive count<=64 check compound across iterations and corrupt
    real blocks. Safe for any number of hashes in one call.
    """
    blocks = class_list_in_record(rec)
    new_rec = bytearray(rec)
    for list_pos, hashes in sorted(blocks, key=lambda b: b[0], reverse=True):
        if not hashes:
            continue
        existing = set(hashes)
        to_add = [int(ch) for ch in class_hashes if int(ch) not in existing]
        if not to_add:
            continue
        new_hashes = list(hashes) + to_add
        new_block = struct.pack("<I", len(new_hashes)) + struct.pack(
            f"<{len(new_hashes)}I", *new_hashes
        )
        old_len = 4 + len(hashes) * 4
        new_rec[list_pos:list_pos + old_len] = new_block
    return bytes(new_rec)


def skill_allows_class(rec: bytes, class_hash: int) -> bool:
    non_empty = [hashes for (_pos, hashes) in class_list_in_record(rec) if hashes]
    if not non_empty:
        return False
    return all(class_hash in hashes for hashes in non_empty)


def rebuild_skill_pair(
    pabgh_blob: bytes,
    pabgb_blob: bytes,
    record_edits: dict[int, Callable[[bytes], bytes]],
) -> tuple[bytes, bytes]:
    entries = parse_skill_pabgh(pabgh_blob, len(pabgb_blob))

    records_by_key: dict[int, bytes] = {}
    for key, off, length in entries:
        rec = pabgb_blob[off:off + length]
        if key in record_edits:
            rec = record_edits[key](rec)
        records_by_key[key] = rec

    spatial = sorted(entries, key=lambda e: e[1])
    new_pabgb = bytearray()
    new_offset_of: dict[int, int] = {}
    for key, _old_off, _old_len in spatial:
        new_offset_of[key] = len(new_pabgb)
        new_pabgb.extend(records_by_key[key])

    new_pabgh = bytearray()
    new_pabgh.extend(struct.pack("<H", len(entries)))
    for key, _old_off, _old_len in entries:
        new_pabgh.extend(struct.pack("<II", key, new_offset_of[key]))

    return bytes(new_pabgh), bytes(new_pabgb)
