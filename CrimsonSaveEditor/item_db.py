from __future__ import annotations

import json
import os
import datetime
import logging
import struct
from typing import Dict, List, Optional

from models import ItemInfo

log = logging.getLogger(__name__)

import sys as _sys
_exe_dir = os.path.dirname(os.path.abspath(_sys.executable)) if getattr(_sys, 'frozen', False) else os.path.dirname(os.path.abspath(__file__))
_bundle_dir = getattr(_sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))

SEARCH_PATHS = [
    os.path.join(_exe_dir, "item_names.json"),
    os.path.join(_bundle_dir, "item_names.json"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "item_names.json"),
    r"C:\Program Files (x86)\Steam\steamapps\common\Crimson Desert\bin64\CrimsonMods\item_names.json",
]


class ItemNameDB:

    def __init__(self) -> None:
        self.items: Dict[int, ItemInfo] = {}
        self.loaded_path: str = ""
        self.version: int = 0
        self.load_auto()

    def load_auto(self) -> str:
        for path in SEARCH_PATHS:
            if os.path.isfile(path):
                self.load(path)
                if self.items:
                    return path

        return ""

    def load(self, path: str) -> None:
        self.items.clear()
        self.loaded_path = path
        self.version = 0

        if not os.path.isfile(path):
            return

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return

        self.version = data.get("version", 0)
        for entry in data.get("items", []):
            key = entry.get("itemKey", 0)
            if key <= 0:
                continue
            self.items[key] = ItemInfo(
                item_key=key,
                name=entry.get("name", ""),
                internal_name=entry.get("internalName", ""),
                category=entry.get("category", "Misc"),
                max_stack=entry.get("maxStack", 0),
            )

    def apply_localization(self) -> int:
        try:
            from localization import get_language, _names_data
            if get_language() == 'en' or not _names_data:
                return 0
            items_map = _names_data.get('items', {})
            if not items_map:
                return 0
            count = 0
            for key, info in self.items.items():
                localized = items_map.get(str(key), '')
                if localized:
                    info.name = localized
                    count += 1
            return count
        except Exception:
            return 0

    def save(self, path: str | None = None) -> None:
        path = path or self.loaded_path
        if not path:
            return

        items_list = []
        for key in sorted(self.items.keys()):
            info = self.items[key]
            entry: dict = {"itemKey": key, "name": info.name}
            if info.internal_name:
                entry["internalName"] = info.internal_name
            entry["category"] = info.category
            if info.max_stack:
                entry["maxStack"] = info.max_stack
            items_list.append(entry)

        data = {"version": self.version, "items": items_list}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def get_name(self, key: int) -> str:
        info = self.items.get(key)
        if info and info.name:
            return info.name
        return f"Unknown ({key})"

    def get_category(self, key: int) -> str:
        info = self.items.get(key)
        return info.category if info else "Misc"

    def rename_item(self, key: int, new_name: str) -> None:
        if key in self.items:
            self.items[key].name = new_name
        else:
            self.items[key] = ItemInfo(
                item_key=key,
                name=new_name,
                category="Misc",
            )

    def get_all_sorted(self) -> List[ItemInfo]:
        return [self.items[k] for k in sorted(self.items.keys())]

    def get_internal_name(self, key: int) -> str:
        info = self.items.get(key)
        return info.internal_name if info else ""

    def search(self, query: str) -> List[ItemInfo]:
        query_lower = query.lower().strip()
        if not query_lower:
            return self.get_all_sorted()

        results = []
        for info in self.items.values():
            if (query_lower in info.name.lower()
                    or query_lower in info.internal_name.lower()
                    or query_lower in str(info.item_key)):
                results.append(info)
        results.sort(key=lambda x: x.item_key)
        return results

    def sync_from_local_game(self, game_path: str) -> tuple[bool, str]:
        """Add/update item keys by reading the installed game's local archives."""
        try:
            from client_archive import load_backend
            crimson_rs = load_backend()
            from crimson_rs.catalog import read_item_catalog, read_item_names
            header = crimson_rs.extract_file(
                game_path, "0008", "gamedata/binarystaticinfo__/bin",
                "iteminfo.staticinfoheader",
            )
            body = crimson_rs.extract_file(
                game_path, "0008", "gamedata/binarystaticinfo__/bin",
                "iteminfo.staticinfobody",
            )
            catalog = read_item_catalog(header, body, crimson_rs.parse_iteminfo_from_bytes)
        except (ImportError, OSError, ValueError, struct.error) as exc:
            return False, f"Could not read local item data: {exc}"

        try:
            names = read_item_names(game_path, catalog.layout, crimson_rs)
        except (OSError, ValueError) as exc:
            log.warning("Client item localization unavailable; retaining existing labels: %s", exc)
            names = {}

        previous = dict(self.items)
        added = updated = 0
        extracted_keys: set[int] = set()
        for item in catalog.items:
            extracted_keys.add(item.key)
            existing = previous.get(item.key)
            display = (existing.name if existing and existing.name else
                       names.get(item.name_index, item.internal_name.replace("_", " ")))
            self.items[item.key] = ItemInfo(
                item_key=item.key, name=display, internal_name=item.internal_name,
                category=(existing.category if existing else _guess_item_category(item.internal_name)),
                max_stack=item.max_stack,
            )
            if existing:
                updated += 1
            else:
                added += 1
        self.version += 1
        # A bundled one-file resource lives under PyInstaller's temporary
        # _MEIPASS directory.  Always persist refreshes beside the executable
        # so the current-client database survives the next launch.
        self.loaded_path = os.path.join(_exe_dir, "item_names.json")
        self.save()
        report = {
            "generatedUtc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "source": "installed_client",
            "gamePath": os.path.abspath(game_path),
            "archive": "0008",
            "headerPath": "gamedata/binarystaticinfo__/bin/iteminfo.staticinfoheader",
            "bodyPath": "gamedata/binarystaticinfo__/bin/iteminfo.staticinfobody",
            "declaredRecords": catalog.declared_count,
            "parsedRecords": len(catalog.items),
            "itemLayout": catalog.layout,
            "clientNames": len(names),
            "uniqueClientItemKeys": len(extracted_keys),
            "databaseItems": len(self.items),
            "added": added,
            "refreshed": updated,
            "preservedLocalOnlyKeys": len(set(previous) - extracted_keys),
        }
        report_path = os.path.join(os.path.dirname(self.loaded_path), "item_scan_report.json")
        try:
            with open(report_path, "w", encoding="utf-8") as report_file:
                json.dump(report, report_file, indent=2, ensure_ascii=False)
        except OSError:
            report_path = ""
        suffix = f" Report: {report_path}" if report_path else ""
        return True, (
            f"Local game scan complete: {len(extracted_keys)} current client items; "
            f"{len(self.items)} total known ({added} added, {updated} refreshed)." + suffix
        )


def _guess_item_category(internal_name: str) -> str:
    name = internal_name.lower()
    if any(token in name for token in ("weapon", "armor", "shield", "helmet", "glove", "boot", "ring", "earring", "necklace")):
        return "Equipment"
    if any(token in name for token in ("potion", "food", "elixir", "meal", "drink")):
        return "Consumable"
    if any(token in name for token in ("ore", "ingot", "leather", "timber", "material")):
        return "Material"
    if "quest" in name:
        return "Quest"
    return "Misc"
