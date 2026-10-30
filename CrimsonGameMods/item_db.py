from __future__ import annotations

import json
import logging
import os
import struct
from typing import Dict, List
from urllib.request import urlopen, Request
from urllib.error import URLError

from data_db import get_connection, get_db_path, open_writable, reset_connection
from models import ItemInfo

log = logging.getLogger(__name__)

GITHUB_URL = (
    "https://raw.githubusercontent.com/"
    "NattKh/CrimsonDesertCommunityItemMapping/main/item_names.json"
)


class ItemNameDB:

    def __init__(self) -> None:
        self.items: Dict[int, ItemInfo] = {}
        self.loaded_path: str = ""
        self.version: int = 0
        self.load_auto()

    def load_auto(self) -> str:
        self.load()
        if self.items:
            return self.loaded_path

        try:
            ok, msg = self.sync_from_github()
            if ok and self.items:
                log.info("Bootstrap sync: %s", msg)
                return self.loaded_path
        except Exception as exc:
            log.warning("Bootstrap GitHub sync failed: %s", exc)
        return ""

    def load(self) -> None:
        self.items.clear()
        self.version = 0
        try:
            db = get_connection()
            rows = db.execute(
                "SELECT item_key, name, internal_name, category, max_stack FROM items"
            ).fetchall()
            for row in rows:
                key = row["item_key"]
                self.items[key] = ItemInfo(
                    item_key=key,
                    name=row["name"],
                    internal_name=row["internal_name"],
                    category=row["category"],
                    max_stack=row["max_stack"],
                )
            self.loaded_path = get_db_path()
            log.info("Loaded %d items from SQLite", len(self.items))
        except Exception as exc:
            log.warning("SQLite item load failed: %s", exc)

    def save(self) -> None:
        if not self.items:
            return
        rows = [
            (
                key,
                info.name,
                info.internal_name,
                info.category,
                info.max_stack,
            )
            for key, info in self.items.items()
        ]
        try:
            conn = open_writable()
            conn.executemany(
                "INSERT OR REPLACE INTO items VALUES (?,?,?,?,?)", rows
            )
            conn.commit()
            conn.close()
            reset_connection()
            log.info("Saved %d items to SQLite", len(rows))
        except Exception as exc:
            log.warning("SQLite item save failed: %s", exc)

    def apply_localization(self) -> int:
        try:
            from localization import get_language, _names_data
            if get_language() == "en" or not _names_data:
                return 0
            items_map = _names_data.get("items", {})
            if not items_map:
                return 0
            count = 0
            for key, info in self.items.items():
                localized = items_map.get(str(key), "")
                if localized:
                    info.name = localized
                    count += 1
            return count
        except Exception:
            return 0

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
            if (
                query_lower in info.name.lower()
                or query_lower in info.internal_name.lower()
                or query_lower in str(info.item_key)
            ):
                results.append(info)
        results.sort(key=lambda x: x.item_key)
        return results

    def sync_from_github(self) -> tuple[bool, str]:
        try:
            req = Request(GITHUB_URL, headers={"User-Agent": "CrimsonSaveEditor/1.0"})
            with urlopen(req, timeout=10) as resp:
                raw = resp.read()
                data = json.loads(raw.decode("utf-8"))
        except (URLError, json.JSONDecodeError, OSError) as exc:
            return False, f"Download failed: {exc}"

        remote_version = data.get("version", 0)
        remote_items = data.get("items", [])

        if remote_version <= self.version and self.items:
            return True, f"Already up to date (v{self.version})."

        added = 0
        updated = 0
        for entry in remote_items:
            key = entry.get("itemKey", 0)
            if key <= 0:
                continue
            name = entry.get("name", "")
            category = entry.get("category", "Misc")
            internal = entry.get("internalName", "")
            max_stack = entry.get("maxStack", 0)

            if key not in self.items:
                self.items[key] = ItemInfo(
                    item_key=key,
                    name=name,
                    internal_name=internal,
                    category=category,
                    max_stack=max_stack,
                )
                added += 1
            else:
                existing = self.items[key]
                if name and (not existing.name or existing.name.startswith("Unknown")):
                    existing.name = name
                    updated += 1
                if internal and not existing.internal_name:
                    existing.internal_name = internal
                if category != "Misc" and existing.category == "Misc":
                    existing.category = category
                if max_stack and not existing.max_stack:
                    existing.max_stack = max_stack

        if remote_version > self.version:
            self.version = remote_version

        self.save()
        return True, f"Synced v{remote_version}: {added} new, {updated} updated."

    def sync_from_local_game(self, game_path: str) -> tuple[bool, str]:
        try:
            import crimson_rs
            from crimson_rs.catalog import read_item_catalog, read_item_names
        except ImportError:
            return False, "crimson_rs module not available."

        dp = "gamedata/binarystaticinfo__/bin"
        try:
            pabgh_data = crimson_rs.extract_file(game_path, "0008", dp, "iteminfo.staticinfoheader")
            pabgb_data = crimson_rs.extract_file(game_path, "0008", dp, "iteminfo.staticinfobody")
            catalog = read_item_catalog(pabgh_data, pabgb_data, crimson_rs.parse_iteminfo_from_bytes)
        except (OSError, ValueError, struct.error) as e:
            return False, f"Failed to extract iteminfo: {e}"

        loc_map: Dict[int, str] = {}
        paloc_source = ""
        try:
            loc_map = read_item_names(game_path, catalog.layout, crimson_rs)
            paloc_source = "game localization"
        except (OSError, ValueError) as exc:
            log.warning("Client item localization unavailable; trying bundled TSV: %s", exc)
            tsv_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "localizationstring_eng_items.tsv")
            if os.path.isfile(tsv_path):
                try:
                    with open(tsv_path, "r", encoding="utf-8") as f:
                        for line in f:
                            parts = line.strip().split(";", 1)
                            if len(parts) == 2 and parts[0].isdigit():
                                loc_map[int(parts[0])] = parts[1]
                    paloc_source = "bundled TSV"
                except OSError as exc:
                    log.warning("Bundled item localization unavailable: %s", exc)

        if not loc_map:
            paloc_source = "none (using internal names)"

        old_keys = set(self.items.keys())
        self.items.clear()
        matched = 0
        for item in catalog.items:
            display_name = loc_map.get(item.name_index, '')
            if display_name:
                matched += 1
            else:
                display_name = item.internal_name.replace('_', ' ')
            self.items[item.key] = ItemInfo(
                item_key=item.key,
                name=display_name,
                internal_name=item.internal_name,
                category=_guess_item_category(item.internal_name),
                max_stack=item.max_stack,
            )

        self.version += 1
        self.save()

        new_keys = set(self.items.keys()) - old_keys
        new_count = len(new_keys)

        return True, (
            f"Synced {len(self.items)} items from game client.\n"
            f"New items: {new_count}\n"
            f"Names matched: {matched} (source: {paloc_source})\n"
            f"Unmatched: {len(self.items) - matched}"
        )


def _guess_item_category(internal_name: str) -> str:
    n = internal_name.lower()
    if n.startswith('money') or n.startswith('currency'):
        return 'Currency'
    if any(n.startswith(p) for p in ['weapon_', 'onehand', 'twohand', 'bow_', 'crossbow']):
        return 'Equipment'
    if any(n.startswith(p) for p in ['armor_', 'helmet_', 'glove_', 'shoe_', 'shield_']):
        return 'Equipment'
    if any(n.startswith(p) for p in ['ring_', 'necklace_', 'earring_', 'belt_', 'accessory_']):
        return 'Equipment'
    if any(p in n for p in ['_ore', '_ingot', '_hide', '_leather', '_timber', '_plank',
                             '_herb', '_reagent', '_fabric', '_thread', '_stone', 'material']):
        return 'Material'
    if any(p in n for p in ['potion', 'food_', 'elixir', 'meal_', 'drink_', 'consumable']):
        return 'Consumable'
    if any(p in n for p in ['arrow', 'bolt_', 'ammo', 'quiver', 'pyeonjeon']):
        return 'Ammo'
    if any(p in n for p in ['quest_', 'quest']):
        return 'Quest'
    return 'Misc'
