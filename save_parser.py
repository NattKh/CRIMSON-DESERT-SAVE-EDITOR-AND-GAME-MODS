from __future__ import annotations

import argparse
import json
import struct
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import save_crypto


RAW_MAGIC = b"\xFF\xFF\x04\x00"
MAIN_BAG_HASH = 0xEBEA2CD2
SOURCE_NAMES = ["Equipment", "Inventory", "Store", "Mercenary", "Other"]


@dataclass
class FieldDef:
    name: str
    type_name: str
    meta_kind: int
    meta_size: int
    meta_aux: int
    start_offset: int = 0
    end_offset: int = 0


@dataclass
class TypeDef:
    index: int
    name: str
    fields: list[FieldDef]
    start_offset: int = 0
    end_offset: int = 0


@dataclass
class TocEntry:
    index: int
    class_index: int
    class_name: str
    sentinel1: int
    sentinel2: int
    data_offset: int
    data_size: int
    entry_offset: int = 0


@dataclass
class CharacterStats:
    found: bool
    level: int = 0
    characterKey: int = 0
    factionKey: int = 0
    currentHp: int = 0
    currentMp: int = 0
    currentHpRawLo: int = 0
    currentHpRawHi: int = 0
    currentMpRawLo: int = 0
    currentMpRawHi: int = 0
    bitmask: int = 0
    dataOffset: int = 0
    dataSize: int = 0


@dataclass
class ItemRecord:
    offset: int
    itemNo: int
    itemKey: int
    slot: int
    stack: int
    enchant: int
    sharpness: int
    endurance: int
    hasEnchant: bool
    isEquipment: bool
    section: int
    sourceType: int
    source: str
    classIndex: int
    className: str
    blockSize: int


@dataclass
class BagSlot:
    offset: int
    category: int
    expansion: int
    hash: int
    isMainBag: bool
    classIndex: int = 0
    className: str = ""
    blockOffset: int = 0
    blockSize: int = 0


@dataclass
class GenericFieldValue:
    field_index: int
    name: str
    type_name: str
    meta_kind: int
    meta_size: int
    meta_aux: int
    present: bool
    decode_kind: str = "unknown"
    start_offset: int = 0
    end_offset: int = 0
    value_repr: str = ""
    edit_format: str = ""
    editable: bool = False
    note: str = ""
    child_prefix_u16: int = 0
    child_prefix_u8: int = 0
    child_mask_byte_count: int = 0
    child_mask_bytes: bytes = b""
    child_type_index: int = -1
    child_type_name: str = ""
    child_reserved_u8: int = 0
    child_sentinel1_u32: int = 0
    child_sentinel2_u32: int = 0
    child_payload_offset: int = 0
    child_reserved_u32: int = 0
    child_size_u32: int = 0
    child_fields: list[GenericFieldValue] | None = None
    child_undecoded_ranges: list[tuple[int, int]] | None = None
    list_prefix_u8: int = 0
    list_count: int = 0
    list_reserved1_u32: int = 0
    list_reserved2_u32: int = 0
    list_reserved3_u32: int = 0
    list_reserved4_u16: int = 0
    list_reserved4_u32: int = 0
    list_header_size: int = 0
    list_elements: list[GenericFieldValue] | None = None


@dataclass
class ObjectBlock:
    entry_index: int
    class_index: int
    class_name: str
    data_offset: int
    data_size: int
    mask_byte_count: int
    header_mask_bytes: bytes
    reserved_u32: int
    fields: list[GenericFieldValue]
    undecoded_ranges: list[tuple[int, int]]


def load_lz4_block():
    return save_crypto.load_lz4_block()


def _decode_ascii(blob: bytes, offset: int, length: int) -> str:
    return blob[offset:offset + length].decode("ascii", errors="strict")


def _u16(blob: bytes, offset: int) -> int:
    return struct.unpack_from("<H", blob, offset)[0]


def _u32(blob: bytes, offset: int) -> int:
    return struct.unpack_from("<I", blob, offset)[0]


def _u24(blob: bytes, offset: int) -> int:
    return blob[offset] | (blob[offset + 1] << 8) | (blob[offset + 2] << 16)


def _u16be(blob: bytes, offset: int) -> int:
    return (blob[offset] << 8) | blob[offset + 1]


def _u64(blob: bytes, offset: int) -> int:
    return struct.unpack_from("<Q", blob, offset)[0]


def _i64(blob: bytes, offset: int) -> int:
    return struct.unpack_from("<q", blob, offset)[0]


def _type_to_edit_format(type_name: str, size: int) -> str:
    lower = type_name.lower()
    if lower == "bool":
        return "bool"
    if "float" in lower and size == 4:
        return "<f"
    if "float" in lower and size == 8:
        return "<d"
    if lower.startswith("int") and size == 1:
        return "<b"
    if lower.startswith("int") and size == 2:
        return "<h"
    if lower.startswith("int") and size == 4:
        return "<i"
    if lower.startswith("int") and size == 8:
        return "<q"
    if size == 1:
        return "<B"
    if size == 2:
        return "<H"
    if size == 4:
        return "<I"
    if size == 8:
        return "<Q"
    return ""


def _looks_like_raw(blob: bytes) -> bool:
    return blob.startswith(RAW_MAGIC)


def _inflate_plaintext_payload(plaintext_payload: bytes, uncompressed_size: int) -> bytes:
    lz4_block = load_lz4_block()
    return lz4_block.decompress(plaintext_payload, uncompressed_size=uncompressed_size)


def load_raw_blob(path: Path, key_hex: str, uncompressed_size: int | None = None) -> tuple[bytes, dict[str, Any]]:
    blob = path.read_bytes()
    meta: dict[str, Any] = {"input_path": str(path), "input_size": len(blob)}

    if blob.startswith(save_crypto.MAGIC):
        info, plaintext_payload, raw = save_crypto.inflate_payload(blob, save_crypto.load_key(key_hex))
        meta["input_kind"] = "save_container"
        meta["container"] = {
            "version": info["version"],
            "flags": info["flags"],
            "float_flag": info["float_flag"],
            "field_0C": info["field_0C"],
            "field_10": info["field_10"],
            "uncompressed_size": info["uncompressed_size"],
            "payload_size": info["payload_size"],
            "nonce": info["nonce"].hex(),
            "hmac": info["hmac"].hex(),
            "hmac_ok": info["hmac_ok"],
        }
        meta["compressed_plaintext_size"] = len(plaintext_payload)
        meta["raw_size"] = len(raw)
        return raw, meta

    if _looks_like_raw(blob):
        meta["input_kind"] = "raw_blob"
        meta["raw_size"] = len(blob)
        return blob, meta

    candidate_size = uncompressed_size
    sibling_save = path.with_suffix("")
    if candidate_size is None and sibling_save.exists():
        sibling_blob = sibling_save.read_bytes()
        if sibling_blob.startswith(save_crypto.MAGIC):
            candidate_size = save_crypto.parse_header(sibling_blob)["uncompressed_size"]
            meta["size_hint_from"] = str(sibling_save)

    if candidate_size is not None:
        try:
            raw = _inflate_plaintext_payload(blob, candidate_size)
        except Exception as exc:
            raise ValueError(
                f"Failed to LZ4-decompress {path} with uncompressed_size={candidate_size}: {exc}"
            ) from exc
        if not _looks_like_raw(raw):
            raise ValueError(
                f"{path} decompressed with size {candidate_size}, but the result does not look like a raw serializer blob"
            )
        meta["input_kind"] = "compressed_plaintext_payload"
        meta["raw_size"] = len(raw)
        meta["compressed_plaintext_size"] = len(blob)
        return raw, meta

    raise ValueError(
        f"Unsupported input format for {path}. Expected SAVE container, raw blob, or compressed payload with a size hint."
    )


def parse_schema(raw: bytes) -> dict[str, Any]:
    pos = 0x0E
    header_tag = _u16(raw, pos)
    pos += 2
    header_zero = _u16(raw, pos)
    pos += 2
    type_count = _u16(raw, pos)
    pos += 2

    root_len = _u32(raw, pos)
    pos += 4
    root_name = _decode_ascii(raw, pos, root_len)
    pos += root_len

    types: list[TypeDef] = []
    current_name = root_name
    for type_index in range(type_count):
        type_start = pos
        field_count = _u16(raw, pos)
        pos += 2

        fields: list[FieldDef] = []
        for _ in range(field_count):
            field_start = pos
            fn_len = _u32(raw, pos)
            pos += 4
            field_name = _decode_ascii(raw, pos, fn_len)
            pos += fn_len

            tn_len = _u32(raw, pos)
            pos += 4
            type_name = _decode_ascii(raw, pos, tn_len)
            pos += tn_len

            meta_kind = _u16(raw, pos)
            meta_size = _u16(raw, pos + 2)
            meta_aux = _u32(raw, pos + 4)
            pos += 8

            fields.append(
                FieldDef(
                    name=field_name,
                    type_name=type_name,
                    meta_kind=meta_kind,
                    meta_size=meta_size,
                    meta_aux=meta_aux,
                    start_offset=field_start,
                    end_offset=pos,
                )
            )

        type_end = pos
        types.append(
            TypeDef(
                index=type_index,
                name=current_name,
                fields=fields,
                start_offset=type_start,
                end_offset=type_end,
            )
        )
        if type_index != type_count - 1:
            next_len = _u32(raw, pos)
            pos += 4
            current_name = _decode_ascii(raw, pos, next_len)
            pos += next_len

    return {
        "header_tag": header_tag,
        "header_zero": header_zero,
        "type_count": type_count,
        "root_type": root_name,
        "types": types,
        "schema_end": pos,
    }


def parse_toc(raw: bytes, schema_end: int, type_names: list[str]) -> dict[str, Any]:
    if schema_end + 12 > len(raw):
        return {"prefix_zero": None, "toc_count": 0, "stream_size": len(raw), "entries": []}

    prefix_zero = _u32(raw, schema_end)
    toc_count = _u32(raw, schema_end + 4)
    stream_size = _u32(raw, schema_end + 8)
    toc_start = schema_end + 12
    entries: list[TocEntry] = []

    for index in range(toc_count):
        entry_off = toc_start + index * 20
        if entry_off + 20 > len(raw):
            break
        class_index, sentinel1, sentinel2, data_offset, data_size = struct.unpack_from("<5I", raw, entry_off)
        class_name = type_names[class_index] if 0 <= class_index < len(type_names) else f"<class_{class_index}>"
        entries.append(
            TocEntry(
                index=index,
                class_index=class_index,
                class_name=class_name,
                sentinel1=sentinel1,
                sentinel2=sentinel2,
                data_offset=data_offset,
                data_size=data_size,
                entry_offset=entry_off,
            )
        )

    return {
        "prefix_zero": prefix_zero,
        "toc_count": toc_count,
        "stream_size": stream_size,
        "entries": entries,
    }


def _field_present(mask_bytes: bytes, field_index: int) -> bool:
    byte_index = field_index // 8
    bit_index = field_index % 8
    if byte_index >= len(mask_bytes):
        return False
    return (mask_bytes[byte_index] & (1 << bit_index)) != 0


def _decode_fixed_value(raw: bytes, offset: int, field: FieldDef) -> tuple[int, str, str, bool]:
    size = field.meta_size
    if size <= 0:
        raise ValueError("Field does not have a fixed scalar size")
    end = offset + size
    data = raw[offset:end]
    edit_format = _type_to_edit_format(field.type_name, size)
    editable = edit_format != ""
    lower = field.type_name.lower()

    if lower == "bool" and size == 1:
        value_repr = "true" if data[0] != 0 else "false"
        return end, value_repr, "bool", True
    if edit_format == "<f":
        value_repr = repr(struct.unpack("<f", data)[0])
        return end, value_repr, edit_format, True
    if edit_format == "<d":
        value_repr = repr(struct.unpack("<d", data)[0])
        return end, value_repr, edit_format, True
    if edit_format in ("<b", "<h", "<i", "<q", "<B", "<H", "<I", "<Q"):
        value_repr = str(struct.unpack(edit_format, data)[0])
        return end, value_repr, edit_format, editable

    value_repr = data.hex()
    return end, value_repr, "", False


def _decode_inline_bytes(raw: bytes, offset: int, field: FieldDef, tail_cursor: int) -> tuple[int, str, str]:
    if offset + 4 > tail_cursor:
        raise ValueError("Inline byte field overruns block")
    count = _u32(raw, offset)
    total = 4 + count * field.meta_size
    end = offset + total
    if end > tail_cursor:
        raise ValueError("Inline byte field overruns block")
    data = raw[offset + 4:end]
    lower = field.type_name.lower()
    if field.meta_size == 1 and ("string" in lower or lower.endswith("a")):
        value_repr = repr(data.rstrip(b"\x00").decode("ascii", errors="replace"))
    elif field.meta_size == 1:
        value_repr = data.hex()
    else:
        preview = data[: min(len(data), 32)].hex()
        value_repr = f"bytes={len(data)} preview={preview}"
    return end, value_repr, f"count={count}"


def _decode_dynamic_array(raw: bytes, offset: int, field: FieldDef, tail_cursor: int) -> tuple[int, str, str]:
    if offset + 5 > tail_cursor:
        raise ValueError("Dynamic array overruns region")
    if (
        offset + 14 <= tail_cursor
        and raw[offset:offset + 5] == b"\x00\x00\x06\x01\x00"
    ):
        count = _u32(raw, offset + 5)
        total = 9 + count * field.meta_size + 5
        end = offset + total
        if count < 0x10000 and end <= tail_cursor and raw[end - 5:end] == b"\x01" * 5:
            data_offset = offset + 9
            data_end = data_offset + count * field.meta_size
            data = raw[data_offset:data_end]
            preview = data[: min(len(data), 16)].hex()
            note = "dynamic primitive array (prefix 0000060100)"
            return end, f"count={count} bytes={len(data)} preview={preview}", note
    if (
        raw[offset] == 1
        and offset + 7 <= tail_cursor
    ):
        marker_end = offset
        while marker_end < tail_cursor and raw[marker_end] == 1:
            marker_end += 1
        if marker_end > offset and marker_end < tail_cursor and raw[marker_end] == 0 and marker_end + 5 <= tail_cursor:
            count = _u32(raw, marker_end + 1)
            total = (marker_end - offset + 1) + 4 + count * field.meta_size
            end = offset + total
            if (
                count < 0x10000
                and end <= tail_cursor
            ):
                if end < tail_cursor and raw[end] == 1:
                    end += 1
                data_offset = marker_end + 5
                data_end = data_offset + count * field.meta_size
                data = raw[data_offset:data_end]
                preview = data[: min(len(data), 16)].hex()
                note = f"dynamic primitive array (marker prefix len={marker_end - offset})"
                return end, f"count={count} bytes={len(data)} preview={preview}", note
    if (
        offset + 6 <= tail_cursor
        and raw[offset] == 0
        and raw[offset + 1] == 0
        and raw[offset + 4] == 0
        and raw[offset + 5] == 0
    ):
        count = _u16(raw, offset + 2)
        total = 6 + count * field.meta_size
        note = "dynamic primitive array (compact header)"
    else:
        prefix = raw[offset]
        count = _u32(raw, offset + 1)
        total = 1 + 4 + count * field.meta_size
        note = "dynamic primitive array"
        if prefix != 0:
            note += f" prefix=0x{prefix:02X}"
    end = offset + total
    if end > tail_cursor:
        raise ValueError("Dynamic array overruns region")
    data_offset = offset + (6 if note.startswith("dynamic primitive array (compact") else 5)
    data = raw[data_offset:end]
    preview = data[: min(len(data), 16)].hex()
    return end, f"count={count} bytes={len(data)} preview={preview}", note


def _compute_undecoded_ranges(
    block_start: int,
    block_end: int,
    header_end: int,
    fields: list[GenericFieldValue],
) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    cursor = header_end
    decoded = sorted(
        [(field.start_offset, field.end_offset) for field in fields if field.start_offset < field.end_offset],
        key=lambda item: item[0],
    )
    for start, end in decoded:
        if cursor < start:
            spans.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < block_end:
        spans.append((cursor, block_end))
    return spans


def _decode_inline_object_payload(
    raw: bytes,
    type_def: TypeDef,
    mask_bytes: bytes,
    payload_start: int,
    tail_cursor: int,
    type_by_index: dict[int, TypeDef],
) -> tuple[int, int, int, list[GenericFieldValue]]:
    if payload_start + 8 > tail_cursor:
        raise ValueError("Inline object payload overruns region")
    reserved_u32 = _u32(raw, payload_start)
    cursor = payload_start + 4
    fields: list[GenericFieldValue] = []

    for index, field in enumerate(type_def.fields):
        present = _field_present(mask_bytes, index)
        target = GenericFieldValue(
            field_index=index,
            name=field.name,
            type_name=field.type_name,
            meta_kind=field.meta_kind,
            meta_size=field.meta_size,
            meta_aux=field.meta_aux,
            present=present,
        )
        fields.append(target)
        if not present:
            target.decode_kind = "absent"
            continue

        if field.meta_kind in (0, 2) and field.meta_size > 0:
            if cursor + field.meta_size > tail_cursor:
                raise ValueError("Inline object scalar overruns region")
            end, value_repr, edit_format, editable = _decode_fixed_value(raw, cursor, field)
            target.decode_kind = "fixed_prefix"
            target.start_offset = cursor
            target.end_offset = end
            target.value_repr = value_repr
            target.edit_format = edit_format
            target.editable = editable
            cursor = end
            continue

        if field.meta_kind == 1 and field.meta_size > 0:
            end, value_repr, note_text = _decode_inline_bytes(raw, cursor, field, tail_cursor)
            target.decode_kind = "inline_bytes"
            target.start_offset = cursor
            target.end_offset = end
            target.value_repr = value_repr
            target.note = note_text
            cursor = end
            continue

        if field.meta_kind == 3 and field.meta_size > 0:
            end, value_repr, note_text = _decode_dynamic_array(raw, cursor, field, tail_cursor)
            target.decode_kind = "dynamic_array"
            target.start_offset = cursor
            target.end_offset = end
            target.value_repr = value_repr
            target.note = note_text
            cursor = end
            continue

        if field.meta_kind in (4, 5):
            end, locator = _decode_inline_object_locator(raw, cursor, tail_cursor, type_by_index, field.meta_kind)
            locator.field_index = index
            locator.name = field.name
            locator.type_name = field.type_name
            locator.meta_kind = field.meta_kind
            locator.meta_size = field.meta_size
            locator.meta_aux = field.meta_aux
            fields[-1] = locator
            cursor = end
            continue

        if field.meta_kind in (6, 7):
            end, list_field = _decode_object_list(raw, cursor, tail_cursor, type_by_index)
            list_field.field_index = index
            list_field.name = field.name
            list_field.type_name = field.type_name
            list_field.meta_kind = field.meta_kind
            list_field.meta_size = field.meta_size
            list_field.meta_aux = field.meta_aux
            fields[-1] = list_field
            cursor = end
            continue

        raise ValueError(f"Unsupported inline field kind {field.meta_kind}")

    size_field_offset = -1
    max_probe = tail_cursor - 4
    for probe in range(cursor, max_probe + 1):
        size_u32 = _u32(raw, probe)
        if size_u32 == probe - payload_start:
            size_field_offset = probe
            break
    if size_field_offset < 0:
        raise ValueError("Inline object missing trailing size")
    size_u32 = _u32(raw, size_field_offset)
    end = size_field_offset + 4
    return end, reserved_u32, size_u32, fields


def _decode_inline_object_locator(
    raw: bytes,
    cursor: int,
    tail_cursor: int,
    type_by_index: dict[int, TypeDef],
    locator_kind: int = 4,
) -> tuple[int, GenericFieldValue]:
    prefix_u16 = 0
    prefix_u8 = 0
    body_cursor = cursor
    if locator_kind == 5:
        if cursor + 1 > tail_cursor:
            raise ValueError("Object pointer locator overruns region")
        candidate_offsets = (0, 1, 2, 3, 4, 5, 6, 7, 8)
        body_cursor = -1
        for delta in candidate_offsets:
            probe = cursor + delta
            if probe + 2 > tail_cursor:
                continue
            child_mask_byte_count = _u16(raw, probe)
            if 0 < child_mask_byte_count <= 16:
                body_cursor = probe
                break
        if body_cursor < 0:
            raise ValueError("Invalid object pointer locator")
        prefix = raw[cursor:body_cursor]
        if len(prefix) >= 2:
            prefix_u16 = prefix[0] | (prefix[1] << 8)
        elif len(prefix) == 1:
            prefix_u16 = prefix[0]
        if len(prefix) >= 3:
            prefix_u8 = prefix[2]
    if body_cursor + 18 > tail_cursor:
        raise ValueError("Object locator overruns region")
    child_mask_byte_count = _u16(raw, body_cursor)
    if child_mask_byte_count <= 0 or child_mask_byte_count > 16:
        raise ValueError("Invalid child mask count")
    wrapper_end = body_cursor + 2 + child_mask_byte_count + 2 + 1 + 4 + 4 + 4
    if wrapper_end > tail_cursor:
        raise ValueError("Object locator overruns region")
    child_mask_bytes = raw[body_cursor + 2:body_cursor + 2 + child_mask_byte_count]
    child_type_index = _u16(raw, body_cursor + 2 + child_mask_byte_count)
    child_reserved_u8 = raw[body_cursor + 2 + child_mask_byte_count + 2]
    child_sentinel1_u32 = _u32(raw, body_cursor + 2 + child_mask_byte_count + 3)
    child_sentinel2_u32 = _u32(raw, body_cursor + 2 + child_mask_byte_count + 7)
    child_payload_offset = _u32(raw, body_cursor + 2 + child_mask_byte_count + 11)
    child_type_def = type_by_index.get(child_type_index)
    child_type_name = child_type_def.name if child_type_def is not None else f"<type {child_type_index}>"
    end = wrapper_end
    field = GenericFieldValue(
        field_index=0,
        name="",
        type_name="",
        meta_kind=locator_kind,
        meta_size=0,
        meta_aux=0,
        present=True,
        decode_kind="object_locator",
        start_offset=cursor,
        end_offset=end,
        value_repr=f"type={child_type_name} mask={child_mask_bytes.hex()} target=0x{child_payload_offset:X}",
        child_prefix_u16=prefix_u16,
        child_prefix_u8=prefix_u8,
        child_mask_byte_count=child_mask_byte_count,
        child_mask_bytes=child_mask_bytes,
        child_type_index=child_type_index,
        child_type_name=child_type_name,
        child_reserved_u8=child_reserved_u8,
        child_sentinel1_u32=child_sentinel1_u32,
        child_sentinel2_u32=child_sentinel2_u32,
        child_payload_offset=child_payload_offset,
    )
    if child_payload_offset == wrapper_end:
        if child_type_def is not None:
            child_end, child_reserved_u32, child_size_u32, child_fields = _decode_inline_object_payload(
                raw,
                child_type_def,
                child_mask_bytes,
                child_payload_offset,
                tail_cursor,
                type_by_index,
            )
            field.child_reserved_u32 = child_reserved_u32
            field.child_size_u32 = child_size_u32
            field.child_fields = child_fields
            field.child_undecoded_ranges = []
            end = child_end
            field.end_offset = child_end
        else:
            for probe in range(child_payload_offset, min(tail_cursor - 4, child_payload_offset + 512) + 1):
                size_u32 = _u32(raw, probe)
                if size_u32 == probe - child_payload_offset and size_u32 > 0:
                    end = probe + 4
                    field.end_offset = end
                    field.child_size_u32 = size_u32
                    break
    return end, field


def _decode_compact_list_element(
    raw: bytes,
    cursor: int,
    tail_cursor: int,
    type_by_index: dict[int, TypeDef],
) -> tuple[int, GenericFieldValue]:
    if cursor + 18 > tail_cursor:
        raise ValueError("Compact list element overruns region")
    child_mask_byte_count = _u16(raw, cursor)
    if child_mask_byte_count < 1 or child_mask_byte_count > 16:
        raise ValueError("Compact list element invalid mask count")
    header_size = 2 + child_mask_byte_count + 2 + 1 + 8 + 4
    if cursor + header_size > tail_cursor:
        raise ValueError("Compact list element overruns region")
    child_mask_bytes = raw[cursor + 2 : cursor + 2 + child_mask_byte_count]
    child_type_index = _u16(raw, cursor + 2 + child_mask_byte_count)
    child_reserved_u8 = raw[cursor + 2 + child_mask_byte_count + 2]
    sentinel_offset = cursor + 2 + child_mask_byte_count + 3
    if _u64(raw, sentinel_offset) != 0xFFFFFFFFFFFFFFFF:
        raise ValueError("Compact list element sentinel mismatch")
    child_payload_offset = _u32(raw, sentinel_offset + 8)
    child_type_def = type_by_index.get(child_type_index)
    if child_type_def is None:
        raise ValueError("Compact list element has invalid child type")
    if child_payload_offset != cursor + header_size:
        raise ValueError("Compact list element payload is not inline")
    child_end, child_reserved_u32, child_size_u32, child_fields = _decode_inline_object_payload(
        raw,
        child_type_def,
        child_mask_bytes,
        child_payload_offset,
        tail_cursor,
        type_by_index,
    )
    field = GenericFieldValue(
        field_index=0,
        name="",
        type_name="",
        meta_kind=6,
        meta_size=0,
        meta_aux=0,
        present=True,
        decode_kind="object_locator",
        start_offset=cursor,
        end_offset=child_end,
        value_repr=f"type={child_type_def.name} target=0x{child_payload_offset:X}",
        child_prefix_u16=child_prefix_u16,
        child_prefix_u8=0,
        child_mask_byte_count=1,
        child_mask_bytes=bytes([child_mask_byte]),
        child_type_index=child_type_index,
        child_type_name=child_type_def.name,
        child_reserved_u8=child_reserved_u8,
        child_sentinel1_u32=0xFFFFFFFF,
        child_sentinel2_u32=0xFFFFFFFF,
        child_payload_offset=child_payload_offset,
        child_reserved_u32=child_reserved_u32,
        child_size_u32=child_size_u32,
        child_fields=child_fields,
        child_undecoded_ranges=[],
        note="compact_list_element",
    )
    return child_end, field


def _decode_object_list_element(
    raw: bytes,
    cursor: int,
    tail_cursor: int,
    type_by_index: dict[int, TypeDef],
) -> tuple[int, GenericFieldValue]:
    last_error: ValueError | None = None
    for decoder in (_decode_inline_object_locator, _decode_compact_list_element):
        try:
            return decoder(raw, cursor, tail_cursor, type_by_index)
        except ValueError as exc:
            last_error = exc
    raise last_error or ValueError("Object list element decode failed")


def _decode_object_list(
    raw: bytes,
    cursor: int,
    tail_cursor: int,
    type_by_index: dict[int, TypeDef],
) -> tuple[int, GenericFieldValue]:
    if cursor + 18 > tail_cursor:
        raise ValueError("Object list overruns region")
    last_error: ValueError | None = None
    best_result: tuple[int, GenericFieldValue] | None = None
    for body_cursor in (cursor, cursor + 1, cursor + 2, cursor + 3):
        if body_cursor + 18 > tail_cursor:
            continue
        try:
            prefix_u8 = raw[body_cursor]
            marker_end = body_cursor
            while marker_end < tail_cursor and raw[marker_end] == 1:
                marker_end += 1
            if (
                marker_end > body_cursor
                and marker_end + 17 <= tail_cursor
                and raw[marker_end] == 0
                and raw[marker_end + 5:marker_end + 18] == b"\x00" * 13
            ):
                count = _u32(raw, marker_end + 1)
                reserved1_u32 = 0
                reserved2_u32 = 0
                reserved3_u32 = 0
                reserved4_u16 = 0
                reserved4_u32 = 0
                header_size = (marker_end - body_cursor + 1) + 4 + 13
            elif prefix_u8 == 0 and raw[body_cursor + 1] == 0 and raw[body_cursor + 2] == 0 and raw[body_cursor + 3] == 0:
                count = _u32(raw, body_cursor + 4)
                reserved1_u32 = 0
                reserved2_u32 = _u32(raw, body_cursor + 8)
                reserved3_u32 = _u32(raw, body_cursor + 12)
                reserved4_u16 = _u16(raw, body_cursor + 16)
                reserved4_u32 = 0
                header_size = 18
            elif prefix_u8 == 0:
                count = _u24(raw, body_cursor + 1)
                reserved1_u32 = _u32(raw, body_cursor + 4)
                reserved2_u32 = _u32(raw, body_cursor + 8)
                reserved3_u32 = _u32(raw, body_cursor + 12)
                reserved4_u16 = _u16(raw, body_cursor + 16)
                reserved4_u32 = 0
                header_size = 18
            elif prefix_u8 == 1 and body_cursor + 21 <= tail_cursor and raw[body_cursor + 1] == 1 and raw[body_cursor + 2] == 1 and raw[body_cursor + 3] == 0:
                count = _u32(raw, body_cursor + 4)
                reserved1_u32 = _u32(raw, body_cursor + 8)
                reserved2_u32 = _u32(raw, body_cursor + 12)
                reserved3_u32 = _u32(raw, body_cursor + 16)
                reserved4_u16 = 0
                reserved4_u32 = 0
                header_size = 21
            elif prefix_u8 == 1:
                if body_cursor + 19 > tail_cursor:
                    raise ValueError("Object list overruns region")
                count = _u16be(raw, body_cursor + 1)
                reserved1_u32 = _u32(raw, body_cursor + 3)
                reserved2_u32 = _u32(raw, body_cursor + 7)
                reserved3_u32 = _u32(raw, body_cursor + 11)
                reserved4_u16 = 0
                reserved4_u32 = _u32(raw, body_cursor + 15)
                header_size = 19
            else:
                raise ValueError(f"Unsupported object list prefix {prefix_u8}")
            element_cursor = body_cursor + header_size
            elements: list[GenericFieldValue] = []
            for index in range(count):
                try:
                    end, element = _decode_object_list_element(raw, element_cursor, tail_cursor, type_by_index)
                except ValueError:
                    try:
                        end, element = _decode_object_list_element(raw, element_cursor, tail_cursor, {})
                    except ValueError:
                        break
                element.field_index = index
                element.name = f"[{index}]"
                element.type_name = element.child_type_name
                element.decode_kind = "list_element"
                elements.append(element)
                element_cursor = end
            field = GenericFieldValue(
                field_index=0,
                name="",
                type_name="",
                meta_kind=6,
                meta_size=0,
                meta_aux=0,
                present=True,
                decode_kind="object_list",
                start_offset=cursor,
                end_offset=element_cursor,
                value_repr=f"prefix={prefix_u8} count={count}",
                list_prefix_u8=prefix_u8,
                list_count=count,
                list_reserved1_u32=reserved1_u32,
                list_reserved2_u32=reserved2_u32,
                list_reserved3_u32=reserved3_u32,
                list_reserved4_u16=reserved4_u16,
                list_reserved4_u32=reserved4_u32,
                list_header_size=element_cursor - cursor if count == 0 else (body_cursor - cursor) + header_size,
                list_elements=elements,
                note=(f"header_offset=+{body_cursor - cursor}" if body_cursor != cursor else ""),
            )
            result = (element_cursor, field)
            if best_result is None or result[0] > best_result[0]:
                best_result = result
        except ValueError as exc:
            last_error = exc
            continue
    if best_result is not None:
        return best_result
    raise last_error or ValueError("Object list decode failed")


def _decode_fields_in_region(
    raw: bytes,
    type_def: TypeDef,
    mask_bytes: bytes,
    region_start: int,
    region_end: int,
    type_by_index: dict[int, TypeDef],
    note: str = "",
) -> tuple[list[GenericFieldValue], list[tuple[int, int]]]:
    fields: list[GenericFieldValue] = [
        GenericFieldValue(
            field_index=i,
            name=f.name,
            type_name=f.type_name,
            meta_kind=f.meta_kind,
            meta_size=f.meta_size,
            meta_aux=f.meta_aux,
            present=_field_present(mask_bytes, i),
            decode_kind="unknown" if _field_present(mask_bytes, i) else "absent",
            note=note,
        )
        for i, f in enumerate(type_def.fields)
    ]

    tail_cursor = region_end
    for index in range(len(type_def.fields) - 1, -1, -1):
        field = type_def.fields[index]
        target = fields[index]
        if not target.present:
            target.decode_kind = "absent"
            continue
        if field.meta_kind not in (0, 2):
            break
        if field.meta_size <= 0 or tail_cursor - field.meta_size < region_start:
            break
        start = tail_cursor - field.meta_size
        end, value_repr, edit_format, editable = _decode_fixed_value(raw, start, field)
        if end != tail_cursor:
            break
        target.decode_kind = "fixed_suffix"
        target.start_offset = start
        target.end_offset = end
        target.value_repr = value_repr
        target.edit_format = edit_format
        target.editable = editable
        tail_cursor = start

    head_cursor = region_start
    for index, field in enumerate(type_def.fields):
        target = fields[index]
        if not target.present:
            target.decode_kind = "absent"
            continue
        if target.start_offset < target.end_offset:
            continue
        if head_cursor >= tail_cursor:
            break

        if field.meta_kind in (0, 2) and field.meta_size > 0:
            if head_cursor + field.meta_size > tail_cursor:
                break
            end, value_repr, edit_format, editable = _decode_fixed_value(raw, head_cursor, field)
            target.decode_kind = "fixed_prefix"
            target.start_offset = head_cursor
            target.end_offset = end
            target.value_repr = value_repr
            target.edit_format = edit_format
            target.editable = editable
            head_cursor = end
            continue

        if field.meta_kind == 1 and field.meta_size > 0:
            try:
                end, value_repr, note_text = _decode_inline_bytes(raw, head_cursor, field, tail_cursor)
            except ValueError:
                break
            target.decode_kind = "inline_bytes"
            target.start_offset = head_cursor
            target.end_offset = end
            target.value_repr = value_repr
            target.note = note_text
            head_cursor = end
            continue

        if field.meta_kind == 3 and field.meta_size > 0:
            try:
                end, value_repr, note_text = _decode_dynamic_array(raw, head_cursor, field, tail_cursor)
            except ValueError:
                break
            target.decode_kind = "dynamic_array"
            target.start_offset = head_cursor
            target.end_offset = end
            target.value_repr = value_repr
            target.note = note_text
            head_cursor = end
            continue

        if field.meta_kind in (4, 5):
            try:
                end, locator = _decode_inline_object_locator(raw, head_cursor, tail_cursor, type_by_index, field.meta_kind)
            except ValueError:
                break
            locator.field_index = index
            locator.name = field.name
            locator.type_name = field.type_name
            locator.meta_kind = field.meta_kind
            locator.meta_size = field.meta_size
            locator.meta_aux = field.meta_aux
            fields[index] = locator
            head_cursor = end
            continue

        if field.meta_kind in (6, 7):
            try:
                end, list_field = _decode_object_list(raw, head_cursor, tail_cursor, type_by_index)
            except ValueError:
                break
            list_field.field_index = index
            list_field.name = field.name
            list_field.type_name = field.type_name
            list_field.meta_kind = field.meta_kind
            list_field.meta_size = field.meta_size
            list_field.meta_aux = field.meta_aux
            fields[index] = list_field
            head_cursor = end
            continue

        break

    undecoded_ranges = _compute_undecoded_ranges(region_start, region_end, region_start, fields)

    return fields, undecoded_ranges


def decode_object_blocks(raw: bytes, toc_entries: list[TocEntry], types: list[TypeDef]) -> list[ObjectBlock]:
    type_by_index = {t.index: t for t in types}
    blocks: list[ObjectBlock] = []

    for entry in toc_entries:
        type_def = type_by_index.get(entry.class_index)
        if type_def is None:
            continue
        block_start = entry.data_offset
        block_end = min(len(raw), entry.data_offset + entry.data_size)
        if block_end <= block_start:
            continue

        expected_mask_bytes = max(1, (len(type_def.fields) + 7) // 8)
        if block_start + 2 > block_end:
            continue
        actual_mask_bytes = _u16(raw, block_start)
        mask_byte_count = expected_mask_bytes
        note = ""
        if 0 < actual_mask_bytes <= 16:
            mask_byte_count = actual_mask_bytes
        if mask_byte_count != expected_mask_bytes:
            note = f"expected_mask_bytes={expected_mask_bytes}"
        header_end = block_start + 2 + mask_byte_count + 4
        if header_end > block_end:
            continue
        mask_bytes = raw[block_start + 2:block_start + 2 + mask_byte_count]
        reserved_u32 = _u32(raw, block_start + 2 + mask_byte_count)
        fields, undecoded_ranges = _decode_fields_in_region(
            raw,
            type_def,
            mask_bytes,
            header_end,
            block_end,
            type_by_index,
            note=note,
        )
        blocks.append(
            ObjectBlock(
                entry_index=entry.index,
                class_index=entry.class_index,
                class_name=entry.class_name,
                data_offset=entry.data_offset,
                data_size=entry.data_size,
                mask_byte_count=mask_byte_count,
                header_mask_bytes=mask_bytes,
                reserved_u32=reserved_u32,
                fields=fields,
                undecoded_ranges=undecoded_ranges,
            )
        )

    return blocks


def classify_type_indices(types: list[TypeDef]) -> dict[str, int]:
    out: dict[str, int] = {}
    for t in types:
        out[t.name] = t.index
    return out


def parse_character_stats(raw: bytes, toc_entries: list[TocEntry], type_map: dict[str, int]) -> CharacterStats:
    target_class = type_map.get("CharacterStatusSaveData")
    if target_class is None:
        return CharacterStats(found=False)

    for entry in toc_entries:
        if entry.class_index != target_class:
            continue
        if entry.data_offset + 7 > len(raw):
            break

        bitmask = raw[entry.data_offset + 2]
        fpos = entry.data_offset + 7
        result = CharacterStats(found=True, bitmask=bitmask, dataOffset=entry.data_offset, dataSize=entry.data_size)

        if bitmask & 0x01:
            result.characterKey = _u32(raw, fpos)
            fpos += 4
        if bitmask & 0x02:
            result.factionKey = _u32(raw, fpos)
            fpos += 4
        if bitmask & 0x04:
            result.level = _u32(raw, fpos)
            fpos += 4
        if bitmask & 0x08:
            fpos += 8
        if bitmask & 0x10:
            fpos += 8
        if bitmask & 0x20:
            fpos += 2
        if bitmask & 0x40:
            hp_bytes = raw[fpos:fpos + 8]
            result.currentHp = _i64(raw, fpos)
            result.currentHpRawLo, result.currentHpRawHi = struct.unpack("<II", hp_bytes)
            fpos += 8
        if bitmask & 0x80:
            mp_bytes = raw[fpos:fpos + 8]
            result.currentMp = _i64(raw, fpos)
            result.currentMpRawLo, result.currentMpRawHi = struct.unpack("<II", mp_bytes)
            fpos += 8
        return result

    return CharacterStats(found=False)


def scan_items(raw: bytes, toc_entries: list[TocEntry], type_map: dict[str, int]) -> list[ItemRecord]:
    class_equipment = type_map.get("EquipmentSaveData", 0xFFFF)
    class_inventory = type_map.get("InventorySaveData", 0xFFFF)
    class_store = type_map.get("StoreSaveData", 0xFFFF)
    class_mercenary = type_map.get("MercenaryClanSaveData", 0xFFFF)
    class_field_npc = type_map.get("FieldNPCSaveData", 0xFFFF)
    items: list[ItemRecord] = []

    target_classes = {
        class_equipment: 0,
        class_inventory: 1,
        class_store: 2,
        class_mercenary: 3,
        class_field_npc: 3,
    }

    seen_offsets: set[int] = set()
    for entry in toc_entries:
        source_type = target_classes.get(entry.class_index)
        if source_type is None:
            continue
        block_start = max(0, entry.data_offset)
        block_end = min(len(raw), entry.data_offset + entry.data_size)
        if block_end - block_start < 34:
            continue

        for off in range(block_start, block_end - 34):
            if off in seen_offsets:
                continue
            if _u32(raw, off) != 1:
                continue

            item_no = _u64(raw, off + 4)
            if item_no < 1 or item_no > 999999:
                continue

            item_key = _u32(raw, off + 12)
            if item_key < 1 or item_key > 0x7FFFFFFF:
                continue

            slot_no = _u16(raw, off + 16)
            if slot_no > 200:
                continue

            stack_count = _u64(raw, off + 18)
            if stack_count < 1 or stack_count > 99999:
                continue

            if off >= 16 and _u64(raw, off - 16) != 0xFFFFFFFFFFFFFFFF:
                continue

            field1A = _u16(raw, off + 26)
            field1E = _u16(raw, off + 30)
            field20 = _u16(raw, off + 32)

            has_enchant = field1A != 0xFFFF
            is_equipment = has_enchant or (field20 > 0)
            enchant = field1A if has_enchant else 0
            sharpness = field1E
            endurance = field20
            section = 0 if source_type in (0, 1) else 1
            seen_offsets.add(off)

            items.append(
                ItemRecord(
                    offset=off,
                    itemNo=item_no,
                    itemKey=item_key,
                    slot=slot_no,
                    stack=stack_count,
                    enchant=enchant,
                    sharpness=sharpness,
                    endurance=endurance,
                    hasEnchant=has_enchant,
                    isEquipment=is_equipment,
                    section=section,
                    sourceType=source_type,
                    source=SOURCE_NAMES[source_type],
                    classIndex=entry.class_index,
                    className=entry.class_name,
                    blockSize=entry.data_size,
                )
            )

    return items


def _scan_bag_expansion_range(
    raw: bytes,
    start: int,
    end: int,
    class_index: int,
    class_name: str,
    block_offset: int,
    block_size: int,
    seen_offsets: set[int],
) -> list[BagSlot]:
    found: list[BagSlot] = []
    end = min(end, len(raw))
    for off in range(max(0, start), end - 25):
        if off in seen_offsets:
            continue
        if _u64(raw, off) != 0xFFFFFFFFFFFFFFFF:
            continue
        if any(raw[off + i] != 0 for i in range(10, 16)):
            continue

        inv_key = _u16(raw, off + 16)
        expand_count = _u16(raw, off + 18)
        if inv_key > 20 or expand_count == 0 or expand_count > 500:
            continue

        list_prefix = raw[off + 20]
        list_count = _u32(raw, off + 21)
        if list_prefix != 0 or list_count > 1000:
            continue

        field_hash = _u32(raw, off - 14) if off >= 14 else 0
        seen_offsets.add(off)
        found.append(
            BagSlot(
                offset=off + 18,
                category=inv_key,
                expansion=expand_count,
                hash=field_hash,
                isMainBag=field_hash == MAIN_BAG_HASH,
                classIndex=class_index,
                className=class_name,
                blockOffset=block_offset,
                blockSize=block_size,
            )
        )
    return found


def scan_bag_expansion(raw: bytes, toc_entries: list[TocEntry], type_map: dict[str, int]) -> list[BagSlot]:
    target_classes = {
        type_map.get("InventoryElementSaveData", -1),
        type_map.get("InventorySaveData", -1),
    }
    found: list[BagSlot] = []
    seen_offsets: set[int] = set()

    for entry in toc_entries:
        if entry.class_index not in target_classes:
            continue
        found.extend(
            _scan_bag_expansion_range(
                raw,
                entry.data_offset,
                entry.data_offset + entry.data_size,
                entry.class_index,
                entry.class_name,
                entry.data_offset,
                entry.data_size,
                seen_offsets,
            )
        )

    if found:
        return found

    scan_end = min(len(raw), 0x100000)
    return _scan_bag_expansion_range(raw, 0x7000, scan_end, 0xFFFF, "<global_scan>", 0, 0, seen_offsets)


def summarize_sources(items: list[ItemRecord]) -> dict[str, int]:
    counts = Counter(item.source for item in items)
    return {name: counts.get(name, 0) for name in SOURCE_NAMES}


def to_jsonable(obj: Any) -> Any:
    if isinstance(obj, Path):
        return str(obj)
    if hasattr(obj, "__dataclass_fields__"):
        return {k: to_jsonable(v) for k, v in asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, tuple):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, (bytes, bytearray)):
        return obj.hex()
    return obj


def build_result(
    path: Path,
    key_hex: str,
    uncompressed_size: int | None = None,
    include_legacy: bool = False,
) -> dict[str, Any]:
    raw, load_meta = load_raw_blob(path, key_hex, uncompressed_size)
    return build_result_from_raw(raw, load_meta, include_legacy=include_legacy)


def build_result_from_raw(
    raw: bytes | bytearray,
    load_meta: dict[str, Any],
    include_legacy: bool = False,
) -> dict[str, Any]:
    raw = bytes(raw)
    schema = parse_schema(raw)
    type_names = [t.name for t in schema["types"]]
    type_map = classify_type_indices(schema["types"])
    toc = parse_toc(raw, schema["schema_end"], type_names)
    objects = decode_object_blocks(raw, toc["entries"], schema["types"])

    result = {
        "input": load_meta,
        "raw": {
            "size": len(raw),
            "schema_end": schema["schema_end"],
            "value_section_offset": schema["schema_end"],
            "value_section_size": len(raw) - schema["schema_end"],
        },
        "schema": {
            "header_tag": schema["header_tag"],
            "header_zero": schema["header_zero"],
            "type_count": schema["type_count"],
            "root_type": schema["root_type"],
            "types": schema["types"],
        },
        "toc": {
            "prefix_zero": toc["prefix_zero"],
            "entry_count": toc["toc_count"],
            "stream_size": toc["stream_size"],
            "entries": toc["entries"],
        },
        "objects": objects,
    }
    if include_legacy:
        character = parse_character_stats(raw, toc["entries"], type_map)
        items = scan_items(raw, toc["entries"], type_map)
        bags = scan_bag_expansion(raw, toc["entries"], type_map)
        result["character"] = character
        result["items"] = items
        result["items_summary"] = {
            "count": len(items),
            "player_count": sum(1 for item in items if item.section == 0),
            "sources": summarize_sources(items),
        }
        result["bagExpansion"] = bags
    return result


def write_inventory_json(result: dict[str, Any], out_path: Path) -> None:
    items: list[ItemRecord] = result["items"]
    character: CharacterStats = result["character"]
    bags: list[BagSlot] = result["bagExpansion"]

    doc: dict[str, Any] = {
        "character": to_jsonable(character) if character.found else None,
        "itemCount": len(items),
        "playerItemCount": sum(1 for item in items if item.section == 0),
        "items": [
            {
                "itemNo": item.itemNo,
                "itemKey": item.itemKey,
                "slot": item.slot,
                "stack": item.stack,
                "enchant": item.enchant,
                "endurance": item.endurance,
                "sharpness": item.sharpness,
                "isEquipment": item.isEquipment,
                "hasEnchant": item.hasEnchant,
                "section": item.section,
                "source": item.source,
                "offset": item.offset,
                "className": item.className,
            }
            for item in items
        ],
        "bagExpansion": [
            {
                "category": bag.category,
                "expansion": bag.expansion,
                "hash": f"{bag.hash:08X}",
                "isMainBag": bag.isMainBag,
                "offset": bag.offset,
            }
            for bag in bags
        ],
    }
    out_path.write_text(json.dumps(doc, indent=2), encoding="utf-8")


def write_refinement_json(result: dict[str, Any], out_path: Path) -> None:
    items: list[ItemRecord] = result["items"]
    equipment = [
        {
            "itemNo": item.itemNo,
            "itemKey": item.itemKey,
            "slot": item.slot,
            "enchant": item.enchant,
            "endurance": item.endurance,
            "sharpness": item.sharpness,
            "offset": item.offset,
            "className": item.className,
        }
        for item in items
        if item.section == 0 and (item.isEquipment or item.hasEnchant)
    ]
    doc = {"equipmentCount": len(equipment), "equipment": equipment}
    out_path.write_text(json.dumps(doc, indent=2), encoding="utf-8")


def cmd_summary(args: argparse.Namespace) -> int:
    result = build_result(Path(args.input_file), args.key, args.uncompressed_size, include_legacy=True)
    print(f"input_kind: {result['input']['input_kind']}")
    print(f"raw_size: {result['raw']['size']}")
    print(f"root_type: {result['schema']['root_type']}")
    print(f"type_count: {result['schema']['type_count']}")
    print(f"schema_end: 0x{result['raw']['schema_end']:X}")
    print(f"toc_entries: {result['toc']['entry_count']}")
    print(f"character_found: {result['character'].found}")
    if result["character"].found:
        ch: CharacterStats = result["character"]
        print(f"character_level: {ch.level}")
        print(f"character_hp: {ch.currentHp}")
        print(f"character_mp: {ch.currentMp}")
    summary = result["items_summary"]
    print(f"item_count: {summary['count']}")
    print(f"player_item_count: {summary['player_count']}")
    for name, count in summary["sources"].items():
        print(f"{name.lower()}_count: {count}")
    print(f"bag_slot_count: {len(result['bagExpansion'])}")
    return 0


def cmd_json(args: argparse.Namespace) -> int:
    result = build_result(Path(args.input_file), args.key, args.uncompressed_size)
    text = json.dumps(to_jsonable(result), indent=2)
    if args.out_file:
        Path(args.out_file).write_text(text, encoding="utf-8")
        print(f"wrote: {args.out_file}")
    else:
        print(text)
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    result = build_result(Path(args.input_file), args.key, args.uncompressed_size, include_legacy=True)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    summary_path = outdir / "summary.json"
    inventory_path = outdir / "inventory.json"
    refinement_path = outdir / "refinement.json"

    summary_path.write_text(json.dumps(to_jsonable(result), indent=2), encoding="utf-8")
    write_inventory_json(result, inventory_path)
    write_refinement_json(result, refinement_path)

    print(f"wrote: {summary_path}")
    print(f"wrote: {inventory_path}")
    print(f"wrote: {refinement_path}")
    return 0


def cmd_dump(args: argparse.Namespace) -> int:
    result = build_result(Path(args.dump), args.key, args.uncompressed_size)
    text = json.dumps(to_jsonable(result), indent=2)
    if args.dump_out:
        Path(args.dump_out).write_text(text, encoding="utf-8")
        print(f"wrote: {args.dump_out}")
    else:
        print(text)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Crimson Desert offline save parser")
    p.add_argument("--key", default=save_crypto.DEFAULT_KEY_HEX, help="32-byte save key as hex")
    p.add_argument("--dump", metavar="INPUT_FILE", help="Read a save/payload/raw file and dump the generic parsed structure as JSON")
    p.add_argument("--dump-out", metavar="OUT_FILE", help="Write --dump output to this JSON path instead of stdout")
    p.add_argument(
        "--uncompressed-size",
        type=int,
        help="Force LZ4 output size when the input is a compressed plaintext payload rather than a SAVE container",
    )
    sub = p.add_subparsers(dest="cmd", required=False)

    s = sub.add_parser("summary", help="Print a concise parse summary")
    s.add_argument("input_file")
    s.set_defaults(func=cmd_summary)

    s = sub.add_parser("json", help="Write the parsed structure as JSON")
    s.add_argument("input_file")
    s.add_argument("out_file", nargs="?")
    s.set_defaults(func=cmd_json)

    s = sub.add_parser("export", help="Export summary.json, inventory.json, and refinement.json")
    s.add_argument("input_file")
    s.add_argument("outdir")
    s.set_defaults(func=cmd_export)

    return p


def main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.dump:
        return cmd_dump(args)
    if not getattr(args, "cmd", None):
        parser.error("either --dump INPUT_FILE or a subcommand is required")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
