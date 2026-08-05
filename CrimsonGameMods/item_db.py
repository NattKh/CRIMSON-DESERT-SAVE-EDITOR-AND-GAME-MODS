from __future__ import annotations

import json
import logging
import os
import struct
from typing import Dict, List

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
        # OFFLINE BUILD: GitHub bootstrap removed — the bundled database is the source.
        log.warning("Item database empty and online bootstrap is removed (offline build)")
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
        # OFFLINE BUILD: GitHub item sync removed. Item names ship in the
        # bundled database and can be rebuilt from the local game client.
        return False, "Online sync removed (offline build) — item names come from the local database."
    def sync_from_local_game(self, game_path: str) -> tuple[bool, str]:
        try:
            import crimson_rs
        except ImportError:
            return False, "crimson_rs module not available."

        dp = "gamedata/binary__/client/bin"
        try:
            pabgh_data = crimson_rs.extract_file(game_path, "0008", dp, "iteminfo.pabgh")
            pabgb_data = crimson_rs.extract_file(game_path, "0008", dp, "iteminfo.pabgb")
        except Exception as e:
            return False, f"Failed to extract iteminfo: {e}"

        count = struct.unpack_from('<H', pabgh_data, 0)[0]
        header_entries = []
        for i in range(count):
            base = 2 + i * 8
            key = struct.unpack_from('<I', pabgh_data, base)[0]
            off = struct.unpack_from('<I', pabgh_data, base + 4)[0]
            header_entries.append((key, off))

        items_raw = []
        for idx, (hdr_key, rec_off) in enumerate(header_entries):
            try:
                rec_end = header_entries[idx + 1][1] if idx + 1 < len(header_entries) else len(pabgb_data)
                if rec_off + 20 > len(pabgb_data):
                    continue
                pos = rec_off
                item_key = struct.unpack_from('<I', pabgb_data, pos)[0]; pos += 4
                str_len = struct.unpack_from('<I', pabgb_data, pos)[0]; pos += 4
                if str_len > 200 or pos + str_len > rec_end:
                    continue
                internal_name = pabgb_data[pos:pos + str_len].decode('ascii', errors='replace'); pos += str_len
                pos += 1
                max_stack = struct.unpack_from('<Q', pabgb_data, pos)[0]; pos += 8
                pos += 1
                if pos + 8 > rec_end:
                    continue
                loc_index = struct.unpack_from('<Q', pabgb_data, pos)[0]
                items_raw.append((item_key, internal_name, loc_index, max_stack))
            except Exception:
                continue

        if not items_raw:
            return False, "No items found in iteminfo.pabgb."

        loc_map: Dict[int, str] = {}
        paloc_source = ""
        try:
            paloc_data = crimson_rs.extract_file(
                game_path, "0020", "gamedata", "localizationstring_eng.paloc")
            pos = 0
            while pos < len(paloc_data) - 12:
                pos += 8
                if pos + 4 > len(paloc_data):
                    break
                kl = struct.unpack_from('<I', paloc_data, pos)[0]; pos += 4
                if kl == 0 or kl > 100 or pos + kl > len(paloc_data):
                    break
                ks = paloc_data[pos:pos + kl].decode('ascii', errors='replace'); pos += kl
                if pos + 4 > len(paloc_data):
                    break
                vl = struct.unpack_from('<I', paloc_data, pos)[0]; pos += 4
                if vl > 50000 or pos + vl > len(paloc_data):
                    break
                vs = paloc_data[pos:pos + vl].decode('utf-8', errors='replace'); pos += vl
                if ks.isdigit():
                    loc_map[int(ks)] = vs
            paloc_source = "game localization"
        except Exception:
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
                except Exception:
                    pass

        if not loc_map:
            paloc_source = "none (using internal names)"

        old_keys = set(self.items.keys())
        self.items.clear()
        matched = 0
        for item_key, internal_name, loc_index, max_stack in items_raw:
            if item_key <= 0:
                continue
            display_name = loc_map.get(loc_index, '')
            if display_name:
                matched += 1
            else:
                display_name = internal_name.replace('_', ' ')
            self.items[item_key] = ItemInfo(
                item_key=item_key,
                name=display_name,
                internal_name=internal_name,
                category=_guess_item_category(internal_name),
                max_stack=max_stack,
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
