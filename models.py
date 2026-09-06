from __future__ import annotations
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Optional


class QuestState(IntEnum):
    LOCKED           = 0x0D01
    AVAILABLE        = 0x0902
    AVAILABLE_PLUS   = 0x0903
    IN_PROGRESS      = 0x0905
    IN_PROGRESS_PLUS = 0x1102
    COMPLETED        = 0x1105
    SIDE_CONTENT     = 0x1502
    FULLY_COMPLETED  = 0x1905


@dataclass
class SaveItem:
    offset: int = 0
    item_no: int = 0
    item_key: int = 0
    slot_no: int = 0
    stack_count: int = 0
    enchant_level: int = 0
    endurance: int = 0
    sharpness: int = 0

    @property
    def actual_endurance(self) -> int:
        return self.endurance & 0xFF

    @property
    def socket_count_from_endurance(self) -> int:
        return (self.endurance >> 8) & 0xFF
    has_enchant: bool = False
    is_equipment: bool = False
    source: str = "Inventory"
    bag: str = ""
    section: int = 0
    name: str = ""
    category: str = "Misc"
    block_size: int = 0
    field_offsets: dict = field(default_factory=dict)
    parc_parsed: bool = False


@dataclass
class SaveData:
    raw_header: bytes = b""
    decompressed_blob: bytearray = field(default_factory=bytearray)
    original_compressed_size: int = 0
    original_decompressed_size: int = 0
    file_path: str = ""
    is_raw_stream: bool = False


@dataclass
class ItemInfo:
    item_key: int = 0
    name: str = ""
    internal_name: str = ""
    category: str = "Misc"
    max_stack: int = 0


@dataclass
class UndoEntry:
    description: str = ""
    offset: int = 0
    old_bytes: bytes = b""
    new_bytes: bytes = b""
    patches: list = field(default_factory=list)
