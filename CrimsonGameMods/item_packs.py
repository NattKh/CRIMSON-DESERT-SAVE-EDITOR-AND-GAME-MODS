from __future__ import annotations

import json
import os
import time
from typing import Dict, List, Optional
from dataclasses import dataclass, field, asdict

PACKS_REPO = "NattKh/CrimsonDesertCommunityItemMapping"
PACKS_BRANCH = "main"
PACKS_DIR = "packs"
INDEX_URL = f"https://raw.githubusercontent.com/{PACKS_REPO}/{PACKS_BRANCH}/{PACKS_DIR}/index.json"
PACK_BASE_URL = f"https://raw.githubusercontent.com/{PACKS_REPO}/{PACKS_BRANCH}/{PACKS_DIR}/"


@dataclass
class PackItem:
    item_key: int
    name: str = ""
    count: int = 1
    enchant: int = -1
    endurance: int = -1
    sharpness: int = -1
    category: str = ""


@dataclass
class ItemPack:
    name: str = ""
    author: str = ""
    description: str = ""
    version: int = 1
    created: str = ""
    filename: str = ""
    items: List[PackItem] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "author": self.author,
            "description": self.description,
            "version": self.version,
            "created": self.created,
            "items": [
                {k: v for k, v in asdict(it).items() if k != "category" and v != -1 and v != ""}
                for it in self.items
            ]
        }

    @staticmethod
    def from_dict(data: dict, filename: str = "") -> ItemPack:
        items = []
        for it in data.get("items", []):
            if not isinstance(it, dict):
                continue
            items.append(PackItem(
                item_key=it.get("item_key", it.get("itemKey", 0)),
                name=it.get("name", ""),
                count=it.get("count", 1),
                enchant=it.get("enchant", -1),
                endurance=it.get("endurance", -1),
                sharpness=it.get("sharpness", -1),
                category=it.get("category", ""),
            ))
        return ItemPack(
            name=data.get("name", "Unnamed Pack"),
            author=data.get("author", "Unknown"),
            description=data.get("description", ""),
            version=data.get("version", 1),
            created=data.get("created", ""),
            filename=filename,
            items=items,
        )


@dataclass
class PackIndexEntry:
    filename: str
    name: str
    author: str
    description: str
    item_count: int
    version: int = 1


class PackManager:

    def __init__(self, local_dir: str = ""):
        import sys
        if not local_dir:
            if getattr(sys, 'frozen', False):
                local_dir = os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "packs")
            else:
                local_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "packs")
        self.local_dir = local_dir
        os.makedirs(self.local_dir, exist_ok=True)

        self._remote_index: List[PackIndexEntry] = []
        self._local_packs: List[ItemPack] = []


    def scan_local(self) -> List[ItemPack]:
        self._local_packs = []
        if not os.path.isdir(self.local_dir):
            return []

        for fname in sorted(os.listdir(self.local_dir)):
            if not fname.endswith(".json") or fname == "index.json":
                continue
            path = os.path.join(self.local_dir, fname)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                pack = ItemPack.from_dict(data, filename=fname)
                self._local_packs.append(pack)
            except (json.JSONDecodeError, OSError):
                continue
        return self._local_packs

    def save_pack(self, pack: ItemPack, filename: str = "") -> str:
        if not filename:
            safe_name = "".join(c if c.isalnum() or c in "._- " else "_" for c in pack.name)
            safe_name = safe_name.strip().replace(" ", "_").lower()
            if not safe_name:
                safe_name = f"pack_{int(time.time())}"
            filename = safe_name + ".json"

        pack.filename = filename
        path = os.path.join(self.local_dir, filename)

        with open(path, "w", encoding="utf-8") as f:
            json.dump(pack.to_dict(), f, indent=2, ensure_ascii=False)

        return path

    def delete_pack(self, filename: str) -> bool:
        path = os.path.join(self.local_dir, filename)
        if os.path.isfile(path):
            os.remove(path)
            return True
        return False

    def load_pack_file(self, path: str) -> Optional[ItemPack]:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return ItemPack.from_dict(data, filename=os.path.basename(path))
        except (json.JSONDecodeError, OSError):
            return None


    def fetch_remote_index(self) -> tuple[bool, str]:
        # OFFLINE BUILD: online sync/download removed.
        return (False, "Online sync removed (offline build)")
    def get_remote_index(self) -> List[PackIndexEntry]:
        return self._remote_index

    def download_pack(self, filename: str) -> tuple[Optional[ItemPack], str]:
        # OFFLINE BUILD: online sync/download removed.
        return (None, "Online sync removed (offline build)")
    def export_pack_json(self, pack: ItemPack) -> str:
        return json.dumps(pack.to_dict(), indent=2, ensure_ascii=False)

    def generate_index_entry(self, pack: ItemPack) -> dict:
        return {
            "filename": pack.filename,
            "name": pack.name,
            "author": pack.author,
            "description": pack.description,
            "itemCount": len(pack.items),
            "version": pack.version,
        }
