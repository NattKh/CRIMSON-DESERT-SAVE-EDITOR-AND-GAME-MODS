"""Read catalog metadata without assuming the current ItemInfo wire layout."""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class CatalogItem:
    key: int
    internal_name: str
    max_stack: int
    name_index: int = 0


@dataclass(frozen=True)
class ItemCatalog:
    items: list[CatalogItem]
    declared_count: int
    layout: str


def read_item_catalog(
    header: bytes, body: bytes, parse_current: Callable[[bytes], list[dict]],
) -> ItemCatalog:
    if len(header) < 2:
        raise ValueError("Local item-data index is invalid.")
    count = struct.unpack_from("<H", header)[0]
    if len(header) < 2 + count * 8:
        raise ValueError("Local item-data index is truncated.")
    if not count:
        raise ValueError("No usable item records were found in the local game data.")
    entries = [struct.unpack_from("<II", header, 2 + index * 8) for index in range(count)]
    keys = {key for key, _ in entries}
    offsets = [offset for _, offset in entries]
    if (0 in keys or len(keys) != count or offsets[0] != 0
            or any(left >= right for left, right in zip(offsets, offsets[1:]))
            or offsets[-1] + 4 > len(body)):
        raise ValueError("Local item-data index has invalid keys or record bounds.")

    # Pre-2.04 records start with their indexed key. 2.04 starts with a CString;
    # let the versioned native parser handle its reordered fields and optionals.
    if struct.unpack_from("<I", body)[0] != entries[0][0]:
        parsed = parse_current(body)
        if len(parsed) != count or {item["key"] for item in parsed} != keys:
            raise ValueError("Parsed item records do not match the item-data index.")
        items = []
        for item in parsed:
            name, limit = item["string_key"], item["max_stack_count"]
            if not isinstance(name, str) or not isinstance(limit, int) or not 0 <= limit < 2**64:
                raise ValueError("Invalid name or stack limit in parsed item records.")
            items.append(CatalogItem(item["key"], name, limit, item["item_name"]["index"]))
        return ItemCatalog(items, count, "2.04")

    items = []
    for index, (key, offset) in enumerate(entries):
        end = offsets[index + 1] if index + 1 < count else len(body)
        if offset + 8 > end:
            continue
        record_key, length = struct.unpack_from("<II", body, offset)
        name_start = offset + 8
        stack_offset = name_start + length + 1
        if record_key != key or length > 512 or stack_offset + 8 > end:
            continue
        name = body[name_start:name_start + length].decode("ascii", "replace")
        limit = struct.unpack_from("<Q", body, stack_offset)[0]
        name_offset = stack_offset + 9
        name_index = struct.unpack_from("<Q", body, name_offset)[0] if name_offset + 8 <= end else 0
        items.append(CatalogItem(key, name, limit, name_index))
    if not items:
        raise ValueError("No usable item records were found in the local game data.")
    return ItemCatalog(items, count, "legacy")


def read_item_names(game_path: str, layout: str, backend) -> dict[int, str]:
    if layout == "2.04":
        directory, filename = "gamedata/stringtable/binary__/eng", "item.paloc"
    else:
        directory, filename = "gamedata", "localizationstring_eng.paloc"
    data = backend.extract_file(game_path, "0020", directory, filename)
    rows = backend.parse_paloc_from_bytes(data)
    names = {int(row["key"]): row["value"] for row in rows
             if row["category"] == 7 and str(row["key"]).isdigit() and row["value"]}
    if not names:
        raise ValueError("No English item names were found in the client localization table.")
    return names
