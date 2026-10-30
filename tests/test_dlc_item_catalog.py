"""Exercise both catalog consumers against the 2.04 native-parser contract."""
import importlib.util
import json
import sqlite3
import struct
import sys
from pathlib import Path
from types import ModuleType

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(params=["CrimsonSaveEditor", "CrimsonGameMods"])
def catalog(request, monkeypatch, tmp_path):
    previous = {name: sys.modules.pop(name, None) for name in ("models", "data_db", "client_archive")}
    directory = ROOT / request.param
    sys.path.insert(0, str(directory))
    try:
        spec = importlib.util.spec_from_file_location("dlc_catalog_under_test", directory / "item_db.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if request.param == "CrimsonSaveEditor":
            monkeypatch.setattr(module, "SEARCH_PATHS", [])
            monkeypatch.setattr(module, "_exe_dir", str(tmp_path))
        else:
            import data_db
            monkeypatch.setattr(data_db, "_exe_dir", lambda: str(tmp_path))
            with sqlite3.connect(tmp_path / "crimson_data.db") as connection:
                connection.execute("CREATE TABLE items (item_key INTEGER PRIMARY KEY, name TEXT, "
                                   "internal_name TEXT, category TEXT, max_stack INTEGER)")
            monkeypatch.setattr(module.ItemNameDB, "load_auto", lambda self: "")
        yield module, request.param, tmp_path
    finally:
        if request.param == "CrimsonGameMods":
            sys.modules["data_db"].reset_connection()
        sys.path.pop(0)
        for name, old in previous.items():
            sys.modules.pop(name, None)
            if old is not None:
                sys.modules[name] = old


def archive(monkeypatch, *, wrong_keys=False, broken_names=False, legacy=False):
    records = []
    body = bytearray()
    header = bytearray(struct.pack("<H", 2))
    for key, name, limit, index in [(101, "Existing_Item", 100, 9001), (102, "Diving_Suit", 1, 9002)]:
        header.extend(struct.pack("<II", key, len(body)))
        encoded = name.encode("ascii")
        if legacy:
            body.extend(struct.pack("<II", key, len(encoded)) + encoded + struct.pack("<BQBQ", 0, limit, 7, index))
        else:
            body.extend(struct.pack("<I", len(encoded)) + encoded + struct.pack("<IBHQ", key, 0, 65535, limit))
        records.append({"key": key, "string_key": name, "max_stack_count": limit,
                        "item_name": {"category": 7, "index": index, "default": str(index)}})
    if wrong_keys:
        records[1]["key"] = 999

    def extract_file(game, group, directory, name):
        if name in ("iteminfo.staticinfoheader", "iteminfo.pabgh"):
            return bytes(header)
        if name in ("iteminfo.staticinfobody", "iteminfo.pabgb"):
            return bytes(body)
        names_location = (("gamedata", "localizationstring_eng.paloc") if legacy else
                          ("gamedata/stringtable/binary__/eng", "item.paloc"))
        if group == "0020" and (directory, name) == names_location:
            if broken_names:
                raise ValueError("localization archive unavailable")
            return b"synthetic PALOC container"
        raise ValueError(f"Unexpected archive request: {group}/{directory}/{name}")

    backend = ModuleType("crimson_rs")
    backend.__path__ = [str(ROOT / "CrimsonGameMods" / "crimson_rs")]
    backend.extract_file = extract_file
    def parse_current(data):
        if legacy:
            raise ValueError("The current native ItemInfo parser cannot read this legacy table")
        return records

    backend.parse_iteminfo_from_bytes = parse_current
    backend.parse_paloc_from_bytes = lambda data: [
        {"category": 7, "key": "9001", "value": "Existing item"},
        {"category": 7, "key": "9002", "value": "Diving Suit"},
    ]
    monkeypatch.setitem(sys.modules, "crimson_rs", backend)


def persisted_items(kind, directory):
    if kind == "CrimsonSaveEditor":
        data = json.loads((directory / "item_names.json").read_text(encoding="utf-8"))
        return {item["itemKey"]: (item["name"], item.get("maxStack", 0)) for item in data["items"]}
    with sqlite3.connect(directory / "crimson_data.db") as connection:
        return {key: (name, limit) for key, name, limit in
                connection.execute("SELECT item_key, name, max_stack FROM items")}


def test_dlc_scan_reads_current_limits_names_and_persists_them(catalog, monkeypatch):
    module, kind, directory = catalog
    archive(monkeypatch)
    db = module.ItemNameDB()
    db.items[101] = module.ItemInfo(item_key=101, name="Custom name", max_stack=20)

    ok, message = db.sync_from_local_game("synthetic-dlc-client")

    assert ok, message
    assert db.items[101].max_stack == 100
    assert db.items[102].max_stack == 1
    assert db.items[102].name == "Diving Suit"
    expected_name = "Custom name" if kind == "CrimsonSaveEditor" else "Existing item"
    assert persisted_items(kind, directory) == {101: (expected_name, 100), 102: ("Diving Suit", 1)}


def test_dlc_scan_rejects_parser_results_that_do_not_match_the_index(catalog, monkeypatch):
    module, kind, directory = catalog
    archive(monkeypatch, wrong_keys=True)
    db = module.ItemNameDB()
    existing = module.ItemInfo(item_key=101, name="Cached item", max_stack=20)
    db.items[101] = existing
    db.version = 4

    ok, message = db.sync_from_local_game("synthetic-dlc-client")

    assert not ok
    assert "index" in message.lower()
    assert db.items == {101: existing}
    assert db.version == 4
    if kind == "CrimsonSaveEditor":
        assert not (directory / "item_names.json").exists()
    else:
        assert persisted_items(kind, directory) == {}


def test_legacy_scan_still_reads_indexed_records_and_names(catalog, monkeypatch):
    module, kind, directory = catalog
    archive(monkeypatch, legacy=True)
    db = module.ItemNameDB()

    ok, message = db.sync_from_local_game("synthetic-legacy-client")

    assert ok, message
    assert persisted_items(kind, directory) == {101: ("Existing item", 100), 102: ("Diving Suit", 1)}


def test_missing_localization_preserves_metadata_and_uses_internal_names(catalog, monkeypatch, caplog):
    module, kind, directory = catalog
    archive(monkeypatch, broken_names=True)
    db = module.ItemNameDB()

    ok, message = db.sync_from_local_game("synthetic-dlc-client")

    assert ok, message
    assert persisted_items(kind, directory) == {101: ("Existing Item", 100), 102: ("Diving Suit", 1)}
    assert "localization unavailable" in caplog.text
