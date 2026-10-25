from __future__ import annotations

import json
import logging
import os
import sys
from typing import Callable, Optional

log = logging.getLogger(__name__)

_SOURCE_DIR = os.path.dirname(os.path.abspath(__file__))
_EXTERNAL_DIR = (
    os.path.dirname(os.path.abspath(sys.executable))
    if getattr(sys, "frozen", False)
    else _SOURCE_DIR
)
_BUNDLE_DIR = getattr(sys, "_MEIPASS", _SOURCE_DIR)
_LOCAL_MASTER = os.path.join(_EXTERNAL_DIR, "master_templates.json")
_BUNDLED_MASTER = os.path.join(_BUNDLE_DIR, "master_templates.json")
_LOCAL_DB = os.path.join(_EXTERNAL_DIR, "item_templates.json")

_SAVE_DIRS = [
    os.path.expandvars(r"%LOCALAPPDATA%\Pearl Abyss\CD\save"),
]


def _empty_master() -> dict:
    return {"version": 1, "total_items": 0, "templates": {}}


def _read_master(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as source:
        data = json.load(source)
    if not isinstance(data, dict):
        raise ValueError("Template file must contain a JSON object.")
    templates = data.get("templates", data)
    if not isinstance(templates, dict):
        raise ValueError("Template file has no valid templates object.")
    if "templates" not in data:
        data = {"version": 1, "total_items": len(templates), "templates": templates}
    return data


def load_local_master() -> dict:
    """Load an external master file first, then the bundled read-only copy."""
    for path in (_LOCAL_MASTER, _BUNDLED_MASTER):
        if os.path.isfile(path):
            try:
                return _read_master(path)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                log.warning("Could not load local template file %s: %s", path, exc)
    return _empty_master()


def import_master(source_path: str) -> tuple[bool, str]:
    """Validate and install a user-selected local master-template JSON file."""
    try:
        data = _read_master(source_path)
        templates = data.get("templates", {})
        with open(_LOCAL_MASTER, "w", encoding="utf-8") as destination:
            json.dump(data, destination, indent=2, ensure_ascii=False)
        return True, f"Imported {len(templates)} templates from a local file."
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return False, f"Could not import template database: {exc}"


def find_all_saves() -> list[str]:
    saves: list[str] = []
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
                for relative_name in ("backups/save.save.PRISTINE.bak", "save.save"):
                    candidate = os.path.join(slot_dir, relative_name)
                    if os.path.isfile(candidate):
                        saves.append(candidate)
                        break
    return saves


def scan_save_for_templates(save_path: str) -> dict:
    try:
        from save_crypto import load_save_file
        from item_template_db import extract_items_from_parse_tree, _get_parser

        parser = _get_parser()
        save_data = load_save_file(save_path)
        raw = bytes(save_data.decompressed_blob)
        result = parser.build_result_from_raw(raw, {"input_kind": "raw_blob"})
        slot_name = os.path.basename(os.path.dirname(save_path))
        templates = extract_items_from_parse_tree(result, raw, slot_name)

        clean: dict = {}
        for key, template in templates.items():
            clean[key] = {
                "hex": template["hex"],
                "mask": template["mask"],
                "size": template["size"],
                "item_key": template["item_key"],
                "field_positions": template.get("field_positions", {}),
            }
        return clean
    except Exception as exc:
        log.warning("Failed to scan %s: %s", save_path, exc)
        return {}


def scan_all_saves() -> dict:
    saves = find_all_saves()
    all_templates: dict = {}
    for save_path in saves:
        templates = scan_save_for_templates(save_path)
        for key, template in templates.items():
            if key not in all_templates or template["size"] < all_templates[key]["size"]:
                all_templates[key] = template
    log.info("Scanned %d saves and found %d unique templates", len(saves), len(all_templates))
    return all_templates


def find_new_templates(local: dict, master: dict) -> dict:
    master_templates = master.get("templates", {})
    return {
        key: template
        for key, template in local.items()
        if key not in master_templates
        or template["size"] < master_templates[key].get("size", 2**31)
    }


def export_contribution(new_templates: dict, destination_path: str) -> tuple[bool, str]:
    """Export discoveries for manual sharing; this function performs no network I/O."""
    if not new_templates:
        return True, "No templates are missing from the current local master database."
    contribution = {
        "version": 1,
        "count": len(new_templates),
        "templates": new_templates,
    }
    try:
        with open(destination_path, "w", encoding="utf-8") as destination:
            json.dump(contribution, destination, indent=2, ensure_ascii=False)
        return True, f"Exported {len(new_templates)} templates to {destination_path}"
    except OSError as exc:
        return False, f"Could not export templates: {exc}"


def get_sync_status() -> dict:
    master = load_local_master()
    master_count = len(master.get("templates", {}))

    local_db: dict = {}
    if os.path.isfile(_LOCAL_DB):
        try:
            with open(_LOCAL_DB, "r", encoding="utf-8") as source:
                loaded = json.load(source)
            if isinstance(loaded, dict):
                local_db = loaded
        except (OSError, json.JSONDecodeError):
            pass

    master_keys = set(master.get("templates", {}))
    local_keys = set(str(key) for key in local_db)
    total_game_items = 6813
    return {
        "master_count": master_count,
        "local_count": len(local_db),
        "new_count": len(local_keys - master_keys),
        "coverage_pct": round((master_count / total_game_items) * 100, 1) if master_count else 0,
        "total_game_items": total_game_items,
    }


def full_sync(progress_callback: Optional[Callable[[str], None]] = None) -> str:
    """Compatibility entry point: scan local saves and write only the local DB."""
    if progress_callback:
        progress_callback("Scanning local saves for item templates...")
    local = scan_all_saves()
    from item_template_db import save_db
    save_db(local)
    missing = find_new_templates(local, load_local_master())
    message = (
        f"Local scan complete: {len(local)} templates found; "
        f"{len(missing)} are not in the installed master database."
    )
    if progress_callback:
        progress_callback(message)
    return message


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    import argparse

    argument_parser = argparse.ArgumentParser(description="Local template tools")
    argument_parser.add_argument("command", choices=["status", "scan"])
    arguments = argument_parser.parse_args()
    if arguments.command == "status":
        print(json.dumps(get_sync_status(), indent=2))
    else:
        print(full_sync(progress_callback=print))
