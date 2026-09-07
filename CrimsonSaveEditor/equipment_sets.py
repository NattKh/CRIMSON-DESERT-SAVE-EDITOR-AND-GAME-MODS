from __future__ import annotations

import json
import os
import time
from typing import Dict, List, Optional
from dataclasses import dataclass, field, asdict


@dataclass
class StatOperation:
    stat_name: str
    stat_hash: int
    size_class: str
    operation: str
    value: int
    target_hash: int = 0

    def to_dict(self) -> dict:
        d = {
            "stat_name": self.stat_name,
            "stat_hash": self.stat_hash,
            "size_class": self.size_class,
            "operation": self.operation,
            "value": self.value,
        }
        if self.target_hash:
            d["target_hash"] = self.target_hash
        return d

    @staticmethod
    def from_dict(data: dict) -> StatOperation:
        return StatOperation(
            stat_name=data.get("stat_name", "?"),
            stat_hash=data.get("stat_hash", 0),
            size_class=data.get("size_class", "flat2"),
            operation=data.get("operation", "set_value"),
            value=data.get("value", 0),
            target_hash=data.get("target_hash", 0),
        )


@dataclass
class SetItem:
    item_key: int
    item_name: str = ""
    operations: List[StatOperation] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "item_key": self.item_key,
            "item_name": self.item_name,
            "operations": [op.to_dict() for op in self.operations],
        }

    @staticmethod
    def from_dict(data: dict) -> SetItem:
        return SetItem(
            item_key=data.get("item_key", 0),
            item_name=data.get("item_name", ""),
            operations=[StatOperation.from_dict(op) for op in data.get("operations", [])],
        )


@dataclass
class EquipmentSet:
    name: str = ""
    author: str = ""
    description: str = ""
    version: int = 1
    created: str = ""
    filename: str = ""
    items: List[SetItem] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "author": self.author,
            "description": self.description,
            "version": self.version,
            "created": self.created,
            "items": [it.to_dict() for it in self.items],
        }

    @staticmethod
    def from_dict(data: dict, filename: str = "") -> EquipmentSet:
        return EquipmentSet(
            name=data.get("name", "Unnamed Set"),
            author=data.get("author", "Unknown"),
            description=data.get("description", ""),
            version=data.get("version", 1),
            created=data.get("created", ""),
            filename=filename,
            items=[SetItem.from_dict(it) for it in data.get("items", [])],
        )


@dataclass
class SetIndexEntry:
    filename: str
    name: str
    author: str
    description: str
    item_count: int
    version: int = 1


class SetManager:

    def __init__(self, local_dir: str = ""):
        import sys
        if not local_dir:
            if getattr(sys, 'frozen', False):
                local_dir = os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "sets")
            else:
                local_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sets")
        self.local_dir = local_dir
        os.makedirs(self.local_dir, exist_ok=True)

        self._local_sets: List[EquipmentSet] = []


    def scan_local(self) -> List[EquipmentSet]:
        self._local_sets = []
        if not os.path.isdir(self.local_dir):
            return []

        for fname in sorted(os.listdir(self.local_dir)):
            if not fname.endswith(".json") or fname == "index.json":
                continue
            path = os.path.join(self.local_dir, fname)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                es = EquipmentSet.from_dict(data, filename=fname)
                self._local_sets.append(es)
            except (json.JSONDecodeError, OSError):
                continue
        return self._local_sets

    def save_set(self, es: EquipmentSet, filename: str = "") -> str:
        if not filename:
            safe_name = "".join(c if c.isalnum() or c in "._- " else "_" for c in es.name)
            safe_name = safe_name.strip().replace(" ", "_").lower()
            if not safe_name:
                safe_name = f"set_{int(time.time())}"
            filename = safe_name + ".json"

        es.filename = filename
        path = os.path.join(self.local_dir, filename)

        with open(path, "w", encoding="utf-8") as f:
            json.dump(es.to_dict(), f, indent=2, ensure_ascii=False)

        return path

    def delete_set(self, filename: str) -> bool:
        path = os.path.join(self.local_dir, filename)
        if os.path.isfile(path):
            os.remove(path)
            return True
        return False

    def load_set_file(self, path: str) -> Optional[EquipmentSet]:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return EquipmentSet.from_dict(data, filename=os.path.basename(path))
        except (json.JSONDecodeError, OSError):
            return None


    def export_set_json(self, es: EquipmentSet) -> str:
        return json.dumps(es.to_dict(), indent=2, ensure_ascii=False)

    def generate_index_entry(self, es: EquipmentSet) -> dict:
        return {
            "filename": es.filename,
            "name": es.name,
            "author": es.author,
            "description": es.description,
            "itemCount": len(es.items),
            "version": es.version,
        }
