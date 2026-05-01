from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field
from typing import Optional

ITEMINFO_MARKER = b"\x00\x01\x00\x00\x00\x00\x00\x00\x00\x07\x70\x00\x00\x00"

CATEGORY_KEYWORDS = [
    ("Helm",        ["_Helm", "_Head", "_Mask", "_Hat", "_Hood", "_Crown"]),
    ("Gloves",      ["_Glove", "_Gauntlet", "_Hand_L", "_Hand_R", "_Hand"]),
    ("Boots",       ["_Boots", "_Foot", "_Shoes"]),
    ("Cloak",       ["_Cloak", "_Cape", "_BackPack", "_Back"]),
    ("Shoulder",    ["_Shoulder", "_Pauldron"]),
    ("TwoHand Sword",  ["_TwoHandSword", "_Twohandsword", "_GreatSword"]),
    ("OneHand Sword",  ["_OneHandSword", "_Onehandsword", "_Rapier", "_Sword"]),
    ("Dual Daggers",   ["_DualDagger", "_Dagger"]),
    ("Dual Sword",     ["_DualSword"]),
    ("TwoHand Axe",    ["_TwoHandAxe"]),
    ("Dual Axe",       ["_DualAxe"]),
    ("Hammer",         ["_Hammer", "_Mace"]),
    ("Spear",          ["_Spear", "_Lance"]),
    ("Bow",            ["_OneHandBow", "_Bow"]),
    ("Shield",         ["_SwordShield", "_Shield"]),
    ("Bracer",         ["_Bracer", "_Bracelet"]),
    ("Lantern",        ["Lantern", "Lamp"]),
    ("Torch",          ["_Torch", "Torch"]),
    ("Necklace",       ["_Necklace", "Necklace"]),
    ("Earring",        ["_Earring", "Earring"]),
    ("Ring",           ["_Ring", "Ring"]),
    ("Belt",           ["_Belt", "Belt"]),
    ("Trinket",        ["_Trinket", "_Charm", "_Talisman"]),
    ("Chest",          ["_UpperBody", "_Upperbody", "_PlateArmor", "_ChainMail",
                        "_FabricArmor", "_LeatherArmor", "_Armor"]),
]


@dataclass
class ArmorItem:
    item_id: int
    internal_name: str
    display_name: str
    category: str
    hashes: list = field(default_factory=list)

    @property
    def key(self) -> int:
        return self.item_id


def get_category(internal_name: str) -> Optional[str]:
    name = internal_name
    for cat, tokens in CATEGORY_KEYWORDS:
        for tok in tokens:
            if tok in name:
                return cat
    return None


def clean_display_name(internal_name: str) -> str:
    s = re.sub(r'([a-z])([A-Z])', r'\1 \2', internal_name)
    s = s.replace('_', ' ')
    s = re.sub(r'\s+', ' ', s).strip()
    s = re.sub(r'\bArmor\s+Armor\b', 'Armor', s)
    return s


def parse_transmog_items_crimson_rs(data: bytes) -> list[ArmorItem]:
    try:
        import crimson_rs
    except ImportError:
        return []

    try:
        items = crimson_rs.parse_iteminfo_from_bytes(data)
    except Exception:
        return []

    results: list[ArmorItem] = []
    for it in items:
        sk = it.get('string_key', '')
        if not sk:
            continue
        hash_values = []
        seen_hv = set()
        for pd in (it.get('prefab_data_list', []) or []):
            for h in (pd.get('prefab_names') or []):
                if h and h != 0 and int(h) not in seen_hv:
                    hash_values.append(int(h))
                    seen_hv.add(int(h))
        for pv in (it.get('gimmick_visual_prefab_data_list', []) or []):
            for h in (pv.get('prefab_names') or []):
                if h and h != 0 and int(h) not in seen_hv:
                    hash_values.append(int(h))
                    seen_hv.add(int(h))
        if not hash_values:
            continue

        needle = sk.encode('ascii', errors='ignore')
        if not needle:
            continue
        anchor = -1
        search_from = 0
        while True:
            found = data.find(needle, search_from)
            if found < 0:
                break
            if found >= 4:
                try:
                    candidate_len = struct.unpack_from('<I', data, found - 4)[0]
                except struct.error:
                    candidate_len = 0
                if candidate_len == len(needle):
                    anchor = found
                    break
            search_from = found + 1
        if anchor < 0:
            continue

        window_start = anchor + len(needle)
        window_end = min(window_start + 20000, len(data) - 4)
        hashes: list[tuple[int, int]] = []
        for hv in hash_values:
            hv_bytes = struct.pack('<I', hv)
            pos = data.find(hv_bytes, window_start, window_end)
            if pos >= 0:
                hashes.append((pos, hv))

        if not hashes:
            continue

        internal_name = sk
        category = get_category(internal_name) or "Other"
        results.append(ArmorItem(
            item_id=it.get('key', 0),
            internal_name=internal_name,
            display_name=clean_display_name(internal_name),
            category=category,
            hashes=hashes,
        ))

    return results


def parse_transmog_items(data: bytes, loc_dict: Optional[dict] = None) -> list[ArmorItem]:
    try:
        rs = parse_transmog_items_crimson_rs(data)
    except Exception:
        rs = []
    legacy = _parse_transmog_items_legacy(data, loc_dict)
    if rs and len(rs) >= len(legacy):
        return rs
    return legacy or []


def _parse_transmog_items_legacy(data: bytes, loc_dict: Optional[dict] = None) -> list[ArmorItem]:
    results: list[ArmorItem] = []
    seen_ids: set[int] = set()

    NAME_RE = re.compile(rb'^[A-Za-z][A-Za-z0-9_]*$')
    LOC_NAME_RE = re.compile(rb'^[A-Za-z0-9_]+$')

    marker = ITEMINFO_MARKER
    idx = 0
    pos = data.find(marker, idx)
    while pos != -1:
        name_end = pos
        name_start = -1
        name_len = 0
        for nl in range(3, 65):
            ns = name_end - nl
            if ns < 8:
                continue
            lp = ns - 4
            try:
                candidate_len = struct.unpack_from('<I', data, lp)[0]
            except struct.error:
                continue
            if candidate_len != nl:
                continue
            nb = data[ns:ns + nl]
            if not NAME_RE.match(nb):
                continue
            ip = lp - 4
            try:
                iid = struct.unpack_from('<I', data, ip)[0]
            except struct.error:
                continue
            if not (100 <= iid < 100000000):
                continue
            name_start = ns
            name_len = nl
            item_id = iid
            break

        pos = data.find(marker, pos + len(marker))
        if name_start < 0:
            continue

        internal_name = data[name_start:name_start + name_len].decode('ascii')
        if item_id in seen_ids:
            continue

        category = get_category(internal_name) or "Other"

        loc_id = 0
        after_marker = name_start + name_len + len(marker)
        try:
            loc_off = struct.unpack_from('<I', data, after_marker)[0]
            if 0 < loc_off < 100:
                loc_bytes = data[after_marker + 4:after_marker + 4 + loc_off]
                if LOC_NAME_RE.match(loc_bytes):
                    try:
                        loc_id = int(loc_bytes)
                    except ValueError:
                        loc_id = 0
        except struct.error:
            pass

        hashes: list[tuple[int, int]] = []
        search_start = after_marker
        search_end = min(search_start + 2000, len(data) - 15)

        for scan in range(search_start, search_end):
            if data[scan] != 0x0E:
                continue
            try:
                count1 = struct.unpack_from('<I', data, scan + 3)[0]
                count2 = struct.unpack_from('<I', data, scan + 7)[0]
            except struct.error:
                continue
            if not (0 < count1 <= 5 and 0 < count2 <= 5):
                continue
            hash_base = scan + 11
            if hash_base + count2 * 4 > len(data):
                continue
            for hi in range(count2):
                hoff = hash_base + hi * 4
                hval = struct.unpack_from('<I', data, hoff)[0]
                if hval != 0:
                    hashes.append((hoff, hval))
            break

        if not hashes:
            continue

        display = ''
        if loc_dict and loc_id:
            display = loc_dict.get(loc_id, '')
        if not display:
            display = clean_display_name(internal_name)

        results.append(ArmorItem(
            item_id=item_id,
            internal_name=internal_name,
            display_name=display,
            category=category,
            hashes=hashes,
        ))
        seen_ids.add(item_id)

    return results


def build_swap_changes(source: ArmorItem, target: ArmorItem) -> list[dict]:
    changes = []
    n = min(len(source.hashes), len(target.hashes))
    if n <= 2:
        positions = [0]
    elif n <= 5:
        positions = [0, 2] if n > 2 else [0]
    else:
        positions = [0, 2, 5]

    positions = [p for p in positions if p < n]

    for p in positions:
        src_off, src_val = source.hashes[p]
        tgt_off, tgt_val = target.hashes[p]
        if src_val == tgt_val:
            continue
        changes.append({
            'offset': tgt_off,
            'label': f"{target.internal_name} <- {source.internal_name}",
            'original': tgt_val,
            'patched': src_val,
        })
    return changes


def apply_swaps_to_blob(blob: bytearray, swaps: list[dict]) -> int:
    fresh_items = parse_transmog_items(bytes(blob))
    by_key = {a.item_id: a for a in fresh_items}

    import logging
    _log = logging.getLogger(__name__)
    _log.info("apply_swaps_to_blob: fresh catalog has %d items for %d queued swap(s)",
              len(fresh_items), len(swaps))

    applied = 0
    missing_src: list[int] = []
    missing_tgt: list[int] = []
    for sw in swaps:
        src_obj = sw['src']
        src_key = src_obj.item_id if hasattr(src_obj, 'item_id') else sw['src_key']
        tgt_key = sw['tgt'].item_id if hasattr(sw['tgt'], 'item_id') else sw['tgt_key']
        tgt = by_key.get(tgt_key)
        if not tgt:
            missing_tgt.append(tgt_key)
            _log.warning("swap %s -> %s: TARGET key %d NOT FOUND in fresh catalog",
                         src_key, tgt_key, tgt_key)
            continue
        _log.info("swap %s -> %s: target=%s hashes=%s",
                  src_key, tgt_key, tgt.internal_name,
                  [(hex(o), hex(v)) for o, v in tgt.hashes])
        is_invisible = getattr(src_obj, 'internal_name', '') == '__INVISIBLE_ZERO__'
        if is_invisible:
            n = len(tgt.hashes)
            if n <= 2:
                positions = [0]
            elif n <= 5:
                positions = [0, 2]
            else:
                positions = [0, 2, 5]
            positions = [p for p in positions if p < n]
            _log.info("  INVISIBLE swap: zeroing tgt positions %s", positions)
            for p in positions:
                tgt_off, orig_val = tgt.hashes[p]
                struct.pack_into('<I', blob, tgt_off, 0)
                _log.info("    [invisible] off=0x%x orig=0x%x -> 0", tgt_off, orig_val)
                applied += 1
            continue
        src = by_key.get(src_key)
        if not src:
            missing_src.append(src_key)
            _log.warning("swap %s -> %s: SOURCE key %d NOT FOUND in fresh catalog",
                         src_key, tgt_key, src_key)
            continue
        _log.info("  source=%s hashes=%s", src.internal_name,
                  [(hex(o), hex(v)) for o, v in src.hashes])
        changes = build_swap_changes(src, tgt)
        _log.info("  build_swap_changes produced %d patch(es)", len(changes))
        for ch in changes:
            struct.pack_into('<I', blob, ch['offset'], ch['patched'])
            _log.info("    [swap] off=0x%x orig=0x%x -> 0x%x (%s)",
                      ch['offset'], ch['original'], ch['patched'], ch.get('label', ''))
            applied += 1
    if missing_src or missing_tgt:
        _log.warning("apply_swaps_to_blob: %d src missing %s, %d tgt missing %s "
                     "— fresh catalog dropped items (likely ItemBuffs expanded "
                     "record past parser window)",
                     len(missing_src), missing_src[:5],
                     len(missing_tgt), missing_tgt[:5])
    if swaps and applied == 0:
        _log.error("apply_swaps_to_blob: ZERO patches applied for %d queued swap(s) "
                   "— transmog will not take effect in-game", len(swaps))
    return applied


parse_armor_items = parse_transmog_items
