from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import platform
import struct
import sys
import time
from typing import Optional

log = logging.getLogger(__name__)

_REPO = "NattKh/CrimsonDesertCommunityItemMapping"
_BRANCH = "main"
_MASTER_URL = f"https://raw.githubusercontent.com/{_REPO}/{_BRANCH}/templates/master_templates.json"
_API_BASE = f"https://api.github.com/repos/{_REPO}/contents"
_TOKEN = os.environ.get("GH_TOKEN", "")

_MY_DIR = os.path.dirname(os.path.abspath(__file__))
_LOCAL_MASTER = os.path.join(_MY_DIR, 'master_templates.json')
_LOCAL_DB = os.path.join(_MY_DIR, 'item_templates.json')

_SAVE_DIRS = [
    os.path.expandvars(r"%LOCALAPPDATA%\Pearl Abyss\CD\save"),
]


def _machine_id() -> str:
    raw = f"{platform.node()}-{platform.machine()}-{os.getlogin()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def download_master() -> dict:
    # OFFLINE BUILD: download removed — uses the locally cached master DB only.
    if os.path.isfile(_LOCAL_MASTER):
        with open(_LOCAL_MASTER, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {'version': 1, 'total_items': 0, 'templates': {}}


def load_local_master() -> dict:
    if os.path.isfile(_LOCAL_MASTER):
        with open(_LOCAL_MASTER, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {'version': 1, 'total_items': 0, 'templates': {}}


def find_all_saves() -> list:
    saves = []
    for base in _SAVE_DIRS:
        if not os.path.isdir(base):
            continue
        for user_dir in os.listdir(base):
            user_path = os.path.join(base, user_dir)
            if not os.path.isdir(user_path):
                continue
            for slot in os.listdir(user_path):
                slot_dir = os.path.join(user_path, slot)
                if not os.path.isdir(slot_dir):
                    continue
                for fname in ['backups/save.save.PRISTINE.bak', 'save.save']:
                    p = os.path.join(slot_dir, fname)
                    if os.path.isfile(p):
                        saves.append(p)
                        break
    return saves


def scan_save_for_templates(save_path: str) -> dict:
    try:
        from save_crypto import load_save_file
        from item_template_db import extract_items_from_parse_tree, _get_parser

        sp = _get_parser()
        sd = load_save_file(save_path)
        raw = bytes(sd.decompressed_blob)
        result = sp.build_result_from_raw(raw, {'input_kind': 'raw_blob'})

        slot_name = os.path.basename(os.path.dirname(save_path))
        templates = extract_items_from_parse_tree(result, raw, slot_name)

        clean = {}
        for key, t in templates.items():
            clean[key] = {
                'hex': t['hex'],
                'mask': t['mask'],
                'size': t['size'],
                'item_key': t['item_key'],
                'field_positions': t.get('field_positions', {}),
            }
        return clean
    except Exception as e:
        log.warning("Failed to scan %s: %s", save_path, e)
        return {}


def scan_all_saves() -> dict:
    saves = find_all_saves()
    log.info("Found %d save files to scan", len(saves))

    all_templates = {}
    for save_path in saves:
        try:
            templates = scan_save_for_templates(save_path)
            for key, t in templates.items():
                if key not in all_templates or t['size'] < all_templates[key]['size']:
                    all_templates[key] = t
        except Exception as e:
            log.warning("Error scanning %s: %s", save_path, e)

    log.info("Scanned %d saves, found %d unique templates", len(saves), len(all_templates))
    return all_templates


def find_new_templates(local: dict, master: dict) -> dict:
    master_templates = master.get('templates', {})
    new = {}
    for key, t in local.items():
        if key not in master_templates:
            new[key] = t
        elif t['size'] < master_templates[key]['size']:
            new[key] = t
    return new


def upload_contribution(new_templates: dict) -> tuple[bool, str]:
    # OFFLINE BUILD: community upload removed.
    return False, "Online sync removed (offline build)"


def get_sync_status() -> dict:
    master = load_local_master()
    master_count = len(master.get('templates', {}))

    local_count = 0
    if os.path.isfile(_LOCAL_DB):
        with open(_LOCAL_DB, 'r', encoding='utf-8') as f:
            local_db = json.load(f)
            local_count = len(local_db)

    new_count = 0
    if local_count > 0 and master_count > 0:
        local_keys = set(str(k) for k in local_db.keys()) if local_count > 0 else set()
        master_keys = set(master.get('templates', {}).keys())
        new_count = len(local_keys - master_keys)

    coverage = (master_count / 5993) * 100 if master_count > 0 else 0

    return {
        'master_count': master_count,
        'local_count': local_count,
        'new_count': new_count,
        'coverage_pct': round(coverage, 1),
        'total_game_items': 5993,
    }


def full_sync(progress_callback=None) -> str:
    if progress_callback:
        progress_callback("Downloading master template database...")

    master = download_master()
    master_count = len(master.get('templates', {}))

    if progress_callback:
        progress_callback(f"Master DB: {master_count} templates. Scanning saves...")

    local = scan_all_saves()

    if progress_callback:
        progress_callback(f"Found {len(local)} local templates. Checking for new...")

    new = find_new_templates(local, master)

    if not new:
        msg = f"All {len(local)} local templates already in master DB ({master_count} total)."
        if progress_callback:
            progress_callback(msg)
        return msg

    if progress_callback:
        progress_callback(f"Uploading {len(new)} new templates...")

    ok, msg = upload_contribution(new)

    if ok:
        master_templates = master.get('templates', {})
        master_templates.update(new)
        master['templates'] = master_templates
        master['total_items'] = len(master_templates)
        with open(_LOCAL_MASTER, 'w', encoding='utf-8') as f:
            json.dump(master, f, separators=(',', ':'))

    return msg


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(message)s')

    import argparse
    parser = argparse.ArgumentParser(description="Template Sync")
    parser.add_argument('command', choices=['status', 'sync', 'download', 'scan'],
                        help='Command to run')
    args = parser.parse_args()

    if args.command == 'status':
        status = get_sync_status()
        print(f"Master DB:    {status['master_count']} templates")
        print(f"Local:        {status['local_count']} templates")
        print(f"New to share: {status['new_count']}")
        print(f"Coverage:     {status['coverage_pct']}% of {status['total_game_items']} game items")

    elif args.command == 'download':
        master = download_master()
        print(f"Downloaded: {len(master.get('templates', {}))} templates")

    elif args.command == 'scan':
        templates = scan_all_saves()
        print(f"Scanned: {len(templates)} unique templates")

    elif args.command == 'sync':
        msg = full_sync(progress_callback=print)
        print(f"\n{msg}")
