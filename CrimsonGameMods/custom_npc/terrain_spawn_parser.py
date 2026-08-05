import os as _os, sys as _sys  # noqa: E402 -- make crimson_rs (parent dir) importable
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import logging
import struct
import json
import sys
import os

log = logging.getLogger(__name__)

DMM_TABLE_NAME = 'terrain_region_auto_spawn_info'
DMM_POOL_TABLE = 'spawning_pool_auto_spawn_info'
DMM_FACTIONSPAWN_TABLE = 'faction_node_spawn_info'


def parse_all_dmm(pabgb: bytes, pabgh: bytes, table_name: str = DMM_TABLE_NAME):
    try:
        import dmm_parser
        return dmm_parser.parse_table(table_name, pabgb, pabgh)
    except Exception:
        return None


def serialize_all_dmm(items: list, table_name: str = DMM_TABLE_NAME) -> bytes | None:
    try:
        import dmm_parser
        return bytes(dmm_parser.serialize_table(table_name, items))
    except Exception:
        return None


def _u32(D, p):
    return struct.unpack_from('<I', D, p)[0], p + 4

def _u16(D, p):
    return struct.unpack_from('<H', D, p)[0], p + 2

def _u8(D, p):
    return D[p], p + 1

def _f32(D, p):
    return struct.unpack_from('<f', D, p)[0], p + 4

def _skip_cstring(D, p):
    slen, p = _u32(D, p)
    if slen > 50000: return None, -1
    return D[p:p+slen].decode('utf-8', errors='replace'), p + slen

def _read_cstring(D, p):
    slen, p = _u32(D, p)
    if slen > 50000: return '', -1
    return D[p:p+slen].decode('utf-8', errors='replace'), p + slen


def _skip_cstring_array(D, p):
    count, p = _u32(D, p)
    if count > 10000: return -1
    for _ in range(count):
        slen, p = _u32(D, p)
        if slen > 50000: return -1
        p += slen
    return p


def _skip_key_lookup_array_u16(D, p):
    count, p = _u32(D, p)
    if count > 100000: return -1
    return p + count * 2


def _skip_key_lookup_array_u32(D, p):
    count, p = _u32(D, p)
    if count > 100000: return -1
    return p + count * 4


def _skip_byte_array(D, p):
    count, p = _u32(D, p)
    if count > 100000: return -1
    return p + count * 1


def parse_character_spawn_list(D, p):
    count, p = _u32(D, p)
    if count > 10000: return [], -1
    chars = []
    for _ in range(count):
        char_key, _ = _u32(D, p)
        chars.append(char_key)
        p += 14
    return chars, p


def parse_party_data(D, p, end):
    party = {}

    chars, p = parse_character_spawn_list(D, p)
    if p < 0: return None, -1
    party['characters'] = chars
    party['character_count'] = len(chars)

    party['spawn_data_name_key'], p = _u32(D, p)

    party['enum_1341'], p = _u32(D, p)
    party['enum_1338'], p = _u32(D, p)
    party['enum_1335'], p = _u32(D, p)

    party['sequencer_spawn_key'], p = _u32(D, p)

    party['spawn_reason'], p = _u32(D, p)

    party['spawn_rate'], p = _f32(D, p)
    party['spawn_rate_offset'] = p - 4

    party['min_water_depth'], p = _f32(D, p)

    party['max_water_depth'], p = _f32(D, p)

    party['color_r'], p = _f32(D, p)
    party['color_g'], p = _f32(D, p)
    party['color_b'], p = _f32(D, p)
    party['color_a'], p = _f32(D, p)

    party['is_duplicatable'], p = _u8(D, p)
    party['is_party_same_team'], p = _u8(D, p)
    party['is_faction_sequencer'], p = _u8(D, p)

    party['spawn_percent'] = struct.unpack_from('<d', D, p)[0]
    party['spawn_percent_offset'] = p
    p += 8

    return party, p


def parse_target_data(D, p, end):
    target = {}

    party_count, p = _u32(D, p)
    if party_count > 1000: return None, -1
    target['party_count'] = party_count
    target['parties'] = []

    for _ in range(party_count):
        party, p = parse_party_data(D, p, end)
        if p < 0 or party is None: return None, -1
        target['parties'].append(party)

    p = _skip_key_lookup_array_u16(D, p)
    if p < 0: return None, -1

    p = _skip_key_lookup_array_u16(D, p)
    if p < 0: return None, -1

    p = _skip_key_lookup_array_u32(D, p)
    if p < 0: return None, -1

    p = _skip_key_lookup_array_u32(D, p)
    if p < 0: return None, -1

    target['spawn_limit_raw'], p = _u32(D, p)
    target['spawn_limit_offset'] = p - 4

    target['meters_per_spawn_raw'], p = _u32(D, p)
    target['meters_per_spawn_offset'] = p - 4
    target['meters_per_spawn'] = struct.unpack_from('<f', D, p - 4)[0]

    target['field_8'], p = _u32(D, p)
    target['field_9'], p = _u32(D, p)

    target['field_10'], p = _u32(D, p)
    target['field_11'], p = _u32(D, p)

    target['indoor_type'], p = _u8(D, p)
    target['stage_category'], p = _u8(D, p)
    target['time_begin'], p = _u8(D, p)
    target['time_end'], p = _u8(D, p)
    target['flag_5'], p = _u8(D, p)
    target['tail_u16'], p = _u16(D, p)

    return target, p


def parse_terrain_entry(D, eoff, end):
    p = eoff
    entry = {}

    try:
        entry['key'], p = _u32(D, p)

        entry['name'], p = _read_cstring(D, p)
        if p < 0: return None

        entry['is_blocked'], p = _u8(D, p)

        p = _skip_byte_array(D, p)
        if p < 0: return None

        p = _skip_cstring_array(D, p)
        if p < 0: return None

        p = _skip_cstring_array(D, p)
        if p < 0: return None

        p = _skip_key_lookup_array_u16(D, p)
        if p < 0: return None

        p = _skip_key_lookup_array_u16(D, p)
        if p < 0: return None

        p = _skip_key_lookup_array_u32(D, p)
        if p < 0: return None

        p = _skip_key_lookup_array_u32(D, p)
        if p < 0: return None

        spawn_count, p = _u32(D, p)
        if spawn_count > 1000: return None
        entry['spawn_count'] = spawn_count
        entry['targets'] = []

        for _ in range(spawn_count):
            target, p = parse_target_data(D, p, end)
            if p < 0 or target is None:
                entry['parse_error'] = True
                return entry
            entry['targets'].append(target)

        entry['parse_complete'] = True
        return entry

    except (struct.error, IndexError) as e:
        entry['parse_error'] = str(e)
        return entry


def parse_pabgh(G):
    c16 = struct.unpack_from('<H', G, 0)[0]
    if 2 + c16 * 8 == len(G):
        idx_start, count = 2, c16
    else:
        count = struct.unpack_from('<I', G, 0)[0]
        idx_start = 4
    entries = []
    for i in range(count):
        pos = idx_start + i * 8
        if pos + 8 > len(G): break
        entries.append((struct.unpack_from('<I', G, pos)[0], struct.unpack_from('<I', G, pos + 4)[0]))
    return entries


def parse_all(pabgb_path, pabgh_path):
    with open(pabgb_path, 'rb') as f: D = f.read()
    with open(pabgh_path, 'rb') as f: G = f.read()

    idx = parse_pabgh(G)
    sorted_offs = sorted(set(off for _, off in idx))

    entries = []
    failures = 0

    for key, eoff in idx:
        bi = sorted_offs.index(eoff)
        end = sorted_offs[bi + 1] if bi + 1 < len(sorted_offs) else len(D)
        entry = parse_terrain_entry(D, eoff, end)
        if entry and not entry.get('parse_error'):
            entries.append(entry)
        else:
            failures += 1
            if entry:
                entries.append(entry)

    return entries, failures, D


def summarize(entries):
    total_targets = sum(e.get('spawn_count', 0) for e in entries)
    total_parties = sum(
        sum(t.get('party_count', 0) for t in e.get('targets', []))
        for e in entries
    )
    total_chars = sum(
        sum(len(p.get('characters', [])) for t in e.get('targets', []) for p in t.get('parties', []))
        for e in entries
    )

    print(f"Regions: {len(entries)}")
    print(f"Total spawn targets: {total_targets}")
    print(f"Total spawn parties: {total_parties}")
    print(f"Total character refs: {total_chars}")

    rates = []
    for e in entries:
        for t in e.get('targets', []):
            for p in t.get('parties', []):
                rates.append(p.get('spawn_rate', 0))

    if rates:
        from collections import Counter
        rate_counts = Counter(f"{r:.2f}" for r in rates)
        print(f"\nSpawn rates distribution:")
        for rate, cnt in rate_counts.most_common(10):
            print(f"  {rate}: {cnt} parties")

    print(f"\nRegions with most spawn targets:")
    for e in sorted(entries, key=lambda x: x.get('spawn_count', 0), reverse=True)[:10]:
        print(f"  {e['name']}: {e.get('spawn_count', 0)} targets, "
              f"{sum(t.get('party_count', 0) for t in e.get('targets', []))} parties")


def find_spawn_rates_by_signature(D):
    target = struct.pack('<f', 1.0)
    zero12 = b'\x00' * 12
    positions = []
    for i in range(12, len(D) - 3, 4):
        if D[i-12:i] == zero12:
            val = struct.unpack_from('<f', D, i)[0]
            if 0.0 < val <= 100.0:
                positions.append((i, val))
    return positions


def find_rates_per_entry(D, G):
    idx = parse_pabgh(G)
    idx_sorted = sorted(idx, key=lambda x: x[1])
    all_rates = find_spawn_rates_by_signature(D)

    results = []
    for i, (key, off) in enumerate(idx_sorted):
        next_off = idx_sorted[i + 1][1] if i + 1 < len(idx_sorted) else len(D)
        k = struct.unpack_from('<I', D, off)[0]
        slen = struct.unpack_from('<I', D, off + 4)[0]
        name = D[off + 8:off + 8 + slen].decode('utf-8', errors='replace') if slen < 500 else '?'

        rates_in_entry = [(p, v) for p, v in all_rates if off <= p < next_off]
        results.append({
            'key': k,
            'name': name,
            'offset': off,
            'end': next_off,
            'rates': rates_in_entry,
        })

    return results


def get_verified_rate_offsets(D, G):
    entries, failures, _ = parse_all_from_bytes(D, G)
    verified = []
    for e in entries:
        if not e.get('parse_complete'):
            continue
        for t in e.get('targets', []):
            for p in t.get('parties', []):
                off = p.get('spawn_rate_offset', -1)
                if off > 0:
                    actual = struct.unpack_from('<f', D, off)[0]
                    if 0.0 <= actual <= 100.0:
                        verified.append((off, actual, e.get('name', '')))
    return verified


def parse_all_from_bytes(D, G):
    idx = parse_pabgh(G)
    sorted_offs = sorted(set(off for _, off in idx))
    entries = []
    failures = 0
    for key, eoff in idx:
        bi = sorted_offs.index(eoff)
        end = sorted_offs[bi + 1] if bi + 1 < len(sorted_offs) else len(D)
        entry = parse_terrain_entry(D, eoff, end)
        if entry and not entry.get('parse_error'):
            entries.append(entry)
        else:
            failures += 1
            if entry:
                entries.append(entry)
    return entries, failures, D


def multiply_spawn_rates(D, G, multiplier):
    verified = get_verified_rate_offsets(bytes(D), G)
    count = 0
    for offset, current, name in verified:
        base = current if current > 0.001 else 1.0
        new_val = min(base * multiplier, 20.0)
        struct.pack_into('<f', D, offset, new_val)
        count += 1
    return count


def parse_spawningpool_entry(D, eoff, end):
    p = eoff
    entry = {}
    try:
        entry['key'], p = _u32(D, p)
        entry['name'], p = _read_cstring(D, p)
        if p < 0: return None
        entry['is_blocked'], p = _u8(D, p)

        spawn_count, p = _u32(D, p)
        if spawn_count > 1000: return None
        entry['spawn_count'] = spawn_count
        entry['targets'] = []
        for _ in range(spawn_count):
            target, p = parse_target_data(D, p, end)
            if p < 0 or target is None:
                entry['parse_error'] = True
                return entry
            entry['targets'].append(target)

        p = _skip_key_lookup_array_u32(D, p)
        if p < 0: return None

        _, p = _read_cstring(D, p)
        if p < 0: return None

        entry['pool_type'], p = _u8(D, p)
        entry['outer_radius'], p = _f32(D, p)
        entry['inner_radius'], p = _f32(D, p)
        entry['safety_distance'], p = _f32(D, p)

        entry['parse_complete'] = True
        return entry
    except (struct.error, IndexError) as e:
        entry['parse_error'] = str(e)
        return entry


def parse_spawningpool_all(pabgb_path_or_bytes, pabgh_path_or_bytes):
    if isinstance(pabgb_path_or_bytes, (bytes, bytearray)):
        D = bytes(pabgb_path_or_bytes)
        G = bytes(pabgh_path_or_bytes)
    else:
        with open(pabgb_path_or_bytes, 'rb') as f: D = f.read()
        with open(pabgh_path_or_bytes, 'rb') as f: G = f.read()

    idx = parse_pabgh(G)
    sorted_offs = sorted(set(off for _, off in idx))
    entries = []
    failures = 0
    for key, eoff in idx:
        bi = sorted_offs.index(eoff)
        end = sorted_offs[bi + 1] if bi + 1 < len(sorted_offs) else len(D)
        entry = parse_spawningpool_entry(D, eoff, end)
        if entry and not entry.get('parse_error'):
            entries.append(entry)
        else:
            failures += 1
            if entry: entries.append(entry)
    return entries, failures


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')

    try:
        import crimson_rs
        game_path = 'C:/Program Files (x86)/Steam/steamapps/common/Crimson Desert'
        dp = 'gamedata/binary__/client/bin'
        body = crimson_rs.extract_file(game_path, '0008', dp, 'terrainregionautospawninfo.pabgb')
        gh = crimson_rs.extract_file(game_path, '0008', dp, 'terrainregionautospawninfo.pabgh')
        import tempfile
        with tempfile.NamedTemporaryFile(suffix='.pabgb', delete=False) as f:
            f.write(body); pb = f.name
        with tempfile.NamedTemporaryFile(suffix='.pabgh', delete=False) as f:
            f.write(gh); pg = f.name
    except:
        EXT = 'C:/Users/Coding/CrimsonDesertModding/extractedpaz/0008_full'
        pb = f'{EXT}/terrainregionautospawninfo.pabgb'
        pg = f'{EXT}/terrainregionautospawninfo.pabgh'

    with open(pb, 'rb') as f: D = f.read()
    with open(pg, 'rb') as f: G = f.read()

    results = find_rates_per_entry(D, G)
    with_rates = [r for r in results if r['rates']]
    total_rates = sum(len(r['rates']) for r in results)

    print(f"Regions: {len(results)}")
    print(f"Regions with spawn rates: {len(with_rates)}")
    print(f"Total spawn rate positions: {total_rates}")
    print()

    categories = {'Terrain': [], 'SideWalk/Town': [], 'GimmickSummon': [],
                   'Air/Bird': [], 'Fish': [], 'Horse/Wagon': [], 'Other': []}
    for r in with_rates:
        n = r['name']
        if n.startswith('Fish'): categories['Fish'].append(r)
        elif 'SideWalk' in n or 'Town' in n: categories['SideWalk/Town'].append(r)
        elif 'GimmickSummon' in n: categories['GimmickSummon'].append(r)
        elif 'Air_Bird' in n or 'Air_Drone' in n: categories['Air/Bird'].append(r)
        elif 'Horse' in n or 'Wagon' in n: categories['Horse/Wagon'].append(r)
        elif any(x in n for x in ['South', 'North', 'Desert', 'Rain', 'Snow', 'Sea']): categories['Terrain'].append(r)
        else: categories['Other'].append(r)

    for cat, items in categories.items():
        if not items: continue
        rates_count = sum(len(r['rates']) for r in items)
        print(f"{cat}: {len(items)} regions, {rates_count} spawn rates")
        for r in items[:5]:
            print(f"  {r['name']}: {len(r['rates'])} rates")
        if len(items) > 5:
            print(f"  ... and {len(items)-5} more")
