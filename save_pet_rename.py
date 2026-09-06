
import argparse
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from ben_save_decrypt import decrypt_save, encrypt_save


def parse_reflection_layout(data: bytes) -> dict:
    off = 0

    marker = struct.unpack_from('<H', data, off)[0]; off += 2
    assert marker == 0xFFFF, f"Expected 0xFFFF marker, got 0x{marker:X}"
    meta_ver = struct.unpack_from('<I', data, off)[0]; off += 4
    ref_hash = struct.unpack_from('<Q', data, off)[0]; off += 8
    ser_ver = struct.unpack_from('<I', data, off)[0]; off += 4
    type_count = struct.unpack_from('<H', data, off)[0]; off += 2

    types = []
    for ti in range(type_count):
        name_len = struct.unpack_from('<I', data, off)[0]; off += 4
        tname = data[off:off + name_len].decode('utf-8'); off += name_len
        prop_count = struct.unpack_from('<H', data, off)[0]; off += 2
        props = []
        for pi in range(prop_count):
            pname_len = struct.unpack_from('<I', data, off)[0]; off += 4
            pname = data[off:off + pname_len].decode('utf-8'); off += pname_len
            ptname_len = struct.unpack_from('<I', data, off)[0]; off += 4
            ptname = data[off:off + ptname_len].decode('utf-8'); off += ptname_len
            prop_type = struct.unpack_from('<H', data, off)[0]; off += 2
            fixed_size = struct.unpack_from('<H', data, off)[0]; off += 2
            flags = struct.unpack_from('<I', data, off)[0]; off += 4
            props.append({
                'name': pname, 'type_name': ptname,
                'prop_type': prop_type, 'fixed_size': fixed_size, 'flags': flags,
            })
        types.append({'name': tname, 'properties': props})

    name_count = struct.unpack_from('<I', data, off)[0]; off += 4
    for _ in range(name_count):
        slen = struct.unpack_from('<I', data, off)[0]; off += 4
        off += slen

    obj_count = struct.unpack_from('<I', data, off)[0]; off += 4
    end_offset_pos = off
    end_offset = struct.unpack_from('<I', data, off)[0]; off += 4

    info_table_pos = off
    infos = []
    for i in range(obj_count):
        rec_start = off
        ti2 = struct.unpack_from('<H', data, off)[0]; off += 2
        _unk1 = struct.unpack_from('<H', data, off)[0]; off += 2
        _unk2 = struct.unpack_from('<q', data, off)[0]; off += 8
        offset_field_pos = off
        obj_off = struct.unpack_from('<I', data, off)[0]; off += 4
        size_field_pos = off
        obj_sz = struct.unpack_from('<I', data, off)[0]; off += 4
        infos.append({
            'type_index': ti2, 'offset': obj_off, 'size': obj_sz,
            'offset_field_pos': offset_field_pos,
            'size_field_pos': size_field_pos,
        })

    return {
        'meta_ver': meta_ver, 'ser_ver': ser_ver,
        'types': types, 'type_count': type_count,
        'obj_count': obj_count,
        'end_offset_pos': end_offset_pos, 'end_offset': end_offset,
        'info_table_pos': info_table_pos,
        'infos': infos,
        'data_start': off,
    }


def find_mercenary_bitmap_positions(data: bytes, layout: dict) -> list:
    types = layout['types']
    ser_ver = layout['ser_ver']

    merc_save_ti = next(i for i, t in enumerate(types) if t['name'].startswith('MercenarySaveData'))
    merc_clan_ti = next(i for i, t in enumerate(types) if t['name'] == 'MercenaryClanSaveData')

    merc_save_type = types[merc_save_ti]
    merc_clan_type = types[merc_clan_ti]

    name_prop_idx = next(i for i, p in enumerate(merc_save_type['properties'])
                         if p['name'] == '_mercenaryName')

    clan_info = next(info for info in layout['infos'] if info['type_index'] == merc_clan_ti)
    clan_offset = clan_info['offset']

    off = clan_offset

    bmp_len = struct.unpack_from('<H', data, off)[0]; off += 2
    bmp = data[off:off + bmp_len]; off += bmp_len

    _unk0 = data[off]; off += 1

    no_tags = data[off]; off += 1
    if not no_tags and ser_ver >= 6:
        tag_count = struct.unpack_from('<H', data, off)[0]; off += 2
        for _ in range(tag_count):
            off += 2
            tag_len = struct.unpack_from('<I', data, off)[0]; off += 4
            off += tag_len


    clan_props = merc_clan_type['properties']

    results = []

    for pi, prop in enumerate(clan_props):
        bit_missing = ((bmp[pi // 8] >> (pi & 7)) & 1) == 0

        if ser_ver >= 9 and (prop['flags'] & ((1 << 7) | (1 << 1))):
            continue

        is_array = prop['prop_type'] in (3, 6, 7, 9, 10)
        if not is_array and bit_missing:
            continue

        if prop['name'] == '_mercenaryDataList':
            results = _parse_mercenary_array(data, off, layout, merc_save_type, name_prop_idx)
            break

        off = _skip_property_value(data, off, prop, layout)

    return results


def _parse_mercenary_array(data, off, layout, merc_type, name_prop_idx):
    ser_ver = layout['ser_ver']
    results = []

    if ser_ver >= 0xF:
        empty_flag = data[off]; off += 1
        if empty_flag == 1:
            return results

    array_count = struct.unpack_from('<I', data, off)[0]; off += 4

    has_named = False
    if ser_ver >= 0xE:
        has_named = data[off] == 1; off += 1

    _unk0 = struct.unpack_from('<q', data, off)[0]; off += 8

    unk_count = struct.unpack_from('<i', data, off)[0]; off += 4
    if unk_count > 0:
        off += unk_count * 8
        if has_named:
            off += unk_count * 4

    for idx in range(array_count):
        merc_info = _parse_single_mercenary(data, off, layout, merc_type, name_prop_idx)
        results.append(merc_info)
        off = merc_info['object_end']

    return results


def _parse_single_mercenary(data, off, layout, merc_type, name_prop_idx):
    ser_ver = layout['ser_ver']
    props = merc_type['properties']

    bitmap_pos = off
    bmp_len = struct.unpack_from('<H', data, off)[0]; off += 2
    bmp = bytearray(data[off:off + bmp_len]); off += bmp_len

    _type_idx = struct.unpack_from('<H', data, off)[0]; off += 2

    _unk1 = data[off]; off += 1
    _unk0 = struct.unpack_from('<q', data, off)[0]; off += 8

    value_offset_pos = off
    value_offset = struct.unpack_from('<I', data, off)[0]; off += 4

    off = value_offset

    _unk_a = data[off]; off += 1

    no_tags = data[off]; off += 1
    if not no_tags and ser_ver >= 6:
        tag_count = struct.unpack_from('<H', data, off)[0]; off += 2
        for _ in range(tag_count):
            off += 2
            tag_len = struct.unpack_from('<I', data, off)[0]; off += 4
            off += tag_len

    name_bit = (bmp[name_prop_idx // 8] >> (name_prop_idx & 7)) & 1
    merc_no = None
    name_insert_pos = None
    name_current_value = None
    extra = {}

    _READ_FIELDS = {
        '_characterKey': '<I',
        '_lastSummoned': '<B',
        '_isMainMercenary': '<B',
        '_isDead': '<B',
        '_isHyosiMercenary': '<B',
        '_currentHp': '<q',
    }

    for pi, prop in enumerate(props):
        bit_missing = ((bmp[pi // 8] >> (pi & 7)) & 1) == 0

        if ser_ver >= 9 and (prop['flags'] & ((1 << 7) | (1 << 1))):
            continue

        is_array = prop['prop_type'] in (3, 6, 7, 9, 10)
        if not is_array and bit_missing:
            if pi == name_prop_idx:
                name_insert_pos = off
            continue

        if prop['name'] == '_mercenaryNo':
            merc_no = struct.unpack_from('<Q', data, off)[0]

        if prop['name'] in _READ_FIELDS and prop['prop_type'] in (0, 2):
            extra[prop['name']] = struct.unpack_from(_READ_FIELDS[prop['name']], data, off)[0]

        if prop['name'] == '_equipItemList':
            arr_off = off
            if ser_ver >= 0xF:
                if data[arr_off] == 1:
                    extra['_equip_count'] = 0
                else:
                    extra['_equip_count'] = struct.unpack_from('<I', data, arr_off + 1)[0]
            else:
                extra['_equip_count'] = struct.unpack_from('<I', data, arr_off)[0]

        if pi == name_prop_idx:
            str_len = struct.unpack_from('<I', data, off)[0]
            name_current_value = data[off + 4:off + 4 + str_len].decode('utf-8', errors='replace')
            name_insert_pos = off

        off = _skip_property_value(data, off, prop, layout)

    _obj_size = struct.unpack_from('<I', data, off)[0]
    obj_end = off + 4

    return {
        'mercenary_no': merc_no,
        'bitmap_pos': bitmap_pos,
        'bitmap_bytes': bytes(bmp),
        'bitmap_len': bmp_len,
        'name_bit_set': bool(name_bit),
        'name_prop_idx': name_prop_idx,
        'name_insert_pos': name_insert_pos,
        'name_current_value': name_current_value,
        'value_offset_pos': value_offset_pos,
        'object_end': obj_end,
        'extra': extra,
    }


def _skip_property_value(data, off, prop, layout):
    ser_ver = layout['ser_ver']
    pt = prop['prop_type']

    if pt == 0:
        return off + prop['fixed_size']
    elif pt == 1:
        str_len = struct.unpack_from('<I', data, off)[0]
        return off + 4 + str_len
    elif pt == 2:
        return off + prop['fixed_size']
    elif pt == 3:
        if ser_ver >= 0xF:
            if data[off] == 1:
                return off + 1
            off += 1
        count = struct.unpack_from('<I', data, off)[0]
        return off + 4 + count * prop['fixed_size']
    elif pt in (4,):
        return _skip_object(data, off, layout)
    elif pt == 5:
        flag = data[off]; off += 1
        if flag == 0:
            return off
        return _skip_object(data, off, layout)
    elif pt in (6, 7):
        if ser_ver >= 0xF:
            if data[off] == 1:
                return off + 1
            off += 1
        count = struct.unpack_from('<I', data, off)[0]; off += 4
        has_named = False
        if ser_ver >= 0xE:
            has_named = data[off] == 1; off += 1
        if ser_ver >= 0xB:
            off += 8
        elif ser_ver >= 8:
            off += 4
        elif ser_ver >= 4:
            off += 2
        if ser_ver >= 0xB:
            unk_count = struct.unpack_from('<i', data, off)[0]; off += 4
            if unk_count > 0:
                off += unk_count * 8
                if has_named:
                    off += unk_count * 4
        for _ in range(count):
            off = _skip_object(data, off, layout)
        return off
    elif pt == 10:
        if ser_ver >= 0xF:
            if data[off] == 1:
                return off + 1
            off += 1
        count = struct.unpack_from('<I', data, off)[0]; off += 4
        for _ in range(count):
            str_len = struct.unpack_from('<I', data, off)[0]; off += 4
            off += str_len * prop['fixed_size']
        return off
    else:
        raise ValueError(f"Unknown property type {pt}")


def _skip_object(data, off, layout):
    ser_ver = layout['ser_ver']
    types = layout['types']

    bmp_len = struct.unpack_from('<H', data, off)[0]; off += 2
    bmp = data[off:off + bmp_len]; off += bmp_len

    type_idx = struct.unpack_from('<H', data, off)[0]; off += 2

    if ser_ver >= 0xB:
        off += 1 + 8
    elif ser_ver >= 8:
        off += 4
    else:
        off += 2

    value_offset = struct.unpack_from('<I', data, off)[0]; off += 4
    off = value_offset

    type_info = types[type_idx]

    if ser_ver >= 0xA:
        off += 1

    if ser_ver >= 5:
        no_tags = data[off]; off += 1
        if not no_tags and ser_ver >= 6:
            tag_count = struct.unpack_from('<H', data, off)[0]; off += 2
            for _ in range(tag_count):
                off += 2
                tag_len = struct.unpack_from('<I', data, off)[0]; off += 4
                off += tag_len

    for pi, prop in enumerate(type_info['properties']):
        bit_missing = ((bmp[pi // 8] >> (pi & 7)) & 1) == 0
        if ser_ver >= 9 and (prop['flags'] & ((1 << 7) | (1 << 1))):
            continue
        is_array = prop['prop_type'] in (3, 6, 7, 9, 10)
        if not is_array and bit_missing:
            continue
        off = _skip_property_value(data, off, prop, layout)

    _size = struct.unpack_from('<I', data, off)[0]; off += 4
    return off


def patch_mercenary_name(data: bytearray, layout: dict, merc_info: dict, new_name: str) -> bytearray:
    name_bytes = new_name.encode('utf-8')
    name_prop_idx = merc_info['name_prop_idx']

    if merc_info['name_bit_set']:
        old_pos = merc_info['name_insert_pos']
        old_len = struct.unpack_from('<I', data, old_pos)[0]
        old_total = 4 + old_len
        new_total = 4 + len(name_bytes)
        delta = new_total - old_total

        new_field = struct.pack('<I', len(name_bytes)) + name_bytes
        data = bytearray(data[:old_pos]) + new_field + bytearray(data[old_pos + old_total:])

    else:
        insert_pos = merc_info['name_insert_pos']
        new_field = struct.pack('<I', len(name_bytes)) + name_bytes
        delta = len(new_field)

        bmp_pos = merc_info['bitmap_pos'] + 2
        byte_idx = name_prop_idx // 8
        bit_idx = name_prop_idx & 7
        data[bmp_pos + byte_idx] |= (1 << bit_idx)

        data = bytearray(data[:insert_pos]) + new_field + bytearray(data[insert_pos:])

    if delta == 0:
        return data

    insertion_point = merc_info['name_insert_pos']

    old_end = struct.unpack_from('<I', data, layout['end_offset_pos'])[0]
    struct.pack_into('<I', data, layout['end_offset_pos'], old_end + delta)

    for info in layout['infos']:
        pos = info['offset_field_pos']
        old_val = struct.unpack_from('<I', data, pos)[0]
        if old_val > insertion_point:
            struct.pack_into('<I', data, pos, old_val + delta)

        if info['offset'] <= insertion_point < info['offset'] + info['size']:
            size_pos = info['size_field_pos']
            old_size = struct.unpack_from('<I', data, size_pos)[0]
            struct.pack_into('<I', data, size_pos, old_size + delta)

    _fix_inline_offsets(data, layout, insertion_point, delta)

    return data


def _fix_inline_offsets(data: bytearray, layout: dict, insertion_point: int, delta: int):
    ser_ver = layout['ser_ver']
    types = layout['types']

    for info in layout['infos']:
        obj_off = struct.unpack_from('<I', data, info['offset_field_pos'])[0]
        type_info = types[info['type_index']]
        try:
            _fix_offsets_in_object_properties(data, obj_off, type_info, layout, insertion_point, delta, is_top_level=True)
        except Exception:
            pass


def _fix_offsets_in_object_properties(data, off, type_info, layout, insertion_point, delta, is_top_level=False):
    ser_ver = layout['ser_ver']
    types = layout['types']

    bmp_len = struct.unpack_from('<H', data, off)[0]; off += 2
    bmp = data[off:off + bmp_len]; off += bmp_len

    if not is_top_level:
        type_idx = struct.unpack_from('<H', data, off)[0]; off += 2
        if ser_ver >= 0xB:
            off += 1 + 8
        elif ser_ver >= 8:
            off += 4
        else:
            off += 2

        vo_pos = off
        vo = struct.unpack_from('<I', data, vo_pos)[0]
        if vo > insertion_point:
            struct.pack_into('<I', data, vo_pos, vo + delta)
            vo += delta
        off += 4
        off = vo

        type_info = types[type_idx]

    if ser_ver >= 0xA:
        off += 1
    if ser_ver >= 5:
        no_tags = data[off]; off += 1
        if not no_tags and ser_ver >= 6:
            tag_count = struct.unpack_from('<H', data, off)[0]; off += 2
            for _ in range(tag_count):
                off += 2
                tag_len = struct.unpack_from('<I', data, off)[0]; off += 4
                off += tag_len

    for pi, prop in enumerate(type_info['properties']):
        bit_missing = ((bmp[pi // 8] >> (pi & 7)) & 1) == 0
        if ser_ver >= 9 and (prop['flags'] & ((1 << 7) | (1 << 1))):
            continue
        is_array = prop['prop_type'] in (3, 6, 7, 9, 10)
        if not is_array and bit_missing:
            continue

        pt = prop['prop_type']
        if pt in (0, 1, 2):
            off = _skip_property_value(data, off, prop, layout)
        elif pt == 3:
            off = _skip_property_value(data, off, prop, layout)
        elif pt == 4:
            _fix_offsets_in_object_properties(data, off, None, layout, insertion_point, delta)
            off = _skip_object_adjusted(data, off, layout)
        elif pt == 5:
            flag = data[off]; off += 1
            if flag != 0:
                _fix_offsets_in_object_properties(data, off, None, layout, insertion_point, delta)
                off = _skip_object_adjusted(data, off, layout)
        elif pt in (6, 7):
            if ser_ver >= 0xF:
                if data[off] == 1:
                    off += 1
                    continue
                off += 1
            count = struct.unpack_from('<I', data, off)[0]; off += 4
            has_named = False
            if ser_ver >= 0xE:
                has_named = data[off] == 1; off += 1
            if ser_ver >= 0xB:
                off += 8
            elif ser_ver >= 8:
                off += 4
            elif ser_ver >= 4:
                off += 2
            if ser_ver >= 0xB:
                unk_count = struct.unpack_from('<i', data, off)[0]; off += 4
                if unk_count > 0:
                    off += unk_count * 8
                    if has_named:
                        off += unk_count * 4
            for _ in range(count):
                _fix_offsets_in_object_properties(data, off, None, layout, insertion_point, delta)
                off = _skip_object_adjusted(data, off, layout)
        elif pt == 10:
            off = _skip_property_value(data, off, prop, layout)
        else:
            off = _skip_property_value(data, off, prop, layout)


def _skip_object_adjusted(data, off, layout):
    return _skip_object(data, off, layout)


def clear_mercenary_name(data: bytearray, layout: dict, merc_info: dict) -> bytearray:
    if not merc_info['name_bit_set']:
        return data

    name_prop_idx = merc_info['name_prop_idx']
    pos = merc_info['name_insert_pos']
    old_len = struct.unpack_from('<I', data, pos)[0]
    old_total = 4 + old_len
    delta = -old_total

    bmp_pos = merc_info['bitmap_pos'] + 2
    byte_idx = name_prop_idx // 8
    bit_idx = name_prop_idx & 7
    data[bmp_pos + byte_idx] &= ~(1 << bit_idx)

    data = bytearray(data[:pos]) + bytearray(data[pos + old_total:])

    insertion_point = pos

    old_end = struct.unpack_from('<I', data, layout['end_offset_pos'])[0]
    struct.pack_into('<I', data, layout['end_offset_pos'], old_end + delta)

    for info in layout['infos']:
        fpos = info['offset_field_pos']
        old_val = struct.unpack_from('<I', data, fpos)[0]
        if old_val > insertion_point:
            struct.pack_into('<I', data, fpos, old_val + delta)

        if info['offset'] <= insertion_point < info['offset'] + info['size']:
            size_pos = info['size_field_pos']
            old_size = struct.unpack_from('<I', data, size_pos)[0]
            struct.pack_into('<I', data, size_pos, old_size + delta)

    _fix_inline_offsets(data, layout, insertion_point, delta)

    return data


def _merc_tags(m):
    ex = m.get('extra', {})
    tags = []
    if ex.get('_equip_count', '?') == 0:
        tags.append('ANIMAL')
    if ex.get('_lastSummoned') == 1:
        tags.append('ACTIVE')
    if ex.get('_isMainMercenary') == 1:
        tags.append('MAIN')
    if ex.get('_isDead') == 1:
        tags.append('DEAD')
    return tags


def _merc_name_display(m):
    return f'"{m["name_current_value"]}"' if m['name_bit_set'] else '<not set>'


def print_merc_list(mercs):
    print(f"\n{'='*90}")
    print("MERCENARY LIST")
    print(f"{'='*90}")
    for i, m in enumerate(mercs):
        ex = m.get('extra', {})
        tags = _merc_tags(m)
        tag_str = f"  [{', '.join(tags)}]" if tags else ""
        print(f"  [{i:2d}] MercNo={m['mercenary_no']:<6d}  charKey={ex.get('_characterKey', '?'):<6}  "
              f"name={_merc_name_display(m):<16s}  equip={ex.get('_equip_count', '?'):<3}  "
              f"summoned={ex.get('_lastSummoned', '?')}  "
              f"main={ex.get('_isMainMercenary', '?')}  dead={ex.get('_isDead', '?')}  "
              f"hp={ex.get('_currentHp', '?')}{tag_str}")


def _apply_renames(plaintext, mercs, pairs, hdr, input_path, output_path=None):
    patched = bytearray(plaintext)
    for no, name in pairs:
        layout = parse_reflection_layout(bytes(patched))
        mercs_now = find_mercenary_bitmap_positions(bytes(patched), layout)
        target = next(t for t in mercs_now if t['mercenary_no'] == no)
        patched = patch_mercenary_name(patched, layout, target, name)
        print(f"  MercNo={no} -> \"{name}\"")
    out = output_path or input_path.with_name(f"{input_path.stem}_renamed{input_path.suffix}")
    encrypted = encrypt_save(bytes(patched), version=hdr['version'])
    out.write_bytes(encrypted)
    print(f"\n  Wrote: {out} ({len(encrypted):,} bytes)")
    return patched


def _apply_clears(plaintext, mercs_to_clear, hdr, input_path, output_path=None, suffix="_cleared"):
    patched = bytearray(plaintext)
    for m in mercs_to_clear:
        layout = parse_reflection_layout(bytes(patched))
        mercs_now = find_mercenary_bitmap_positions(bytes(patched), layout)
        target = next(t for t in mercs_now if t['mercenary_no'] == m['mercenary_no'])
        if target['name_bit_set']:
            patched = clear_mercenary_name(patched, layout, target)
            print(f"  MercNo={m['mercenary_no']:<6d}  cleared (was \"{m['name_current_value']}\")")
    out = output_path or input_path.with_name(f"{input_path.stem}{suffix}{input_path.suffix}")
    encrypted = encrypt_save(bytes(patched), version=hdr['version'])
    out.write_bytes(encrypted)
    print(f"\n  Wrote: {out} ({len(encrypted):,} bytes)")
    return patched


def _ask_output_path(input_path, default_suffix):
    default = input_path.with_name(f"{input_path.stem}{default_suffix}{input_path.suffix}")
    print(f"\n  Default output: {default}")
    print(f"  Press Enter to accept, or type a path (use '!' to overwrite input file)")
    raw = input("  Output: ").strip()
    if raw == '!':
        return input_path
    if raw:
        return Path(raw)
    return default


def run_interactive(plaintext, hdr, mercs, input_path):
    import re

    print_merc_list(mercs)

    while True:
        has_labels = any(m['name_bit_set'] and re.match(r'^M_\d+$', m['name_current_value'] or '')
                         for m in mercs)
        has_any_names = any(m['name_bit_set'] for m in mercs)

        print(f"\n{'─'*50}")
        print("ACTIONS:")
        print("  1) Label all        — set every merc to M_<no> for identification")
        print("  2) Rename           — choose names for individual mercenaries")
        if has_labels:
            print("  3) Clean up labels  — remove M_<no> labels, keep intentional names")
        if has_any_names:
            print("  4) Clear all names  — remove ALL custom names")
        print("  q) Quit")
        print(f"{'─'*50}")

        choice = input("\nSelect an action: ").strip().lower()

        if choice == '1':
            print(f"\nLabeling all {len(mercs)} mercenaries...")
            patched = bytearray(plaintext)
            for i, m in enumerate(mercs):
                label = f"M_{m['mercenary_no']}"
                layout = parse_reflection_layout(bytes(patched))
                mercs_now = find_mercenary_bitmap_positions(bytes(patched), layout)
                target = next(t for t in mercs_now if t['mercenary_no'] == m['mercenary_no'])
                patched = patch_mercenary_name(patched, layout, target, label)
                print(f"  [{i:2d}] MercenaryNo={m['mercenary_no']:<6d} -> \"{label}\"")

            output_path = _ask_output_path(input_path, "_all_named")
            encrypted = encrypt_save(bytes(patched), version=hdr['version'])
            output_path.write_bytes(encrypted)
            print(f"\n  Wrote: {output_path} ({len(encrypted):,} bytes)")
            print("\nLoad this save in-game to see which M_xxx appears on which character.")
            break

        elif choice == '2':
            merc_by_no = {m['mercenary_no']: m for m in mercs}
            pairs = []
            print(f"\nEnter MercNo and new name. Type 'done' when finished.")
            print(f"{'─'*50}")
            while True:
                raw = input("  MercNo (or 'done'): ").strip()
                if raw.lower() == 'done':
                    break
                try:
                    no = int(raw)
                except ValueError:
                    print(f"    Invalid number: \"{raw}\"")
                    continue
                if no not in merc_by_no:
                    print(f"    MercNo {no} not found. Available: {sorted(merc_by_no.keys())}")
                    continue
                m = merc_by_no[no]
                tags = _merc_tags(m)
                tag_str = f" [{', '.join(tags)}]" if tags else ""
                print(f"    Current: {_merc_name_display(m)}{tag_str}")
                new_name = input("    New name: ").strip()
                if not new_name:
                    print("    Skipped.")
                    continue
                pairs.append((no, new_name))
                print(f"    OK: MercNo={no} -> \"{new_name}\"")

            if not pairs:
                print("\nNo renames entered.")
                continue

            print(f"\n{'─'*50}")
            print("SUMMARY — will apply these renames:")
            for no, name in pairs:
                print(f"  MercNo={no} -> \"{name}\"")

            confirm = input("\nProceed? [Y/n]: ").strip().lower()
            if confirm and confirm != 'y':
                print("Cancelled.")
                continue

            output_path = _ask_output_path(input_path, "_renamed")
            print()
            _apply_renames(plaintext, mercs, pairs, hdr, input_path, output_path)
            print("\nDone! Copy the output file to your save folder.")
            break

        elif choice == '3' and has_labels:
            pattern = re.compile(r'^M_\d+$')
            labeled = [m for m in mercs if m['name_bit_set'] and pattern.match(m['name_current_value'] or '')]
            kept = [m for m in mercs if m['name_bit_set'] and not pattern.match(m['name_current_value'] or '')]

            print(f"\nWill clear {len(labeled)} auto-labels.")
            if kept:
                print(f"Keeping {len(kept)} intentional names:")
                for m in kept:
                    print(f"    MercNo={m['mercenary_no']:<6d}  \"{m['name_current_value']}\"")

            confirm = input("\nProceed? [Y/n]: ").strip().lower()
            if confirm and confirm != 'y':
                print("Cancelled.")
                continue

            output_path = _ask_output_path(input_path, "_cleaned")
            print()
            _apply_clears(plaintext, labeled, hdr, input_path, output_path, suffix="_cleaned")
            break

        elif choice == '4' and has_any_names:
            named = [m for m in mercs if m['name_bit_set']]
            print(f"\nWill clear ALL {len(named)} custom names.")
            confirm = input("Proceed? [Y/n]: ").strip().lower()
            if confirm and confirm != 'y':
                print("Cancelled.")
                continue

            output_path = _ask_output_path(input_path, "_cleared")
            print()
            _apply_clears(plaintext, named, hdr, input_path, output_path)
            break

        elif choice == 'q':
            print("Bye!")
            break
        else:
            print("Invalid choice.")


def main():
    parser = argparse.ArgumentParser(
        description="Crimson Desert — Pet & Companion Rename Tool",
        epilog="Run with no action flags for interactive mode.",
    )
    parser.add_argument("input", help="Path to .save file")
    parser.add_argument("--list", action="store_true", help="List all mercenaries and exit")
    parser.add_argument("--rename", nargs=2, metavar=("MERC_NO", "NAME"),
                        help="Rename a single mercenary by MercenaryNo")
    parser.add_argument("--rename-multi", nargs='+', metavar="MERC_NO=NAME",
                        help="Rename multiple mercenaries, e.g. 1027=Kraken 615=Grendel")
    parser.add_argument("--rename-all", action="store_true",
                        help="Rename ALL mercenaries with traceable names (M_<no>)")
    parser.add_argument("--clear", nargs='+', metavar="MERC_NO", type=int,
                        help="Clear (remove) custom names from specific mercenaries")
    parser.add_argument("--clear-unnamed", action="store_true",
                        help="Clear names matching M_<number> pattern (from --rename-all labeling)")
    parser.add_argument("--clear-all", action="store_true",
                        help="Clear ALL custom mercenary names (restore to game defaults)")
    parser.add_argument("-o", "--output", help="Output path for patched .save file")
    args = parser.parse_args()

    input_path = Path(args.input)
    file_data = input_path.read_bytes()

    print("Decrypting save file...")
    hdr, plaintext = decrypt_save(file_data)
    print(f"  Decrypted: {len(plaintext):,} bytes")

    print("Parsing reflection layout...")
    layout = parse_reflection_layout(plaintext)
    print(f"  Types: {layout['type_count']}, Objects: {layout['obj_count']}")

    print("Locating mercenary data...")
    mercs = find_mercenary_bitmap_positions(plaintext, layout)
    print(f"  Found {len(mercs)} mercenaries")

    has_action = (args.rename or args.rename_multi or args.rename_all
                  or args.clear or args.clear_all or args.clear_unnamed or args.list)

    if not has_action:
        run_interactive(plaintext, hdr, mercs, input_path)
        return

    if args.list:
        print_merc_list(mercs)
        return

    output_path = Path(args.output) if args.output else None

    if args.clear_unnamed:
        import re
        pattern = re.compile(r'^M_\d+$')
        labeled = [m for m in mercs if m['name_bit_set'] and pattern.match(m['name_current_value'] or '')]
        if not labeled:
            print("\nNo M_<number> labels found. Nothing to clear.")
            return
        kept = [m for m in mercs if m['name_bit_set'] and not pattern.match(m['name_current_value'] or '')]
        print(f"\nClearing {len(labeled)} auto-labels (keeping {len(kept)} intentional names)...")
        _apply_clears(plaintext, labeled, hdr, input_path, output_path, suffix="_cleaned")
        if kept:
            print(f"  Kept:")
            for m in kept:
                print(f"    MercNo={m['mercenary_no']:<6d}  \"{m['name_current_value']}\"")

    elif args.clear_all:
        named = [m for m in mercs if m['name_bit_set']]
        if not named:
            print("\nNo mercenaries have custom names. Nothing to clear.")
            return
        print(f"\nClearing names from {len(named)} mercenaries...")
        _apply_clears(plaintext, named, hdr, input_path, output_path)

    elif args.clear:
        available = {m['mercenary_no'] for m in mercs}
        for no in args.clear:
            if no not in available:
                print(f"Error: MercenaryNo {no} not found. Available: {sorted(available)}", file=sys.stderr)
                sys.exit(1)
        to_clear = [m for m in mercs if m['mercenary_no'] in args.clear]
        _apply_clears(plaintext, to_clear, hdr, input_path, output_path)

    elif args.rename_all:
        pairs = [(m['mercenary_no'], f"M_{m['mercenary_no']}") for m in mercs]
        print(f"\nLabeling all {len(mercs)} mercenaries...")
        out = output_path or input_path.with_name(f"{input_path.stem}_all_named{input_path.suffix}")
        _apply_renames(plaintext, mercs, pairs, hdr, input_path, out)
        print("\nLoad this save in-game to see which M_xxx appears on which character.")

    elif args.rename_multi:
        pairs = []
        for item in args.rename_multi:
            if '=' not in item:
                print(f"Error: expected MERC_NO=NAME, got \"{item}\"", file=sys.stderr)
                sys.exit(1)
            no_str, name = item.split('=', 1)
            pairs.append((int(no_str), name))

        available = {m['mercenary_no'] for m in mercs}
        for no, name in pairs:
            if no not in available:
                print(f"Error: MercenaryNo {no} not found. Available: {sorted(available)}", file=sys.stderr)
                sys.exit(1)

        print(f"\nRenaming {len(pairs)} mercenaries...")
        _apply_renames(plaintext, mercs, pairs, hdr, input_path, output_path)
        print("\nDone! Copy this file to your save folder and test in-game.")

    elif args.rename:
        target_no = int(args.rename[0])
        new_name = args.rename[1]

        target = next((m for m in mercs if m['mercenary_no'] == target_no), None)
        if target is None:
            print(f"\nError: MercenaryNo {target_no} not found.", file=sys.stderr)
            print(f"Available: {[m['mercenary_no'] for m in mercs]}")
            sys.exit(1)

        print(f"\nRenaming MercenaryNo {target_no} to \"{new_name}\"...")
        _apply_renames(plaintext, mercs, [(target_no, new_name)], hdr, input_path, output_path)
        print("\nDone! Copy this file to your save folder and test in-game.")


if __name__ == "__main__":
    main()
