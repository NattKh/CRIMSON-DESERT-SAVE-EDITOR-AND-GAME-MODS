from __future__ import annotations

import struct
import logging
from typing import List, Optional, Tuple, Dict

from models import SaveItem

log = logging.getLogger(__name__)

_ITEM_FIELDS = [
    (0, "_saveVersion", 4),
    (1, "_itemNo", 8),
    (2, "_itemKey", 4),
    (3, "_slotNo", 2),
    (4, "_stackCount", 8),
    (5, "_enchantLevel", 2),
    (6, "_useableCtc", 8),
    (7, "_endurance", 2),
    (8, "_sharpness", 2),
    (9, "_batteryStat", 8),
    (10, "_maxBatteryStat", 8),
    (11, "_maxSocketCount", 1),
    (12, "_validSocketCount", 1),
]


def compute_item_field_offsets(data: bytes, item_offset: int) -> Dict[str, int]:
    result = {}
    payload_start = item_offset - 4

    mbc = 0
    mask_bytes = b''
    for try_mbc_pos in [item_offset - 24, item_offset - 22, item_offset - 20]:
        if try_mbc_pos < 0:
            continue
        candidate_mbc = struct.unpack_from("<H", data, try_mbc_pos)[0]
        if 1 <= candidate_mbc <= 8:
            sent_pos = try_mbc_pos + 2 + candidate_mbc + 3
            if sent_pos + 8 <= len(data):
                s1 = struct.unpack_from("<I", data, sent_pos)[0]
                s2 = struct.unpack_from("<I", data, sent_pos + 4)[0]
                if s1 == 0xFFFFFFFF and s2 == 0xFFFFFFFF:
                    mbc = candidate_mbc
                    mask_bytes = data[try_mbc_pos + 2:try_mbc_pos + 2 + mbc]
                    break

    if not mask_bytes:
        return result

    pos = item_offset
    for bit_idx, name, size in _ITEM_FIELDS:
        if bit_idx >= len(mask_bytes) * 8:
            break
        present = bool(mask_bytes[bit_idx // 8] & (1 << (bit_idx % 8)))
        if present:
            result[name] = pos
            pos += size

    return result


def _get_item_bearing_ranges(data: bytes | bytearray) -> List[Tuple[int, int, str]]:
    ITEM_BEARING = {"EquipmentSaveData", "InventorySaveData", "StoreSaveData",
                    "MercenaryClanSaveData"}
    length = len(data)
    if length < 20 or data[0:2] != b'\xff\xff':
        return [(20, length - 40, "Inventory")]

    type_names: Dict[int, str] = {}
    try:
        pos = 14
        pos += 4
        type_count = struct.unpack_from("<H", data, pos)[0]
        pos += 2
        for i in range(min(type_count, 200)):
            if pos + 4 > length:
                break
            name_len = struct.unpack_from("<I", data, pos)[0]
            if name_len > 200:
                break
            pos += 4
            if pos + name_len > length:
                break
            name = data[pos:pos + name_len].decode("ascii", errors="replace")
            type_names[i] = name
            pos += name_len
            if pos + 2 > length:
                break
            field_count = struct.unpack_from("<H", data, pos)[0]
            pos += 2
            for _ in range(field_count):
                if pos + 4 > length:
                    break
                fn_len = struct.unpack_from("<I", data, pos)[0]
                pos += 4 + fn_len
                if pos + 4 > length:
                    break
                tn_len = struct.unpack_from("<I", data, pos)[0]
                pos += 4 + tn_len
                pos += 8
    except Exception:
        pass

    scan_limit = min(length, 0x15000)
    ranges: List[Tuple[int, int, str]] = []
    for toc_pos in range(0, scan_limit - 20):
        sentinel = struct.unpack_from("<q", data, toc_pos + 4)[0]
        if sentinel != -1:
            continue
        cls = struct.unpack_from("<i", data, toc_pos)[0]
        d_off = struct.unpack_from("<i", data, toc_pos + 12)[0]
        d_size = struct.unpack_from("<i", data, toc_pos + 16)[0]
        if 0 <= cls < 200 and 0 < d_off < length and 0 < d_size < length:
            name = type_names.get(cls, "")
            if name in ITEM_BEARING:
                ranges.append((d_off, d_off + d_size, name))

    if not ranges:
        return [(20, length - 40, "Inventory")]

    return ranges


def scan_items(data: bytes | bytearray) -> List[SaveItem]:
    items: List[SaveItem] = []
    length = len(data)

    valid_ranges = _get_item_bearing_ranges(data)
    log.debug("Scanning %d item-bearing ranges", len(valid_ranges))

    for range_start, range_end, range_name in valid_ranges:
        _scan_range(data, items, max(20, range_start), min(range_end, length - 40), range_name)

    _classify_items(data, items)
    return items


def _scan_range(data: bytes | bytearray, items: List[SaveItem],
                start: int, end: int, range_name: str) -> None:
    for off in range(start, end):
        if struct.unpack_from("<I", data, off)[0] != 1:
            continue

        item_no = struct.unpack_from("<q", data, off + 4)[0]
        if item_no < 1 or item_no > 999999:
            continue

        item_key = struct.unpack_from("<I", data, off + 12)[0]
        if item_key < 1 or item_key > 0x7FFFFFFF:
            continue

        slot_no = struct.unpack_from("<H", data, off + 16)[0]

        stack = struct.unpack_from("<q", data, off + 18)[0]
        if stack < 1 or stack > 9_000_000_000_000_000_000:
            continue

        if off >= 16:
            sentinel = struct.unpack_from("<q", data, off - 16)[0]
            if sentinel != -1:
                continue
        else:
            continue

        enchant_raw = struct.unpack_from("<H", data, off + 26)[0]
        endurance = struct.unpack_from("<H", data, off + 30)[0]
        sharpness = struct.unpack_from("<H", data, off + 32)[0]

        has_enchant = enchant_raw != 0xFFFF
        is_equipment = has_enchant

        payload_start = off - 4
        block_size = 0
        locator_start = 0
        if off >= 24:
            mbc = struct.unpack_from("<H", data, off - 24)[0]
            if 1 <= mbc <= 8:
                locator_start = off - 24
            elif off >= 20:
                locator_start = off - 20

        for probe in range(off + 34, min(off + 400, len(data) - 4)):
            ts = struct.unpack_from("<I", data, probe)[0]
            if ts == probe - payload_start and ts > 20:
                record_end = probe + 4
                if locator_start > 0:
                    block_size = record_end - locator_start
                else:
                    block_size = record_end - (off - 20)
                break

        item = SaveItem(
            offset=off,
            item_no=item_no,
            item_key=item_key,
            slot_no=slot_no,
            stack_count=stack,
            enchant_level=enchant_raw if has_enchant else 0,
            endurance=endurance,
            sharpness=sharpness,
            has_enchant=has_enchant,
            is_equipment=is_equipment,
            block_size=block_size,
        )
        items.append(item)


def _classify_items(data: bytes | bytearray, items: List[SaveItem]) -> None:
    length = len(data)

    type_names: Dict[int, str] = {}
    if len(data) > 20 and data[0:2] == b'\xff\xff':
        try:
            pos = 14
            pos += 4
            if pos + 2 <= len(data):
                type_count = struct.unpack_from("<H", data, pos)[0]
                pos += 2
                for i in range(min(type_count, 200)):
                    if pos + 4 > len(data):
                        break
                    name_len = struct.unpack_from("<I", data, pos)[0]
                    pos += 4
                    if pos + name_len > len(data) or name_len > 200:
                        break
                    name = data[pos:pos + name_len].decode("ascii", errors="replace")
                    type_names[i] = name
                    pos += name_len
                    if pos + 2 > len(data):
                        break
                    field_count = data[pos]
                    pos += 2
                    for _ in range(field_count):
                        if pos + 4 > len(data):
                            break
                        fn_len = struct.unpack_from("<I", data, pos)[0]
                        pos += 4 + fn_len
                        if pos + 4 > len(data):
                            break
                        tn_len = struct.unpack_from("<I", data, pos)[0]
                        pos += 4 + tn_len
                        pos += 8
        except Exception:
            pass

    toc_entries: List[Tuple[str, int, int]] = []
    scan_limit = min(length, 0x15000)

    for pos in range(0, scan_limit - 20):
        sentinel = struct.unpack_from("<q", data, pos + 4)[0]
        if sentinel != -1:
            continue
        cls = struct.unpack_from("<i", data, pos)[0]
        d_off = struct.unpack_from("<i", data, pos + 12)[0]
        d_size = struct.unpack_from("<i", data, pos + 16)[0]

        if 0 <= cls < 200 and 0 < d_off < length and 0 < d_size < length:
            name = type_names.get(cls, "")
            toc_entries.append((name, d_off, d_size))

    for item in items:
        item.section = 0
        item.source = "Inventory"

        for type_name, d_off, d_size in toc_entries:
            if d_off <= item.offset < d_off + d_size:
                if "Equipment" in type_name and "Inventory" not in type_name:
                    item.source = "Equipment"
                    item.section = 0
                elif "Inventory" in type_name:
                    item.source = "Inventory"
                    item.section = 0
                elif "Store" in type_name:
                    item.source = "Sold to Vendor"
                    item.section = 0
                elif "Mercenary" in type_name:
                    item.source = "Mercenary"
                    item.section = 1
                elif "Quest" in type_name:
                    item.source = "Inventory"
                    item.section = 0
                else:
                    item.source = "Inventory"
                    item.section = 0
                break


def apply_stack_edit(
    data: bytearray,
    item: SaveItem,
    new_stack: int,
) -> bytes:
    old = data[item.offset + 18:item.offset + 26]
    struct.pack_into("<q", data, item.offset + 18, new_stack)
    item.stack_count = new_stack
    return bytes(old)


def apply_itemno_edit(
    data: bytearray,
    item: SaveItem,
    new_itemno: int,
) -> bytes:
    old = data[item.offset + 4:item.offset + 12]
    struct.pack_into("<q", data, item.offset + 4, new_itemno)
    item.item_no = new_itemno
    return bytes(old)


def get_max_itemno(items: List[SaveItem]) -> int:
    if not items:
        return 0
    return max(it.item_no for it in items)


def apply_enchant_edit(
    data: bytearray,
    item: SaveItem,
    new_enchant: int,
) -> bytes:
    old = data[item.offset + 26:item.offset + 28]
    struct.pack_into("<H", data, item.offset + 26, new_enchant)
    item.enchant_level = new_enchant
    item.has_enchant = new_enchant != 0xFFFF
    return bytes(old)


def apply_endurance_edit(
    data: bytearray,
    item: SaveItem,
    new_endurance: int,
) -> bytes:
    old = data[item.offset + 30:item.offset + 32]
    struct.pack_into("<H", data, item.offset + 30, new_endurance)
    item.endurance = new_endurance
    return bytes(old)


def apply_sharpness_edit(
    data: bytearray,
    item: SaveItem,
    new_sharpness: int,
) -> bytes:
    old = data[item.offset + 32:item.offset + 34]
    struct.pack_into("<H", data, item.offset + 32, new_sharpness)
    item.sharpness = new_sharpness
    return bytes(old)


def apply_item_swap(
    data: bytearray,
    item: SaveItem,
    new_key: int,
) -> List[Tuple[int, bytes, bytes]]:
    patches: List[Tuple[int, bytes, bytes]] = []
    old_key = struct.unpack_from("<I", data, item.offset + 12)[0]
    old_key_bytes = struct.pack("<I", old_key)
    new_key_bytes = struct.pack("<I", new_key)

    if old_key == new_key:
        return patches

    item_no = item.item_no
    item_no_bytes = struct.pack("<q", item_no)
    patched_offsets: set = set()

    safe_ranges = _get_item_bearing_ranges(data)

    def _in_safe_range(pos: int) -> bool:
        for rs, re, _ in safe_ranges:
            if rs <= pos < re:
                return True
        return False

    def patch_at(pos: int) -> None:
        if pos not in patched_offsets:
            data[pos:pos + 4] = new_key_bytes
            patches.append((pos, old_key_bytes, new_key_bytes))
            patched_offsets.add(pos)

    patch_at(item.offset + 12)

    region_end = min(item.offset + 300, len(data) - 4)
    for pos in range(item.offset + 16, region_end):
        if data[pos:pos + 4] == old_key_bytes:
            patch_at(pos)

    for rs, re, _ in safe_ranges:
        for pos in range(rs, min(re, len(data) - 12)):
            if pos in patched_offsets:
                continue
            if data[pos:pos + 8] == item_no_bytes:
                key_pos = pos + 8
                if key_pos + 4 <= len(data) and data[key_pos:key_pos + 4] == old_key_bytes:
                    patch_at(key_pos)

    equip_scan_start = max(0, item.offset - 16)
    equip_scan_end = min(len(data) - 40, item.offset + 300)
    for scan in range(equip_scan_start, equip_scan_end):
        if scan + 6 in patched_offsets:
            continue
        if (struct.unpack_from("<I", data, scan)[0] == 4
                and struct.unpack_from("<H", data, scan + 4)[0] == 0x0101
                and data[scan + 6:scan + 10] == old_key_bytes):
            patch_at(scan + 6)

    for rs, re, _ in safe_ranges:
        for dup_off in range(rs, min(re, len(data) - 40)):
            if dup_off == item.offset:
                continue
            if struct.unpack_from("<I", data, dup_off)[0] != 1:
                continue
            dup_no = struct.unpack_from("<q", data, dup_off + 4)[0]
            if dup_no != item_no:
                continue
            dup_key_off = dup_off + 12
            if data[dup_key_off:dup_key_off + 4] == old_key_bytes:
                patch_at(dup_key_off)
                dup_end = min(dup_off + 300, len(data) - 4)
                for pos in range(dup_off + 16, dup_end):
                    if data[pos:pos + 4] == old_key_bytes:
                        patch_at(pos)

    item.item_key = new_key
    return patches


_ITEM_FIELD_NAMES = [
    "_saveVersion", "_itemNo", "_itemKey", "_slotNo", "_stackCount",
    "_enchantLevel", "_useableCtc", "_endurance", "_sharpness",
    "_batteryStat", "_maxBatteryStat", "_maxSocketCount", "_validSocketCount",
    "_socketSaveDataList", "_itemDyeDataList", "_dropResultSubSaveItemList",
    "_transferredItemKey", "_currentGimmickState", "_chargedUseableCount",
    "_timeWhenPushItem", "_characterConversionData", "_isNewMark",
]


def _try_import_parc():
    try:
        import parc_serializer
        return parc_serializer, None
    except ImportError as e:
        return None, f"parc_serializer not available: {e}"
    except Exception as e:
        return None, f"parc_serializer import error: {e}"


def scan_items_parc(data: bytes | bytearray) -> Tuple[List[SaveItem], str]:
    parc_mod, err = _try_import_parc()
    if parc_mod is None:
        return [], err

    try:
        parc_blob = parc_mod.parse_parc_blob(bytes(data))
    except Exception as e:
        return [], f"PARC parse failed: {e}"

    item_type_idx = None
    for idx, td in parc_blob.type_by_index.items():
        if td.name == "ItemSaveData":
            item_type_idx = idx
            break

    if item_type_idx is None:
        return [], "ItemSaveData type not found in schema"

    # ItemSaveData locators can live inside deeply nested lists.  Parsing only
    # the first level of a root block misses most inventory entries in newer
    # saves, whose item ids are also above the old 999999 heuristic limit.
    # Scan the known item-owning root blocks for valid PARC locators instead.
    root_sources = {
        "EquipmentSaveData": ("Equipment", 0),
        "InventorySaveData": ("Inventory", 0),
        "StoreSaveData": ("Sold to Vendor", 0),
        "MercenaryClanSaveData": ("Mercenary", 1),
        "FieldSaveData": ("World / NPC", 1),
    }
    root_ranges: List[Tuple[int, int, str, int]] = []
    for entry in parc_blob.toc_entries:
        td = parc_blob.type_by_index.get(entry.class_index)
        source = root_sources.get(td.name if td else "")
        if source:
            root_ranges.append((entry.data_offset, entry.data_offset + entry.data_size, *source))

    items: List[SaveItem] = []
    seen_locators: set[int] = set()
    for start, end, source, section in root_ranges:
        start = max(0, start)
        end = min(len(data), end)
        for locator in range(start, end - 24):
            if locator in seen_locators:
                continue
            mask_count = struct.unpack_from("<H", data, locator)[0]
            if not 0 < mask_count <= 16:
                continue
            type_offset = locator + 2 + mask_count
            if type_offset + 15 > end:
                continue
            if struct.unpack_from("<H", data, type_offset)[0] != item_type_idx:
                continue
            sentinel1, sentinel2, payload_offset = struct.unpack_from("<III", data, type_offset + 3)
            if sentinel1 != 0xFFFFFFFF or sentinel2 != 0xFFFFFFFF:
                continue
            if payload_offset != type_offset + 15 or payload_offset + 4 > end:
                continue

            item = _parse_item_payload(
                data, data[locator + 2:locator + 2 + mask_count], payload_offset,
                parc_blob.type_by_index[item_type_idx], locator, source, "",
            )
            if item is None:
                continue
            item.source = source
            item.section = section
            items.append(item)
            seen_locators.add(locator)

    if not items:
        return [], "PARC parsed 0 valid items"

    _classify_items(data, items)

    return items, f"PARC mode: {len(items)} items, 22 fields tracked per item"


def _find_item_fields_in_parsed(
    data: bytes | bytearray,
    parsed_block: dict,
    parent_name: str,
    item_type_idx: int,
    parc_blob,
) -> List[SaveItem]:
    items: List[SaveItem] = []

    for finfo in parsed_block.get("fields", []):
        if not finfo.get("present"):
            continue

        raw = finfo.get("raw", b"")
        if not raw or len(raw) < 20:
            continue

        val = finfo.get("value")
        if not isinstance(val, dict):
            continue

        kind = val.get("kind", "")
        if kind != "object_list":
            continue

        field_start = finfo.get("start", 0)
        _scan_raw_for_item_elements(
            data, raw, field_start, item_type_idx, parc_blob, parent_name,
            finfo.get("name", ""), items,
        )

    return items


def _scan_raw_for_item_elements(
    blob: bytes | bytearray,
    raw: bytes,
    field_abs_start: int,
    item_type_idx: int,
    parc_blob,
    parent_name: str,
    field_name: str,
    items_out: List[SaveItem],
) -> None:
    item_td = parc_blob.type_by_index[item_type_idx]

    pos = 0
    while pos < len(raw) - 20:
        mbc = struct.unpack_from("<H", raw, pos)[0]
        if mbc == 0 or mbc > 16 or pos + 2 + mbc + 15 > len(raw):
            pos += 1
            continue

        type_off = pos + 2 + mbc
        if type_off + 2 > len(raw):
            pos += 1
            continue

        found_type = struct.unpack_from("<H", raw, type_off)[0]
        if found_type != item_type_idx:
            pos += 1
            continue

        sentinel_off = type_off + 3
        if sentinel_off + 12 > len(raw):
            pos += 1
            continue

        s1 = struct.unpack_from("<I", raw, sentinel_off)[0]
        s2 = struct.unpack_from("<I", raw, sentinel_off + 4)[0]
        if s1 != 0xFFFFFFFF or s2 != 0xFFFFFFFF:
            pos += 1
            continue

        payload_offset_raw = struct.unpack_from("<I", raw, sentinel_off + 8)[0]
        wrapper_end = sentinel_off + 12
        expected_payload = field_abs_start + wrapper_end
        if payload_offset_raw != expected_payload:
            pos += 1
            continue

        mask_bytes = raw[pos + 2:pos + 2 + mbc]
        abs_payload = field_abs_start + wrapper_end

        item = _parse_item_payload(
            blob, mask_bytes, abs_payload, item_td,
            field_abs_start + pos, parent_name, field_name,
        )
        if item is not None:
            items_out.append(item)
            if item.field_offsets.get("_record_end", 0) > field_abs_start + pos:
                pos = item.field_offsets["_record_end"] - field_abs_start
            else:
                pos = wrapper_end + 4
        else:
            pos += 1
        continue


def _field_present_check(mask_bytes: bytes, field_index: int) -> bool:
    byte_idx = field_index // 8
    bit_idx = field_index % 8
    if byte_idx >= len(mask_bytes):
        return False
    return bool(mask_bytes[byte_idx] & (1 << bit_idx))


def _parse_item_payload(
    blob: bytes | bytearray,
    mask_bytes: bytes,
    abs_payload: int,
    item_td,
    abs_locator: int,
    parent_name: str,
    field_name: str,
) -> Optional[SaveItem]:
    try:
        pos = abs_payload + 4

        field_offsets: Dict[str, int] = {}
        values: Dict[str, any] = {}

        # Fields 0..13 are fixed-size in the current schema.  Field 13 is
        # _validSocketCount; keep its offset so socket edits target the real
        # record rather than the pre-release field layout.
        for i in range(min(14, len(item_td.fields))):
            fdef = item_td.fields[i]
            present = _field_present_check(mask_bytes, i)
            if not present:
                continue

            mk = fdef.meta_kind
            ms = fdef.meta_size

            if mk in (0, 2) and ms > 0:
                field_offsets[fdef.name] = pos
                if ms == 1:
                    values[fdef.name] = blob[pos]
                elif ms == 2:
                    values[fdef.name] = struct.unpack_from("<H", blob, pos)[0]
                elif ms == 4:
                    values[fdef.name] = struct.unpack_from("<I", blob, pos)[0]
                elif ms == 8:
                    values[fdef.name] = struct.unpack_from("<Q", blob, pos)[0]
                pos += ms
            else:
                field_offsets[fdef.name] = pos
                return None

        record_end = None
        max_scan = min(len(blob) - 4, abs_payload + 4096)
        for probe in range(pos, max_scan + 1):
            ts = struct.unpack_from("<I", blob, probe)[0]
            if ts == probe - abs_payload and ts > 20:
                record_end = probe + 4
                break

        if record_end is None:
            record_end = pos + 300

        field_offsets["_record_end"] = record_end
        trailing_pos = record_end - 4

        back_pos = trailing_pos

        back_fields = [
            (21, "_isNewMark", 1, (0, 2)),
            (20, "_characterConversionData", 0, (5,)),
            (19, "_timeWhenPushItem", 8, (0,)),
            (18, "_chargedUseableCount", 8, (0,)),
            (17, "_currentGimmickState", 4, (0,)),
            (16, "_transferredItemKey", 4, (0,)),
        ]

        back_ok = True
        for fidx, fname, fsize, valid_mks in back_fields:
            if fidx >= len(item_td.fields):
                continue
            fdef = item_td.fields[fidx]
            present = _field_present_check(mask_bytes, fidx)
            if not present:
                continue

            if fdef.meta_kind in (4, 5, 6, 7):
                back_ok = False
                break

            if fsize <= 0:
                back_ok = False
                break

            back_pos -= fsize
            field_offsets[fname] = back_pos

            if fdef.meta_kind in (0, 2) and fsize > 0:
                if fsize == 1:
                    values[fname] = blob[back_pos]
                elif fsize == 2:
                    values[fname] = struct.unpack_from("<H", blob, back_pos)[0]
                elif fsize == 4:
                    values[fname] = struct.unpack_from("<I", blob, back_pos)[0]
                elif fsize == 8:
                    values[fname] = struct.unpack_from("<Q", blob, back_pos)[0]

        save_ver = values.get("_saveVersion", 0)
        if save_ver != 1:
            return None

        item_no = values.get("_itemNo", 0)
        item_key = values.get("_itemKey", 0)
        slot_no = values.get("_slotNo", 0)
        stack_count = values.get("_stackCount", 0)
        enchant_raw = values.get("_enchantLevel", 0xFFFF)
        endurance = values.get("_endurance", 0)
        sharpness = values.get("_sharpness", 0)

        # Modern Crimson Desert saves use seven-digit item numbers.  The old
        # six-digit cap made almost every real item disappear from the editor.
        if item_no < 1 or item_no > 50_000_000:
            return None
        if item_key < 1 or item_key > 0x7FFFFFFF:
            return None

        has_enchant = enchant_raw != 0xFFFF
        is_equipment = has_enchant

        item = SaveItem(
            offset=field_offsets.get("_saveVersion", abs_payload + 4),
            item_no=item_no,
            item_key=item_key,
            slot_no=slot_no,
            stack_count=stack_count,
            enchant_level=enchant_raw if has_enchant else 0,
            endurance=endurance,
            sharpness=sharpness,
            has_enchant=has_enchant,
            is_equipment=is_equipment,
            field_offsets=field_offsets,
            parc_parsed=True,
        )
        return item

    except Exception as e:
        log.debug(f"PARC item parse error at 0x{abs_payload:X}: {e}")
        return None


def _skip_dynamic_array(blob: bytes, pos: int, ms: int) -> int:
    if pos + 14 <= len(blob) and blob[pos:pos + 5] == b'\x00\x00\x06\x01\x00':
        count = struct.unpack_from("<I", blob, pos + 5)[0]
        end = pos + 9 + count * ms + 5
        if count < 0x10000 and end <= len(blob) and blob[end - 5:end] == b'\x01\x01\x01\x01\x01':
            return end

    if blob[pos] == 1:
        marker_end = pos
        while marker_end < len(blob) and blob[marker_end] == 1:
            marker_end += 1
        if (marker_end > pos and marker_end < len(blob)
                and blob[marker_end] == 0 and marker_end + 5 <= len(blob)):
            count = struct.unpack_from("<I", blob, marker_end + 1)[0]
            end = pos + (marker_end - pos + 1) + 4 + count * ms
            if count < 0x10000 and end <= len(blob):
                if end < len(blob) and blob[end] == 1:
                    end += 1
                return end

    if (pos + 6 <= len(blob) and blob[pos] == 0 and blob[pos + 1] == 0
            and blob[pos + 4] == 0 and blob[pos + 5] == 0):
        count = struct.unpack_from("<H", blob, pos + 2)[0]
        end = pos + 6 + count * ms
        if end <= len(blob):
            return end

    count = struct.unpack_from("<I", blob, pos + 1)[0]
    end = pos + 5 + count * ms
    if count < 0x1000000 and end <= len(blob):
        return end

    raise ValueError(f"Dynamic array skip failed at 0x{pos:X}")


def _skip_object_locator(blob: bytes, pos: int, mk: int) -> int:
    body = pos
    if mk == 5:
        for delta in [0, 1, 3]:
            probe = pos + delta
            if probe + 2 <= len(blob):
                mbc = struct.unpack_from("<H", blob, probe)[0]
                if 0 < mbc <= 16:
                    body = probe
                    break

    mbc = struct.unpack_from("<H", blob, body)[0]
    off = body + 2 + mbc
    wrapper_end = off + 2 + 1 + 4 + 4 + 4

    payload_off = struct.unpack_from("<I", blob, off + 11)[0]

    if payload_off == wrapper_end:
        cursor = payload_off + 4
        max_scan = min(len(blob), payload_off + 4096)
        for probe in range(cursor, max_scan):
            if probe + 4 <= len(blob):
                ts = struct.unpack_from("<I", blob, probe)[0]
                if ts == probe - payload_off and ts > 4:
                    return probe + 4
        return wrapper_end
    return wrapper_end


def _skip_object_list(blob: bytes, pos: int) -> int:
    if pos + 18 <= len(blob):
        b0 = blob[pos]

        marker_end = pos
        while marker_end < len(blob) and blob[marker_end] == 1:
            marker_end += 1
        if (marker_end > pos and marker_end + 17 <= len(blob)
                and blob[marker_end] == 0
                and blob[marker_end + 5:marker_end + 18] == b'\x00' * 13):
            count = struct.unpack_from("<I", blob, marker_end + 1)[0]
            header_size = (marker_end - pos + 1) + 4 + 13
            if count == 0:
                return pos + header_size
            cursor = pos + header_size
            for _ in range(count):
                cursor = _skip_list_element(blob, cursor)
            return cursor

        if b0 == 0 and blob[pos + 1] == 0 and blob[pos + 2] == 0 and blob[pos + 3] == 0:
            count = struct.unpack_from("<I", blob, pos + 4)[0]
            if count == 0:
                return pos + 18
            cursor = pos + 18
            for _ in range(count):
                cursor = _skip_list_element(blob, cursor)
            return cursor

        if b0 == 0:
            count = blob[pos + 1] | (blob[pos + 2] << 8) | (blob[pos + 3] << 16)
            if count == 0:
                return pos + 18
            cursor = pos + 18
            for _ in range(count):
                cursor = _skip_list_element(blob, cursor)
            return cursor

    raise ValueError(f"Object list skip failed at 0x{pos:X}")


def _skip_list_element(blob: bytes, cursor: int) -> int:
    if cursor + 18 > len(blob):
        raise ValueError("List element overruns")

    mbc = struct.unpack_from("<H", blob, cursor)[0]
    if mbc == 0 or mbc > 16:
        type_idx = struct.unpack_from("<H", blob, cursor + 3)[0]
        sentinel = struct.unpack_from("<Q", blob, cursor + 6)[0]
        if sentinel == 0xFFFFFFFFFFFFFFFF:
            payload_off = struct.unpack_from("<I", blob, cursor + 14)[0]
            if payload_off == cursor + 18:
                return _skip_inline_payload(blob, payload_off)
        raise ValueError(f"Bad list element at 0x{cursor:X}")

    off = cursor + 2 + mbc
    wrapper_end = off + 2 + 1 + 4 + 4 + 4
    payload_off = struct.unpack_from("<I", blob, off + 11)[0]
    if payload_off == wrapper_end:
        return _skip_inline_payload(blob, payload_off)
    return wrapper_end


def _skip_inline_payload(blob: bytes, payload_start: int) -> int:
    cursor = payload_start + 4
    max_scan = min(len(blob), payload_start + 4096)
    for probe in range(cursor, max_scan):
        if probe + 4 <= len(blob):
            ts = struct.unpack_from("<I", blob, probe)[0]
            if ts == probe - payload_start and ts > 4:
                return probe + 4
    return cursor


BAG_KEY_NAMES = {
    1: "Money",
    2: "Character",
    3: "PearlUser",
    4: "PearlCharacter",
    5: "Quest",
    6: "Wagon",
    7: "PetAndVehicle",
    8: "CampWarehouse",
    9: "Warehouse",
    10: "Bank",
    11: "CampStraw",
    12: "Recovery",
    13: "Kuku",
    14: "Invisible",
}


def _extract_bag_ranges(data: bytes | bytearray) -> List[Tuple[int, int, str]]:
    bag_ranges: List[Tuple[int, int, str]] = []
    try:
        _ensure = 'Communitydump/desktopeditor'
        if _ensure not in __import__('sys').path:
            __import__('sys').path.insert(0, _ensure)
        from save_parser import build_result_from_raw
        result = build_result_from_raw(bytes(data), {'input_kind': 'raw_blob'})
        for obj in result['objects']:
            if obj.class_name != 'InventorySaveData':
                continue
            for f in obj.fields:
                if f.name != '_inventorylist' or not f.list_elements:
                    continue
                for bag in f.list_elements:
                    if not bag.child_fields:
                        continue
                    inv_key = -1
                    item_start = 0
                    item_end = 0
                    for cf in bag.child_fields:
                        if cf.name == '_inventoryKey' and cf.present:
                            try:
                                inv_key = int(cf.value_repr or -1)
                            except (ValueError, TypeError):
                                inv_key = -1
                        elif cf.name == '_itemList' and cf.list_elements:
                            item_start = cf.list_elements[0].start_offset
                            item_end = cf.list_elements[-1].end_offset
                    if inv_key >= 0 and item_start > 0 and item_end > item_start:
                        bag_name = BAG_KEY_NAMES.get(inv_key, f"Bag_{inv_key}")
                        bag_ranges.append((item_start, item_end, bag_name))
    except Exception:
        pass
    return bag_ranges


def enrich_items_with_parc(
    data: bytes | bytearray,
    items: List[SaveItem],
) -> Tuple[int, str]:
    parc_items, status = scan_items_parc(data)
    if not parc_items:
        return 0, status

    parc_by_no: Dict[int, SaveItem] = {}
    for pi in parc_items:
        parc_by_no[pi.item_no] = pi

    bag_ranges = _extract_bag_ranges(data)

    enriched = 0
    for item in items:
        pi = parc_by_no.get(item.item_no)
        if pi is not None and pi.field_offsets:
            item.field_offsets = pi.field_offsets
            item.parc_parsed = True
            enriched += 1

        for bstart, bend, bname in bag_ranges:
            if bstart <= item.offset < bend:
                item.bag = bname
                break

    return enriched, f"PARC mode: {enriched}/{len(items)} items enriched with exact field offsets"


def apply_item_swap_parc(
    data: bytearray,
    item: SaveItem,
    new_key: int,
) -> List[Tuple[int, bytes, bytes]]:
    patches: List[Tuple[int, bytes, bytes]] = []
    old_key = struct.unpack_from("<I", data, item.offset + 12)[0]
    old_key_bytes = struct.pack("<I", old_key)
    new_key_bytes = struct.pack("<I", new_key)

    if old_key == new_key:
        return patches

    item_no = item.item_no
    item_no_bytes = struct.pack("<q", item_no)
    patched_offsets: set = set()

    def patch_at(pos: int) -> None:
        if pos not in patched_offsets:
            old = bytes(data[pos:pos + 4])
            data[pos:pos + 4] = new_key_bytes
            patches.append((pos, old, new_key_bytes))
            patched_offsets.add(pos)

    item_key_off = item.field_offsets.get("_itemKey", item.offset + 12)
    patch_at(item_key_off)

    tk_off = item.field_offsets.get("_transferredItemKey")
    if tk_off is not None and tk_off > 0:
        patch_at(tk_off)

    record_end_off = item.field_offsets.get("_record_end", item.offset + 300)
    region_start = item_key_off + 4
    region_end = min(record_end_off, len(data) - 4)
    for pos in range(region_start, region_end):
        if data[pos:pos + 4] == old_key_bytes:
            patch_at(pos)

    safe_ranges = _get_item_bearing_ranges(data)
    for rs, re, _ in safe_ranges:
        for pos in range(rs, min(re, len(data) - 12)):
            if pos in patched_offsets:
                continue
            if data[pos:pos + 8] == item_no_bytes:
                key_pos = pos + 8
                if key_pos + 4 <= len(data) and data[key_pos:key_pos + 4] == old_key_bytes:
                    patch_at(key_pos)

    equip_scan_start = max(0, item.offset - 16)
    equip_scan_end = min(len(data) - 40, record_end_off)
    for scan in range(equip_scan_start, equip_scan_end):
        if scan + 6 in patched_offsets:
            continue
        if (struct.unpack_from("<I", data, scan)[0] == 4
                and struct.unpack_from("<H", data, scan + 4)[0] == 0x0101
                and data[scan + 6:scan + 10] == old_key_bytes):
            patch_at(scan + 6)

    for rs, re, _ in safe_ranges:
        for dup_off in range(rs, min(re, len(data) - 40)):
            if dup_off == item.offset:
                continue
            if struct.unpack_from("<I", data, dup_off)[0] != 1:
                continue
            dup_no = struct.unpack_from("<q", data, dup_off + 4)[0]
            if dup_no != item_no:
                continue
            dup_key_off = dup_off + 12
            if data[dup_key_off:dup_key_off + 4] == old_key_bytes:
                patch_at(dup_key_off)
                dup_end = min(dup_off + 300, len(data) - 4)
                for pos in range(dup_off + 16, dup_end):
                    if data[pos:pos + 4] == old_key_bytes:
                        patch_at(pos)

    item.item_key = new_key
    return patches


def template_item_swap(
    data: bytearray,
    item: SaveItem,
    new_key: int,
) -> List[Tuple[int, bytes, bytes]]:
    try:
        from parc_inserter3 import load_item_template
    except ImportError:
        return smart_item_swap(data, item, new_key)

    tmpl = load_item_template(new_key)
    if not tmpl:
        log.info("No template for %d, falling back to key-only swap", new_key)
        return smart_item_swap(data, item, new_key)

    template_binary, field_positions = tmpl

    if not item.parc_parsed or not item.field_offsets:
        log.info("Item not PARC-parsed, falling back to key-only swap")
        return smart_item_swap(data, item, new_key)

    record_start = item.field_offsets.get('_record_start', 0)
    record_end = item.field_offsets.get('_record_end', 0)
    if record_start <= 0 or record_end <= record_start:
        log.info("No record boundaries, falling back to key-only swap")
        return smart_item_swap(data, item, new_key)

    current_size = record_end - record_start
    template_size = len(template_binary)

    if current_size != template_size:
        log.info("Size mismatch: current=%d template=%d, falling back to key-only swap",
                 current_size, template_size)
        return smart_item_swap(data, item, new_key)

    replacement = bytearray(template_binary)

    if '_itemNo' in field_positions:
        orig_no_off = item.field_offsets.get('_itemNo', item.offset + 4)
        orig_no = struct.unpack_from('<q', data, orig_no_off)[0]
        struct.pack_into('<q', replacement, field_positions['_itemNo']['rel_offset'], orig_no)

    if '_slotNo' in field_positions:
        orig_slot_off = item.field_offsets.get('_slotNo', item.offset + 16)
        orig_slot = struct.unpack_from('<H', data, orig_slot_off)[0]
        struct.pack_into('<H', replacement, field_positions['_slotNo']['rel_offset'], orig_slot)

    if '_stackCount' in field_positions:
        orig_stack_off = item.field_offsets.get('_stackCount', item.offset + 18)
        orig_stack = struct.unpack_from('<q', data, orig_stack_off)[0]
        struct.pack_into('<q', replacement, field_positions['_stackCount']['rel_offset'], orig_stack)

    if '_itemKey' in field_positions:
        struct.pack_into('<I', replacement, field_positions['_itemKey']['rel_offset'], new_key)

    orig_tmpl_key = struct.unpack_from('<I', template_binary,
                                        field_positions['_itemKey']['rel_offset'])[0]
    key_bytes = struct.pack('<I', orig_tmpl_key)
    first = replacement.find(key_bytes)
    if first >= 0:
        second = replacement.find(key_bytes, first + 4)
        if second >= 0:
            struct.pack_into('<I', replacement, second, new_key)

    mbc_orig = struct.unpack_from('<H', data, record_start)[0]
    header_size = 2 + mbc_orig + 2 + 1 + 8 + 4
    if header_size <= current_size:
        replacement[:header_size] = data[record_start:record_start + header_size]
        if '_itemKey' in field_positions:
            struct.pack_into('<I', replacement, field_positions['_itemKey']['rel_offset'], new_key)

    old_bytes = bytes(data[record_start:record_end])
    data[record_start:record_end] = replacement
    patches = [(record_start, old_bytes, bytes(replacement))]

    old_key = item.item_key
    old_key_bytes = struct.pack('<I', old_key)
    new_key_bytes = struct.pack('<I', new_key)
    item_no_bytes = struct.pack('<q', item.item_no)

    safe_ranges = _get_item_bearing_ranges(data)
    for rs, re_end, _ in safe_ranges:
        for pos in range(rs, min(re_end, len(data) - 12)):
            if pos >= record_start and pos < record_end:
                continue
            if data[pos:pos + 8] == item_no_bytes:
                key_pos = pos + 8
                if key_pos + 4 <= len(data) and data[key_pos:key_pos + 4] == old_key_bytes:
                    old = bytes(data[key_pos:key_pos + 4])
                    data[key_pos:key_pos + 4] = new_key_bytes
                    patches.append((key_pos, old, new_key_bytes))

    item.item_key = new_key
    log.info("Template swap: %d -> %d (%dB replaced + %d extra patches)",
             old_key, new_key, current_size, len(patches) - 1)
    return patches


def smart_item_swap(
    data: bytearray,
    item: SaveItem,
    new_key: int,
) -> List[Tuple[int, bytes, bytes]]:
    if item.parc_parsed and item.field_offsets:
        return apply_item_swap_parc(data, item, new_key)
    return apply_item_swap(data, item, new_key)


def apply_item_swap_all(
    data: bytearray,
    item: SaveItem,
    new_key: int,
) -> List[Tuple[int, bytes, bytes]]:
    patches: List[Tuple[int, bytes, bytes]] = []
    old_key = struct.unpack_from("<I", data, item.offset + 12)[0]
    old_key_bytes = struct.pack("<I", old_key)
    new_key_bytes = struct.pack("<I", new_key)

    if old_key == new_key:
        return patches

    safe_ranges = _get_item_bearing_ranges(data)
    for rs, re, _ in safe_ranges:
        pos = rs
        while pos <= min(re, len(data)) - 4:
            if data[pos:pos + 4] == old_key_bytes:
                data[pos:pos + 4] = new_key_bytes
                patches.append((pos, old_key_bytes, new_key_bytes))
                pos += 4
            else:
                pos += 1

    item.item_key = new_key
    return patches
