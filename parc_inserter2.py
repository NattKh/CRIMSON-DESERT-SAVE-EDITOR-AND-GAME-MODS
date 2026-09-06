from __future__ import annotations

import logging
import os
import struct
import sys
from typing import List, Tuple, Optional

log = logging.getLogger(__name__)

_MY_DIR = os.path.dirname(os.path.abspath(__file__))
_DESKTOP_DIR = os.path.join(_MY_DIR, 'desktopeditor')
if not os.path.isdir(_DESKTOP_DIR):
    _DESKTOP_DIR = os.path.join(_MY_DIR, 'Communitydump', 'desktopeditor')

def _get_full_parser():
    if _DESKTOP_DIR not in sys.path:
        sys.path.insert(0, _DESKTOP_DIR)
    import save_parser as sp
    return sp


_SENTINEL = b'\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF'


def _u16(data: bytes, off: int) -> int:
    return struct.unpack_from('<H', data, off)[0]


def _u32(data: bytes, off: int) -> int:
    return struct.unpack_from('<I', data, off)[0]


def _u64(data: bytes, off: int) -> int:
    return struct.unpack_from('<Q', data, off)[0]


_BLANK_ITEM_HEX = "03009f280d0d0000ffffffffffffffffe4cc020000000000010000007701000000000000a2490f0000000100000000000000ffff05000500000000000000000000000000000000010000120000ffffffffffffffff29cd02000000000004000000010000120000ffffffffffffffff43cd02000000000004000000010000120000ffffffffffffffff5dcd02000000000004000000010000120000ffffffffffffffff77cd02000000000004000000010000120000ffffffffffffffff91cd020000000000040000000101a2490f000000000000000000703bc16900000000cb000000"


def build_blank_item(insert_at: int, item_key: int, item_no: int,
                     stack: int = 1, slot: int = 0,
                     item_type_index: int = -1, socket_type_index: int = -1) -> bytes:
    entry = bytearray(bytes.fromhex(_BLANK_ITEM_HEX))

    if item_type_index >= 0:
        struct.pack_into('<H', entry, 5, item_type_index)

    struct.pack_into('<I', entry, 16, insert_at + 20)

    struct.pack_into('<I', entry, 24, 1)
    struct.pack_into('<q', entry, 28, item_no)
    struct.pack_into('<I', entry, 36, item_key)
    struct.pack_into('<H', entry, 40, slot)
    struct.pack_into('<q', entry, 42, stack)
    struct.pack_into('<I', entry, 203, item_key)

    socket_start = 71
    socket_size = 26
    for i in range(5):
        so = socket_start + i * socket_size
        if socket_type_index >= 0:
            struct.pack_into('<H', entry, so + 3, socket_type_index)
        abs_payload = insert_at + so + 18
        struct.pack_into('<I', entry, so + 14, abs_payload)


    return bytes(entry)


def _compute_payload_offset_pos(f, raw: bytes) -> int:
    if f.child_payload_offset <= 0 or f.start_offset <= 0:
        return -1
    if f.child_mask_byte_count <= 0:
        return -1

    expected = f.child_payload_offset

    if getattr(f, 'note', '') == 'compact_list_element':
        pos = f.start_offset + 14
        if pos + 4 <= len(raw) and _u32(raw, pos) == expected:
            return pos
        return -1

    if f.meta_kind == 4:
        pos = f.start_offset + 2 + f.child_mask_byte_count + 11
        if pos + 4 <= len(raw) and _u32(raw, pos) == expected:
            return pos
        return -1

    if f.meta_kind == 5:
        for prefix_delta in (0, 1, 3):
            body = f.start_offset + prefix_delta
            pos = body + 2 + f.child_mask_byte_count + 11
            if pos + 4 <= len(raw) and _u32(raw, pos) == expected:
                return pos
        return -1

    if f.meta_kind in (6, 7):
        pos = f.start_offset + 2 + f.child_mask_byte_count + 11
        if pos + 4 <= len(raw) and _u32(raw, pos) == expected:
            return pos
        for prefix_delta in (1, 3):
            body = f.start_offset + prefix_delta
            pos = body + 2 + f.child_mask_byte_count + 11
            if pos + 4 <= len(raw) and _u32(raw, pos) == expected:
                return pos
        return -1

    return -1


def _collect_from_fields(fields, raw: bytes, offset_positions, trailing_sizes):
    for f in fields:
        if f.child_payload_offset > 0 and f.start_offset > 0:
            pos = _compute_payload_offset_pos(f, raw)
            if pos >= 0:
                offset_positions.append((pos, f.child_payload_offset))
            else:
                log.debug(
                    "Could not compute pos for payload_offset=0x%X at start=0x%X mk=%d note=%s",
                    f.child_payload_offset, f.start_offset, f.meta_kind, getattr(f, 'note', ''),
                )

        if f.child_size_u32 > 0 and f.child_payload_offset > 0:
            size_pos = f.child_payload_offset + f.child_size_u32
            trailing_sizes.append((size_pos, f.child_payload_offset))

        if f.child_fields:
            _collect_from_fields(f.child_fields, raw, offset_positions, trailing_sizes)
        if f.list_elements:
            _collect_from_fields(f.list_elements, raw, offset_positions, trailing_sizes)


def collect_all_positions(result, raw: bytes):
    offset_positions = []
    trailing_sizes = []

    for obj in result['objects']:
        _collect_from_fields(obj.fields, raw, offset_positions, trailing_sizes)

    verified = 0
    bad = 0
    for pos, expected in offset_positions:
        if pos + 4 <= len(raw):
            actual = _u32(raw, pos)
            if actual == expected:
                verified += 1
            else:
                bad += 1

    log.info("Offset positions: %d collected, %d verified, %d bad", len(offset_positions), verified, bad)
    log.info("Trailing sizes: %d collected", len(trailing_sizes))

    if bad > 0:
        log.warning("%d offset positions failed verification — these will be skipped", bad)
        offset_positions = [
            (pos, val) for pos, val in offset_positions
            if pos + 4 <= len(raw) and _u32(raw, pos) == val
        ]

    return offset_positions, trailing_sizes


def parse_and_collect(raw: bytes):
    sp = _get_full_parser()
    log.info("Parsing full save structure (%d bytes)...", len(raw))
    container_info = {"input_kind": "raw_blob"}
    result = sp.build_result_from_raw(raw, container_info)
    log.info(
        "Parsed: %d types, %d TOC entries, %d object blocks",
        len(result['schema']['types']),
        result['toc']['entry_count'],
        len(result['objects']),
    )
    offset_positions, trailing_sizes = collect_all_positions(result, raw)
    return result, offset_positions, trailing_sizes


def insert_and_fix(
    raw: bytes,
    insertion_point: int,
    new_bytes: bytes,
    offset_positions: List[Tuple[int, int]],
    trailing_sizes: List[Tuple[int, int]],
    toc_entries,
    schema_end: int,
    target_toc_idx: int,
    list_count_pos: int = -1,
) -> bytes:
    delta = len(new_bytes)
    result = bytearray(raw[:insertion_point]) + bytearray(new_bytes) + bytearray(raw[insertion_point:])
    log.info("Inserted %d bytes at offset 0x%X (blob %d -> %d)", delta, insertion_point, len(raw), len(result))

    fixed_payloads = 0
    for pos, old_val in offset_positions:
        new_pos = pos + delta if pos >= insertion_point else pos
        if new_pos + 4 > len(result):
            continue
        if old_val >= insertion_point:
            struct.pack_into('<I', result, new_pos, old_val + delta)
            fixed_payloads += 1

    log.info("Fixed %d/%d child_payload_offset values", fixed_payloads, len(offset_positions))

    fixed_sizes = 0
    for size_pos, payload_start in trailing_sizes:
        if payload_start < insertion_point <= size_pos:
            new_size_pos = size_pos + delta
            if new_size_pos + 4 > len(result):
                continue
            old_val = _u32(result, new_size_pos)
            struct.pack_into('<I', result, new_size_pos, old_val + delta)
            fixed_sizes += 1

    log.info("Fixed %d/%d trailing size fields", fixed_sizes, len(trailing_sizes))

    stream_size_pos = schema_end + 8
    old_stream_size = _u32(result, stream_size_pos)
    struct.pack_into('<I', result, stream_size_pos, old_stream_size + delta)
    log.info("stream_size: %d -> %d", old_stream_size, old_stream_size + delta)

    fixed_toc = 0
    for entry in toc_entries:
        doff_pos = entry.entry_offset + 12
        dsize_pos = entry.entry_offset + 16

        if entry.index == target_toc_idx:
            old_size = _u32(result, dsize_pos)
            struct.pack_into('<I', result, dsize_pos, old_size + delta)
            log.info("TOC[%d] %s size: %d -> %d", entry.index, entry.class_name, old_size, old_size + delta)

        if entry.data_offset >= insertion_point:
            struct.pack_into('<I', result, doff_pos, entry.data_offset + delta)
            fixed_toc += 1

    log.info("Fixed %d TOC data_offset values", fixed_toc)

    if list_count_pos >= 0:
        old_count = _u32(result, list_count_pos)
        struct.pack_into('<I', result, list_count_pos, old_count + 1)
        log.info("List count: %d -> %d at 0x%X", old_count, old_count + 1, list_count_pos)

    return bytes(result)


def clone_and_patch_entry(
    raw: bytes,
    template_start: int,
    entry_size: int,
    new_item_key: int,
    new_item_no: int,
    insert_at: int,
    new_stack: int = 1,
    new_slot: int = -1,
    new_enchant: int = -1,
) -> bytes:
    entry = bytearray(raw[template_start:template_start + entry_size])

    patched = False
    for off in range(len(entry) - 16):
        sv = struct.unpack_from('<I', entry, off)[0]
        if sv != 1:
            continue
        ino = struct.unpack_from('<q', entry, off + 4)[0]
        if ino < 1 or ino > 999999:
            continue
        key = struct.unpack_from('<I', entry, off + 12)[0]
        if key < 1 or key > 0x7FFFFFFF:
            continue

        old_key = key
        old_key_bytes = struct.pack('<I', old_key)
        new_key_bytes = struct.pack('<I', new_item_key)

        struct.pack_into('<q', entry, off + 4, new_item_no)
        if new_slot >= 0:
            struct.pack_into('<H', entry, off + 16, new_slot)
        struct.pack_into('<q', entry, off + 18, new_stack)
        if new_enchant >= 0:
            struct.pack_into('<H', entry, off + 26, new_enchant)

        for p in range(len(entry) - 4):
            if entry[p:p + 4] == old_key_bytes:
                entry[p:p + 4] = new_key_bytes

        patched = True
        log.info("Patched item: key=%d->%d no=%d slot=%d stack=%d enchant=%s",
                 old_key, new_item_key, new_item_no,
                 new_slot if new_slot >= 0 else struct.unpack_from('<H', entry, off + 16)[0],
                 new_stack, new_enchant if new_enchant >= 0 else 'kept')
        break

    if not patched:
        log.warning("Could not find ItemSaveData fields in template entry")


    if len(entry) >= 18 and entry[6:14] == _SENTINEL:
        old_po = _u32(entry, 14)
        relative = old_po - template_start
        if 0 <= relative <= entry_size + 100:
            struct.pack_into('<I', entry, 14, insert_at + relative)

    for off in range(len(entry) - 15):
        mbc = _u16(entry, off)
        if mbc < 1 or mbc > 8:
            continue
        sent_start = off + 2 + mbc + 3
        if sent_start + 12 > len(entry):
            continue
        if entry[sent_start:sent_start + 8] != _SENTINEL:
            continue
        type_idx = _u16(entry, off + 2 + mbc)
        if type_idx > 200:
            continue
        if entry[off + 2 + mbc + 2] != 0:
            continue

        po_pos = sent_start + 8
        old_po = _u32(entry, po_pos)
        relative = old_po - template_start
        if 0 <= relative <= entry_size + 100:
            new_po = insert_at + relative
            struct.pack_into('<I', entry, po_pos, new_po)

    return bytes(entry)


def _find_items_in_block(raw: bytes, block_offset: int, block_size: int, max_slot: int = 200):
    block_raw = raw[block_offset:block_offset + block_size]
    items = []
    for off in range(20, len(block_raw) - 40):
        if struct.unpack_from('<I', block_raw, off)[0] != 1:
            continue
        ino = struct.unpack_from('<q', block_raw, off + 4)[0]
        if ino < 1 or ino > 999999:
            continue
        key = struct.unpack_from('<I', block_raw, off + 12)[0]
        if key < 1 or key > 0x7FFFFFFF:
            continue
        slot = struct.unpack_from('<H', block_raw, off + 16)[0]
        if slot > max_slot:
            continue
        stk = struct.unpack_from('<q', block_raw, off + 18)[0]
        if stk < 1 or stk > 99999:
            continue
        if off >= 16 and struct.unpack_from('<q', block_raw, off - 16)[0] != -1:
            continue
        items.append((off, ino, key))
    return items


def _find_locator_before_item(block_raw: bytes, item_off: int) -> int:
    for back in range(2, 30):
        check = item_off - back
        if check < 0:
            break
        mbc = _u16(block_raw, check)
        if 1 <= mbc <= 8:
            sent_pos = check + 2 + mbc + 3
            if sent_pos + 8 <= len(block_raw) and block_raw[sent_pos:sent_pos + 8] == _SENTINEL:
                return check
        if check + 14 <= len(block_raw):
            if block_raw[check + 6:check + 14] == _SENTINEL:
                return check
    return item_off


def _find_list_count(raw: bytes, block_offset: int, first_item_abs: int, expected_count: int) -> int:
    for back in range(20, 50):
        check = first_item_abs - back
        if check < block_offset:
            break
        if raw[check] == 0:
            count_val = _u32(raw, check + 1)
            if count_val == expected_count:
                return check + 1
        if check % 4 == 0:
            count_val = _u32(raw, check)
            if count_val == expected_count:
                return check
    return -1


def _find_sold_item_list_from_tree(result):
    store_block = None
    for obj in result['objects']:
        if obj.class_name == 'StoreSaveData':
            store_block = obj
            break
    if not store_block:
        return None, -1, -1

    store_data_list = None
    for f in store_block.fields:
        if f.name == '_storeDataList' and f.list_elements:
            store_data_list = f
            break
    if not store_data_list:
        return None, -1, -1

    best_list = None
    best_count = 0
    best_vendor_idx = -1
    best_store_key = -1

    for i, vendor in enumerate(store_data_list.list_elements):
        if not vendor.child_fields:
            continue
        store_key = -1
        for cf in vendor.child_fields:
            if cf.name == '_storeKey' and cf.present:
                store_key = struct.unpack_from('<H', b'\x00' * 2, 0)[0]
                break

        for cf in vendor.child_fields:
            if cf.name == '_storeSoldItemDataList' and cf.present and cf.list_elements:
                count = len(cf.list_elements)
                if count > best_count:
                    best_count = count
                    best_list = cf
                    best_vendor_idx = i

    return best_list, best_vendor_idx, best_count


def _find_item_list_from_tree(result, block_name, list_field_name):
    block = None
    for obj in result['objects']:
        if obj.class_name == block_name:
            block = obj
            break
    if not block:
        return None

    if block_name == 'StoreSaveData':
        store_data_list = None
        for f in block.fields:
            if f.name == '_storeDataList' and f.list_elements:
                store_data_list = f
                break
        if not store_data_list:
            return None

        best = None
        best_count = 0
        for vendor in store_data_list.list_elements:
            if not vendor.child_fields:
                continue
            for cf in vendor.child_fields:
                if cf.name == list_field_name and cf.present and cf.list_elements:
                    if len(cf.list_elements) > best_count:
                        best = cf
                        best_count = len(cf.list_elements)
        return best

    if block_name == 'InventorySaveData':
        inv_list = None
        for f in block.fields:
            if f.name == '_inventorylist' and f.list_elements:
                inv_list = f
                break
        if not inv_list:
            return None

        best = None
        best_count = 0
        for bag in inv_list.list_elements:
            if not bag.child_fields:
                continue
            for cf in bag.child_fields:
                if cf.name == list_field_name and cf.present and cf.list_elements:
                    if len(cf.list_elements) > best_count:
                        best = cf
                        best_count = len(cf.list_elements)
        return best

    return None


def _find_vendor_with_sold_items(result):
    block = None
    for obj in result['objects']:
        if obj.class_name == 'StoreSaveData':
            block = obj
            break
    if not block:
        return None, None, None

    store_data_list = None
    for f in block.fields:
        if f.name == '_storeDataList' and f.list_elements:
            store_data_list = f
            break
    if not store_data_list:
        return None, None, None

    best_vendor = None
    best_sold = None
    best_times = None
    best_count = 0

    for vendor in store_data_list.list_elements:
        if not vendor.child_fields:
            continue
        sold_field = None
        time_field = None
        for cf in vendor.child_fields:
            if cf.name == '_storeSoldItemDataList' and cf.present and cf.list_elements:
                sold_field = cf
            if cf.name == '_itemSoldFieldTimeRawList' and cf.present:
                time_field = cf
        if sold_field and len(sold_field.list_elements) > best_count:
            best_count = len(sold_field.list_elements)
            best_vendor = vendor
            best_sold = sold_field
            best_times = time_field

    return best_vendor, best_sold, best_times


def _find_list_count_from_tree(raw: bytes, list_field) -> int:
    expected = list_field.list_count
    header_start = list_field.start_offset
    header_size = list_field.list_header_size

    for off in range(header_size):
        pos = header_start + off
        if pos + 4 > len(raw):
            continue
        if _u32(raw, pos) == expected:
            return pos

    for off in range(header_size + 20):
        pos = header_start + off
        if pos + 4 > len(raw):
            continue
        if _u32(raw, pos) == expected:
            return pos

    return -1


def add_item_to_store(
    raw: bytes,
    new_item_key: int,
    new_item_no: int,
    new_stack: int = 1,
) -> Optional[bytes]:
    log.info("=== Add Item to Store (parse-tree v2) ===")
    log.info("Item key: %d, no: %d, stack: %d", new_item_key, new_item_no, new_stack)

    result, offset_positions, trailing_sizes = parse_and_collect(raw)

    toc_entries = result['toc']['entries']
    schema_end = result['raw']['schema_end']

    store_toc_idx = -1
    for entry in toc_entries:
        if entry.class_name == 'StoreSaveData':
            store_toc_idx = entry.index
            log.info("StoreSaveData: TOC[%d] offset=0x%X size=%d",
                      entry.index, entry.data_offset, entry.data_size)
            break
    if store_toc_idx < 0:
        log.error("StoreSaveData not found in TOC")
        return None

    vendor_elem, sold_list, time_list = _find_vendor_with_sold_items(result)
    if not sold_list or not sold_list.list_elements:
        log.error("No _storeSoldItemDataList with items found in parse tree")
        return None

    elements = sold_list.list_elements
    log.info("Found _storeSoldItemDataList: %d items, list at 0x%X-0x%X",
             len(elements), sold_list.start_offset, sold_list.end_offset)

    if time_list:
        log.info("Found _itemSoldFieldTimeRawList: %s at 0x%X-0x%X",
                 time_list.value_repr[:50], time_list.start_offset, time_list.end_offset)
    else:
        log.warning("No _itemSoldFieldTimeRawList found — timestamp won't be added")

    last_elem = elements[-1]
    item_insert_at = last_elem.end_offset
    new_entry = None

    try:
        from item_template_db import get_template, build_item_from_template
        template = get_template(new_item_key)
        if template:
            log.info("Using template from DB for key %d (mask=%s, %dB)",
                     new_item_key, template['mask'], template['size'])

            item_type_idx = -1
            socket_type_idx = -1
            for t in result['schema']['types']:
                if t.name == 'ItemSaveData':
                    item_type_idx = t.index
                elif t.name == 'ItemSocketSaveData':
                    socket_type_idx = t.index

            new_entry = bytearray(bytes.fromhex(template['hex']))
            fp = template.get('field_positions', {})

            if '_itemNo' in fp:
                struct.pack_into('<q', new_entry, fp['_itemNo']['rel_offset'], new_item_no)
            if '_itemKey' in fp:
                struct.pack_into('<I', new_entry, fp['_itemKey']['rel_offset'], new_item_key)
            if '_slotNo' in fp:
                struct.pack_into('<H', new_entry, fp['_slotNo']['rel_offset'], 0)
            if '_stackCount' in fp:
                struct.pack_into('<q', new_entry, fp['_stackCount']['rel_offset'], new_stack)
            if '_transferredItemKey' in fp:
                struct.pack_into('<I', new_entry, fp['_transferredItemKey']['rel_offset'], new_item_key)

            mbc = struct.unpack_from('<H', new_entry, 0)[0]
            if item_type_idx >= 0:
                struct.pack_into('<H', new_entry, 2 + mbc, item_type_idx)

            po_offset = 2 + mbc + 2 + 1 + 8
            struct.pack_into('<I', new_entry, po_offset, item_insert_at + po_offset + 4)

            _SENT = b'\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF'
            for off in range(len(new_entry) - 15):
                m = struct.unpack_from('<H', new_entry, off)[0]
                if m < 1 or m > 8:
                    continue
                s = off + 2 + m + 3
                if s + 12 > len(new_entry):
                    continue
                if new_entry[s:s + 8] != _SENT:
                    continue
                t = struct.unpack_from('<H', new_entry, off + 2 + m)[0]
                if t > 200 or new_entry[off + 2 + m + 2] != 0:
                    continue
                pp = s + 8
                if socket_type_idx >= 0:
                    struct.pack_into('<H', new_entry, off + 2 + m, socket_type_idx)
                wrapper_end = off + 2 + m + 2 + 1 + 4 + 4 + 4
                struct.pack_into('<I', new_entry, pp, item_insert_at + wrapper_end)

            for off in range(len(new_entry) - 18):
                if new_entry[off + 6:off + 14] == _SENT:
                    if socket_type_idx >= 0:
                        struct.pack_into('<H', new_entry, off + 3, socket_type_idx)
                    struct.pack_into('<I', new_entry, off + 14, item_insert_at + off + 18)

            new_entry = bytes(new_entry)
            log.info("Template item: %d bytes, insert at 0x%X", len(new_entry), item_insert_at)
    except Exception as e:
        log.warning("Template DB lookup failed: %s — falling back to clone", e)
        new_entry = None

    if new_entry is None:
        sorted_elems = sorted(elements, key=lambda e: e.end_offset - e.start_offset)
        donor = sorted_elems[0]
        donor_start = donor.start_offset
        donor_size = donor.end_offset - donor.start_offset
        new_entry = bytearray(raw[donor_start:donor.end_offset])

        log.info("Cloning donor item: 0x%X-0x%X (%dB, mask=%s)",
                 donor_start, donor.end_offset, donor_size, donor.child_mask_bytes.hex())

        if donor.child_fields:
            for f in donor.child_fields:
                if not f.present:
                    continue
                rel = f.start_offset - donor_start
                if f.name == '_itemNo':
                    struct.pack_into('<q', new_entry, rel, new_item_no)
                elif f.name == '_itemKey':
                    struct.pack_into('<I', new_entry, rel, new_item_key)
                elif f.name == '_slotNo':
                    struct.pack_into('<H', new_entry, rel, 0)
                elif f.name == '_stackCount':
                    struct.pack_into('<q', new_entry, rel, new_stack)
                elif f.name == '_transferredItemKey':
                    struct.pack_into('<I', new_entry, rel, new_item_key)

        mbc = struct.unpack_from('<H', new_entry, 0)[0]
        po_offset = 2 + mbc + 2 + 1 + 8
        old_po = struct.unpack_from('<I', new_entry, po_offset)[0]
        struct.pack_into('<I', new_entry, po_offset, item_insert_at + (old_po - donor_start))

        _SENT = b'\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF'
        for off in range(len(new_entry) - 15):
            m = struct.unpack_from('<H', new_entry, off)[0]
            if m < 1 or m > 8:
                continue
            s = off + 2 + m + 3
            if s + 12 > len(new_entry):
                continue
            if new_entry[s:s + 8] != _SENT:
                continue
            t = struct.unpack_from('<H', new_entry, off + 2 + m)[0]
            if t > 200 or new_entry[off + 2 + m + 2] != 0:
                continue
            pp = s + 8
            old_val = struct.unpack_from('<I', new_entry, pp)[0]
            rel_val = old_val - donor_start
            if 0 <= rel_val <= donor_size + 100:
                struct.pack_into('<I', new_entry, pp, item_insert_at + rel_val)

        for off in range(len(new_entry) - 18):
            if new_entry[off + 6:off + 14] == _SENT:
                old_val = struct.unpack_from('<I', new_entry, off + 14)[0]
                rel_val = old_val - donor_start
                if 0 <= rel_val <= donor_size + 100:
                    struct.pack_into('<I', new_entry, off + 14, item_insert_at + rel_val)

        new_entry = bytes(new_entry)
        log.info("Cloned item: %d bytes, insert at 0x%X", len(new_entry), item_insert_at)

    sold_list_count_pos = _find_list_count_from_tree(raw, sold_list)
    if sold_list_count_pos >= 0:
        log.info("Sold list count at 0x%X = %d", sold_list_count_pos, _u32(raw, sold_list_count_pos))

    time_insert_at = -1
    time_count_pos = -1
    time_bytes = b''

    if time_list and time_list.start_offset > 0 and time_list.end_offset > time_list.start_offset:
        time_insert_at = time_list.end_offset
        time_bytes = struct.pack('<Q', 0)

        time_count_candidate = time_list.start_offset + 1
        if time_count_candidate + 4 <= len(raw):
            existing_time_count = _u32(raw, time_count_candidate)
            sold_count = len(elements)
            if existing_time_count == sold_count:
                time_count_pos = time_count_candidate
                log.info("Time count at 0x%X = %d (matches sold count)", time_count_pos, existing_time_count)
            else:
                for delta_off in (2, 5, 9):
                    pos = time_list.start_offset + delta_off
                    if pos + 4 <= len(raw) and _u32(raw, pos) == sold_count:
                        time_count_pos = pos
                        log.info("Time count at 0x%X = %d (alt offset +%d)", time_count_pos, sold_count, delta_off)
                        break

        if time_count_pos < 0:
            log.warning("Could not find timestamp count position — skipping timestamp insertion")
            time_insert_at = -1

        log.info("Timestamp insertion: %d bytes at 0x%X", len(time_bytes), time_insert_at)


    item_delta = len(new_entry)
    new_blob = insert_and_fix(
        raw, item_insert_at, new_entry,
        offset_positions, trailing_sizes,
        toc_entries, schema_end, store_toc_idx,
        sold_list_count_pos,
    )

    log.info("Item insertion done: %d -> %d bytes (+%d)", len(raw), len(new_blob), item_delta)

    if time_insert_at >= 0 and len(time_bytes) > 0:
        adj_time_insert = time_insert_at + item_delta if time_insert_at >= item_insert_at else time_insert_at
        adj_time_count = time_count_pos + item_delta if time_count_pos >= item_insert_at else time_count_pos

        log.info("Timestamp insert (adjusted): 0x%X, count at 0x%X", adj_time_insert, adj_time_count)

        log.info("Re-parsing for timestamp insertion...")
        result2, offset_positions2, trailing_sizes2 = parse_and_collect(new_blob)
        toc_entries2 = result2['toc']['entries']
        schema_end2 = result2['raw']['schema_end']

        time_delta = len(time_bytes)
        new_blob2 = insert_and_fix(
            new_blob, adj_time_insert, time_bytes,
            offset_positions2, trailing_sizes2,
            toc_entries2, schema_end2, store_toc_idx,
            list_count_pos=-1,
        )

        if adj_time_count < adj_time_insert:
            old_tc = _u32(new_blob2, adj_time_count)
            struct.pack_into('<I', bytearray(new_blob2) if isinstance(new_blob2, bytes) else new_blob2,
                             adj_time_count, old_tc + 1)
            if isinstance(new_blob2, bytes):
                ba = bytearray(new_blob2)
                struct.pack_into('<I', ba, adj_time_count, old_tc + 1)
                new_blob2 = bytes(ba)
            log.info("Timestamp count: %d -> %d at 0x%X", old_tc, old_tc + 1, adj_time_count)

        new_blob = new_blob2
        log.info("Timestamp insertion done: total blob %d bytes (+%d)", len(new_blob), time_delta)

        del result2, offset_positions2, trailing_sizes2, toc_entries2

    log.info("Store insertion complete: %d -> %d bytes", len(raw), len(new_blob))
    return new_blob


def add_item_to_inventory(
    raw: bytes,
    new_item_key: int,
    new_item_no: int,
    new_stack: int = 1,
) -> Optional[bytes]:
    log.info("=== Add Item to Inventory (parse-tree v2) ===")
    log.info("Item key: %d, no: %d, stack: %d", new_item_key, new_item_no, new_stack)

    result, offset_positions, trailing_sizes = parse_and_collect(raw)

    toc_entries = result['toc']['entries']
    schema_end = result['raw']['schema_end']

    inv_toc_idx = -1
    for entry in toc_entries:
        if entry.class_name == 'InventorySaveData':
            inv_toc_idx = entry.index
            log.info("InventorySaveData: TOC[%d] offset=0x%X size=%d",
                      entry.index, entry.data_offset, entry.data_size)
            break
    if inv_toc_idx < 0:
        log.error("InventorySaveData not found in TOC")
        return None

    item_list = _find_item_list_from_tree(result, 'InventorySaveData', '_itemList')
    if not item_list or not item_list.list_elements:
        log.error("No _itemList with items found in parse tree")
        return None

    elements = item_list.list_elements
    log.info("Found _itemList: %d items, list at 0x%X-0x%X",
             len(elements), item_list.start_offset, item_list.end_offset)

    last_elem = elements[-1]
    template_start = last_elem.start_offset
    template_end = last_elem.end_offset
    entry_size = template_end - template_start
    insert_at = template_end

    log.info("Template element: 0x%X-0x%X (%d bytes), insert at 0x%X",
             template_start, template_end, entry_size, insert_at)

    max_slot = 0
    for obj in result['objects']:
        if obj.class_name != 'InventorySaveData':
            continue
        for f in obj.fields:
            if f.name == '_inventorylist' and f.list_elements:
                for bag in f.list_elements:
                    if not bag.child_fields:
                        continue
                    for cf in bag.child_fields:
                        if cf.name == '_itemList' and cf.list_elements:
                            for elem in cf.list_elements:
                                if elem.child_fields:
                                    for ef in elem.child_fields:
                                        if ef.name == '_slotNo' and ef.present:
                                            slot_val = struct.unpack_from('<H', raw, ef.start_offset)[0]
                                            if slot_val > max_slot:
                                                max_slot = slot_val
    new_slot = max_slot + 1
    log.info("Max slot in inventory: %d, assigning slot %d", max_slot, new_slot)

    new_entry = None
    try:
        from item_template_db import get_template
        template = get_template(new_item_key)
        if template:
            log.info("Using template from DB for key %d (mask=%s, %dB)",
                     new_item_key, template['mask'], template['size'])

            item_type_idx = -1
            socket_type_idx = -1
            for t in result['schema']['types']:
                if t.name == 'ItemSaveData':
                    item_type_idx = t.index
                elif t.name == 'ItemSocketSaveData':
                    socket_type_idx = t.index

            new_entry = bytearray(bytes.fromhex(template['hex']))
            fp = template.get('field_positions', {})

            if '_itemNo' in fp:
                struct.pack_into('<q', new_entry, fp['_itemNo']['rel_offset'], new_item_no)
            if '_itemKey' in fp:
                struct.pack_into('<I', new_entry, fp['_itemKey']['rel_offset'], new_item_key)
            if '_slotNo' in fp:
                struct.pack_into('<H', new_entry, fp['_slotNo']['rel_offset'], new_slot)
            if '_stackCount' in fp:
                struct.pack_into('<q', new_entry, fp['_stackCount']['rel_offset'], new_stack)
            if '_transferredItemKey' in fp:
                struct.pack_into('<I', new_entry, fp['_transferredItemKey']['rel_offset'], new_item_key)

            mbc = struct.unpack_from('<H', new_entry, 0)[0]
            if item_type_idx >= 0:
                struct.pack_into('<H', new_entry, 2 + mbc, item_type_idx)

            po_offset = 2 + mbc + 2 + 1 + 8
            struct.pack_into('<I', new_entry, po_offset, insert_at + po_offset + 4)

            _SENT = b'\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF'
            for off in range(len(new_entry) - 15):
                m = struct.unpack_from('<H', new_entry, off)[0]
                if m < 1 or m > 8:
                    continue
                s = off + 2 + m + 3
                if s + 12 > len(new_entry):
                    continue
                if new_entry[s:s + 8] != _SENT:
                    continue
                t = struct.unpack_from('<H', new_entry, off + 2 + m)[0]
                if t > 200 or new_entry[off + 2 + m + 2] != 0:
                    continue
                pp = s + 8
                if socket_type_idx >= 0:
                    struct.pack_into('<H', new_entry, off + 2 + m, socket_type_idx)
                wrapper_end = off + 2 + m + 2 + 1 + 4 + 4 + 4
                struct.pack_into('<I', new_entry, pp, insert_at + wrapper_end)

            for off in range(len(new_entry) - 18):
                if new_entry[off + 6:off + 14] == _SENT:
                    if socket_type_idx >= 0:
                        struct.pack_into('<H', new_entry, off + 3, socket_type_idx)
                    struct.pack_into('<I', new_entry, off + 14, insert_at + off + 18)

            new_entry = bytes(new_entry)
            log.info("Template item: %d bytes for inventory slot %d", len(new_entry), new_slot)
    except Exception as e:
        log.warning("Template DB lookup failed: %s — falling back to clone", e)
        new_entry = None

    if new_entry is None:
        new_entry = clone_and_patch_entry(
            raw, template_start, entry_size,
            new_item_key, new_item_no, insert_at, new_stack,
            new_slot=new_slot,
        )

    list_count_pos = _find_list_count_from_tree(raw, item_list)
    if list_count_pos >= 0:
        log.info("List count at 0x%X = %d", list_count_pos, _u32(raw, list_count_pos))
    else:
        log.warning("Could not find list count position")

    new_blob = insert_and_fix(
        raw, insert_at, new_entry,
        offset_positions, trailing_sizes,
        toc_entries, schema_end, inv_toc_idx,
        list_count_pos,
    )

    log.info("Inventory insertion complete: %d -> %d bytes (+%d)", len(raw), len(new_blob), len(new_entry))
    return new_blob
