"""Refresh item metadata from synthetic staticinfo records, without a game."""
import importlib.util
import json
import struct
import sys
from pathlib import Path
from types import ModuleType

import pytest


@pytest.fixture
def catalog(monkeypatch, tmp_path):
    directory = Path(__file__).resolve().parents[1] / "CrimsonSaveEditor"
    previous_models = sys.modules.pop("models", None)
    sys.path.insert(0, str(directory))
    try:
        spec = importlib.util.spec_from_file_location("catalog_under_test", directory / "item_db.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        monkeypatch.setattr(module, "SEARCH_PATHS", [])
        monkeypatch.setattr(module, "_exe_dir", str(tmp_path))
        yield module
    finally:
        sys.path.pop(0)
        sys.modules.pop("models", None)
        if previous_models is not None:
            sys.modules["models"] = previous_models


def item_record(key, name, max_stack):
    encoded = name.encode("ascii")
    return struct.pack("<II", key, len(encoded)) + encoded + b"\x00" + struct.pack("<Q", max_stack)


def install_archive(monkeypatch, records):
    header = bytearray(struct.pack("<H", len(records)))
    body = bytearray()
    for key, record in records:
        header.extend(struct.pack("<II", key, len(body)))
        body.extend(record)
    files = {"iteminfo.staticinfoheader": bytes(header), "iteminfo.staticinfobody": bytes(body)}

    def extract_file(game, group, directory, name):
        if group == "0020":
            raise ValueError("No localization in this synthetic archive")
        assert group == "0008"
        assert directory == "gamedata/binarystaticinfo__/bin"
        return files[name]

    backend = ModuleType("crimson_rs")
    backend.__path__ = [str(Path(__file__).resolve().parents[1] / "CrimsonGameMods" / "crimson_rs")]
    backend.extract_file = extract_file

    def reject_unknown_layout(data):
        raise ValueError("No usable item records in this synthetic archive")

    backend.parse_iteminfo_from_bytes = reject_unknown_layout
    monkeypatch.setitem(sys.modules, "crimson_rs", backend)


@pytest.mark.parametrize("max_stack", [0, 20, 500, 2**40])
def test_refresh_updates_and_persists_client_stack_limits(catalog, monkeypatch, tmp_path, max_stack):
    db = catalog.ItemNameDB()
    db.items[101] = catalog.ItemInfo(item_key=101, name="Custom name", category="Material", max_stack=50)
    db.items[999] = catalog.ItemInfo(item_key=999, name="Local only", max_stack=7)
    install_archive(monkeypatch, [
        (101, item_record(101, "Existing_Item", max_stack)),
        (102, item_record(102, "New_Ore", 200)),
    ])

    ok, message = db.sync_from_local_game("synthetic-client")

    assert ok, message
    assert db.items[101].max_stack == max_stack
    assert db.items[101].name == "Custom name"
    assert db.items[101].category == "Material"
    assert db.items[102].max_stack == 200
    assert db.items[999].max_stack == 7
    reloaded = catalog.ItemNameDB()
    reloaded.load(str(tmp_path / "item_names.json"))
    assert {key: item.max_stack for key, item in reloaded.items.items()} == {
        101: max_stack, 102: 200, 999: 7,
    }


def test_refresh_does_not_read_stack_limit_from_the_next_record(catalog, monkeypatch, tmp_path):
    truncated = item_record(101, "Truncated_Item", 500)[:-8]
    install_archive(monkeypatch, [
        (101, truncated),
        (102, item_record(102, "Valid_Ore", 200)),
    ])
    db = catalog.ItemNameDB()

    ok, message = db.sync_from_local_game("synthetic-client")

    assert ok, message
    assert set(db.items) == {102}
    assert db.items[102].max_stack == 200
    report = json.loads((tmp_path / "item_scan_report.json").read_text(encoding="utf-8"))
    assert report["declaredRecords"] == 2
    assert report["parsedRecords"] == 1


@pytest.mark.parametrize("empty", [False, True])
def test_refresh_does_not_report_cached_items_as_a_successful_scan(catalog, monkeypatch, tmp_path, empty):
    db = catalog.ItemNameDB()
    existing = catalog.ItemInfo(item_key=101, name="Cached item", max_stack=50)
    db.items[101] = existing
    db.version = 7
    # 2.04 moves the name before the key and inserts endurance before the limit.
    name = b"New_Item"
    new_record = struct.pack("<I", len(name)) + name + struct.pack("<IBHQ", 101, 0, 65535, 200)
    install_archive(monkeypatch, [] if empty else [(101, new_record)])

    ok, message = db.sync_from_local_game("synthetic-client")

    assert not ok
    assert "No usable item records" in message
    assert db.items == {101: existing}
    assert db.version == 7
    assert not (tmp_path / "item_names.json").exists()
    assert not (tmp_path / "item_scan_report.json").exists()
