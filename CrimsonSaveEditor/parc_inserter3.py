
import struct
import json
import os
import logging
from typing import Optional, Tuple, List, Dict, Any, Callable

log = logging.getLogger(__name__)

_ITEM_SOCKET_FIELD_SIZES: Dict[int, int] = {
    0: 4, 1: 8, 2: 4, 3: 2, 4: 8, 5: 8, 6: 2, 7: 8,
    8: 2, 9: 2, 10: 8, 11: 8, 12: 1, 13: 1,
}


def _item_socket_field_present(bitmask: bytes, field_idx: int) -> bool:
    byte_idx = field_idx // 8
    return bool(bitmask[byte_idx] & (1 << (field_idx % 8))) if byte_idx < len(bitmask) else False

_MY_DIR = os.path.dirname(os.path.abspath(__file__))
import sys
if _MY_DIR not in sys.path:
    sys.path.insert(0, _MY_DIR)
_MEIPASS = getattr(sys, '_MEIPASS', None)
_base = _MEIPASS if _MEIPASS else _MY_DIR
for _sub in ['Communitydump/desktopeditor', 'desktopeditor', 'Communitydump/source', 'Communitydump/BestCrypto']:
    _p = os.path.join(_base, _sub)
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.append(_p)
    _p2 = os.path.join(_MY_DIR, _sub)
    if _p2 != _p and os.path.isdir(_p2) and _p2 not in sys.path:
        sys.path.append(_p2)

SENTINEL = b'\xff\xff\xff\xff\xff\xff\xff\xff'


import ctypes
import json as _json
import tempfile

_dll = None
_dll_loaded = False


def _load_dll():
    global _dll, _dll_loaded
    if _dll_loaded:
        return _dll
    _dll_loaded = True
    for base in [_MY_DIR, getattr(sys, '_MEIPASS', _MY_DIR)]:
        dll_path = os.path.join(base, 'parc_parser.dll')
        if os.path.isfile(dll_path):
            try:
                _dll = ctypes.CDLL(dll_path)
                _dll.parc_version.restype = ctypes.c_char_p
                _dll.parc_parse_raw_file.argtypes = [
                    ctypes.c_char_p,
                    ctypes.POINTER(ctypes.c_char_p),
                    ctypes.POINTER(ctypes.c_uint32),
                ]
                _dll.parc_parse_raw_file.restype = ctypes.c_int
                _dll.parc_parse_blob.argtypes = [
                    ctypes.POINTER(ctypes.c_uint8),
                    ctypes.c_uint32,
                    ctypes.POINTER(ctypes.c_char_p),
                    ctypes.POINTER(ctypes.c_uint32),
                ]
                _dll.parc_parse_blob.restype = ctypes.c_int
                _dll.parc_free.argtypes = [ctypes.c_void_p]
                log.info("Loaded parc_parser.dll from %s (version: %s)",
                         dll_path, _dll.parc_version().decode())
                return _dll
            except Exception as e:
                log.warning("Failed to load parc_parser.dll: %s", e)
                _dll = None
    return None


class _DllField:
    __slots__ = ('name', 'type_name', 'meta_kind', 'present', 'decode_kind',
                 'start_offset', 'end_offset', 'value_repr', 'child_type_index',
                 'child_type_name', 'child_payload_offset', 'child_mask_byte_count',
                 'child_size_u32', 'child_fields', 'list_elements', 'list_count',
                 'list_header_size', 'list_prefix_u8', 'note', 'field_index')

    def __init__(self, d: dict):
        self.name = d.get('name', '')
        self.type_name = d.get('type_name', '')
        self.meta_kind = d.get('meta_kind', 0)
        self.present = d.get('present', False)
        self.decode_kind = d.get('decode_kind', 'unknown')
        self.start_offset = d.get('start_offset', 0)
        self.end_offset = d.get('end_offset', 0)
        self.value_repr = d.get('value_repr', '')
        self.child_type_index = d.get('child_type_index', -1)
        self.child_type_name = d.get('child_type_name', '')
        self.child_payload_offset = d.get('child_payload_offset', 0)
        self.child_mask_byte_count = d.get('child_mask_byte_count', 0)
        self.child_size_u32 = d.get('child_size_u32', 0)
        self.note = d.get('note', '')
        self.field_index = d.get('field_index', 0)
        self.list_count = d.get('list_count', 0)
        self.list_header_size = d.get('list_header_size', 0)
        self.list_prefix_u8 = d.get('list_prefix_u8', 0)
        self.child_fields = [_DllField(cf) for cf in d.get('child_fields', []) or []]
        self.list_elements = [_DllField(el) for el in d.get('list_elements', []) or []]


class _DllBlock:
    __slots__ = ('class_name', 'class_index', 'data_offset', 'data_size',
                 'entry_index', 'fields', 'mask_byte_count')

    def __init__(self, d: dict):
        self.class_name = d.get('class_name', '')
        self.class_index = d.get('class_index', 0)
        self.data_offset = d.get('data_offset', 0)
        self.data_size = d.get('data_size', 0)
        self.entry_index = d.get('entry_index', 0)
        self.mask_byte_count = d.get('mask_byte_count', 0)
        self.fields = [_DllField(f) for f in d.get('fields', [])]


class _DllType:
    __slots__ = ('index', 'name', 'fields')

    def __init__(self, d: dict):
        self.index = d.get('index', 0)
        self.name = d.get('name', '')
        self.fields = d.get('fields', [])


def _parse_tree_dll(blob: bytes) -> dict:
    dll = _load_dll()
    if dll is None:
        raise RuntimeError("parc_parser.dll not available")

    fd, temp_path = tempfile.mkstemp(suffix='.bin')
    try:
        os.write(fd, blob)
        os.close(fd)

        out_json = ctypes.c_char_p()
        out_size = ctypes.c_uint32()
        ret = dll.parc_parse_raw_file(
            temp_path.encode(),
            ctypes.byref(out_json),
            ctypes.byref(out_size),
        )
        if ret != 0 or not out_json.value:
            raise RuntimeError("DLL parse failed")

        data = _json.loads(out_json.value.decode('utf-8'))
        dll.parc_free(out_json)
    finally:
        try:
            os.unlink(temp_path)
        except OSError:
            pass

    if 'error' in data:
        raise RuntimeError(f"DLL parse error: {data['error']}")

    objects = [_DllBlock(o) for o in data.get('objects', [])]
    types = [_DllType(t) for t in data.get('schema', {}).get('types', [])]

    return {'objects': objects, 'types': types, 'schema': data.get('schema', {})}


def _parse_tree(blob: bytes) -> dict:
    from save_parser import build_result_from_raw
    return build_result_from_raw(blob, {'source': 'inserter', 'path': 'inserter'})


def _parse_tree_fast(blob: bytes) -> Optional[dict]:
    try:
        dll = _load_dll()
        if dll is not None:
            return _parse_tree_dll(blob)
    except Exception:
        pass
    return None


def _parse_schema(blob: bytes) -> dict:
    from save_parser import parse_schema
    schema = parse_schema(blob)
    return {td.index: td for td in schema['types']}


def _find_block(result: dict, class_name: str):
    for obj in result['objects']:
        if obj.class_name == class_name:
            return obj
    return None


def _find_field(obj, field_name: str):
    fields = getattr(obj, 'child_fields', None) or getattr(obj, 'fields', None) or []
    for f in fields:
        if f.name == field_name and f.present:
            return f
    return None


def _get_max_item_no(result: dict) -> int:
    _SAFE_BLOCKS = {
        'InventorySaveData', 'EquipmentSaveData',
        'StoreSaveData', 'MercenaryClanSaveData',
    }
    max_no = 0

    def _walk(field):
        nonlocal max_no
        if field.name == '_itemNo' and field.value_repr:
            try:
                v = int(field.value_repr)
                if 0 < v < 999999 and v > max_no:
                    max_no = v
            except (ValueError, TypeError):
                pass
        for cf in (field.child_fields or []):
            _walk(cf)
        for el in (field.list_elements or []):
            _walk(el)

    for obj in result['objects']:
        if obj.class_name in _SAFE_BLOCKS:
            for f in obj.fields:
                _walk(f)
    return max_no


_templates_cache = None
_category_map_cache = None


def clear_caches():
    global _templates_cache, _category_map_cache
    _templates_cache = None
    _category_map_cache = None


def _get_item_bag_key(item_key: int) -> int:
    global _category_map_cache
    if _category_map_cache is None:
        for base in [_MY_DIR, getattr(sys, '_MEIPASS', _MY_DIR)]:
            path = os.path.join(base, 'item_category_map.json')
            if os.path.isfile(path):
                with open(path) as f:
                    _category_map_cache = json.load(f)
                _category_map_cache = {int(k): v for k, v in _category_map_cache.items()}
                break
        if _category_map_cache is None:
            _category_map_cache = {}

    return _category_map_cache.get(item_key, 2)

def _get_templates() -> dict:
    global _templates_cache
    if _templates_cache is not None:
        return _templates_cache

    _templates_cache = {}

    for base in [_MY_DIR, getattr(sys, '_MEIPASS', _MY_DIR)]:
        path = os.path.join(base, 'master_templates.json')
        if os.path.isfile(path):
            with open(path) as f:
                data = json.load(f)
            _templates_cache = data.get('templates', data) if isinstance(data, dict) else {}
            break

    for base in [_MY_DIR, getattr(sys, '_MEIPASS', _MY_DIR)]:
        path = os.path.join(base, 'item_templates.json')
        if os.path.isfile(path):
            with open(path) as f:
                item_data = json.load(f)
            for k, v in item_data.items():
                _templates_cache[k] = v
            break

    return _templates_cache


def load_item_template(item_key: int) -> Optional[Tuple[bytearray, dict]]:
    templates = _get_templates()
    key_str = str(item_key)

    if key_str in templates:
        t = templates[key_str]
        return bytearray(bytes.fromhex(t['hex'])), t.get('field_positions', {})

    _CATEGORY_DEFAULT_MASK = {
        'Weapon': 'bf2825',
        'Shield': 'bf280d',
        'Armor': '9f280d',
        'Accessory': '9f280d',
        'Equipment': '9f280d',
        'Consumable': '9f280d',
        'Material': '9f280d',
        'Misc': '9f280d',
    }

    try:
        names_path = os.path.join(_MY_DIR if not getattr(sys, '_MEIPASS', None) else sys._MEIPASS, 'item_names.json')
        if os.path.isfile(names_path):
            with open(names_path) as f:
                names_data = json.load(f)
            for item in names_data.get('items', []):
                if item.get('itemKey') == item_key:
                    cat = item.get('category', '')
                    target_mask = _CATEGORY_DEFAULT_MASK.get(cat, '9f280d')
                    for tk, tv in templates.items():
                        if tv.get('mask') == target_mask:
                            return bytearray(bytes.fromhex(tv['hex'])), tv.get('field_positions', {})
                    break
    except Exception:
        pass

    if '751134' in templates:
        t = templates['751134']
        return bytearray(bytes.fromhex(t['hex'])), t.get('field_positions', {})

    return None


def build_item_from_template(
    template_binary: bytearray,
    field_positions: dict,
    item_key: int,
    item_no: int,
    stack_count: int = 1,
    slot_no: int = 200,
    target_abs: int = 0,
    save_type_index: int = -1,
    socket_type_index: int = -1,
) -> bytearray:
    item = bytearray(template_binary)
    fp = field_positions

    if '_saveVersion' in fp:
        struct.pack_into('<I', item, fp['_saveVersion']['rel_offset'], 1)
    if '_itemNo' in fp:
        struct.pack_into('<q', item, fp['_itemNo']['rel_offset'], item_no)
    if '_itemKey' in fp:
        struct.pack_into('<I', item, fp['_itemKey']['rel_offset'], item_key)
    if '_slotNo' in fp:
        struct.pack_into('<H', item, fp['_slotNo']['rel_offset'], slot_no)
    if '_stackCount' in fp:
        struct.pack_into('<q', item, fp['_stackCount']['rel_offset'], stack_count)
    tik_patched = False
    if '_maxSocketCount' in fp:
        msc_off = fp['_maxSocketCount']['rel_offset']
        sock_count = template_binary[msc_off]
        tik_value_pos = msc_off + 1 + 18 + sock_count * 26 + 2
        if tik_value_pos + 4 <= len(item):
            struct.pack_into('<I', item, tik_value_pos, item_key)
            tik_patched = True
            log.info("Patched _transferredItemKey via socket calc at +%d = %d", tik_value_pos, item_key)

    if not tik_patched and '_transferredItemKey' in fp:
        tik_off = fp['_transferredItemKey']['rel_offset']
        struct.pack_into('<I', item, tik_off, item_key)
        tik_patched = True
        log.info("Patched _transferredItemKey via fp offset at +%d = %d", tik_off, item_key)

    if not tik_patched and '_itemKey' in fp:
        orig_ik = struct.unpack_from('<I', template_binary, fp['_itemKey']['rel_offset'])[0]
        if orig_ik:
            ik_bytes = struct.pack('<I', orig_ik)
            search_start = fp['_itemKey']['rel_offset'] + 4
            while True:
                pos = template_binary.find(ik_bytes, search_start)
                if pos < 0: break
                struct.pack_into('<I', item, pos, item_key)
                tik_patched = True
                log.info("Patched _transferredItemKey via byte search at +%d = %d", pos, item_key)
                search_start = pos + 4

    if save_type_index >= 0:
        mbc = struct.unpack_from('<H', item, 0)[0]
        struct.pack_into('<H', item, 2 + mbc, save_type_index)

    sentinel_positions = []
    for i in range(len(item) - 12):
        if item[i:i + 8] == SENTINEL:
            sentinel_positions.append(i)

    if socket_type_index >= 0 and len(sentinel_positions) > 1:
        for sent_pos in sentinel_positions[1:]:
            ti_pos = sent_pos - 3
            if ti_pos >= 0:
                struct.pack_into('<H', item, ti_pos, socket_type_index)
        log.info("Patched %d socket type_indices to %d", len(sentinel_positions) - 1, socket_type_index)

    for sent_pos in sentinel_positions:
        po_pos = sent_pos + 8
        struct.pack_into('<I', item, po_pos, target_abs + po_pos + 4)

    return item


def _infer_field_positions(template: bytearray) -> dict:
    fp = {}
    for i in range(len(template) - 30):
        if struct.unpack_from('<I', template, i)[0] == 1:
            ino = struct.unpack_from('<q', template, i + 4)[0]
            key = struct.unpack_from('<I', template, i + 12)[0]
            if ino >= 1 and key >= 1:
                fp['_saveVersion'] = {'rel_offset': i, 'size': 4}
                fp['_itemNo'] = {'rel_offset': i + 4, 'size': 8}
                fp['_itemKey'] = {'rel_offset': i + 12, 'size': 4}
                fp['_slotNo'] = {'rel_offset': i + 16, 'size': 2}
                fp['_stackCount'] = {'rel_offset': i + 18, 'size': 8}
                fp['_endurance'] = {'rel_offset': i + 26, 'size': 2}
                key_bytes = struct.pack('<I', key)
                for j in range(i + 30, len(template) - 4):
                    if template[j:j + 4] == key_bytes:
                        fp['_transferredItemKey'] = {'rel_offset': j, 'size': 4}
                        break
                break
    return fp


def clone_item_from_save(
    blob: bytes,
    item_element,
    item_key: int,
    item_no: int,
    stack_count: int = 1,
    slot_no: int = 200,
    target_abs: int = 0,
) -> bytearray:
    item = bytearray(blob[item_element.start_offset:item_element.end_offset])
    item_size = len(item)
    po_shift = target_abs - item_element.start_offset

    for i in range(len(item) - 30):
        if struct.unpack_from('<I', item, i)[0] == 1:
            old_no = struct.unpack_from('<q', item, i + 4)[0]
            old_key = struct.unpack_from('<I', item, i + 12)[0]
            if old_no >= 1 and old_key >= 1:
                struct.pack_into('<q', item, i + 4, item_no)
                struct.pack_into('<I', item, i + 12, item_key)
                struct.pack_into('<q', item, i + 18, stack_count)
                new_kb = struct.pack('<I', item_key)
                transferred_key = (new_kb[1] << 24) | (new_kb[0] << 16) | 0x0101
                old_kb = struct.pack('<I', old_key)
                old_transferred = (old_kb[1] << 24) | (old_kb[0] << 16) | 0x0101
                old_tk_bytes = struct.pack('<I', old_transferred)
                new_tk_bytes = struct.pack('<I', transferred_key)
                for j in range(i + 20, len(item) - 4):
                    if item[j:j + 4] == old_tk_bytes:
                        item[j:j + 4] = new_tk_bytes
                        break
                break

    for i in range(len(item) - 12):
        if item[i:i + 8] == SENTINEL:
            po_pos = i + 8
            old_po = struct.unpack_from('<I', item, po_pos)[0]
            struct.pack_into('<I', item, po_pos, old_po + po_shift)

    return item


def _rebuild_element_with_item(
    blob: bytes,
    element,
    item_list_field,
    new_item: bytearray,
    ts_array_field=None,
) -> Tuple[bytearray, int]:
    e_start = element.start_offset
    e_end = element.end_offset
    e_raw = bytearray(blob[e_start:e_end])

    last_item = item_list_field.list_elements[-1]
    r_item_end = last_item.end_offset - e_start

    if ts_array_field:
        r_ts_start = ts_array_field.start_offset - e_start
        r_ts_end = ts_array_field.end_offset - e_start
        ts_data_rel = r_ts_start + 5
        ts_entry = e_raw[ts_data_rel:ts_data_rel + 8]

        ne = bytearray()
        ne += e_raw[:r_item_end]
        ne += new_item
        ne += e_raw[r_item_end:r_ts_end]
        ne += ts_entry
        ne += e_raw[r_ts_end:]

        item_delta = len(new_item)
        ts_delta = len(ts_entry)

        tc_off = r_ts_start + 1 + item_delta
        old_tc = struct.unpack_from('<I', ne, tc_off)[0]
        struct.pack_into('<I', ne, tc_off, old_tc + 1)
    else:
        ne = bytearray()
        ne += e_raw[:r_item_end]
        ne += new_item
        ne += e_raw[r_item_end:]
        item_delta = len(new_item)

    ilc_off = item_list_field.start_offset - e_start + 1
    ne[ilc_off] = ne[ilc_off] + 1

    mbc = struct.unpack_from('<H', ne, 0)[0]
    locator_end = 2 + mbc + 2 + 1 + 8 + 4
    struct.pack_into('<I', ne, len(ne) - 4, (len(ne) - 4) - locator_end)

    growth = len(ne) - len(e_raw)
    return ne, growth


def _is_real_po(sentinel_abs: int, po_value: int) -> bool:
    expected = sentinel_abs + 12
    return po_value == expected


def _fixup_external(
    blob: bytearray,
    orig_blob: bytes,
    parc,
    target_block_toc_idx: int,
    element_end: int,
    growth: int,
) -> int:
    import parc_serializer as ps

    target_block_end = parc.toc_entries[target_block_toc_idx].data_offset + \
                       parc.toc_entries[target_block_toc_idx].data_size
    target_block_abs = parc.toc_entries[target_block_toc_idx].data_offset
    toc_base = parc.toc_offset + 12
    fixed = 0

    for e in parc.toc_entries:
        if e.data_offset < target_block_end:
            continue
        new_block_start = e.data_offset + growth
        blk = parc.block_raw.get(e.index, b'')
        for boff in range(len(blk) - 12):
            if blk[boff:boff + 8] == SENTINEL:
                orig_sentinel_abs = e.data_offset + boff
                orig_po_pos = orig_sentinel_abs + 8
                if orig_po_pos + 4 > len(orig_blob):
                    continue
                orig_po_val = struct.unpack_from('<I', orig_blob, orig_po_pos)[0]
                if not _is_real_po(orig_sentinel_abs, orig_po_val):
                    continue
                pa = new_block_start + boff + 8
                if pa + 4 > len(blob):
                    continue
                ov = struct.unpack_from('<I', blob, pa)[0]
                if ov >= element_end:
                    struct.pack_into('<I', blob, pa, ov + growth)
                    fixed += 1

    target_raw = parc.block_raw[target_block_toc_idx]
    for boff in range(len(target_raw) - 12):
        ap = target_block_abs + boff
        if ap < element_end:
            continue
        if target_raw[boff:boff + 8] == SENTINEL:
            orig_po_val = struct.unpack_from('<I', orig_blob, ap + 8)[0]
            if not _is_real_po(ap, orig_po_val):
                continue
            pp = ap + 8 + growth
            if pp + 4 > len(blob):
                continue
            ov = struct.unpack_from('<I', blob, pp)[0]
            if ov >= element_end:
                struct.pack_into('<I', blob, pp, ov + growth)
                fixed += 1

    for e in parc.toc_entries:
        tp = toc_base + e.index * 20 + 12
        ov = struct.unpack_from('<I', blob, tp)[0]
        if ov >= element_end:
            struct.pack_into('<I', blob, tp, ov + growth)
        if e.index == target_block_toc_idx:
            sp = toc_base + e.index * 20 + 16
            struct.pack_into('<I', blob, sp,
                             struct.unpack_from('<I', blob, sp)[0] + growth)

    ssp = parc.toc_offset + 8
    struct.pack_into('<I', blob, ssp,
                     struct.unpack_from('<I', blob, ssp)[0] + growth)

    return fixed


def _fixup_trailing_sizes(
    blob: bytearray,
    orig_blob: bytes,
    insert_pos: int,
    growth: int,
    block_class_name: str,
) -> int:
    import save_parser as sp

    result = sp.build_result_from_raw(orig_blob, {'input_kind': 'raw_blob'})
    ts_positions = []

    def _find_containing(field, depth=0):
        s = field.start_offset or 0
        e = field.end_offset or 0
        dk = getattr(field, 'decode_kind', '') or ''
        is_inline_object = (dk == 'list_element' or 'locator' in dk)
        if is_inline_object and s and e and e - s > 4:
            ts_pos = e - 4
            if s < insert_pos <= ts_pos and ts_pos + 4 <= len(orig_blob):
                ts_val = struct.unpack_from('<I', orig_blob, ts_pos)[0]
                field_size = e - s
                if 0 < ts_val < field_size:
                    if ts_pos not in ts_positions:
                        ts_positions.append(ts_pos)
        for cf in (field.child_fields or []):
            _find_containing(cf, depth + 1)
        for el in (field.list_elements or []):
            _find_containing(el, depth + 1)

    for obj in result['objects']:
        if obj.class_name == block_class_name:
            for f in obj.fields:
                _find_containing(f)
            break

    fixed = 0
    for orig_ts_pos in ts_positions:
        if orig_ts_pos >= insert_pos:
            new_ts_pos = orig_ts_pos + growth
        else:
            new_ts_pos = orig_ts_pos
        if new_ts_pos + 4 > len(blob):
            continue
        old_val = struct.unpack_from('<I', blob, new_ts_pos)[0]
        struct.pack_into('<I', blob, new_ts_pos, old_val + growth)
        fixed += 1

    return fixed


def verify_runtime(
    blob: bytes,
    insert_abs: int,
    item_size: int,
    new_item: bytes,
    list_count_pos: int,
    expected_count: int,
    toc_offset: int,
    target_toc_idx: int,
    expected_block_size: int,
) -> Tuple[bool, str]:
    issues = []

    actual_item = blob[insert_abs:insert_abs + item_size]
    if actual_item != new_item:
        for i in range(min(len(actual_item), len(new_item))):
            if actual_item[i] != new_item[i]:
                issues.append(f"Item byte mismatch at insert+{i}: expected 0x{new_item[i]:02X} got 0x{actual_item[i]:02X}")
                break

    actual_count = blob[list_count_pos] | (blob[list_count_pos+1] << 8) | (blob[list_count_pos+2] << 16)
    if actual_count != expected_count:
        issues.append(f"List count at 0x{list_count_pos:X}: expected {expected_count}, got {actual_count}")

    toc_base = toc_offset + 12
    size_pos = toc_base + target_toc_idx * 20 + 16
    actual_size = struct.unpack_from('<I', blob, size_pos)[0]
    if actual_size != expected_block_size:
        issues.append(f"TOC block size: expected {expected_block_size}, got {actual_size}")

    if blob[0:2] != b'\xff\xff':
        issues.append("PARC header magic missing")

    if issues:
        return False, "; ".join(issues)
    return True, "Runtime verification passed"


def verify_tree(orig_blob: bytes, mod_blob: bytes,
                expected_list_field: str = '_itemList') -> Tuple[bool, str]:
    try:
        orig_result = _parse_tree(orig_blob)
        mod_result = _parse_tree(mod_blob)
    except Exception as e:
        return False, f"Tree parse failed: {e}"

    orig_objs = orig_result['objects']
    mod_objs = mod_result['objects']

    if len(orig_objs) != len(mod_objs):
        return False, f"Block count mismatch: {len(orig_objs)} vs {len(mod_objs)}"

    issues = []
    for i, (oo, vo) in enumerate(zip(orig_objs, mod_objs)):
        if oo.class_name != vo.class_name:
            issues.append(f"[{i}] type: {oo.class_name} -> {vo.class_name}")
            continue
        for j, (of, vf) in enumerate(zip(oo.fields, vo.fields)):
            if of.present != vf.present:
                issues.append(f"[{i}].{of.name}: present {of.present}->{vf.present}")
            ole = len(of.list_elements or [])
            vle = len(vf.list_elements or [])
            if ole != vle:
                if of.name == expected_list_field and vle > ole:
                    pass
                elif of.name == '_storeSoldItemDataList' and vle == ole + 1:
                    pass
                elif of.name == '_itemSoldFieldTimeRawList' and vle > ole:
                    pass
                else:
                    issues.append(f"[{i}].{of.name}: {ole} -> {vle} elements")

    if issues:
        return False, f"{len(issues)} issues:\n" + "\n".join(issues[:10])
    return True, "All verifications passed"


def insert_items_batch(
    blob: bytearray,
    items: List[Tuple[int, int, int]],
    bag_key: int = 2,
    strict_bag: bool = False,
) -> Tuple[bool, bytearray, str]:
    if not items:
        return False, blob, "No items to insert"

    current_blob = bytearray(blob)
    orig_blob = bytes(blob)
    inserted = 0

    for item_key, stack_count, slot_no in items:
        ok, new_blob, msg = insert_item_to_inventory(
            current_blob, item_key=item_key, stack_count=stack_count,
            slot_no=slot_no, bag_key=bag_key, skip_verify=True,
            strict_bag=strict_bag,
        )
        if not ok:
            if inserted == 0:
                return False, blob, f"First item failed: {msg}"
            break
        current_blob = bytearray(new_blob)
        inserted += 1

    ok, msg = verify_tree(orig_blob, bytes(current_blob))
    if not ok:
        return False, blob, f"Batch verification failed after {inserted} items: {msg}"

    return True, bytes(current_blob), f"Inserted {inserted}/{len(items)} items. {msg}"


def insert_item_to_inventory(
    blob: bytearray,
    item_key: int,
    stack_count: int = 1,
    slot_no: int = 200,
    bag_key: int = -1,
    template_key: int = -1,
    skip_verify: bool = False,
    strict_bag: bool = False,
) -> Tuple[bool, bytearray, str]:
    _ensure_desktop_path()
    from save_parser import build_result_from_raw as _brfr
    import parc_serializer as _ps

    if bag_key < 0:
        bag_key = _get_item_bag_key(item_key)
        log.info("Auto-detected bag_key=%d for item_key=%d", bag_key, item_key)

    orig_blob = bytes(blob)
    result = _parse_tree(orig_blob)

    inv_obj = _find_block(result, 'InventorySaveData')
    if not inv_obj:
        return False, blob, "InventorySaveData not found"

    inv_list = _find_field(inv_obj, '_inventorylist')
    if not inv_list or not inv_list.list_elements:
        return False, blob, "No inventory bags found"

    target_bag = target_items = None
    for bag in inv_list.list_elements:
        inv_key_field = _find_field(bag, '_inventoryKey')
        if inv_key_field and int(inv_key_field.value_repr or 0) == bag_key:
            items_field = _find_field(bag, '_itemList')
            if items_field and items_field.list_elements:
                target_bag = bag
                target_items = items_field
                break

    if not target_bag and not strict_bag:
        for bag in inv_list.list_elements:
            inv_key_field = _find_field(bag, '_inventoryKey')
            if inv_key_field and int(inv_key_field.value_repr or 0) == 2:
                items_field = _find_field(bag, '_itemList')
                if items_field and items_field.list_elements:
                    target_bag = bag
                    target_items = items_field
                    log.info("Bag key=%d empty, falling back to bag 2", bag_key)
                    break

    if not target_bag:
        return False, blob, "No inventory bag with items found"

    max_no = _get_max_item_no(result)
    new_no = max_no + 1

    used_slots = set()
    if target_items and target_items.list_elements:
        for elem in target_items.list_elements:
            for ef in (elem.child_fields or []):
                if ef.name == '_slotNo' and ef.present:
                    used_slots.add(struct.unpack_from('<H', orig_blob, ef.start_offset)[0])
    max_slot = max(used_slots) if used_slots else -1
    slot_no = max_slot + 1
    log.info("Bag max slot: %d, assigning slot: %d, itemNo: %d", max_slot, slot_no, new_no)

    tk = template_key if template_key > 0 else item_key
    tmpl = load_item_template(tk)
    if not tmpl:
        if target_items.list_elements:
            last_item_elem = target_items.list_elements[-1]
            tmpl_binary = bytearray(orig_blob[last_item_elem.start_offset:last_item_elem.end_offset])
            tmpl_fp = _infer_field_positions(tmpl_binary)
            if tmpl_fp:
                tmpl = (tmpl_binary, tmpl_fp)
                log.info("Cloned template from bag item (%dB)", len(tmpl_binary))
    if not tmpl:
        tmpl = load_item_template(751134)
    if not tmpl:
        return False, blob, "No template available"

    template_binary, field_positions = tmpl

    schema = _parse_schema(orig_blob)
    save_type_idx = -1
    socket_type_idx = -1
    for idx, td in schema.items():
        if td.name == 'ItemSaveData':
            save_type_idx = idx
        elif td.name == 'ItemSocketSaveData':
            socket_type_idx = idx
    log.info("Schema: ItemSaveData=%d, ItemSocketSaveData=%d", save_type_idx, socket_type_idx)

    last_item = target_items.list_elements[-1]
    insert_pos = last_item.end_offset

    new_item = build_item_from_template(
        template_binary, field_positions,
        item_key=item_key, item_no=new_no,
        stack_count=stack_count, slot_no=slot_no,
        target_abs=insert_pos, save_type_index=save_type_idx,
        socket_type_index=socket_type_idx,
    )
    growth = len(new_item)
    log.info("Inserting %dB item at 0x%X (after last item)", growth, insert_pos)

    blob = bytearray(orig_blob)
    blob[insert_pos:insert_pos] = new_item

    list_start = target_items.start_offset
    list_header_size = getattr(target_items, 'list_header_size', 18)
    prefix = target_items.list_prefix_u8 if hasattr(target_items, 'list_prefix_u8') else 0
    old_count = len(target_items.list_elements)
    new_count = old_count + 1

    if prefix == 1:
        struct.pack_into('>H', blob, list_start + 1, new_count)
    elif prefix == 0:
        orig_byte1 = orig_blob[list_start + 1]
        orig_byte2 = orig_blob[list_start + 2]
        if orig_byte1 == 0 and orig_byte2 == 0:
            struct.pack_into('<I', blob, list_start + 4, new_count)
        else:
            blob[list_start + 1] = new_count & 0xFF
            blob[list_start + 2] = (new_count >> 8) & 0xFF
            blob[list_start + 3] = (new_count >> 16) & 0xFF
    log.info("List count: %d -> %d (prefix=%d at 0x%X)", old_count, new_count, prefix, list_start)

    from parc_inserter2 import parse_and_collect
    _, offset_positions, trailing_sizes_tree = parse_and_collect(orig_blob)

    fixed_po = 0
    blob_len = len(blob)
    for po_pos, po_val in offset_positions:
        if po_val < insert_pos:
            continue

        if po_pos >= insert_pos:
            new_po_pos = po_pos + growth
        else:
            new_po_pos = po_pos

        if new_po_pos + 4 > blob_len:
            continue
        cur_val = struct.unpack_from('<I', blob, new_po_pos)[0]
        struct.pack_into('<I', blob, new_po_pos, cur_val + growth)
        fixed_po += 1

    log.info("Fixed %d POs via validated sentinel scan", fixed_po)

    parc = _ps.parse_parc_blob(orig_blob)
    inv_toc_idx = None
    for e in parc.toc_entries:
        if e.class_index < len(parc.types) and parc.types[e.class_index].name == 'InventorySaveData':
            inv_toc_idx = e.index
            break

    if inv_toc_idx is None:
        return False, blob, "InventorySaveData not found in TOC"

    toc_base = parc.toc_offset + 12
    fixed_toc = 0
    for e in parc.toc_entries:
        tp = toc_base + e.index * 20 + 12
        ov = struct.unpack_from('<I', blob, tp)[0]
        if ov >= insert_pos:
            struct.pack_into('<I', blob, tp, ov + growth)
            fixed_toc += 1
        if e.index == inv_toc_idx:
            sp = toc_base + e.index * 20 + 16
            struct.pack_into('<I', blob, sp,
                             struct.unpack_from('<I', blob, sp)[0] + growth)

    ssp = parc.toc_offset + 8
    struct.pack_into('<I', blob, ssp,
                     struct.unpack_from('<I', blob, ssp)[0] + growth)

    log.info("Fixed %d TOC entries", fixed_toc)

    fixed_ts = _fixup_trailing_sizes(blob, orig_blob, insert_pos, growth, 'InventorySaveData')
    log.info("Fixed %d trailing sizes", fixed_ts)

    check_len = min(len(orig_blob) - insert_pos, len(blob) - insert_pos - growth)
    alignment_errors = 0
    if check_len > 0:
        orig_tail = orig_blob[insert_pos:]
        new_tail = bytes(blob[insert_pos + growth:insert_pos + growth + check_len])
        for i in range(check_len):
            if orig_tail[i] != new_tail[i]:
                alignment_errors += 1

    expected_changed = (fixed_po * 4) + (fixed_toc * 4) + (fixed_ts * 4) + 4 + 4
    log.info("Alignment: %d bytes differ, %d expected from %d PO + %d TOC + %d TS fixes",
             alignment_errors, expected_changed, fixed_po, fixed_toc, fixed_ts)

    if alignment_errors > expected_changed + 16:
        return False, blob, (
            f"CORRUPTION DETECTED: {alignment_errors} bytes differ but only "
            f"{expected_changed} expected from {fixed_po} PO + {fixed_toc} TOC + "
            f"{fixed_ts} trailing size fixes. Aborting."
        )

    if not skip_verify:
        try:
            verify_result = _brfr(bytes(blob), {'input_kind': 'raw_blob'})
            obj_count = len(verify_result['objects'])

            found_item = False
            for vobj in verify_result['objects']:
                if vobj.class_name != 'InventorySaveData':
                    continue
                for vf in vobj.fields:
                    if vf.name != '_inventorylist' or not vf.list_elements:
                        continue
                    for vbag in vf.list_elements:
                        verified_bag_key = None
                        for vcf in (vbag.child_fields or []):
                            if vcf.name == '_inventoryKey' and vcf.present:
                                verified_bag_key = struct.unpack_from('<H', blob, vcf.start_offset)[0]
                                break
                        if verified_bag_key != bag_key:
                            continue
                        for vcf in (vbag.child_fields or []):
                            if vcf.name != '_itemList' or not vcf.list_elements:
                                continue
                            for vitem in vcf.list_elements:
                                for vicf in (vitem.child_fields or []):
                                    if vicf.name == '_itemKey' and vicf.present:
                                        vk = struct.unpack_from('<I', blob, vicf.start_offset)[0]
                                        if vk == item_key:
                                            found_item = True
            if not found_item:
                return False, blob, (
                    f"Tree verification: item {item_key} not found in storage key {bag_key} after insertion. "
                    f"Fixed {fixed_po} POs + {fixed_toc} TOC + {fixed_ts} trailing sizes."
                )
        except Exception as e:
            return False, blob, f"Tree verification error: {e}"

        msg = (
            f"Item {item_key} x{stack_count} inserted (no={new_no}, slot={slot_no}).\n"
            f"Fixed {fixed_po} POs + {fixed_toc} TOC + {fixed_ts} trailing sizes.\n"
            f"Alignment: {alignment_errors} bytes changed ({expected_changed} expected).\n"
            f"Tree re-parse OK ({obj_count} objects)."
        )
        return True, bytes(blob), msg

    return True, bytes(blob), (
        f"Item {item_key} x{stack_count} inserted (no={new_no}, slot={slot_no}). "
        f"{fixed_po} POs + {fixed_toc} TOC + {fixed_ts} TS fixed."
    )


def insert_item_to_store(
    blob: bytearray,
    item_key: int,
    stack_count: int = 1,
    store_index: int = -1,
    template_key: int = -1,
) -> Tuple[bool, bytearray, str]:
    orig_blob = bytes(blob)
    result = _parse_tree(orig_blob)

    store_obj = _find_block(result, 'StoreSaveData')
    if not store_obj:
        return False, blob, "StoreSaveData not found"

    store_list = _find_field(store_obj, '_storeDataList')
    if not store_list or not store_list.list_elements:
        return False, blob, "No stores found"

    target_store = sold_list = ts_array = None
    for i, store in enumerate(store_list.list_elements):
        if store_index >= 0 and i != store_index:
            continue
        sl = _find_field(store, '_storeSoldItemDataList')
        ts = _find_field(store, '_itemSoldFieldTimeRawList')
        if sl and sl.list_elements and len(sl.list_elements) > 0:
            target_store = store
            sold_list = sl
            ts_array = ts
            store_key_f = _find_field(store, '_storeKey')
            log.info("Target store[%d] key=%s, %d sold items",
                     i, store_key_f.value_repr if store_key_f else '?', len(sl.list_elements))
            break

    if not target_store:
        return False, blob, "No store with sold items found. Sell an item to a vendor first."

    store_max = 0
    for elem in sold_list.list_elements:
        for ef in (elem.child_fields or []):
            if ef.name == '_itemNo' and ef.present:
                v = struct.unpack_from('<q', orig_blob, ef.start_offset)[0]
                if 0 < v < 999999 and v > store_max:
                    store_max = v
    new_no = store_max + 1
    log.info("Store max itemNo: %d, assigning: %d", store_max, new_no)

    last_sold = sold_list.list_elements[-1]
    target_abs = last_sold.end_offset

    new_item = None
    tk = template_key if template_key > 0 else item_key
    tmpl = load_item_template(tk)
    if tmpl:
        template_binary, field_positions = tmpl
        schema = result.get('schema', {})
        save_type_idx = -1
        socket_type_idx = -1
        if isinstance(schema, dict):
            for idx, td in schema.items():
                if hasattr(td, 'name') and td.name == 'ItemSaveData':
                    save_type_idx = idx
                elif hasattr(td, 'name') and td.name == 'ItemSocketSaveData':
                    socket_type_idx = idx
        new_item = build_item_from_template(
            template_binary, field_positions,
            item_key=item_key, item_no=new_no,
            stack_count=stack_count, slot_no=0,
            target_abs=target_abs, save_type_index=save_type_idx,
            socket_type_index=socket_type_idx,
        )
        log.info("Using master template for store item %d (%dB)", item_key, len(new_item))

    if new_item is None:
        new_item = clone_item_from_save(
            orig_blob, last_sold,
            item_key=item_key, item_no=new_no,
            stack_count=stack_count, slot_no=0,
            target_abs=target_abs,
        )
        log.info("Using clone from existing sold item for store item %d", item_key)

    new_element, growth = _rebuild_element_with_item(
        orig_blob, target_store, sold_list, new_item, ts_array,
    )

    e_start = target_store.start_offset
    e_end = target_store.end_offset
    blob[e_start:e_end] = new_element

    import parc_serializer as ps
    parc = ps.parse_parc_blob(orig_blob)
    store_toc = None
    for e in parc.toc_entries:
        if e.class_index < len(parc.types) and parc.types[e.class_index].name == 'StoreSaveData':
            store_toc = e.index
            break

    fixed = _fixup_external(blob, orig_blob, parc, store_toc, e_end, growth)
    _fixup_trailing_sizes(blob, orig_blob, e_end, growth, 'StoreSaveData')

    ok, msg = verify_tree(orig_blob, bytes(blob), '_storeSoldItemDataList')
    if not ok:
        return False, blob, f"Verification failed: {msg}"

    return True, bytes(blob), f"Inserted item key={item_key} x{stack_count} to store. Fixed {fixed} external POs. {msg}"


def clone_block_section(
    target_blob: bytearray,
    source_blob: bytes,
    block_class_name: str,
) -> Tuple[bool, bytearray, str]:
    import parc_serializer as ps

    orig_target = bytes(target_blob)
    src_parc = ps.parse_parc_blob(source_blob)
    tgt_parc = ps.parse_parc_blob(orig_target)

    src_entry = tgt_entry = None
    for e in src_parc.toc_entries:
        if e.class_index < len(src_parc.types) and src_parc.types[e.class_index].name == block_class_name:
            src_entry = e
            break
    for e in tgt_parc.toc_entries:
        if e.class_index < len(tgt_parc.types) and tgt_parc.types[e.class_index].name == block_class_name:
            tgt_entry = e
            break

    if not src_entry:
        return False, target_blob, f"Source has no {block_class_name}"
    if not tgt_entry:
        return False, target_blob, f"Target has no {block_class_name}"

    src_raw = src_parc.block_raw[src_entry.index]
    tgt_raw = tgt_parc.block_raw[tgt_entry.index]
    size_diff = len(src_raw) - len(tgt_raw)

    new_block = bytearray(src_raw)
    offset_delta = tgt_entry.data_offset - src_entry.data_offset

    src_result = _parse_tree(source_blob)
    src_po_positions = []

    def _collect_block_pos(field, src_blob_data, positions, block_start, block_end):
        if field.present and field.child_payload_offset > 0:
            if block_start <= field.start_offset < block_end:
                if field.note and 'compact' in str(field.note):
                    pp = field.start_offset + 14
                else:
                    pp = field.start_offset + 2 + field.child_mask_byte_count + 11
                if pp + 4 <= len(src_blob_data):
                    actual = struct.unpack_from('<I', src_blob_data, pp)[0]
                    if actual == field.child_payload_offset:
                        positions.append(pp - block_start)
        for cf in (field.child_fields or []):
            _collect_block_pos(cf, src_blob_data, positions, block_start, block_end)
        for el in (field.list_elements or []):
            _collect_block_pos(el, src_blob_data, positions, block_start, block_end)

    src_block_start = src_entry.data_offset
    src_block_end = src_block_start + len(src_raw)
    for obj in src_result['objects']:
        if obj.class_name == block_class_name:
            for f in obj.fields:
                _collect_block_pos(f, source_blob, src_po_positions, src_block_start, src_block_end)
            break

    sentinel_pos = []
    for off in range(len(new_block) - 12):
        if new_block[off:off + 8] == SENTINEL:
            po_off = off + 8
            po_val = struct.unpack_from('<I', new_block, po_off)[0]
            if src_block_start <= po_val < src_block_end:
                if po_off not in src_po_positions:
                    sentinel_pos.append(po_off)
    all_positions = sorted(set(src_po_positions + sentinel_pos))
    log.info("Block clone: %d tree POs + %d sentinel POs = %d total",
             len(src_po_positions), len(sentinel_pos), len(all_positions))

    for po_off in all_positions:
        po_val = struct.unpack_from('<I', new_block, po_off)[0]
        new_po = po_val + offset_delta
        if 0 < new_po <= 0xFFFFFFFF:
            struct.pack_into('<I', new_block, po_off, new_po)

    tgt_start = tgt_entry.data_offset
    tgt_end = tgt_start + len(tgt_raw)
    target_blob[tgt_start:tgt_end] = new_block

    import parc_serializer as _ps2
    toc_base = tgt_parc.toc_offset + 12
    fixed = 0

    for e in tgt_parc.toc_entries:
        if e.data_offset < tgt_end:
            continue
        nbs = e.data_offset + size_diff
        blk = tgt_parc.block_raw.get(e.index, b'')
        for boff in range(len(blk) - 12):
            if blk[boff:boff + 8] == SENTINEL:
                pa = nbs + boff + 8
                if pa + 4 > len(target_blob):
                    continue
                ov = struct.unpack_from('<I', target_blob, pa)[0]
                if ov >= tgt_end and ov + size_diff <= 0xFFFFFFFF:
                    struct.pack_into('<I', target_blob, pa, ov + size_diff)
                    fixed += 1

    for e in tgt_parc.toc_entries:
        tp = toc_base + e.index * 20 + 12
        ov = struct.unpack_from('<I', target_blob, tp)[0]
        if ov >= tgt_end:
            struct.pack_into('<I', target_blob, tp, ov + size_diff)
        if e.index == tgt_entry.index:
            sp = toc_base + e.index * 20 + 16
            struct.pack_into('<I', target_blob, sp, len(src_raw))
    ssp = tgt_parc.toc_offset + 8
    struct.pack_into('<I', target_blob, ssp,
                     struct.unpack_from('<I', target_blob, ssp)[0] + size_diff)

    try:
        _parse_tree(bytes(target_blob))
        parse_ok = True
    except Exception as vex:
        parse_ok = False
        return False, target_blob, f"Cloned blob fails to parse: {vex}"

    return True, bytes(target_blob), (
        f"Cloned {block_class_name}: {len(tgt_raw)}B -> {len(src_raw)}B. "
        f"Fixed {fixed} external POs. Tree parse OK."
    )


def load_waypoint_templates() -> List[Dict[str, Any]]:
    for base in [_MY_DIR, getattr(sys, '_MEIPASS', _MY_DIR)]:
        p = os.path.join(base, 'waypoint_templates_community.json')
        if os.path.isfile(p):
            with open(p, 'r') as f:
                data = json.load(f)
            return data.get('entries', [])
    return []


def _get_user_waypoint_uuids(result: dict) -> set:
    uuids = set()
    obj = _find_block(result, 'DiscoveredLevelGimmickSceneObjectSaveData')
    if not obj:
        return uuids
    field = _find_field(obj, '_discoveredLevelGimmickSceneObjectSaveDataList')
    if not field or not field.list_elements:
        return uuids
    for elem in field.list_elements:
        for cf in (elem.child_fields or []):
            if cf.name == '_sceneObjectUuid' and cf.present:
                uuids.add(cf.start_offset)
    return uuids


def insert_waypoints(
    blob: bytearray,
    waypoint_entries: List[Dict[str, Any]],
    skip_verify: bool = False,
) -> Tuple[bool, bytearray, str]:
    if not waypoint_entries:
        return False, blob, "No waypoint entries to insert"

    orig_blob = bytes(blob)
    result = _parse_tree(orig_blob)

    obj = _find_block(result, 'DiscoveredLevelGimmickSceneObjectSaveData')
    if not obj:
        return False, blob, "DiscoveredLevelGimmickSceneObjectSaveData not found"

    field = _find_field(obj, '_discoveredLevelGimmickSceneObjectSaveDataList')
    if not field:
        return False, blob, "_discoveredLevelGimmickSceneObjectSaveDataList not found"

    if not field.list_elements:
        return False, blob, "Waypoint list is empty — need at least one existing entry"

    existing_uuids = set()
    for elem in field.list_elements:
        for cf in (elem.child_fields or []):
            if cf.name == '_sceneObjectUuid' and cf.present:
                uuid_hex = orig_blob[cf.start_offset:cf.start_offset + 16].hex()
                existing_uuids.add(uuid_hex)

    to_insert = []
    for wp in waypoint_entries:
        if wp.get('uuid', '') not in existing_uuids:
            raw = bytes.fromhex(wp['binary'])
            to_insert.append(raw)

    if not to_insert:
        return True, blob, "All waypoints already present — nothing to insert"

    log.info("Inserting %d waypoints (%d already present)",
             len(to_insert), len(existing_uuids))

    last_elem = field.list_elements[-1]
    insert_abs = last_elem.end_offset

    schema = _parse_schema(orig_blob)
    wp_type_name = 'LevelGimmickSceneObjectElementSaveData'
    wp_type_idx = -1
    for idx, td in schema.items():
        if td.name == wp_type_name:
            wp_type_idx = idx
            break

    all_new_bytes = bytearray()
    cursor = insert_abs
    for raw_entry in to_insert:
        entry = bytearray(raw_entry)
        mbc_val = struct.unpack_from('<H', entry, 0)[0]
        mask_len = mbc_val & 0xFF
        locator_end = 2 + mask_len + 2 + 1 + 8 + 4
        if wp_type_idx >= 0:
            struct.pack_into('<H', entry, 2 + mask_len, wp_type_idx)
        po_abs = cursor + locator_end
        struct.pack_into('<I', entry, locator_end - 4, po_abs)
        all_new_bytes += entry
        cursor += len(entry)

    total_growth = len(all_new_bytes)

    block_start = obj.data_offset
    block_end = block_start + obj.data_size
    r_insert = insert_abs - block_start

    block_raw = bytearray(orig_blob[block_start:block_end])
    new_block = bytearray()
    new_block += block_raw[:r_insert]
    new_block += all_new_bytes
    new_block += block_raw[r_insert:]

    list_rel = field.start_offset - block_start
    orig_count = len(field.list_elements)
    new_count = orig_count + len(to_insert)
    new_block[list_rel + 1] = new_count & 0xFF
    new_block[list_rel + 2] = (new_count >> 8) & 0xFF
    new_block[list_rel + 3] = (new_count >> 16) & 0xFF
    log.info("List count: %d -> %d", orig_count, new_count)


    blob[block_start:block_end] = new_block

    import parc_serializer as ps
    parc = ps.parse_parc_blob(orig_blob)
    gimmick_toc_idx = None
    for e in parc.toc_entries:
        if e.class_index < len(parc.types) and parc.types[e.class_index].name == 'DiscoveredLevelGimmickSceneObjectSaveData':
            gimmick_toc_idx = e.index
            break

    if gimmick_toc_idx is None:
        return False, blob, "Could not find gimmick block in TOC"

    fixed = _fixup_external(blob, orig_blob, parc, gimmick_toc_idx, block_end, total_growth)
    _fixup_trailing_sizes(blob, orig_blob, block_end, total_growth, 'DiscoveredLevelGimmickSceneObjectSaveData')

    if not skip_verify:
        ok, msg = verify_tree(orig_blob, bytes(blob),
                              expected_list_field='_discoveredLevelGimmickSceneObjectSaveDataList')
        if not ok:
            return False, blob, f"Verification failed: {msg}"
        return True, bytes(blob), (
            f"Inserted {len(to_insert)} waypoints ({len(existing_uuids)} already present). "
            f"Fixed {fixed} external POs. {msg}"
        )

    return True, bytes(blob), (
        f"Inserted {len(to_insert)} waypoints. {fixed} POs fixed."
    )


def _load_community_knowledge_keys() -> List[Dict[str, Any]]:
    for base in [_MY_DIR, getattr(sys, '_MEIPASS', _MY_DIR)]:
        p = os.path.join(base, 'community_knowledge_keys.json')
        if os.path.isfile(p):
            with open(p, 'r') as f:
                return json.load(f)
    return []


def insert_quest_completed(
    blob: bytearray,
    quest_key: int,
) -> Tuple[bool, bytearray, str]:
    _ensure_desktop_path()
    from save_parser import build_result_from_raw as _brfr

    orig_blob = bytes(blob)
    result = _brfr(orig_blob, {'input_kind': 'raw_blob'})

    quest_obj = quest_field = None
    for obj in result['objects']:
        if obj.class_name == 'QuestSaveData':
            for f in obj.fields:
                if f.name == '_questStateList' and f.list_elements:
                    quest_obj = obj
                    quest_field = f
                    break
            break

    if not quest_obj or not quest_field:
        return False, blob, "QuestSaveData._questStateList not found"

    for elem in quest_field.list_elements:
        for cf in (elem.child_fields or []):
            if cf.name == '_questKey' and cf.present:
                if struct.unpack_from('<I', orig_blob, cf.start_offset)[0] == quest_key:
                    return False, blob, f"Quest {quest_key} already exists in save"

    template_elem = None
    for elem in quest_field.list_elements:
        mask = elem.child_mask_bytes.hex() if elem.child_mask_bytes else ''
        if mask == '39':
            template_elem = elem
            break

    if not template_elem:
        template_elem = quest_field.list_elements[-1]

    tmpl_raw = orig_blob[template_elem.start_offset:template_elem.end_offset]
    tmpl_start = template_elem.start_offset

    key_rel = state_rel = None
    for cf in (template_elem.child_fields or []):
        if cf.name == '_questKey' and cf.present:
            key_rel = cf.start_offset - tmpl_start
        elif cf.name == '_state' and cf.present:
            state_rel = cf.start_offset - tmpl_start

    if key_rel is None:
        return False, blob, "Could not find _questKey field"

    clone = bytearray(tmpl_raw)
    struct.pack_into('<I', clone, key_rel, quest_key)
    if state_rel is not None:
        struct.pack_into('<I', clone, state_rel, 0x1905)

    mbc = struct.unpack_from('<H', clone, 0)[0]
    mask_len = mbc & 0xFF
    locator_end = 2 + mask_len + 2 + 1 + 8 + 4

    last_elem = quest_field.list_elements[-1]
    insert_abs = last_elem.end_offset

    orig_main_po = struct.unpack_from('<I', tmpl_raw, locator_end - 4)[0]
    new_main_po = insert_abs + locator_end
    po_delta = new_main_po - orig_main_po

    for boff in range(0, len(clone) - 12):
        if clone[boff:boff+8] == SENTINEL:
            po_off = boff + 8
            if po_off + 4 <= len(clone):
                old_po = struct.unpack_from('<I', tmpl_raw, po_off)[0]
                if old_po >= tmpl_start and old_po < tmpl_start + len(tmpl_raw) + 4096:
                    struct.pack_into('<I', clone, po_off, old_po + po_delta)

    growth = len(clone)

    block_start = quest_obj.data_offset
    block_end = block_start + quest_obj.data_size
    r_insert = insert_abs - block_start

    block_raw = bytearray(orig_blob[block_start:block_end])
    new_block = block_raw[:r_insert] + clone + block_raw[r_insert:]

    list_rel = quest_field.start_offset - block_start
    orig_count = len(quest_field.list_elements)
    new_block[list_rel + 1] = (orig_count + 1) & 0xFF
    new_block[list_rel + 2] = ((orig_count + 1) >> 8) & 0xFF
    new_block[list_rel + 3] = ((orig_count + 1) >> 16) & 0xFF

    for boff in range(r_insert + growth, len(new_block) - 12):
        if new_block[boff:boff+8] == SENTINEL:
            po_off = boff + 8
            if po_off + 4 <= len(new_block):
                ov = struct.unpack_from('<I', new_block, po_off)[0]
                if ov >= insert_abs and ov < insert_abs + 10000000:
                    struct.pack_into('<I', new_block, po_off, ov + growth)

    blob[block_start:block_end] = new_block

    import parc_serializer as _ps
    parc = _ps.parse_parc_blob(orig_blob)
    toc_idx = None
    for e in parc.toc_entries:
        if e.class_index < len(parc.types) and parc.types[e.class_index].name == 'QuestSaveData':
            toc_idx = e.index
            break

    if toc_idx is None:
        return False, blob, "QuestSaveData not found in TOC"

    fixed = _fixup_external(blob, orig_blob, parc, toc_idx, block_end, growth)
    _fixup_trailing_sizes(blob, orig_blob, block_end, growth, 'QuestSaveData')

    return True, bytes(blob), (
        f"Inserted quest {quest_key} as completed. Fixed {fixed} external POs."
    )


def complete_mission_simple(
    blob: bytearray,
    mission_key: int,
) -> Tuple[bool, bytearray, str]:
    _ensure_desktop_path()
    from save_parser import build_result_from_raw as _brfr

    orig_blob = bytes(blob)
    result = _brfr(orig_blob, {'input_kind': 'raw_blob'})

    for obj in result['objects']:
        if obj.class_name != 'QuestSaveData':
            continue
        for f in obj.fields:
            if f.name != '_missionStateList' or not f.list_elements:
                continue
            for elem in f.list_elements:
                for cf in (elem.child_fields or []):
                    if cf.name == '_key' and cf.present:
                        key_val = struct.unpack_from('<I', orig_blob, cf.start_offset)[0]
                        if key_val == (mission_key & 0xFFFFFFFF):
                            for cf2 in (elem.child_fields or []):
                                if cf2.name == '_state' and cf2.present:
                                    new_blob = bytearray(orig_blob)
                                    new_blob[cf2.start_offset] = 5
                                    return True, bytes(new_blob), (
                                        f"Mission {mission_key} state set to 5 (completed). "
                                        f"One byte changed at offset 0x{cf2.start_offset:X}."
                                    )
                            return False, blob, f"Mission {mission_key} found but _state field missing"
            break
        break

    return False, blob, f"Mission {mission_key} not found in _missionStateList"


def complete_mission_entry(
    blob: bytearray,
    mission_key: int,
    game_time: int = 0,
) -> Tuple[bool, bytearray, str]:
    _ensure_desktop_path()
    from save_parser import build_result_from_raw as _brfr
    import parc_serializer as _ps

    orig_blob = bytes(blob)
    result = _brfr(orig_blob, {'input_kind': 'raw_blob'})

    if game_time <= 0:
        max_ct = 0
        for obj in result['objects']:
            if obj.class_name != 'QuestSaveData':
                continue
            for f in obj.fields:
                if f.name not in ('_questStateList', '_missionStateList'):
                    continue
                if not f.list_elements:
                    continue
                for elem in f.list_elements:
                    for cf in (elem.child_fields or []):
                        if cf.name == '_completedTime' and cf.present:
                            ct = struct.unpack_from('<Q', orig_blob, cf.start_offset)[0]
                            if ct > max_ct:
                                max_ct = ct
        game_time = max_ct + 100 if max_ct > 0 else 10000000
        log.info("Auto-detected game_time=%d (max existing + 100)", game_time)

    quest_obj = mission_field = target_elem = None
    for obj in result['objects']:
        if obj.class_name != 'QuestSaveData':
            continue
        quest_obj = obj
        for f in obj.fields:
            if f.name != '_missionStateList' or not f.list_elements:
                continue
            mission_field = f
            for elem in f.list_elements:
                for cf in (elem.child_fields or []):
                    if cf.name == '_key' and cf.present:
                        if struct.unpack_from('<I', orig_blob, cf.start_offset)[0] == (mission_key & 0xFFFFFFFF):
                            target_elem = elem
                            break
                if target_elem:
                    break
            break
        break

    if not quest_obj:
        return False, blob, "QuestSaveData not found"
    if not target_elem:
        return False, blob, f"Mission {mission_key} not found in _missionStateList"

    for cf in (target_elem.child_fields or []):
        if cf.name == '_completedTime' and cf.present:
            return False, blob, f"Mission {mission_key} already has _completedTime"

    state_off = uistate_end = mask_abs = None
    state_size = 0
    for cf in (target_elem.child_fields or []):
        if cf.name == '_state' and cf.present:
            state_off = cf.start_offset
            state_size = cf.end_offset - cf.start_offset
        if cf.name == '_uiState' and cf.present:
            uistate_end = cf.end_offset

    if state_off is None or uistate_end is None:
        return False, blob, "Could not find _state or _uiState fields"

    elem_start = target_elem.start_offset
    mbc = target_elem.child_mask_byte_count
    mask_abs = elem_start + 2

    old_mask = bytearray(orig_blob[mask_abs:mask_abs + mbc])
    log.info("Mission %d: old mask = %s", mission_key, old_mask.hex())

    new_mask = bytearray(old_mask)
    new_mask[0] |= (1 << 3) | (1 << 4)
    log.info("Mission %d: new mask = %s", mission_key, new_mask.hex())

    branched_time = struct.pack('<Q', game_time - 500)
    completed_time = struct.pack('<Q', game_time)
    insert_data = branched_time + completed_time
    growth = len(insert_data)
    insert_pos = uistate_end

    log.info("Inserting %d bytes at 0x%X (after _uiState)", growth, insert_pos)

    blob = bytearray(orig_blob)

    blob[mask_abs:mask_abs + mbc] = new_mask

    if state_size == 1:
        blob[state_off] = 0x05
    elif state_size == 2:
        struct.pack_into('<H', blob, state_off, 0x05)
    else:
        struct.pack_into('<I', blob, state_off, 0x1905)

    blob[insert_pos:insert_pos] = insert_data

    po_abs = elem_start + 2 + mbc + 2 + 1 + 8
    old_po = struct.unpack_from('<I', blob, po_abs)[0]

    block_start = quest_obj.data_offset
    block_end = block_start + quest_obj.data_size

    fixed_po = 0
    for scan_pos in range(len(orig_blob) - 12):
        if orig_blob[scan_pos:scan_pos + 8] != SENTINEL:
            continue
        orig_po_val = struct.unpack_from('<I', orig_blob, scan_pos + 8)[0]
        if orig_po_val != scan_pos + 12:
            continue
        if orig_po_val < insert_pos:
            continue

        if scan_pos >= insert_pos:
            mod_po_pos = scan_pos + growth + 8
        else:
            mod_po_pos = scan_pos + 8

        if mod_po_pos + 4 > len(blob):
            continue
        cur_val = struct.unpack_from('<I', blob, mod_po_pos)[0]
        struct.pack_into('<I', blob, mod_po_pos, cur_val + growth)
        fixed_po += 1

    parc = _ps.parse_parc_blob(orig_blob)
    toc_idx = None
    for e in parc.toc_entries:
        if e.class_index < len(parc.types) and parc.types[e.class_index].name == 'QuestSaveData':
            toc_idx = e.index
            break

    if toc_idx is None:
        return False, blob, "QuestSaveData not found in TOC"

    toc_base = parc.toc_offset + 12
    fixed_toc = 0
    for e in parc.toc_entries:
        tp = toc_base + e.index * 20 + 12
        ov = struct.unpack_from('<I', blob, tp)[0]
        if ov >= insert_pos:
            struct.pack_into('<I', blob, tp, ov + growth)
            fixed_toc += 1
        if e.index == toc_idx:
            sp = toc_base + e.index * 20 + 16
            struct.pack_into('<I', blob, sp,
                             struct.unpack_from('<I', blob, sp)[0] + growth)

    ssp = parc.toc_offset + 8
    struct.pack_into('<I', blob, ssp,
                     struct.unpack_from('<I', blob, ssp)[0] + growth)

    fixed_ts = _fixup_trailing_sizes(blob, orig_blob, insert_pos, growth, 'QuestSaveData')

    try:
        verify_result = _brfr(bytes(blob), {'input_kind': 'raw_blob'})
        obj_count = len(verify_result['objects'])
        found_ok = False
        for vobj in verify_result['objects']:
            if vobj.class_name != 'QuestSaveData':
                continue
            for vf in vobj.fields:
                if vf.name != '_missionStateList' or not vf.list_elements:
                    continue
                for velem in vf.list_elements:
                    vkey = None
                    vstate = vcomp = None
                    for vcf in (velem.child_fields or []):
                        if vcf.name == '_key' and vcf.present:
                            vkey = struct.unpack_from('<I', blob, vcf.start_offset)[0]
                        if vcf.name == '_state' and vcf.present:
                            vstate = blob[vcf.start_offset]
                        if vcf.name == '_completedTime' and vcf.present:
                            vcomp = True
                    if vkey == (mission_key & 0xFFFFFFFF):
                        if vstate == 0x05:
                            found_ok = True
                        break
        if not found_ok:
            return False, blob, (
                f"Tree verification failed: mission {mission_key} not correctly completed "
                f"after insertion. Fixed {fixed_po} POs + {fixed_toc} TOC + {fixed_ts} trailing sizes."
            )
    except Exception as e:
        return False, blob, f"Tree verification error: {e}"

    msg = (
        f"Mission {mission_key} completed: state->0x05, +16B timestamps inserted.\n"
        f"Fixed {fixed_po} POs + {fixed_toc} TOC + {fixed_ts} trailing sizes.\n"
        f"Tree re-parse OK ({obj_count} objects)."
    )
    return True, bytes(blob), msg


def _compute_dye_mask(entry: dict, is_base: bool) -> int:
    mask = 0x01  # _dyeSlotNo — always include
    mask |= 0x02  # _dyeColorR (always include — 0 is valid black)
    mask |= 0x04  # _dyeColorG
    mask |= 0x08  # _dyeColorB
    mask |= 0x10  # _dyeColorA
    mask |= 0x20  # _grimeOpacity
    if entry.get('group', 0) != 0:
        mask |= 0x40  # _dyeColorGroupInfoKey
    if entry.get('material', 0) != 0:
        mask |= 0x80  # _texturePalleteKey
    return mask


def _build_dye_element(entry: dict, is_base: bool, dye_type_index: int) -> bytearray:
    mask = _compute_dye_mask(entry, is_base)

    fields = bytearray()
    if mask & 0x01:
        fields += struct.pack('<b', entry.get('slot', 0))
    if mask & 0x02:
        fields += struct.pack('<B', entry['r'])
    if mask & 0x04:
        fields += struct.pack('<B', entry['g'])
    if mask & 0x08:
        fields += struct.pack('<B', entry['b'])
    if mask & 0x10:
        fields += struct.pack('<B', entry.get('a', 255))
    if mask & 0x20:
        fields += struct.pack('<b', entry.get('grime', 0))
    if mask & 0x40:
        fields += struct.pack('<I', entry['group'])
    if mask & 0x80:
        fields += struct.pack('<H', entry['material'])

    elem = bytearray()
    elem += struct.pack('<H', 1)
    elem += struct.pack('<B', mask)
    elem += struct.pack('<H', dye_type_index)
    elem += struct.pack('<B', 0)
    elem += SENTINEL
    elem += struct.pack('<I', 0)
    elem += struct.pack('<I', 0)
    elem += fields
    elem += struct.pack('<I', 4 + len(fields))

    return elem


def rebuild_dye_list(
    blob: bytearray,
    item_key: int,
    updated_entries: Optional[List[Dict]] = None,
) -> Tuple[bool, bytearray, str]:
    _ensure_desktop_path()
    from save_parser import build_result_from_raw as _brfr
    from parc_inserter2 import parse_and_collect, collect_all_positions

    orig_blob = bytes(blob)
    result, offset_positions, trailing_sizes = parse_and_collect(orig_blob)

    dye_list_field = None
    equip_toc_idx = None
    for obj in result['objects']:
        if obj.class_name != 'EquipmentSaveData':
            continue
        for f in obj.fields:
            if f.name != '_list' or not f.list_elements:
                continue
            for elem in f.list_elements:
                if not elem.child_fields:
                    continue
                for cf in elem.child_fields:
                    if cf.name != '_item' or not cf.child_fields:
                        continue
                    found_key = False
                    dye_field = None
                    for icf in cf.child_fields:
                        if icf.name == '_itemKey' and icf.present:
                            if struct.unpack_from('<I', orig_blob, icf.start_offset)[0] == item_key:
                                found_key = True
                        if icf.name == '_itemDyeDataList' and icf.present:
                            dye_field = icf
                    if found_key and dye_field:
                        dye_list_field = dye_field
                        break
                if dye_list_field:
                    break
            break
        break

    if not dye_list_field or not dye_list_field.list_elements:
        return False, blob, f"Item {item_key} has no dye data to fix"

    for entry in result['toc']['entries']:
        if entry.class_name == 'EquipmentSaveData':
            equip_toc_idx = entry.index
            break

    if updated_entries is not None:
        entries = updated_entries
    else:
        entries = []
        for dye_elem in dye_list_field.list_elements:
            entry = {
                'slot': 0, 'r': 0, 'g': 0, 'b': 0, 'a': 255,
                'grime': 0, 'group': 0, 'material': 0, 'has_grime': False,
            }
            for dcf in (dye_elem.child_fields or []):
                if not dcf.present:
                    continue
                if dcf.name == '_dyeSlotNo':
                    entry['slot'] = orig_blob[dcf.start_offset]
                elif dcf.name == '_dyeColorR':
                    entry['r'] = orig_blob[dcf.start_offset]
                elif dcf.name == '_dyeColorG':
                    entry['g'] = orig_blob[dcf.start_offset]
                elif dcf.name == '_dyeColorB':
                    entry['b'] = orig_blob[dcf.start_offset]
                elif dcf.name == '_dyeColorA':
                    entry['a'] = orig_blob[dcf.start_offset]
                elif dcf.name == '_grimeOpacity':
                    entry['grime'] = orig_blob[dcf.start_offset]
                    entry['has_grime'] = True
                elif dcf.name == '_dyeColorGroupInfoKey':
                    entry['group'] = struct.unpack_from('<I', orig_blob, dcf.start_offset)[0]
                elif dcf.name == '_texturePalleteKey':
                    sz = dcf.end_offset - dcf.start_offset
                    if sz == 2:
                        entry['material'] = struct.unpack_from('<H', orig_blob, dcf.start_offset)[0]
                    else:
                        entry['material'] = struct.unpack_from('<I', orig_blob, dcf.start_offset)[0]
            entries.append(entry)

    dye_type_index = dye_list_field.list_elements[0].child_type_index
    log.info("rebuild_dye_list: using type_index=%d, %d entries", dye_type_index, len(entries))

    new_list = bytearray()
    new_list += struct.pack('<B', 0)
    new_list += struct.pack('<I', len(entries))
    new_list += b'\x00' * 13

    elem_sizes = []
    for i, entry in enumerate(entries):
        is_base = (i == 0)
        elem = _build_dye_element(entry, is_base, dye_type_index)
        elem_sizes.append(len(elem))
        new_list += elem

    old_start = dye_list_field.start_offset
    old_end = dye_list_field.end_offset
    old_size = old_end - old_start
    new_size = len(new_list)
    delta = new_size - old_size

    log.info("ensure_dye_full_channels: item %d, %d entries, old=%dB new=%dB delta=%+d, range [0x%X, 0x%X)",
             item_key, len(entries), old_size, new_size, delta, old_start, old_end)

    new_blob = bytearray(orig_blob[:old_start]) + new_list + bytearray(orig_blob[old_end:])

    elem_cursor = old_start + 18
    for i, entry in enumerate(entries):
        sentinel_pos = elem_cursor + 6
        po_pos = sentinel_pos + 8
        struct.pack_into('<I', new_blob, po_pos, sentinel_pos + 12)
        elem_cursor += elem_sizes[i]

    if delta == 0:
        log.info("ensure_dye_full_channels: delta=0, no offset fixup needed")
        return True, bytes(new_blob), f"Rebuilt {len(entries)} dye entries with full masks (no size change)"

    fixed_po = 0
    for pos, old_val in offset_positions:
        if old_start <= pos < old_end:
            continue
        new_pos = pos + delta if pos >= old_end else pos
        if new_pos + 4 > len(new_blob):
            continue
        if old_val >= old_end:
            struct.pack_into('<I', new_blob, new_pos, old_val + delta)
            fixed_po += 1

    fixed_ts = 0
    for size_pos, payload_start in trailing_sizes:
        if old_start <= size_pos < old_end:
            continue
        if payload_start < old_end <= size_pos:
            new_size_pos = size_pos + delta
            if new_size_pos + 4 > len(new_blob):
                continue
            old_val = struct.unpack_from('<I', new_blob, new_size_pos)[0]
            struct.pack_into('<I', new_blob, new_size_pos, old_val + delta)
            fixed_ts += 1

    schema_end = result['raw']['schema_end']
    stream_size_pos = schema_end + 8
    old_stream_size = struct.unpack_from('<I', new_blob, stream_size_pos)[0]
    struct.pack_into('<I', new_blob, stream_size_pos, old_stream_size + delta)

    fixed_toc = 0
    for entry in result['toc']['entries']:
        doff_pos = entry.entry_offset + 12
        dsize_pos = entry.entry_offset + 16
        if entry.index == equip_toc_idx:
            old_sz = struct.unpack_from('<I', new_blob, dsize_pos)[0]
            struct.pack_into('<I', new_blob, dsize_pos, old_sz + delta)
        if entry.data_offset >= old_end:
            struct.pack_into('<I', new_blob, doff_pos, entry.data_offset + delta)
            fixed_toc += 1

    try:
        verify_result = _brfr(bytes(new_blob), {'input_kind': 'raw_blob'})
        found_full = 0
        for vobj in verify_result['objects']:
            if vobj.class_name != 'EquipmentSaveData':
                continue
            for vf in vobj.fields:
                if vf.name != '_list' or not vf.list_elements:
                    continue
                for velem in vf.list_elements:
                    if not velem.child_fields:
                        continue
                    for vcf in velem.child_fields:
                        if vcf.name != '_item' or not vcf.child_fields:
                            continue
                        for vicf in vcf.child_fields:
                            if vicf.name == '_itemKey' and vicf.present:
                                if struct.unpack_from('<I', new_blob, vicf.start_offset)[0] == item_key:
                                    for vicf2 in vcf.child_fields:
                                        if vicf2.name == '_itemDyeDataList' and vicf2.present and vicf2.list_elements:
                                            found_full = len(vicf2.list_elements)
        if found_full != len(entries):
            return False, blob, (
                f"Verification failed: expected {len(entries)} dye entries, found {found_full}. "
                f"Fixed {fixed_po} POs + {fixed_toc} TOC + {fixed_ts} trailing sizes."
            )
    except Exception as e:
        return False, blob, f"Verification error after ensure_dye_full_channels: {e}"

    msg = (
        f"Rebuilt {len(entries)} dye entries with correct masks (delta={delta:+d}B). "
        f"Fixed {fixed_po} POs + {fixed_toc} TOC + {fixed_ts} trailing sizes."
    )
    log.info("rebuild_dye_list: %s", msg)
    return True, bytes(new_blob), msg


def ensure_dye_full_channels(blob: bytearray, item_key: int) -> Tuple[bool, bytearray, str]:
    return rebuild_dye_list(blob, item_key)


def insert_dye_to_item(
    blob: bytearray,
    item_key: int,
    num_slots: int = 1,
    r: int = 255, g: int = 0, b: int = 0, a: int = 255,
    color_group: int = 0xC88211F5,
    material: int = 1,
    grime: int = 0,
) -> Tuple[bool, bytearray, str]:
    _ensure_desktop_path()
    from save_parser import build_result_from_raw as _brfr
    import parc_serializer as _ps

    orig_blob = bytes(blob)
    result = _brfr(orig_blob, {'input_kind': 'raw_blob'})

    equip_obj = item_field = target_item = None
    item_slot_idx = -1
    for obj in result['objects']:
        if obj.class_name != 'EquipmentSaveData':
            continue
        equip_obj = obj
        for f in obj.fields:
            if f.name != '_list' or not f.list_elements:
                continue
            item_field = f
            for slot_idx, elem in enumerate(f.list_elements):
                if not elem.child_fields:
                    continue
                for cf in elem.child_fields:
                    if cf.name != '_item' or not cf.child_fields:
                        continue
                    for icf in cf.child_fields:
                        if icf.name == '_itemKey' and icf.present:
                            if struct.unpack_from('<I', orig_blob, icf.start_offset)[0] == item_key:
                                target_item = cf
                                item_slot_idx = slot_idx
                                break
                    if target_item:
                        break
                if target_item:
                    break
            break
        break

    if not equip_obj:
        return False, blob, "EquipmentSaveData not found"
    if not target_item:
        return False, blob, f"Item {item_key} not found in equipped items"

    for icf in target_item.child_fields:
        if icf.name == '_itemDyeDataList' and icf.present and icf.list_elements:
            return False, blob, f"Item {item_key} already has dye data ({len(icf.list_elements)} entries)"

    dye_type_index = None
    for i, t in enumerate(result['schema']['types']):
        if t.name == 'ItemDyeSaveData':
            dye_type_index = i
            break
    if dye_type_index is None:
        return False, blob, (
            "ItemDyeSaveData type not found in save schema. "
            "The game adds this type when you dye an item for the first time. "
            "Please dye any item in-game first, then save, and try again."
        )
    log.info("insert_dye_to_item: ItemDyeSaveData is type_index=%d", dye_type_index)

    insert_pos = None
    mask_abs = None
    for icf in target_item.child_fields:
        if icf.name == '_socketSaveDataList' and icf.present:
            insert_pos = icf.end_offset
        elif icf.name == '_itemDyeDataList':
            if insert_pos is None and icf.start_offset > 0:
                insert_pos = icf.start_offset

    if insert_pos is None:
        for icf in target_item.child_fields:
            if icf.name == '_transferredItemKey' and icf.present:
                insert_pos = icf.start_offset
                break

    if insert_pos is None:
        return False, blob, "Could not determine insertion point for dye data"

    item_start = target_item.start_offset
    mbc = target_item.child_mask_byte_count or len(target_item.child_mask_bytes)

    old_mask = bytearray(target_item.child_mask_bytes)
    if len(old_mask) < 2:
        return False, blob, f"Item mask too short ({len(old_mask)} bytes)"

    if old_mask[1] & 0x40:
        return False, blob, f"Item {item_key} mask already has dye bit set (mask={old_mask.hex()})"

    mask_search = bytes(old_mask)
    mask_pos = orig_blob.find(mask_search, max(0, item_start - 20), item_start + 20)
    if mask_pos < 0:
        return False, blob, f"Could not find mask bytes {old_mask.hex()} near item at {hex(item_start)}"

    new_mask = bytearray(old_mask)
    new_mask[1] |= 0x40

    log.info("Item %d: mask %s -> %s at 0x%X, insert at 0x%X",
             item_key, old_mask.hex(), new_mask.hex(), mask_pos, insert_pos)


    dye_data = bytearray()

    dye_data += struct.pack('<B', 0)
    dye_data += struct.pack('<I', num_slots)
    dye_data += b'\x00' * 13

    elem_sizes = []
    for slot_no in range(num_slots):
        is_base = (slot_no == 0)
        entry = {
            'slot': slot_no,
            'r': r, 'g': g, 'b': b, 'a': a,
            'grime': grime, 'has_grime': (grime != 0),
            'group': (color_group if color_group else 0xC88211F5) if is_base else 0,
            'material': material,
        }
        elem = _build_dye_element(entry, is_base, dye_type_index)
        elem_sizes.append(len(elem))
        dye_data += elem

    growth = len(dye_data)
    log.info("Built %d bytes of dye data (%d slots)", growth, num_slots)

    blob = bytearray(orig_blob)

    blob[mask_pos:mask_pos + len(new_mask)] = new_mask

    blob[insert_pos:insert_pos] = dye_data

    elem_cursor = insert_pos + 18
    for slot_no in range(num_slots):
        sentinel_pos = elem_cursor + 6
        po_pos = sentinel_pos + 8
        correct_po = sentinel_pos + 12
        struct.pack_into('<I', blob, po_pos, correct_po)
        elem_cursor += elem_sizes[slot_no]

    from parc_inserter2 import collect_all_positions
    offset_positions, _ = collect_all_positions(result, orig_blob)

    fixed_po = 0
    for pos, old_val in offset_positions:
        new_pos = pos + growth if pos >= insert_pos else pos
        if new_pos + 4 > len(blob):
            continue
        if old_val >= insert_pos:
            struct.pack_into('<I', blob, new_pos, old_val + growth)
            fixed_po += 1

    parc = _ps.parse_parc_blob(orig_blob)
    toc_idx = None
    for e in parc.toc_entries:
        if e.class_index < len(parc.types) and parc.types[e.class_index].name == 'EquipmentSaveData':
            toc_idx = e.index
            break

    if toc_idx is None:
        return False, blob, "EquipmentSaveData not found in TOC"

    toc_base = parc.toc_offset + 12
    fixed_toc = 0
    for e in parc.toc_entries:
        tp = toc_base + e.index * 20 + 12
        ov = struct.unpack_from('<I', blob, tp)[0]
        if ov >= insert_pos:
            struct.pack_into('<I', blob, tp, ov + growth)
            fixed_toc += 1
        if e.index == toc_idx:
            sp = toc_base + e.index * 20 + 16
            struct.pack_into('<I', blob, sp,
                             struct.unpack_from('<I', blob, sp)[0] + growth)

    ssp = parc.toc_offset + 8
    struct.pack_into('<I', blob, ssp,
                     struct.unpack_from('<I', blob, ssp)[0] + growth)

    fixed_ts = _fixup_trailing_sizes(blob, orig_blob, insert_pos, growth, 'EquipmentSaveData')

    try:
        verify_result = _brfr(bytes(blob), {'input_kind': 'raw_blob'})
        obj_count = len(verify_result['objects'])
        found_dye = False
        for vobj in verify_result['objects']:
            if vobj.class_name != 'EquipmentSaveData':
                continue
            for vf in vobj.fields:
                if vf.name != '_list' or not vf.list_elements:
                    continue
                for velem in vf.list_elements:
                    if not velem.child_fields:
                        continue
                    for vcf in velem.child_fields:
                        if vcf.name != '_item' or not vcf.child_fields:
                            continue
                        for vicf in vcf.child_fields:
                            if vicf.name == '_itemKey' and vicf.present:
                                if struct.unpack_from('<I', blob, vicf.start_offset)[0] == item_key:
                                    for vicf2 in vcf.child_fields:
                                        if vicf2.name == '_itemDyeDataList' and vicf2.present and vicf2.list_elements:
                                            found_dye = True
                                            log.info("Verification: item %d now has %d dye entries",
                                                     item_key, len(vicf2.list_elements))
        if not found_dye:
            return False, blob, (
                f"Tree verification failed: item {item_key} does not have dye data after insertion. "
                f"Fixed {fixed_po} POs + {fixed_toc} TOC + {fixed_ts} trailing sizes."
            )
    except Exception as e:
        return False, blob, f"Tree verification error: {e}"

    msg = (
        f"Dye data inserted for item {item_key}: {num_slots} slot(s), "
        f"RGB({r},{g},{b}), material={material}.\n"
        f"Fixed {fixed_po} POs + {fixed_toc} TOC + {fixed_ts} trailing sizes.\n"
        f"Tree re-parse OK ({obj_count} objects)."
    )
    return True, bytes(blob), msg


def inject_community_knowledge(
    blob: bytearray,
) -> Tuple[bool, bytearray, str]:
    _ensure_desktop_path()
    from save_parser import build_result_from_raw as _brfr

    orig_blob = bytes(blob)
    result = _brfr(orig_blob, {'input_kind': 'raw_blob'})

    know_obj = know_field = None
    for obj in result['objects']:
        if obj.class_name == 'KnowledgeSaveData':
            for f in obj.fields:
                if f.name == '_list' and f.list_elements:
                    know_obj = obj
                    know_field = f
                    break
            break

    if not know_obj or not know_field:
        return False, blob, "KnowledgeSaveData._list not found"

    existing_keys = set()
    for elem in know_field.list_elements:
        for cf in (elem.child_fields or []):
            if cf.name == '_key' and cf.present:
                existing_keys.add(struct.unpack_from('<I', orig_blob, cf.start_offset)[0])

    comm_data = _load_community_knowledge_keys()
    if not comm_data:
        return False, blob, "community_knowledge_keys.json not found"

    tmpl_mask = ''
    if know_field.list_elements:
        tmpl_mask = know_field.list_elements[-1].child_mask_bytes.hex() if know_field.list_elements[-1].child_mask_bytes else ''

    to_insert = sorted(set(
        e['key'] for e in comm_data
        if e.get('mask') == tmpl_mask and e['key'] not in existing_keys
    ))

    if not to_insert:
        return True, blob, "All community knowledge already present"

    return _insert_knowledge_keys(blob, orig_blob, know_obj, know_field, to_insert, len(existing_keys))


def inject_knowledge_locations_only(
    blob: bytearray,
) -> Tuple[bool, bytearray, str]:
    _ensure_desktop_path()
    from save_parser import build_result_from_raw as _brfr

    orig_blob = bytes(blob)
    result = _brfr(orig_blob, {'input_kind': 'raw_blob'})

    know_obj = know_field = None
    for obj in result['objects']:
        if obj.class_name == 'KnowledgeSaveData':
            for f in obj.fields:
                if f.name == '_list' and f.list_elements:
                    know_obj = obj
                    know_field = f
                    break
            break

    if not know_obj or not know_field:
        return False, blob, "KnowledgeSaveData._list not found"

    existing_keys = set()
    for elem in know_field.list_elements:
        for cf in (elem.child_fields or []):
            if cf.name == '_key' and cf.present:
                existing_keys.add(struct.unpack_from('<I', orig_blob, cf.start_offset)[0])

    comm_data = _load_community_knowledge_keys()
    if not comm_data:
        return False, blob, "community_knowledge_keys.json not found"

    tmpl_mask = ''
    if know_field.list_elements:
        tmpl_mask = know_field.list_elements[-1].child_mask_bytes.hex() if know_field.list_elements[-1].child_mask_bytes else ''

    to_insert = sorted(set(
        e['key'] for e in comm_data
        if e.get('mask') == tmpl_mask and e['key'] not in existing_keys
    ))

    if not to_insert:
        return True, blob, "All community knowledge already present"

    return _insert_knowledge_keys(blob, orig_blob, know_obj, know_field, to_insert, len(existing_keys), override_level=0)


def _insert_knowledge_keys(
    blob: bytearray,
    orig_blob: bytes,
    know_obj,
    know_field,
    keys_to_insert: List[int],
    existing_count: int,
    override_level: int = -1,
) -> Tuple[bool, bytearray, str]:
    template_elem = know_field.list_elements[-1]
    tmpl_raw = orig_blob[template_elem.start_offset:template_elem.end_offset]
    tmpl_start = template_elem.start_offset

    key_rel = None
    level_rel = None
    for cf in (template_elem.child_fields or []):
        if cf.name == '_key' and cf.present:
            key_rel = cf.start_offset - tmpl_start
        elif cf.name == '_level' and cf.present:
            level_rel = cf.start_offset - tmpl_start

    if key_rel is None:
        return False, blob, "Could not find _key field in knowledge template"

    mbc = struct.unpack_from('<H', tmpl_raw, 0)[0]
    mask_len = mbc & 0xFF
    locator_end = 2 + mask_len + 2 + 1 + 8 + 4

    last_elem = know_field.list_elements[-1]
    insert_abs = last_elem.end_offset
    all_new = bytearray()
    cursor = insert_abs

    for key in keys_to_insert:
        clone = bytearray(tmpl_raw)
        struct.pack_into('<I', clone, key_rel, key)
        if override_level >= 0 and level_rel is not None:
            struct.pack_into('<I', clone, level_rel, override_level)

        orig_main_po = struct.unpack_from('<I', tmpl_raw, locator_end - 4)[0]
        new_main_po = cursor + locator_end
        po_delta = new_main_po - orig_main_po

        for boff in range(0, len(clone) - 12):
            if clone[boff:boff+8] == SENTINEL:
                po_off = boff + 8
                if po_off + 4 <= len(clone):
                    old_po = struct.unpack_from('<I', tmpl_raw, po_off)[0]
                    if old_po >= tmpl_start and old_po < tmpl_start + len(tmpl_raw) + 4096:
                        struct.pack_into('<I', clone, po_off, old_po + po_delta)

        all_new += clone
        cursor += len(clone)

    total_growth = len(all_new)
    inserted = len(keys_to_insert)
    log.info("Inserting %d knowledge entries (%d bytes)", inserted, total_growth)

    block_start = know_obj.data_offset
    block_end = block_start + know_obj.data_size
    r_insert = insert_abs - block_start

    block_raw = bytearray(orig_blob[block_start:block_end])
    new_block = block_raw[:r_insert] + all_new + block_raw[r_insert:]

    list_rel = know_field.start_offset - block_start
    orig_count = len(know_field.list_elements)
    new_count = orig_count + inserted
    new_block[list_rel + 1] = new_count & 0xFF
    new_block[list_rel + 2] = (new_count >> 8) & 0xFF
    new_block[list_rel + 3] = (new_count >> 16) & 0xFF

    for boff in range(r_insert + total_growth, len(new_block) - 12):
        if new_block[boff:boff+8] == SENTINEL:
            po_off = boff + 8
            if po_off + 4 <= len(new_block):
                ov = struct.unpack_from('<I', new_block, po_off)[0]
                if ov >= insert_abs and ov < insert_abs + 10000000:
                    struct.pack_into('<I', new_block, po_off, ov + total_growth)

    blob[block_start:block_end] = new_block

    import parc_serializer as _ps
    parc = _ps.parse_parc_blob(orig_blob)
    toc_idx = None
    for e in parc.toc_entries:
        if e.class_index < len(parc.types) and parc.types[e.class_index].name == 'KnowledgeSaveData':
            toc_idx = e.index
            break

    if toc_idx is None:
        return False, blob, "KnowledgeSaveData not found in TOC"

    fixed = _fixup_external(blob, orig_blob, parc, toc_idx, block_end, total_growth)
    _fixup_trailing_sizes(blob, orig_blob, block_end, total_growth, 'KnowledgeSaveData')

    return True, bytes(blob), (
        f"Injected {inserted} knowledge entries "
        f"({existing_count} already present). "
        f"Fixed {fixed} external POs."
    )


def inject_knowledge_fast(
    blob: bytearray,
    keys_filter: Optional[List[int]] = None,
) -> Tuple[bool, bytearray, str]:
    _ensure_desktop_path()
    from save_parser import build_result_from_raw as _brfr
    import parc_serializer as _ps
    import time

    t0 = time.time()

    orig_blob = bytes(blob)
    result = _brfr(orig_blob, {'input_kind': 'raw_blob'})

    know_obj = know_field = None
    for obj in result['objects']:
        if obj.class_name == 'KnowledgeSaveData':
            for f in obj.fields:
                if f.name == '_list' and f.list_elements:
                    know_obj = obj
                    know_field = f
                    break
            break

    if not know_obj or not know_field:
        return False, blob, "KnowledgeSaveData._list not found"

    existing_keys = set()
    for elem in know_field.list_elements:
        for cf in (elem.child_fields or []):
            if cf.name == '_key' and cf.present:
                existing_keys.add(struct.unpack_from('<I', orig_blob, cf.start_offset)[0])

    if keys_filter is not None:
        target_keys = set(keys_filter)
    else:
        all_keys = _load_all_knowledge_keys()
        if not all_keys:
            return False, blob, "knowledge_keys_all.json not found"
        target_keys = set(all_keys)

    to_insert = sorted(target_keys - existing_keys)
    if not to_insert:
        return True, blob, "All requested knowledge already present"

    t1 = time.time()

    template_elem = know_field.list_elements[-1]
    tmpl_raw = orig_blob[template_elem.start_offset:template_elem.end_offset]
    tmpl_start = template_elem.start_offset

    key_rel = level_rel = None
    for cf in (template_elem.child_fields or []):
        if cf.name == '_key' and cf.present:
            key_rel = cf.start_offset - tmpl_start
        elif cf.name == '_level' and cf.present:
            level_rel = cf.start_offset - tmpl_start

    if key_rel is None:
        return False, blob, "Could not find _key field in knowledge template"

    mbc = struct.unpack_from('<H', tmpl_raw, 0)[0]
    mask_len = mbc & 0xFF
    locator_end = 2 + mask_len + 2 + 1 + 8 + 4

    last_elem = know_field.list_elements[-1]
    insert_abs = last_elem.end_offset
    all_new = bytearray()
    cursor = insert_abs

    for key in to_insert:
        clone = bytearray(tmpl_raw)
        struct.pack_into('<I', clone, key_rel, key)

        orig_main_po = struct.unpack_from('<I', tmpl_raw, locator_end - 4)[0]
        new_main_po = cursor + locator_end
        po_delta = new_main_po - orig_main_po

        for boff in range(0, len(clone) - 12):
            if clone[boff:boff+8] == SENTINEL:
                po_off = boff + 8
                if po_off + 4 <= len(clone):
                    old_po = struct.unpack_from('<I', tmpl_raw, po_off)[0]
                    if old_po >= tmpl_start and old_po < tmpl_start + len(tmpl_raw) + 4096:
                        struct.pack_into('<I', clone, po_off, old_po + po_delta)

        all_new += clone
        cursor += len(clone)

    total_growth = len(all_new)
    inserted = len(to_insert)

    t2 = time.time()

    block_start = know_obj.data_offset
    block_end = block_start + know_obj.data_size
    r_insert = insert_abs - block_start

    block_raw = bytearray(orig_blob[block_start:block_end])
    new_block = block_raw[:r_insert] + all_new + block_raw[r_insert:]

    list_rel = know_field.start_offset - block_start
    orig_count = len(know_field.list_elements)
    new_count = orig_count + inserted
    new_block[list_rel + 1] = new_count & 0xFF
    new_block[list_rel + 2] = (new_count >> 8) & 0xFF
    new_block[list_rel + 3] = (new_count >> 16) & 0xFF

    for boff in range(r_insert + total_growth, len(new_block) - 12):
        if new_block[boff:boff+8] == SENTINEL:
            po_off = boff + 8
            if po_off + 4 <= len(new_block):
                ov = struct.unpack_from('<I', new_block, po_off)[0]
                if ov >= insert_abs and ov < insert_abs + 10000000:
                    struct.pack_into('<I', new_block, po_off, ov + total_growth)

    blob[block_start:block_end] = new_block

    t3 = time.time()

    parc = _ps.parse_parc_blob(orig_blob)
    toc_idx = None
    for e in parc.toc_entries:
        if e.class_index < len(parc.types) and parc.types[e.class_index].name == 'KnowledgeSaveData':
            toc_idx = e.index
            break

    if toc_idx is None:
        return False, blob, "KnowledgeSaveData not found in TOC"

    fixed_po = 0
    for scan_pos in range(len(orig_blob) - 12):
        if orig_blob[scan_pos:scan_pos + 8] != SENTINEL:
            continue
        orig_po_val = struct.unpack_from('<I', orig_blob, scan_pos + 8)[0]
        if orig_po_val != scan_pos + 12:
            continue
        if orig_po_val < block_end:
            continue

        if scan_pos >= block_end:
            mod_po_pos = scan_pos + total_growth + 8
        else:
            mod_po_pos = scan_pos + 8

        if mod_po_pos + 4 > len(blob):
            continue
        cur_val = struct.unpack_from('<I', blob, mod_po_pos)[0]
        struct.pack_into('<I', blob, mod_po_pos, cur_val + total_growth)
        fixed_po += 1

    toc_base = parc.toc_offset + 12
    fixed_toc = 0
    for e in parc.toc_entries:
        tp = toc_base + e.index * 20 + 12
        ov = struct.unpack_from('<I', blob, tp)[0]
        if ov >= block_end:
            struct.pack_into('<I', blob, tp, ov + total_growth)
            fixed_toc += 1
        if e.index == toc_idx:
            sp = toc_base + e.index * 20 + 16
            struct.pack_into('<I', blob, sp,
                             struct.unpack_from('<I', blob, sp)[0] + total_growth)

    ssp = parc.toc_offset + 8
    struct.pack_into('<I', blob, ssp,
                     struct.unpack_from('<I', blob, ssp)[0] + total_growth)

    t4 = time.time()

    ts_fixed = 0
    def _find_ts_from_tree(field):
        nonlocal ts_fixed
        dk = getattr(field, 'decode_kind', '') or ''
        s = getattr(field, 'start_offset', 0) or 0
        e = getattr(field, 'end_offset', 0) or 0
        is_inline = (dk == 'list_element' or 'locator' in dk)
        if is_inline and s and e and e - s > 4:
            ts_pos = e - 4
            if s < block_end < ts_pos and ts_pos + 4 <= len(orig_blob):
                ts_val = struct.unpack_from('<I', orig_blob, ts_pos)[0]
                if 0 < ts_val < (e - s):
                    new_ts_pos = ts_pos + total_growth if ts_pos >= block_end else ts_pos
                    if new_ts_pos + 4 <= len(blob):
                        old_val = struct.unpack_from('<I', blob, new_ts_pos)[0]
                        struct.pack_into('<I', blob, new_ts_pos, old_val + total_growth)
                        ts_fixed += 1
        for cf in (getattr(field, 'child_fields', None) or []):
            _find_ts_from_tree(cf)
        for le in (getattr(field, 'list_elements', None) or []):
            _find_ts_from_tree(le)

    for obj in result['objects']:
        if obj.class_name == 'KnowledgeSaveData':
            for f in obj.fields:
                _find_ts_from_tree(f)
            break

    t5 = time.time()

    msg = (
        f"Injected {inserted} knowledge entries "
        f"({len(existing_keys)} already present). "
        f"Fixed {fixed_po} POs + {fixed_toc} TOC + {ts_fixed} trailing sizes. "
        f"Time: parse={t1-t0:.1f}s clone={t2-t1:.1f}s splice={t3-t2:.1f}s "
        f"fixup={t4-t3:.1f}s trailing={t5-t4:.1f}s total={t5-t0:.1f}s"
    )
    return True, bytes(blob), msg


def _load_all_knowledge_keys() -> List[int]:
    try:
        for base in [_MY_DIR, getattr(sys, '_MEIPASS', _MY_DIR)]:
            p = os.path.join(base, 'knowledge_keys_all.json')
            if os.path.isfile(p):
                with open(p, 'r') as f:
                    data = json.load(f)
                return [e['key'] for e in data if isinstance(e.get('key'), int)]
    except Exception:
        pass
    return []


def inject_all_knowledge(
    blob: bytearray,
    keys_filter: Optional[List[int]] = None,
) -> Tuple[bool, bytearray, str]:
    _ensure_desktop_path()
    from save_parser import build_result_from_raw as _brfr

    orig_blob = bytes(blob)
    result = _brfr(orig_blob, {'input_kind': 'raw_blob'})

    know_obj = know_field = None
    for obj in result['objects']:
        if obj.class_name == 'KnowledgeSaveData':
            for f in obj.fields:
                if f.name == '_list' and f.list_elements:
                    know_obj = obj
                    know_field = f
                    break
            break

    if not know_obj or not know_field:
        return False, blob, "KnowledgeSaveData._list not found"

    existing_keys = set()
    for elem in know_field.list_elements:
        for cf in (elem.child_fields or []):
            if cf.name == '_key' and cf.present:
                existing_keys.add(struct.unpack_from('<I', orig_blob, cf.start_offset)[0])

    if keys_filter is not None:
        target_keys = set(keys_filter)
    else:
        all_keys = _load_all_knowledge_keys()
        if not all_keys:
            return False, blob, "knowledge_keys_all.json not found"
        target_keys = set(all_keys)

    to_insert = sorted(target_keys - existing_keys)
    if not to_insert:
        return True, blob, "All requested knowledge already present"

    return _insert_knowledge_keys(blob, orig_blob, know_obj, know_field, to_insert, len(existing_keys))


ABYSS_GIMMICK_KEYS = {1003226, 1008610, 1005450, 1008572, 1001815}
ABYSS_MASK = 'db33b0000802'
ABYSS_STATE_HASH = 0x150b14d0


def load_abyss_templates() -> List[Dict[str, Any]]:
    for base in [_MY_DIR, getattr(sys, '_MEIPASS', _MY_DIR)]:
        p = os.path.join(base, 'abyss_gimmick_templates.json')
        if os.path.isfile(p):
            with open(p, 'r') as f:
                return json.load(f)
    return []


def _parse_tree_full(blob: bytes) -> dict:
    _ensure_desktop_path()
    from save_parser import build_result_from_raw
    return build_result_from_raw(blob, {'input_kind': 'raw_blob'})


def _ensure_desktop_path():
    for sub in ['Communitydump/desktopeditor', 'desktopeditor']:
        for base in [_MY_DIR, getattr(sys, '_MEIPASS', _MY_DIR)]:
            p = os.path.join(base, sub)
            if os.path.isdir(p) and p not in sys.path:
                sys.path.append(p)


def insert_abyss_gates(
    blob: bytearray,
    community_entries: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[bool, bytearray, str]:
    if community_entries is None:
        community_entries = load_abyss_templates()
    if not community_entries:
        return False, blob, "No community abyss data found (abyss_gimmick_templates.json)"

    comm_same_mask = [e for e in community_entries
                      if bytes.fromhex(e['binary'])[2:8].hex() == ABYSS_MASK
                      or e.get('disc_type') in (1000001, 1000008)]

    orig_blob = bytes(blob)
    result = _parse_tree_full(orig_blob)

    field_obj = gimmick_field = None
    for obj in result['objects']:
        if obj.class_name == 'FieldSaveData':
            for f in obj.fields:
                if f.name == '_fieldGimmickSaveDataList' and f.list_elements:
                    if len(f.list_elements) > 10:
                        field_obj = obj
                        gimmick_field = f
                        break
            if field_obj:
                break

    if not field_obj or not gimmick_field:
        return False, blob, "FieldSaveData with gimmick list not found"

    template_elem = None
    for elem in gimmick_field.list_elements:
        mask = elem.child_mask_bytes.hex() if elem.child_mask_bytes else ''
        if mask != ABYSS_MASK:
            continue
        for cf in (elem.child_fields or []):
            if cf.name == '_gimmickInfoKey' and cf.present:
                k = struct.unpack_from('<I', orig_blob, cf.start_offset)[0]
                if k in ABYSS_GIMMICK_KEYS:
                    template_elem = elem
                    break
        if template_elem:
            break

    if not template_elem:
        return False, blob, (
            "No existing abyss gate found in this save.\n"
            "You need to discover at least one abyss gate in-game first,\n"
            "then save and try again."
        )

    tmpl_raw = orig_blob[template_elem.start_offset:template_elem.end_offset]
    tmpl_start = template_elem.start_offset
    log.info("Abyss template: %dB at %d, mask=%s", len(tmpl_raw), tmpl_start,
             template_elem.child_mask_bytes.hex())

    field_offsets = {}
    for cf in (template_elem.child_fields or []):
        if cf.present:
            field_offsets[cf.name] = cf.start_offset - tmpl_start

    comm_parsed = []
    for ce in comm_same_mask:
        raw_entry = bytes.fromhex(ce['binary'])
        comm_parsed.append({
            'uuid': bytes.fromhex(ce['uuid']) if ce.get('uuid') else b'',
            'gimmickInfoKey': ce.get('disc_type', 1003226),
        })

    _ensure_desktop_path()
    from save_parser import build_result_from_raw as _brfr
    try:
        cdata_path = None
        for base in [_MY_DIR, getattr(sys, '_MEIPASS', _MY_DIR)]:
            p = os.path.join(base, 'Communitydump', 'savewp', 'save.save')
            if os.path.isfile(p):
                cdata_path = p
                break
        if cdata_path:
            from save_crypto import load_save_file as _lsf
            cdata = _lsf(cdata_path)
            craw = bytes(cdata.decompressed_blob)
            cresult = _brfr(craw, {'input_kind': 'raw_blob'})

            comm_parsed = []
            for obj in cresult['objects']:
                if obj.class_name != 'FieldSaveData' or obj.data_size < 1000:
                    continue
                for f in obj.fields:
                    if f.name != '_fieldGimmickSaveDataList' or not f.list_elements:
                        continue
                    for elem in f.list_elements:
                        mask = elem.child_mask_bytes.hex() if elem.child_mask_bytes else ''
                        if mask != ABYSS_MASK:
                            continue
                        entry = {}
                        for cf in (elem.child_fields or []):
                            if not cf.present:
                                continue
                            if cf.name == '_gimmickInfoKey':
                                entry['gimmickInfoKey'] = struct.unpack_from('<I', craw, cf.start_offset)[0]
                            elif cf.name == '_fieldGimmickSaveDataKey':
                                entry['key'] = struct.unpack_from('<I', craw, cf.start_offset)[0]
                            elif cf.name == '_levelOriginSceneObjectUuid':
                                entry['uuid'] = craw[cf.start_offset:cf.start_offset+16]
                            elif cf.name == '_ownerLevelName':
                                slen = struct.unpack_from('<I', craw, cf.start_offset)[0]
                                entry['levelName'] = craw[cf.start_offset:cf.start_offset+4+slen+1]
                            elif cf.name == '_initStateNameHash':
                                entry['stateHash'] = struct.unpack_from('<I', craw, cf.start_offset)[0]
                        if entry.get('gimmickInfoKey') in ABYSS_GIMMICK_KEYS:
                            comm_parsed.append(entry)
                    break
                break
            log.info("Parsed %d community abyss entries", len(comm_parsed))
    except Exception as e:
        log.warning("Could not parse community save: %s", e)

    if not comm_parsed:
        return False, blob, "No community abyss data could be parsed"

    existing_uuids = set()
    for elem in gimmick_field.list_elements:
        for cf in (elem.child_fields or []):
            if cf.name == '_levelOriginSceneObjectUuid' and cf.present:
                existing_uuids.add(orig_blob[cf.start_offset:cf.start_offset+16])

    to_insert = [e for e in comm_parsed if e.get('uuid') and e['uuid'] not in existing_uuids]
    if not to_insert:
        return True, blob, "All abyss gates already present"

    max_gkey = 0
    for elem in gimmick_field.list_elements:
        for cf in (elem.child_fields or []):
            if cf.name == '_fieldGimmickSaveDataKey' and cf.present:
                k = struct.unpack_from('<I', orig_blob, cf.start_offset)[0]
                if k > max_gkey:
                    max_gkey = k

    last_elem = gimmick_field.list_elements[-1]
    insert_abs = last_elem.end_offset
    mbc = struct.unpack_from('<H', tmpl_raw, 0)[0]
    mask_len = mbc & 0xFF
    locator_end = 2 + mask_len + 2 + 1 + 8 + 4

    all_new = bytearray()
    cursor = insert_abs

    for ce in to_insert:
        clone = bytearray(tmpl_raw)
        max_gkey += 1

        for cf in (template_elem.child_fields or []):
            if not cf.present:
                continue
            rel = cf.start_offset - tmpl_start
            if cf.name == '_fieldGimmickSaveDataKey':
                struct.pack_into('<I', clone, rel, max_gkey)
            elif cf.name == '_levelOriginSceneObjectUuid' and ce.get('uuid'):
                clone[rel:rel+16] = ce['uuid']
            elif cf.name == '_gimmickInfoKey' and ce.get('gimmickInfoKey'):
                struct.pack_into('<I', clone, rel, ce['gimmickInfoKey'])
            elif cf.name == '_initStateNameHash':
                struct.pack_into('<I', clone, rel, ce.get('stateHash', ABYSS_STATE_HASH))
            elif cf.name == '_ownerLevelName' and ce.get('levelName'):
                tmpl_slen = struct.unpack_from('<I', clone, rel)[0]
                new_slen = struct.unpack_from('<I', ce['levelName'], 0)[0]
                if tmpl_slen == new_slen:
                    clone[rel:rel+4+new_slen+1] = ce['levelName']

        orig_main_po = struct.unpack_from('<I', tmpl_raw, locator_end - 4)[0]
        new_main_po = cursor + locator_end
        po_delta = new_main_po - orig_main_po

        for boff in range(0, len(clone) - 12):
            if clone[boff:boff+8] == SENTINEL:
                po_off = boff + 8
                if po_off + 4 <= len(clone):
                    old_po = struct.unpack_from('<I', tmpl_raw, po_off)[0]
                    if old_po >= tmpl_start and old_po < tmpl_start + len(tmpl_raw) + 4096:
                        struct.pack_into('<I', clone, po_off, old_po + po_delta)

        all_new += clone
        cursor += len(clone)

    total_growth = len(all_new)
    log.info("Inserting %d abyss gates (%d bytes)", len(to_insert), total_growth)

    block_start = field_obj.data_offset
    block_end = block_start + field_obj.data_size
    r_insert = insert_abs - block_start

    block_raw = bytearray(orig_blob[block_start:block_end])
    new_block = block_raw[:r_insert] + all_new + block_raw[r_insert:]

    list_rel = gimmick_field.start_offset - block_start
    orig_count = len(gimmick_field.list_elements)
    new_count = orig_count + len(to_insert)
    new_block[list_rel + 1] = new_count & 0xFF
    new_block[list_rel + 2] = (new_count >> 8) & 0xFF
    new_block[list_rel + 3] = (new_count >> 16) & 0xFF

    for boff in range(r_insert + total_growth, len(new_block) - 12):
        if new_block[boff:boff+8] == SENTINEL:
            po_off = boff + 8
            if po_off + 4 <= len(new_block):
                ov = struct.unpack_from('<I', new_block, po_off)[0]
                if ov >= insert_abs and ov < insert_abs + 10000000:
                    struct.pack_into('<I', new_block, po_off, ov + total_growth)

    blob[block_start:block_end] = new_block

    import parc_serializer as _ps
    parc = _ps.parse_parc_blob(orig_blob)
    toc_idx = None
    for e in parc.toc_entries:
        if e.class_index < len(parc.types) and parc.types[e.class_index].name == 'FieldSaveData' and e.data_size > 1000:
            toc_idx = e.index
            break

    if toc_idx is None:
        return False, blob, "FieldSaveData not found in TOC"

    fixed = _fixup_external(blob, orig_blob, parc, toc_idx, block_end, total_growth)
    _fixup_trailing_sizes(blob, orig_blob, block_end, total_growth, 'FieldSaveData')

    return True, bytes(blob), (
        f"Inserted {len(to_insert)} abyss gates "
        f"({len(existing_uuids)} already present). "
        f"Fixed {fixed} external POs."
    )


def _splice_socket_elements(
    blob: bytearray,
    item: 'SaveItem',
    target_slots: Dict[int, Any],
    expected_mask: int,
    build_target_elem: Callable[[dict, int], bytes],
    valid_count_fn: Callable[[int, int], int],
    fn_name: str,
    verb: str,
) -> Tuple[bool, bytearray, str]:
    from parc_inserter2 import parse_and_collect

    orig_blob = bytes(blob)
    _result, offset_positions, trailing_sizes = parse_and_collect(orig_blob)
    schema_end_cached = _result['raw']['schema_end']
    toc_entries_cached = list(_result['toc']['entries'])

    locator_start = item.offset - 24
    if locator_start + 5 > len(orig_blob):
        return False, blob, "Item locator out of range"
    # Pre-game-v1.0.5 layout: [mbc:2 = 3] [bitmask:3] [pad:3]
    # Post-game-v1.0.5 layout: [discrim:1 = 0] [bitmask:4] [pad:3]
    mbc = struct.unpack_from('<H', orig_blob, locator_start)[0]
    if mbc == 3:
        bitmask = bytes(orig_blob[locator_start + 2: locator_start + 5])
    elif orig_blob[locator_start] == 0x00:
        bitmask = bytes(orig_blob[locator_start + 1: locator_start + 5])
    else:
        return False, blob, (
            f"Unrecognised item locator at 0x{locator_start:08X} "
            f"(leading bytes 0x{orig_blob[locator_start]:02X} 0x{orig_blob[locator_start+1]:02X}). "
            "Expected old-format mbc=3 or new-format discriminator=0."
        )

    fp = _item_socket_field_present
    sock_rel = sum(_ITEM_SOCKET_FIELD_SIZES[i] for i in range(14) if fp(bitmask, i))
    sock_abs = item.offset + sock_rel

    count = struct.unpack_from('<I', orig_blob, sock_abs + 1)[0]
    if count < 1 or count > 6:
        return False, blob, f"Unexpected socket list count: {count}"

    pos = sock_abs + 18
    elements = []
    for si in range(count):
        mask = orig_blob[pos + 2]
        ti = struct.unpack_from('<H', orig_blob, pos + 3)[0]
        sz = 32 if mask == 0x03 else 26 if mask == 0x00 else \
            18 + 4 + (2 if mask & 1 else 0) + (4 if mask & 2 else 0) + 4
        elements.append({'slot': si, 'start': pos, 'size': sz, 'mask': mask, 'type_index': ti})
        pos += sz

    validated: Dict[int, dict] = {}
    for slot_idx in target_slots:
        if slot_idx >= len(elements):
            return False, blob, f"Slot index {slot_idx} out of range (max {len(elements) - 1})"
        e = elements[slot_idx]
        if e['mask'] != expected_mask:
            return False, blob, (
                f"Slot {slot_idx + 1} has unexpected mask "
                f"(got 0x{e['mask']:02X}, expected 0x{expected_mask:02X})"
            )
        validated[slot_idx] = e

    if not validated:
        return False, blob, "No slots to process"

    first_idx = min(validated.keys())
    last_idx = max(validated.keys())
    in_range = [e for e in elements if first_idx <= e['slot'] <= last_idx]
    old_start = in_range[0]['start']
    old_end = in_range[-1]['start'] + in_range[-1]['size']

    new_elems = bytearray()
    cursor = old_start
    for e in in_range:
        if e['slot'] in validated:
            replacement = build_target_elem(e, cursor)
            new_elems += replacement
            cursor += len(replacement)
        else:
            chunk = orig_blob[e['start']: e['start'] + e['size']]
            new_elems += chunk
            cursor += e['size']

    delta = len(new_elems) - (old_end - old_start)
    if delta == 0:
        return False, blob, "No size change"

    new_blob = bytearray(orig_blob[:old_start]) + new_elems + bytearray(orig_blob[old_end:])

    fixed_po = 0
    for po_pos, old_val in offset_positions:
        if old_start <= po_pos < old_end:
            continue
        new_pos = po_pos + delta if po_pos >= old_end else po_pos
        if new_pos + 4 > len(new_blob):
            continue
        if old_val >= old_end:
            struct.pack_into('<I', new_blob, new_pos, old_val + delta)
            fixed_po += 1

    fixed_ts = 0
    for size_pos, payload_start in trailing_sizes:
        if old_start <= size_pos < old_end:
            continue
        if payload_start < old_end <= size_pos:
            new_size_pos = size_pos + delta
            if new_size_pos + 4 > len(new_blob):
                continue
            old_val = struct.unpack_from('<I', new_blob, new_size_pos)[0]
            struct.pack_into('<I', new_blob, new_size_pos, old_val + delta)
            fixed_ts += 1

    schema_end = schema_end_cached
    old_ss = struct.unpack_from('<I', new_blob, schema_end + 8)[0]
    struct.pack_into('<I', new_blob, schema_end + 8, old_ss + delta)

    equip_idx = next(
        (e.index for e in toc_entries_cached if e.data_offset <= item.offset < e.data_offset + e.data_size),
        None,
    )
    fixed_toc = 0
    for e in toc_entries_cached:
        if e.index == equip_idx:
            szp = e.entry_offset + 16
            old = struct.unpack_from('<I', new_blob, szp)[0]
            struct.pack_into('<I', new_blob, szp, old + delta)
        if e.data_offset >= old_end:
            struct.pack_into('<I', new_blob, e.entry_offset + 12, e.data_offset + delta)
            fixed_toc += 1

    valid_updated = False
    if fp(bitmask, 13):
        valid_sock_rel = sum(_ITEM_SOCKET_FIELD_SIZES[i] for i in range(13) if fp(bitmask, i))
        valid_sock_abs = item.offset + valid_sock_rel
        old_valid = new_blob[valid_sock_abs]
        new_blob[valid_sock_abs] = valid_count_fn(old_valid, len(validated))
        log.info("%s: _validSocketCount %d -> %d", fn_name, old_valid, new_blob[valid_sock_abs])
        valid_updated = True

    log.info(
        "%s: %d slot(s) in item %d, delta=%+d, po=%d ts=%d toc=%d valid_updated=%s",
        fn_name, len(validated), item.item_no, delta, fixed_po, fixed_ts, fixed_toc, valid_updated,
    )

    return True, bytes(new_blob), (
        f"{verb} {len(validated)} socket slot(s) — "
        f"{fixed_po} POs, {fixed_ts} trailing sizes, {fixed_toc} TOC entries updated"
    )


def fill_socket_slots(
    blob: bytearray,
    item: 'SaveItem',
    slot_gem_map: Dict[int, int],
    endurance: int = 0xFFFF,
    endurance_map: Optional[Dict[int, int]] = None,
) -> Tuple[bool, bytearray, str]:
    _ensure_desktop_path()

    def _build_filled(e: dict, cursor: int) -> bytes:
        gem_key = slot_gem_map[e['slot']]
        slot_endurance = endurance_map.get(e['slot'], endurance) if endurance_map else endurance
        elem = bytearray(32)
        struct.pack_into('<H', elem, 0, 1)
        elem[2] = 0x03
        struct.pack_into('<H', elem, 3, e['type_index'])
        struct.pack_into('<Q', elem, 6, 0xFFFFFFFFFFFFFFFF)
        struct.pack_into('<I', elem, 14, cursor + 18)
        struct.pack_into('<H', elem, 22, slot_endurance)
        struct.pack_into('<I', elem, 24, gem_key)
        struct.pack_into('<I', elem, 28, 10)
        return bytes(elem)

    return _splice_socket_elements(
        blob, item,
        target_slots=slot_gem_map,
        expected_mask=0x00,
        build_target_elem=_build_filled,
        valid_count_fn=lambda old, n: old,  # filling does not unlock slots
        fn_name="fill_socket_slots",
        verb="Filled",
    )


def clear_socket_slots(
    blob: bytearray,
    item: 'SaveItem',
    slot_indices: List[int],
) -> Tuple[bool, bytearray, str]:
    _ensure_desktop_path()

    def _build_empty(e: dict, cursor: int) -> bytes:
        elem = bytearray(26)
        struct.pack_into('<H', elem, 0, 1)
        elem[2] = 0x00
        struct.pack_into('<H', elem, 3, e['type_index'])
        struct.pack_into('<Q', elem, 6, 0xFFFFFFFFFFFFFFFF)
        struct.pack_into('<I', elem, 14, cursor + 18)
        struct.pack_into('<I', elem, 22, 4)
        return bytes(elem)

    return _splice_socket_elements(
        blob, item,
        target_slots={idx: None for idx in slot_indices},
        expected_mask=0x03,
        build_target_elem=_build_empty,
        valid_count_fn=lambda old, n: old,  # clearing does not lock slots
        fn_name="clear_socket_slots",
        verb="Cleared",
    )
