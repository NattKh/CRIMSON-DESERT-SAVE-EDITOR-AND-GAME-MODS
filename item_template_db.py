import json
import logging
import os
import struct
import sys
import gc

log = logging.getLogger(__name__)

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(_BASE_DIR, 'item_templates.json')
MASTER_PATH = os.path.join(_BASE_DIR, 'master_templates.json')
MASTER_URL = (
    "https://raw.githubusercontent.com/"
    "NattKh/CrimsonDesertCommunityItemMapping/main/templates/master_templates.json"
)


def _get_parser():
    desktop_dir = os.path.join(_BASE_DIR, 'desktopeditor')
    if not os.path.isdir(desktop_dir):
        desktop_dir = os.path.join(_BASE_DIR, 'Communitydump', 'desktopeditor')
    if desktop_dir not in sys.path:
        sys.path.insert(0, desktop_dir)
    import save_parser as sp
    return sp


_db_cache: dict = None

def load_db() -> dict:
    global _db_cache
    if _db_cache is not None:
        return _db_cache

    _db_cache = {}

    if os.path.isfile(MASTER_PATH):
        try:
            with open(MASTER_PATH, 'r', encoding='utf-8') as f:
                data = json.load(f)
            _db_cache = data.get('templates', data)
            log.info("Loaded %d templates from master_templates.json", len(_db_cache))
        except Exception as e:
            log.warning("Failed to load master_templates: %s", e)

    if os.path.isfile(DB_PATH):
        try:
            with open(DB_PATH, 'r', encoding='utf-8') as f:
                item_data = json.load(f)
            before = len(_db_cache)
            for k, v in item_data.items():
                if k not in _db_cache:
                    _db_cache[k] = v
            log.info("Merged %d from item_templates.json (%d new)",
                     len(item_data), len(_db_cache) - before)
        except Exception as e:
            log.warning("Failed to load item_templates: %s", e)

    if not _db_cache:
        log.info("No local templates found, downloading from GitHub...")
        if download_master_templates():
            return load_db.__wrapped__() if hasattr(load_db, '__wrapped__') else _reload_db()

    return _db_cache


def _reload_db() -> dict:
    global _db_cache
    _db_cache = None
    return load_db()


def download_master_templates() -> bool:
    try:
        from offline_network import urlopen, Request
        req = Request(MASTER_URL, headers={"User-Agent": "CrimsonSaveEditor/1.0"})
        with urlopen(req, timeout=30) as resp:
            raw = resp.read()
            data = json.loads(raw.decode('utf-8'))

        templates = data.get('templates', data)
        version = data.get('version', 0)
        total = data.get('total_items', len(templates))

        with open(MASTER_PATH, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)

        log.info("Downloaded %d templates (v%d) from GitHub", len(templates), version)
        return True
    except Exception as e:
        log.warning("GitHub download failed: %s", e)
        return False


def save_db(db: dict) -> None:
    global _db_cache
    with open(DB_PATH, 'w', encoding='utf-8') as f:
        json.dump(db, f, indent=2)
    _db_cache = db
    log.info("Saved %d templates to %s", len(db), DB_PATH)


def extract_items_from_parse_tree(result, raw: bytes, source_label: str = '') -> dict:
    templates = {}

    def scan_fields(fields, location=''):
        for f in fields:
            if not f.present:
                continue

            if (hasattr(f, 'child_type_name') and f.child_type_name == 'ItemSaveData'
                    and f.child_fields and f.start_offset < f.end_offset):
                _extract_one(f, location)

            if f.list_elements:
                for elem in f.list_elements:
                    if (hasattr(elem, 'child_type_name') and elem.child_type_name == 'ItemSaveData'
                            and elem.child_fields and elem.start_offset < elem.end_offset):
                        _extract_one(elem, location)
                    if elem.child_fields:
                        scan_fields(elem.child_fields, location)

            if f.child_fields:
                scan_fields(f.child_fields, location)

    def _extract_one(elem, location):
        item_key = 0
        item_no = 0
        stack = 0
        for cf in elem.child_fields:
            if cf.name == '_itemKey' and cf.present:
                item_key = struct.unpack_from('<I', raw, cf.start_offset)[0]
            if cf.name == '_itemNo' and cf.present:
                item_no = struct.unpack_from('<q', raw, cf.start_offset)[0]
            if cf.name == '_stackCount' and cf.present:
                stack = struct.unpack_from('<q', raw, cf.start_offset)[0]

        if item_key == 0:
            return

        entry_bytes = raw[elem.start_offset:elem.end_offset]
        mask = elem.child_mask_bytes.hex() if elem.child_mask_bytes else ''
        size = len(entry_bytes)

        key_str = str(item_key)

        if key_str in templates:
            existing_size = templates[key_str]['size']
            if size >= existing_size:
                return

        field_positions = {}
        for cf in elem.child_fields:
            if cf.present and cf.start_offset > 0:
                field_positions[cf.name] = {
                    'rel_offset': cf.start_offset - elem.start_offset,
                    'size': cf.end_offset - cf.start_offset,
                }

        templates[key_str] = {
            'hex': entry_bytes.hex(),
            'mask': mask,
            'size': size,
            'item_key': item_key,
            'item_no': item_no,
            'stack': stack,
            'location': location,
            'source': source_label,
            'field_positions': field_positions,
        }

    for obj in result['objects']:
        scan_fields(obj.fields, obj.class_name)

    return templates


def ingest_save(save_path: str, db: dict = None) -> dict:
    if db is None:
        db = load_db()

    from save_crypto import load_save_file
    sp = _get_parser()

    log.info("Loading %s...", save_path)
    sd = load_save_file(save_path)
    raw = bytes(sd.decompressed_blob)
    log.info("  %d bytes decompressed", len(raw))

    log.info("  Parsing...")
    result = sp.build_result_from_raw(raw, {'input_kind': 'raw_blob'})

    source = os.path.basename(os.path.dirname(save_path))
    templates = extract_items_from_parse_tree(result, raw, source)

    LOCATION_PRIORITY = {
        'StoreSaveData': 1,
        'InventorySaveData': 2,
        'MercenaryClanSaveData': 3,
        'EquipmentSaveData': 4,
        'FieldSaveData': 5,
    }

    def _template_priority(t):
        loc = t.get('location', '')
        return LOCATION_PRIORITY.get(loc, 99)

    new_count = 0
    updated_count = 0
    for key_str, template in templates.items():
        if key_str not in db:
            db[key_str] = template
            new_count += 1
        else:
            existing = db[key_str]
            new_prio = _template_priority(template)
            old_prio = _template_priority(existing)
            if new_prio < old_prio:
                db[key_str] = template
                updated_count += 1
            elif new_prio == old_prio and template['size'] < existing['size']:
                db[key_str] = template
                updated_count += 1

    log.info("  Found %d items: %d new, %d updated, %d total in DB",
             len(templates), new_count, updated_count, len(db))

    del result, raw, sd
    gc.collect()

    return db


def ingest_all_saves(saves_dir: str) -> dict:
    db = load_db()

    for slot_name in sorted(os.listdir(saves_dir)):
        slot_dir = os.path.join(saves_dir, slot_name)
        if not os.path.isdir(slot_dir):
            continue

        for fname in ['backups/save.save.PRISTINE.bak', 'save.save']:
            save_path = os.path.join(slot_dir, fname)
            if os.path.isfile(save_path):
                try:
                    db = ingest_save(save_path, db)
                except Exception as e:
                    log.warning("  Failed: %s", e)
                break

        gc.collect()

    save_db(db)
    return db


def get_template(item_key: int, db: dict = None) -> dict:
    if db is None:
        db = load_db()
    return db.get(str(item_key))


def build_item_from_template(template: dict, insert_at: int,
                              new_item_key: int, new_item_no: int,
                              new_stack: int = 1) -> bytes:
    entry = bytearray(bytes.fromhex(template['hex']))
    donor_start = 0
    donor_size = len(entry)

    fp = template.get('field_positions', {})

    if '_itemNo' in fp:
        off = fp['_itemNo']['rel_offset']
        struct.pack_into('<q', entry, off, new_item_no)

    if '_itemKey' in fp:
        off = fp['_itemKey']['rel_offset']
        old_key = struct.unpack_from('<I', entry, off)[0]
        struct.pack_into('<I', entry, off, new_item_key)

        if '_transferredItemKey' in fp:
            tk_off = fp['_transferredItemKey']['rel_offset']
            old_tk = struct.unpack_from('<I', entry, tk_off)[0]
            struct.pack_into('<I', entry, tk_off, new_item_key)

    if '_slotNo' in fp:
        off = fp['_slotNo']['rel_offset']
        struct.pack_into('<H', entry, off, 0)

    if '_stackCount' in fp:
        off = fp['_stackCount']['rel_offset']
        struct.pack_into('<q', entry, off, new_stack)

    mbc = struct.unpack_from('<H', entry, 0)[0]
    po_offset = 2 + mbc + 2 + 1 + 8
    original_entry_start = template.get('_original_start', 0)
    old_po = struct.unpack_from('<I', entry, po_offset)[0]
    new_po = insert_at + (po_offset + 4)
    struct.pack_into('<I', entry, po_offset, new_po)

    _SENT = b'\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF'

    for off in range(len(entry) - 15):
        m = struct.unpack_from('<H', entry, off)[0]
        if m < 1 or m > 8:
            continue
        s = off + 2 + m + 3
        if s + 12 > len(entry):
            continue
        if entry[s:s + 8] != _SENT:
            continue
        t = struct.unpack_from('<H', entry, off + 2 + m)[0]
        if t > 200 or entry[off + 2 + m + 2] != 0:
            continue
        pp = s + 8
        old_val = struct.unpack_from('<I', entry, pp)[0]
        wrapper_end_rel = off + 2 + m + 2 + 1 + 4 + 4 + 4
        struct.pack_into('<I', entry, pp, insert_at + wrapper_end_rel)

    for off in range(len(entry) - 18):
        if entry[off + 6:off + 14] == _SENT:
            pp = off + 14
            struct.pack_into('<I', entry, pp, insert_at + off + 18)

    return bytes(entry)


def main():
    import argparse
    logging.basicConfig(level=logging.INFO, format='%(message)s')

    parser = argparse.ArgumentParser(description="Item Template Database Builder")
    sub = parser.add_subparsers(dest='command')

    ing = sub.add_parser('ingest', help='Ingest items from a save file')
    ing.add_argument('save_path', help='Path to .save file or directory of slots')

    sub.add_parser('stats', help='Show database statistics')

    lk = sub.add_parser('lookup', help='Look up a specific item')
    lk.add_argument('item_key', type=int, help='Item key to look up')

    ia = sub.add_parser('ingest-all', help='Ingest all saves from the default directory')

    args = parser.parse_args()

    if args.command == 'ingest':
        if os.path.isdir(args.save_path):
            db = ingest_all_saves(args.save_path)
        else:
            db = ingest_save(args.save_path)
            save_db(db)
        print(f"Database: {len(db)} templates")

    elif args.command == 'ingest-all':
        saves_dir = r"C:\Users\Coding\AppData\Local\Pearl Abyss\CD\save\57173764"
        db = ingest_all_saves(saves_dir)
        print(f"Database: {len(db)} templates")

    elif args.command == 'stats':
        db = load_db()
        print(f"Templates: {len(db)}")
        masks = {}
        sizes = {}
        for k, v in db.items():
            m = v.get('mask', '')
            masks[m] = masks.get(m, 0) + 1
            s = v.get('size', 0)
            sizes[s] = sizes.get(s, 0) + 1
        print(f"\nMask distribution:")
        for m, c in sorted(masks.items(), key=lambda x: -x[1]):
            print(f"  {m}: {c}")
        print(f"\nSize distribution:")
        for s, c in sorted(sizes.items()):
            print(f"  {s}B: {c}")

    elif args.command == 'lookup':
        db = load_db()
        t = get_template(args.item_key, db)
        if t:
            print(f"Key: {t['item_key']}")
            print(f"Mask: {t['mask']}")
            print(f"Size: {t['size']}B")
            print(f"Source: {t.get('source', '?')}")
            print(f"Location: {t.get('location', '?')}")
            print(f"Fields: {list(t.get('field_positions', {}).keys())}")
        else:
            print(f"Not found: {args.item_key}")

    else:
        parser.print_help()


if __name__ == '__main__':
    main()
