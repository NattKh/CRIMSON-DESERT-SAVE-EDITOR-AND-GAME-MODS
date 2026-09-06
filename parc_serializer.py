
import struct
from dataclasses import dataclass, field as dataclass_field
from typing import List, Dict, Optional, Tuple, Any


@dataclass
class FieldDef:
    name: str
    type_name: str
    meta_kind: int
    meta_size: int
    meta_aux: int


@dataclass
class TypeDef:
    index: int
    name: str
    fields: List[FieldDef]

    def bitmask_width(self) -> int:
        n = len(self.fields)
        if n <= 8: return 1
        elif n <= 16: return 2
        elif n <= 32: return 4
        elif n <= 64: return 8
        return 8


@dataclass
class TOCEntry:
    index: int
    class_index: int
    sentinel1: int
    sentinel2: int
    data_offset: int
    data_size: int


@dataclass
class ParcBlob:
    raw: bytes
    header: bytes
    schema_bytes: bytes
    schema_offset: int
    schema_end: int
    toc_header_bytes: bytes
    toc_offset: int
    types: List[TypeDef]
    type_by_index: Dict[int, TypeDef]
    toc_entries: List[TOCEntry]
    data_start: int
    num_root_entries: int
    stream_size: int

    block_raw: Dict[int, bytes]

    modified_blocks: Dict[int, bytes]


def _u16(data, off): return struct.unpack_from("<H", data, off)[0]
def _u32(data, off): return struct.unpack_from("<I", data, off)[0]
def _u64(data, off): return struct.unpack_from("<Q", data, off)[0]
def _i32(data, off): return struct.unpack_from("<i", data, off)[0]
def _i64(data, off): return struct.unpack_from("<q", data, off)[0]
def _f32(data, off): return struct.unpack_from("<f", data, off)[0]
def _f64(data, off): return struct.unpack_from("<d", data, off)[0]


def _field_present(mask_bytes: bytes, field_index: int) -> bool:
    byte_idx = field_index // 8
    bit_idx = field_index % 8
    if byte_idx >= len(mask_bytes):
        return False
    return bool(mask_bytes[byte_idx] & (1 << bit_idx))


def _bitmask_width(num_fields: int) -> int:
    if num_fields <= 8: return 1
    elif num_fields <= 16: return 2
    elif num_fields <= 32: return 4
    elif num_fields <= 64: return 8
    return 8


def parse_parc_blob(data: bytes) -> ParcBlob:

    if len(data) < 14:
        raise ValueError("Blob too small")
    magic = _u16(data, 0)
    if magic != 0xFFFF:
        raise ValueError(f"Bad inner magic: 0x{magic:04X}")
    header = data[:14]

    schema_offset = 14
    pos = schema_offset
    num_root_entries = _u32(data, pos)
    num_types = _u16(data, pos + 4)
    pos += 6

    types = []
    type_by_index = {}
    for i in range(num_types):
        name_len = _u32(data, pos); pos += 4
        name = data[pos:pos + name_len].decode("utf-8"); pos += name_len
        field_count = _u16(data, pos); pos += 2

        fields = []
        for j in range(field_count):
            fname_len = _u32(data, pos); pos += 4
            fname = data[pos:pos + fname_len].decode("utf-8"); pos += fname_len
            tname_len = _u32(data, pos); pos += 4
            tname = data[pos:pos + tname_len].decode("utf-8"); pos += tname_len
            mk = _u16(data, pos)
            ms = _u16(data, pos + 2)
            ma = _u32(data, pos + 4)
            pos += 8
            fields.append(FieldDef(name=fname, type_name=tname, meta_kind=mk, meta_size=ms, meta_aux=ma))

        td = TypeDef(index=i, name=name, fields=fields)
        types.append(td)
        type_by_index[i] = td

    schema_end = pos
    schema_bytes = data[schema_offset:schema_end]

    toc_offset = schema_end
    reserved = _u32(data, pos)
    entry_count = _u32(data, pos + 4)
    stream_size = _u32(data, pos + 8)
    toc_header_bytes = data[pos:pos + 12]
    pos += 12

    toc_entries = []
    for i in range(entry_count):
        ci = _u32(data, pos)
        s1 = _u32(data, pos + 4)
        s2 = _u32(data, pos + 8)
        doff = _u32(data, pos + 12)
        dsz = _u32(data, pos + 16)
        toc_entries.append(TOCEntry(index=i, class_index=ci, sentinel1=s1, sentinel2=s2,
                                     data_offset=doff, data_size=dsz))
        pos += 20

    data_start = pos

    block_raw = {}
    for entry in toc_entries:
        block_raw[entry.index] = data[entry.data_offset:entry.data_offset + entry.data_size]

    return ParcBlob(
        raw=data,
        header=header,
        schema_bytes=schema_bytes,
        schema_offset=schema_offset,
        schema_end=schema_end,
        toc_header_bytes=toc_header_bytes,
        toc_offset=toc_offset,
        types=types,
        type_by_index=type_by_index,
        toc_entries=toc_entries,
        data_start=data_start,
        num_root_entries=num_root_entries,
        stream_size=stream_size,
        block_raw=block_raw,
        modified_blocks={},
    )


def serialize_parc(parc: ParcBlob) -> bytes:
    out = bytearray()

    out.extend(parc.header)

    out.extend(parc.schema_bytes)

    new_block_data = {}
    for entry in parc.toc_entries:
        if entry.index in parc.modified_blocks:
            new_block_data[entry.index] = parc.modified_blocks[entry.index]
        else:
            new_block_data[entry.index] = parc.block_raw[entry.index]

    toc_size = 12 + len(parc.toc_entries) * 20
    new_data_start = parc.schema_end + toc_size

    current_offset = new_data_start
    new_toc_entries = []
    for entry in parc.toc_entries:
        block_bytes = new_block_data[entry.index]
        new_entry = TOCEntry(
            index=entry.index,
            class_index=entry.class_index,
            sentinel1=entry.sentinel1,
            sentinel2=entry.sentinel2,
            data_offset=current_offset,
            data_size=len(block_bytes),
        )
        new_toc_entries.append(new_entry)
        current_offset += len(block_bytes)

    total_size = current_offset

    out.extend(struct.pack("<I", 0))
    out.extend(struct.pack("<I", len(new_toc_entries)))
    out.extend(struct.pack("<I", total_size))

    for entry in new_toc_entries:
        out.extend(struct.pack("<I", entry.class_index))
        out.extend(struct.pack("<I", entry.sentinel1))
        out.extend(struct.pack("<I", entry.sentinel2))
        out.extend(struct.pack("<I", entry.data_offset))
        out.extend(struct.pack("<I", entry.data_size))

    for entry in new_toc_entries:
        out.extend(new_block_data[entry.index])

    _fixup_global_self_references(out, parc, new_toc_entries)

    return bytes(out)


def _fixup_global_self_references(
    out: bytearray,
    old_parc: ParcBlob,
    new_toc_entries: list,
) -> None:
    shifted_blocks = []
    for i, new_entry in enumerate(new_toc_entries):
        old_entry = old_parc.toc_entries[i]
        d = new_entry.data_offset - old_entry.data_offset
        if d != 0:
            shifted_blocks.append((new_entry.data_offset,
                                    new_entry.data_offset + new_entry.data_size, d))

    if not shifted_blocks:
        return

    delta_set = set(d for _, _, d in shifted_blocks)
    if len(delta_set) != 1:
        return
    delta = delta_set.pop()

    type_indices = set(old_parc.type_by_index.keys())

    sentinel = b'\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF'

    for block_start, block_end, _ in shifted_blocks:
        pos = block_start
        while pos < block_end - 12:
            if out[pos:pos + 8] != sentinel:
                pos += 1
                continue

            ref_pos = pos + 8
            if ref_pos + 4 > block_end:
                pos += 1
                continue

            old_ref = struct.unpack_from('<I', out, ref_pos)[0]

            expected_old_ref = (pos - delta) + 12
            if old_ref == expected_old_ref:
                new_ref = old_ref + delta
                struct.pack_into('<I', out, ref_pos, new_ref)
                pos += 12
                continue

            found_valid = False
            for mbc in (1, 2, 3, 4, 8):
                loc_start = pos - (mbc + 5)
                if loc_start < block_start:
                    continue
                mbc_read = struct.unpack_from('<H', out, loc_start)[0]
                if mbc_read != mbc:
                    continue
                type_idx_pos = loc_start + 2 + mbc
                if type_idx_pos + 3 > pos:
                    continue
                type_idx = struct.unpack_from('<H', out, type_idx_pos)[0]
                if type_idx not in type_indices:
                    continue
                reserved = out[type_idx_pos + 2]
                if reserved != 0:
                    continue
                new_ref = old_ref + delta
                struct.pack_into('<I', out, ref_pos, new_ref)
                found_valid = True
                break

            pos += 12 if found_valid else 1


class BlockParser:

    def __init__(self, parc: ParcBlob):
        self.parc = parc
        self.data = parc.raw

    def parse_root_block(self, toc_index: int) -> dict:
        entry = self.parc.toc_entries[toc_index]
        typedef = self.parc.type_by_index[entry.class_index]
        pos = entry.data_offset
        tail = pos + entry.data_size

        mask_byte_count = _u16(self.data, pos); pos += 2
        mask_bytes = self.data[pos:pos + mask_byte_count]; pos += mask_byte_count
        context = _u32(self.data, pos); pos += 4

        fields, pos = self._parse_fields(typedef, mask_bytes, pos, tail)

        return {
            "toc_index": toc_index,
            "class_index": entry.class_index,
            "class_name": typedef.name,
            "mask_byte_count": mask_byte_count,
            "mask_bytes": mask_bytes,
            "context": context,
            "fields": fields,
            "raw_tail": self.data[pos:tail],
        }

    def _parse_fields(self, typedef: TypeDef, mask_bytes: bytes, pos: int, tail: int) -> Tuple[list, int]:
        fields = []
        for i, fdef in enumerate(typedef.fields):
            present = _field_present(mask_bytes, i)
            if not present:
                fields.append({
                    "field_index": i,
                    "name": fdef.name,
                    "present": False,
                })
                continue

            start = pos
            value, pos = self._parse_field_value(fdef, pos, tail)
            fields.append({
                "field_index": i,
                "name": fdef.name,
                "present": True,
                "raw": self.data[start:pos],
                "value": value,
                "start": start,
                "end": pos,
            })

        return fields, pos

    def _parse_field_value(self, fdef: FieldDef, pos: int, tail: int) -> Tuple[Any, int]:
        mk = fdef.meta_kind
        ms = fdef.meta_size

        if mk in (0, 2) and ms > 0:
            return self._read_scalar(fdef, pos), pos + ms

        if mk == 1 and ms > 0:
            count = _u32(self.data, pos)
            end = pos + 4 + count * ms
            return {"kind": "inline_bytes", "count": count, "data": self.data[pos + 4:end]}, end

        if mk == 3 and ms > 0:
            return self._parse_dynamic_array(fdef, pos, tail)

        if mk in (4, 5):
            return self._parse_object_locator(fdef, pos, tail, mk)

        if mk in (6, 7):
            return self._parse_object_list(fdef, pos, tail)

        raise ValueError(f"Unknown meta_kind={mk} for field {fdef.name}")

    def _parse_dynamic_array(self, fdef: FieldDef, pos: int, tail: int):
        ms = fdef.meta_size
        start = pos

        if (pos + 14 <= tail and
            self.data[pos:pos + 5] == b'\x00\x00\x06\x01\x00'):
            count = _u32(self.data, pos + 5)
            end = pos + 9 + count * ms + 5
            if count < 0x10000 and end <= tail and self.data[end - 5:end] == b'\x01\x01\x01\x01\x01':
                return {"kind": "dynamic_array", "raw": self.data[start:end]}, end

        if self.data[pos] == 1 and pos + 7 <= tail:
            marker_end = pos
            while marker_end < tail and self.data[marker_end] == 1:
                marker_end += 1
            if (marker_end > pos and marker_end < tail and
                self.data[marker_end] == 0 and marker_end + 5 <= tail):
                count = _u32(self.data, marker_end + 1)
                end = pos + (marker_end - pos + 1) + 4 + count * ms
                if count < 0x10000 and end <= tail:
                    if end < tail and self.data[end] == 1:
                        end += 1
                    return {"kind": "dynamic_array", "raw": self.data[start:end]}, end

        if (pos + 6 <= tail and
            self.data[pos] == 0 and self.data[pos + 1] == 0 and
            self.data[pos + 4] == 0 and self.data[pos + 5] == 0):
            count = _u16(self.data, pos + 2)
            end = pos + 6 + count * ms
            if end <= tail:
                return {"kind": "dynamic_array", "raw": self.data[start:end]}, end

        prefix = self.data[pos]
        count = _u32(self.data, pos + 1)
        end = pos + 5 + count * ms
        if count < 0x1000000 and end <= tail:
            return {"kind": "dynamic_array", "raw": self.data[start:end]}, end

        raise ValueError(f"Dynamic array decode failed at 0x{pos:X}")

    def _read_scalar(self, fdef: FieldDef, pos: int):
        ms = fdef.meta_size
        tname = fdef.type_name.lower()
        if ms == 1: return self.data[pos]
        elif ms == 2:
            if "int16" in tname: return struct.unpack_from("<h", self.data, pos)[0]
            return _u16(self.data, pos)
        elif ms == 4:
            if "float" in tname: return _f32(self.data, pos)
            if "int32" in tname: return _i32(self.data, pos)
            return _u32(self.data, pos)
        elif ms == 8:
            if "double" in tname: return _f64(self.data, pos)
            if "int64" in tname: return _i64(self.data, pos)
            return _u64(self.data, pos)
        return int.from_bytes(self.data[pos:pos + ms], "little")

    def _parse_object_locator(self, fdef, pos, tail, locator_kind):
        start = pos
        body_cursor = pos

        if locator_kind == 5:
            body_cursor = None
            for delta in [0, 1, 3]:
                probe = pos + delta
                if probe + 2 > tail:
                    continue
                mbc = _u16(self.data, probe)
                if 0 < mbc <= 16:
                    body_cursor = probe
                    break
            if body_cursor is None:
                raise ValueError(f"Invalid object pointer locator at 0x{pos:X}")

        if body_cursor + 18 > tail:
            raise ValueError(f"Object locator overruns at 0x{body_cursor:X}")

        child_mask_byte_count = _u16(self.data, body_cursor)
        if child_mask_byte_count == 0 or child_mask_byte_count > 16:
            raise ValueError(f"Invalid mask count {child_mask_byte_count}")

        off = body_cursor + 2 + child_mask_byte_count
        wrapper_end = off + 2 + 1 + 4 + 4 + 4

        child_mask_bytes = self.data[body_cursor + 2:body_cursor + 2 + child_mask_byte_count]
        child_type_index = _u16(self.data, off)
        child_payload_offset = _u32(self.data, off + 11)

        end = wrapper_end

        if child_type_index in self.parc.type_by_index and child_payload_offset == wrapper_end:
            child_typedef = self.parc.type_by_index[child_type_index]
            _, end = self._parse_inline_payload(child_typedef, child_mask_bytes,
                                                 child_payload_offset, tail)

        raw = self.data[start:end]
        return {"kind": "object_locator", "raw": raw, "locator_kind": locator_kind}, end

    def _parse_inline_payload(self, typedef, mask_bytes, payload_start, tail):
        if payload_start + 8 > tail:
            raise ValueError(f"Inline payload overruns at 0x{payload_start:X}")

        reserved = _u32(self.data, payload_start)
        cursor = payload_start + 4

        for i, fdef in enumerate(typedef.fields):
            if not _field_present(mask_bytes, i):
                continue
            _, cursor = self._parse_field_value(fdef, cursor, tail)

        size_field_offset = -1
        max_probe = min(tail - 4, cursor + 64)
        for probe in range(cursor, max_probe + 1):
            if _u32(self.data, probe) == probe - payload_start:
                size_field_offset = probe
                break

        if size_field_offset < 0:
            end = cursor
        else:
            end = size_field_offset + 4

        return None, end

    def _parse_object_list(self, fdef, pos, tail):
        start = pos
        best_end = None
        last_error = None

        for delta in [0, 1, 2, 3]:
            body = pos + delta
            if body + 18 > tail:
                continue
            try:
                prefix_u8 = self.data[body]
                count = 0
                header_size = 0

                marker_end = body
                while marker_end < tail and self.data[marker_end] == 1:
                    marker_end += 1

                if (marker_end > body and marker_end + 17 <= tail
                        and self.data[marker_end] == 0
                        and self.data[marker_end + 5:marker_end + 18] == b'\x00' * 13):
                    count = _u32(self.data, marker_end + 1)
                    header_size = (marker_end - body + 1) + 4 + 13
                elif (prefix_u8 == 0 and self.data[body + 1] == 0
                      and self.data[body + 2] == 0 and self.data[body + 3] == 0):
                    count = _u32(self.data, body + 4)
                    header_size = 18
                elif prefix_u8 == 0:
                    count = self.data[body + 1] | (self.data[body + 2] << 8) | (self.data[body + 3] << 16)
                    header_size = 18
                elif (prefix_u8 == 1 and body + 21 <= tail
                      and self.data[body + 1] == 1 and self.data[body + 2] == 1
                      and self.data[body + 3] == 0):
                    count = _u32(self.data, body + 4)
                    header_size = 21
                elif prefix_u8 == 1:
                    count = (self.data[body + 1] << 8) | self.data[body + 2]
                    header_size = 19
                else:
                    raise ValueError(f"Unsupported list prefix 0x{prefix_u8:02X}")

                if count > 100000:
                    raise ValueError(f"Implausible count {count}")

                element_cursor = body + header_size
                for i in range(count):
                    element_cursor = self._parse_list_element(element_cursor, tail)

                if best_end is None or element_cursor > best_end:
                    best_end = element_cursor
            except Exception as e:
                last_error = e

        if best_end is None:
            raise last_error or ValueError("Object list decode failed")

        raw = self.data[start:best_end]
        return {"kind": "object_list", "raw": raw}, best_end

    def _parse_list_element(self, cursor, tail):
        try:
            return self._parse_full_locator_element(cursor, tail)
        except Exception:
            pass
        return self._parse_compact_element(cursor, tail)

    def _parse_full_locator_element(self, cursor, tail):
        if cursor + 18 > tail:
            raise ValueError("Overruns")

        child_mask_byte_count = _u16(self.data, cursor)
        if child_mask_byte_count == 0 or child_mask_byte_count > 16:
            raise ValueError(f"Invalid mask count {child_mask_byte_count}")

        off = cursor + 2 + child_mask_byte_count
        wrapper_end = off + 2 + 1 + 4 + 4 + 4

        if wrapper_end > tail:
            raise ValueError("Wrapper overruns")

        child_mask_bytes = self.data[cursor + 2:cursor + 2 + child_mask_byte_count]
        child_type_index = _u16(self.data, off)
        child_payload_offset = _u32(self.data, off + 11)

        if child_type_index not in self.parc.type_by_index:
            raise ValueError(f"Invalid type index {child_type_index}")

        if child_payload_offset != wrapper_end:
            raise ValueError("Payload not inline")

        typedef = self.parc.type_by_index[child_type_index]
        _, end = self._parse_inline_payload(typedef, child_mask_bytes, child_payload_offset, tail)
        return end

    def _parse_compact_element(self, cursor, tail):
        if cursor + 18 > tail:
            raise ValueError("Overruns")

        child_type_index = _u16(self.data, cursor + 3)
        if _u64(self.data, cursor + 6) != 0xFFFFFFFFFFFFFFFF:
            raise ValueError("Compact sentinel mismatch")

        child_payload_offset = _u32(self.data, cursor + 14)
        if child_payload_offset != cursor + 18:
            raise ValueError("Compact payload not inline")

        if child_type_index not in self.parc.type_by_index:
            raise ValueError(f"Invalid type index {child_type_index}")

        typedef = self.parc.type_by_index[child_type_index]
        child_mask_bytes = bytes([self.data[cursor + 2]])
        _, end = self._parse_inline_payload(typedef, child_mask_bytes, child_payload_offset, tail)
        return end


def serialize_root_block(parsed_block: dict) -> bytes:
    out = bytearray()

    mask_bytes = parsed_block["mask_bytes"]
    out.extend(struct.pack("<H", parsed_block["mask_byte_count"]))
    out.extend(mask_bytes)
    out.extend(struct.pack("<I", parsed_block["context"]))

    for finfo in parsed_block["fields"]:
        if not finfo["present"]:
            continue
        out.extend(finfo["raw"])

    if parsed_block.get("raw_tail"):
        out.extend(parsed_block["raw_tail"])

    return bytes(out)


def _write_scalar(value, meta_size: int, type_name: str) -> bytes:
    tname = type_name.lower()
    if meta_size == 1:
        return struct.pack("<B", value & 0xFF)
    elif meta_size == 2:
        if "int16" in tname:
            return struct.pack("<h", value)
        return struct.pack("<H", value)
    elif meta_size == 4:
        if "float" in tname:
            return struct.pack("<f", value)
        if "int32" in tname:
            return struct.pack("<i", value)
        return struct.pack("<I", value)
    elif meta_size == 8:
        if "double" in tname:
            return struct.pack("<d", value)
        if "int64" in tname:
            return struct.pack("<q", value)
        return struct.pack("<Q", value)
    return value.to_bytes(meta_size, "little")


def serialize_inline_object(type_def: TypeDef, field_values: Dict[str, Any],
                            mask_bytes: bytes, parc: ParcBlob) -> bytes:
    payload = bytearray()
    payload.extend(struct.pack("<I", 0))

    for i, fdef in enumerate(type_def.fields):
        if not _field_present(mask_bytes, i):
            continue

        value = field_values.get(fdef.name)
        if value is None:
            value = _default_scalar(fdef)

        mk = fdef.meta_kind
        ms = fdef.meta_size

        if mk in (0, 2) and ms > 0:
            payload.extend(_write_scalar(value, ms, fdef.type_name))
        elif mk == 1 and ms > 0:
            if isinstance(value, str):
                data = value.encode("utf-8")
            elif isinstance(value, bytes):
                data = value
            else:
                data = b""
            count = len(data) // ms if ms > 0 else 0
            payload.extend(struct.pack("<I", count))
            payload.extend(data)
        elif mk == 3 and ms > 0:
            if isinstance(value, bytes):
                count = len(value) // ms
                payload.extend(struct.pack("<I", count))
                payload.extend(value)
            else:
                payload.extend(struct.pack("<I", 0))
        elif mk in (4, 5):
            if isinstance(value, bytes):
                payload.extend(value)
            else:
                raise ValueError(f"Object ref field {fdef.name} requires raw bytes")
        elif mk in (6, 7):
            if isinstance(value, bytes):
                payload.extend(value)
            else:
                payload.extend(b'\x00' * 18)
        else:
            raise ValueError(f"Cannot serialize field {fdef.name} with mk={mk}")

    trailing_size = len(payload)
    payload.extend(struct.pack("<I", trailing_size))

    locator = bytearray()
    locator.extend(struct.pack("<H", len(mask_bytes)))
    locator.extend(mask_bytes)
    locator.extend(struct.pack("<H", type_def.index))
    locator.extend(struct.pack("<B", 0))
    locator.extend(struct.pack("<I", 0xFFFFFFFF))
    locator.extend(struct.pack("<I", 0xFFFFFFFF))

    payload_offset_pos = len(locator)
    locator.extend(struct.pack("<I", 0))

    result = bytes(locator) + bytes(payload)
    return result


def fixup_inline_object_offset(obj_bytes: bytes, absolute_offset: int) -> bytes:
    result = bytearray(obj_bytes)

    mask_byte_count = _u16(result, 0)
    payload_offset_pos = 2 + mask_byte_count + 2 + 1 + 4 + 4
    wrapper_end = payload_offset_pos + 4

    actual_payload_offset = absolute_offset + wrapper_end
    struct.pack_into("<I", result, payload_offset_pos, actual_payload_offset)

    return bytes(result)


def _default_scalar(fdef: FieldDef):
    return 0


ITEM_BITMASK_BASIC = bytes([0x9f, 0x28, 0x0d])


def create_item_save_data(
    parc: ParcBlob,
    item_key: int,
    item_no: int,
    slot_no: int = 0,
    stack_count: int = 1,
    enchant_level: int = 0,
    useable_ctc: int = 0,
    endurance: int = 100,
    sharpness: int = 100,
    battery_stat: int = 0,
    max_battery_stat: int = 0,
    max_socket_count: int = 0,
    valid_socket_count: int = 0,
    transferred_item_key: int = 0,
    current_gimmick_state: int = 0,
    bitmask: bytes = ITEM_BITMASK_BASIC,
) -> bytes:
    item_type = None
    for td in parc.types:
        if td.name == "ItemSaveData":
            item_type = td
            break
    if item_type is None:
        raise ValueError("ItemSaveData type not found in schema")

    field_values = {
        "_saveVersion": 1,
        "_itemNo": item_no,
        "_itemKey": item_key,
        "_slotNo": slot_no,
        "_stackCount": stack_count,
        "_enchantLevel": enchant_level,
        "_useableCtc": useable_ctc,
        "_endurance": endurance,
        "_sharpness": sharpness,
        "_batteryStat": battery_stat,
        "_maxBatteryStat": max_battery_stat,
        "_maxSocketCount": max_socket_count,
        "_validSocketCount": valid_socket_count,
        "_transferredItemKey": transferred_item_key,
        "_currentGimmickState": current_gimmick_state,
        "_chargedUseableCount": 0,
        "_timeWhenPushItem": 0,
        "_isNewMark": 0,
    }

    return serialize_inline_object(item_type, field_values, bitmask, parc)


def find_inventory_toc_index(parc: ParcBlob) -> Optional[int]:
    for entry in parc.toc_entries:
        if entry.class_index < len(parc.types):
            if parc.types[entry.class_index].name == "InventorySaveData":
                return entry.index
    return None


def _find_inventory_categories(parc: ParcBlob, inv_toc: int) -> List[dict]:
    entry = parc.toc_entries[inv_toc]
    data = parc.raw
    abs_start = entry.data_offset
    block_end = abs_start + entry.data_size

    pos = abs_start
    mbc = _u16(data, pos); pos += 2
    mask = data[pos:pos + mbc]; pos += mbc
    pos += 4

    list_raw = data[pos:]
    if list_raw[0] == 0:
        if list_raw[1] == 0 and list_raw[2] == 0 and list_raw[3] == 0:
            cat_count = _u32(data, pos + 4)
        else:
            cat_count = list_raw[1] | (list_raw[2] << 8) | (list_raw[3] << 16)
    else:
        raise ValueError(f"Unexpected _inventorylist header prefix: 0x{list_raw[0]:02X}")
    elem_cursor = pos + 18

    parser = BlockParser(parc)
    categories = []

    for i in range(cat_count):
        cat_info = {"locator_abs": elem_cursor}

        elem_mbc = _u16(data, elem_cursor)
        elem_mask = data[elem_cursor + 2:elem_cursor + 2 + elem_mbc]
        off = elem_cursor + 2 + elem_mbc
        type_idx = _u16(data, off)
        payload_abs = _u32(data, off + 11)
        cat_info["payload_abs"] = payload_abs
        cat_info["mask_bytes"] = elem_mask

        ppos = payload_abs + 4

        inv_key = -1
        if _field_present(elem_mask, 0):
            inv_key = _u16(data, ppos)
            ppos += 2
        cat_info["inventory_key"] = inv_key

        if _field_present(elem_mask, 1):
            ppos += 2

        has_item_list = _field_present(elem_mask, 2)
        cat_info["has_item_list"] = has_item_list

        if has_item_list:
            cat_info["item_list_abs"] = ppos
            il_raw = data[ppos:]
            if il_raw[0] == 0:
                if il_raw[1] == 0 and il_raw[2] == 0 and il_raw[3] == 0:
                    item_count = _u32(data, ppos + 4)
                else:
                    item_count = il_raw[1] | (il_raw[2] << 8) | (il_raw[3] << 16)
            else:
                item_count = 0
            cat_info["item_count"] = item_count
            cat_info["item_count_offset_abs"] = ppos + 1

            item_cursor = ppos + 18
            for j in range(item_count):
                item_cursor = parser._parse_full_locator_element(item_cursor, block_end)
            cat_info["items_end_abs"] = item_cursor
        else:
            cat_info["item_list_abs"] = -1
            cat_info["item_count"] = 0
            cat_info["items_end_abs"] = ppos

        elem_end = parser._parse_full_locator_element(elem_cursor, block_end)
        cat_info["elem_end_abs"] = elem_end

        categories.append(cat_info)
        elem_cursor = elem_end

    return categories


def _find_max_item_no(parc: ParcBlob) -> int:
    from item_scanner import scan_items
    items = scan_items(parc.raw)
    if items:
        return max(it.item_no for it in items)
    return 1000000


def clone_item_from_template(
    parc: ParcBlob,
    template_abs: int,
    template_size: int,
    item_key: int,
    item_no: int,
    slot_no: int = 0,
    stack_count: int = 1,
    enchant_level: int = 0,
    endurance: int = 100,
    sharpness: int = 100,
) -> bytes:
    data = parc.raw
    item = bytearray(data[template_abs:template_abs + template_size])

    mbc = _u16(item, 0)
    mask = item[2:2 + mbc]
    locator_end = 2 + mbc + 2 + 1 + 8 + 4
    payload_start_local = locator_end

    struct.pack_into("<I", item, 2 + mbc + 11, 0)

    p = payload_start_local + 4

    field_sizes_scalar = [
        (0, "_saveVersion", 4), (1, "_itemNo", 8), (2, "_itemKey", 4),
        (3, "_slotNo", 2), (4, "_stackCount", 8), (5, "_enchantLevel", 2),
        (6, "_useableCtc", 8), (7, "_endurance", 2), (8, "_sharpness", 2),
        (9, "_batteryStat", 8), (10, "_maxBatteryStat", 8),
        (11, "_maxSocketCount", 1), (12, "_validSocketCount", 1),
    ]

    offsets = {}
    for idx, name, size in field_sizes_scalar:
        if _field_present(mask, idx):
            offsets[name] = p
            p += size

    if "_itemNo" in offsets:
        struct.pack_into("<q", item, offsets["_itemNo"], item_no)
    if "_itemKey" in offsets:
        struct.pack_into("<I", item, offsets["_itemKey"], item_key)
    if "_slotNo" in offsets:
        struct.pack_into("<H", item, offsets["_slotNo"], slot_no)
    if "_stackCount" in offsets:
        struct.pack_into("<q", item, offsets["_stackCount"], stack_count)
    if "_enchantLevel" in offsets:
        struct.pack_into("<H", item, offsets["_enchantLevel"], enchant_level)
    if "_endurance" in offsets:
        struct.pack_into("<H", item, offsets["_endurance"], endurance)
    if "_sharpness" in offsets:
        struct.pack_into("<H", item, offsets["_sharpness"], sharpness)

    end = len(item)
    trailing_size_local = end - 4

    rev = trailing_size_local
    if _field_present(mask, 21):
        rev -= 1
    if _field_present(mask, 20):
        pass
    else:
        if _field_present(mask, 19):
            rev -= 8
        if _field_present(mask, 18):
            rev -= 8
        if _field_present(mask, 17):
            rev -= 4
        if _field_present(mask, 16):
            rev -= 4
            struct.pack_into("<I", item, rev, item_key)

    if _field_present(mask, 19) and not _field_present(mask, 20):
        tp_off = trailing_size_local
        if _field_present(mask, 21):
            tp_off -= 1
        tp_off -= 8
        struct.pack_into("<q", item, tp_off, 0)

    if _field_present(mask, 18) and not _field_present(mask, 20):
        cuc_off = trailing_size_local
        if _field_present(mask, 21):
            cuc_off -= 1
        cuc_off -= 8
        cuc_off -= 8
        struct.pack_into("<q", item, cuc_off, 0)

    return bytes(item)


def insert_item_into_inventory(
    parc: ParcBlob,
    item_key: int,
    stack_count: int = 1,
    enchant_level: int = 0,
    endurance: int = 100,
    sharpness: int = 100,
    category_key: int = 2,
) -> bytes:
    inv_toc = find_inventory_toc_index(parc)
    if inv_toc is None:
        raise ValueError("InventorySaveData not found in TOC")

    entry = parc.toc_entries[inv_toc]
    abs_start = entry.data_offset

    categories = _find_inventory_categories(parc, inv_toc)

    target_cat = None
    for cat in categories:
        if cat["inventory_key"] == category_key:
            target_cat = cat
            break
    if target_cat is None:
        raise ValueError(f"Inventory category key={category_key} not found")
    if not target_cat["has_item_list"]:
        raise ValueError(f"Category key={category_key} has no _itemList field")
    if target_cat["item_count"] == 0:
        raise ValueError(
            f"Category key={category_key} has 0 items; need at least one as template"
        )

    first_item_abs = target_cat["item_list_abs"] + 18
    parser = BlockParser(parc)
    block_end = abs_start + entry.data_size
    second_item_abs = parser._parse_full_locator_element(first_item_abs, block_end)
    template_size = second_item_abs - first_item_abs

    max_item_no = _find_max_item_no(parc)
    new_item_no = max_item_no + 1

    new_item = clone_item_from_template(
        parc, first_item_abs, template_size,
        item_key=item_key, item_no=new_item_no,
        slot_no=0, stack_count=stack_count,
        enchant_level=enchant_level,
        endurance=endurance, sharpness=sharpness,
    )
    delta = len(new_item)

    insert_abs = target_cat["items_end_abs"]
    insert_block_off = insert_abs - abs_start

    old_block = parc.block_raw[inv_toc]
    new_block = bytearray()
    new_block.extend(old_block[:insert_block_off])
    new_block.extend(new_item)
    new_block.extend(old_block[insert_block_off:])

    count_block_off = target_cat["item_count_offset_abs"] - abs_start
    old_count = new_block[count_block_off]
    new_block[count_block_off] = old_count + 1

    ts_block_off = (target_cat["elem_end_abs"] - abs_start) - 4 + delta
    old_ts = _u32(new_block, ts_block_off)
    struct.pack_into("<I", new_block, ts_block_off, old_ts + delta)

    _fixup_payload_offsets(new_block, abs_start, insert_abs, delta)

    new_item_block_off = insert_block_off
    ni_mbc = _u16(new_block, new_item_block_off)
    ni_locator_end = new_item_block_off + 2 + ni_mbc + 2 + 1 + 8 + 4
    ni_payload_abs = abs_start + ni_locator_end
    ni_po_off = new_item_block_off + 2 + ni_mbc + 11
    struct.pack_into("<I", new_block, ni_po_off, ni_payload_abs)

    _fixup_nested_payload_offsets(new_block, new_item_block_off, len(new_item), abs_start)

    ni_ts_block_off = new_item_block_off + len(new_item) - 4
    ni_payload_start = ni_locator_end
    correct_ts = ni_ts_block_off - ni_payload_start
    struct.pack_into("<I", new_block, ni_ts_block_off, correct_ts)

    replace_block_raw(parc, inv_toc, bytes(new_block))
    return serialize_parc(parc)


def find_store_toc_index(parc: ParcBlob) -> Optional[int]:
    for entry in parc.toc_entries:
        if entry.class_index < len(parc.types):
            if parc.types[entry.class_index].name == "StoreSaveData":
                return entry.index
    return None


def insert_item_into_store(
    parc: ParcBlob,
    item_key: int,
    stack_count: int = 1,
    enchant_level: int = 0,
    endurance: int = 5,
    sharpness: int = 0,
) -> bytes:
    store_toc = find_store_toc_index(parc)
    if store_toc is None:
        raise ValueError("StoreSaveData not found in TOC")

    entry = parc.toc_entries[store_toc]
    abs_start = entry.data_offset
    raw = parc.block_raw[store_toc]

    store_items = []
    for off in range(20, len(raw) - 40):
        if struct.unpack_from("<I", raw, off)[0] != 1:
            continue
        ino = struct.unpack_from("<q", raw, off + 4)[0]
        if ino < 1 or ino > 999999:
            continue
        key = struct.unpack_from("<I", raw, off + 12)[0]
        if key < 1 or key > 0x7FFFFFFF:
            continue
        slot = struct.unpack_from("<H", raw, off + 16)[0]
        stk = struct.unpack_from("<q", raw, off + 18)[0]
        if stk < 1 or stk > 99999:
            continue
        if off >= 16 and struct.unpack_from("<q", raw, off - 16)[0] != -1:
            continue
        store_items.append((abs_start + off, ino, key))

    if not store_items:
        raise ValueError("No existing items in StoreSaveData to use as template. "
                         "Sell an item to a vendor in-game first, then try again.")

    template_abs = store_items[-1][0] - 16
    template_start = template_abs - abs_start
    next_sentinel = None
    search_start = template_start + 20
    sentinel_bytes = b'\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF'
    while search_start < len(raw) - 8:
        if raw[search_start:search_start + 8] == sentinel_bytes:
            next_sentinel = search_start
            break
        search_start += 1

    if next_sentinel is None:
        template_end = len(raw)
    else:
        template_end = next_sentinel

    locator_start = template_start

    for back in range(0, 20):
        check = template_start - back
        if check < 0:
            break
        mbc_candidate = struct.unpack_from("<H", raw, check)[0]
        if 1 <= mbc_candidate <= 5:
            after_mask = check + 2 + mbc_candidate
            if after_mask + 11 <= len(raw):
                type_idx = struct.unpack_from("<H", raw, after_mask)[0]
                reserved = raw[after_mask + 2]
                sent_check = raw[after_mask + 3:after_mask + 11]
                if type_idx < 100 and reserved == 0 and sent_check == sentinel_bytes:
                    locator_start = check
                    break

    template_abs_real = abs_start + locator_start
    template_size = template_end - locator_start

    max_item_no = _find_max_item_no(parc)
    new_item_no = max_item_no + 1

    new_item = clone_item_from_template(
        parc, template_abs_real, template_size,
        item_key=item_key, item_no=new_item_no,
        slot_no=0, stack_count=stack_count,
        enchant_level=enchant_level,
        endurance=endurance, sharpness=sharpness,
    )
    delta = len(new_item)

    insert_abs = abs_start + template_end
    insert_block_off = template_end

    old_block = parc.block_raw[store_toc]
    new_block = bytearray()
    new_block.extend(old_block[:insert_block_off])
    new_block.extend(new_item)
    new_block.extend(old_block[insert_block_off:])

    first_item_block_off = store_items[0][0] - abs_start - 16
    for back in range(18, 30):
        check = first_item_block_off - back
        if check < 0:
            break
        val = raw[check]
        if val == len(store_items):
            new_block[check] = val + 1
            break

    _fixup_payload_offsets(new_block, abs_start, insert_abs, delta)

    ni_block_off = insert_block_off
    ni_mbc = _u16(new_block, ni_block_off)
    ni_locator_end = ni_block_off + 2 + ni_mbc + 2 + 1 + 8 + 4
    ni_payload_abs = abs_start + ni_locator_end
    ni_po_off = ni_block_off + 2 + ni_mbc + 11
    struct.pack_into("<I", new_block, ni_po_off, ni_payload_abs)

    _fixup_nested_payload_offsets(new_block, ni_block_off, len(new_item), abs_start)

    ni_ts_off = ni_block_off + len(new_item) - 4
    ni_payload_start = ni_locator_end
    struct.pack_into("<I", new_block, ni_ts_off, ni_ts_off - ni_payload_start)

    replace_block_raw(parc, store_toc, bytes(new_block))
    return serialize_parc(parc)


def _fixup_payload_offsets(block: bytearray, block_abs_start: int,
                            insert_abs: int, delta: int) -> None:
    pos = 0
    while pos < len(block) - 20:

        if (_u32(block, pos) == 0xFFFFFFFF and
                pos + 4 < len(block) and _u32(block, pos + 4) == 0xFFFFFFFF):
            if pos + 12 <= len(block):
                po = _u32(block, pos + 8)
                if block_abs_start <= po < block_abs_start + len(block) + delta:
                    if po >= insert_abs:
                        new_po = po + delta
                        struct.pack_into("<I", block, pos + 8, new_po)
            pos += 12
        else:
            pos += 1


def _fixup_nested_payload_offsets(block: bytearray, item_start: int,
                                    item_size: int, block_abs_start: int) -> None:
    pos = item_start
    end = item_start + item_size
    first = True
    while pos < end - 20:
        if (_u32(block, pos) == 0xFFFFFFFF and
                pos + 4 < end and _u32(block, pos + 4) == 0xFFFFFFFF):
            if pos + 12 <= end:
                if first:
                    first = False
                    pos += 12
                    continue
                expected_payload = block_abs_start + pos + 12
                struct.pack_into("<I", block, pos + 8, expected_payload)
            pos += 12
        else:
            pos += 1


def modify_field_in_block(parc: ParcBlob, toc_index: int,
                          field_name: str, new_value) -> None:
    entry = parc.toc_entries[toc_index]
    typedef = parc.type_by_index[entry.class_index]
    block = bytearray(parc.block_raw[toc_index])

    pos = 0
    mask_byte_count = struct.unpack_from("<H", block, pos)[0]; pos += 2
    mask_bytes = block[pos:pos + mask_byte_count]; pos += mask_byte_count
    pos += 4

    for i, fdef in enumerate(typedef.fields):
        if not _field_present(mask_bytes, i):
            continue

        mk = fdef.meta_kind
        ms = fdef.meta_size

        if fdef.name == field_name:
            if mk not in (0, 2) or ms <= 0:
                raise ValueError(f"Field {field_name} is not a scalar (mk={mk})")
            new_bytes = _write_scalar(new_value, ms, fdef.type_name)
            block[pos:pos + ms] = new_bytes
            parc.modified_blocks[toc_index] = bytes(block)
            return

        if mk in (0, 2) and ms > 0:
            pos += ms
        elif mk == 1 and ms > 0:
            count = struct.unpack_from("<I", block, pos)[0]
            pos += 4 + count * ms
        elif mk == 3 and ms > 0:
            count = struct.unpack_from("<I", block, pos)[0]
            pos += 4 + count * ms
        elif mk in (4, 5, 6, 7):
            raise ValueError(f"Cannot skip complex field {fdef.name} (mk={mk}) "
                           f"to reach {field_name}. Use BlockParser for complex edits.")
        else:
            raise ValueError(f"Unknown field kind mk={mk}")

    raise ValueError(f"Field {field_name} not found or not present in block")


def replace_block_raw(parc: ParcBlob, toc_index: int, new_raw: bytes) -> None:
    parc.modified_blocks[toc_index] = new_raw


def verify_round_trip(original_blob: bytes) -> Tuple[bool, str]:
    parc = parse_parc_blob(original_blob)
    serialized = serialize_parc(parc)

    if serialized == original_blob:
        return True, f"Round-trip OK: {len(original_blob)} bytes identical"

    min_len = min(len(serialized), len(original_blob))
    for i in range(min_len):
        if serialized[i] != original_blob[i]:
            start = max(0, i - 8)
            end = min(min_len, i + 8)
            orig_hex = original_blob[start:end].hex()
            ser_hex = serialized[start:end].hex()
            return False, (
                f"Mismatch at offset 0x{i:X} (byte {i}).\n"
                f"  Original length: {len(original_blob)}\n"
                f"  Serialized length: {len(serialized)}\n"
                f"  Original  [{start:X}..{end:X}]: {orig_hex}\n"
                f"  Serialized [{start:X}..{end:X}]: {ser_hex}"
            )

    return False, (
        f"Length mismatch: original={len(original_blob)}, serialized={len(serialized)}"
    )


if __name__ == "__main__":
    import sys
    import os

    SAVE_PATH = r"C:\Users\Coding\AppData\Local\Pearl Abyss\CD\save\57173764\slot0\save.save"
    RAW_PATH = r"C:\Users\Coding\CrimsonDesertModding\test_blob.bin"

    blob = None

    if os.path.exists(RAW_PATH):
        print(f"Loading raw blob: {RAW_PATH}")
        with open(RAW_PATH, "rb") as f:
            blob = f.read()
    elif os.path.exists(SAVE_PATH):
        print(f"Loading save file: {SAVE_PATH}")
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        try:
            from save_crypto import load_save_file
            save_data = load_save_file(SAVE_PATH)
            blob = bytes(save_data.decompressed_blob)
        except ImportError:
            import lz4.block
            KEY = bytes.fromhex("9a4beb127f9e748b148d6690c25cc9379a315bd56c28af6319fd559f1152ac00")
            with open(SAVE_PATH, "rb") as f:
                file_data = f.read()
            uncomp = struct.unpack_from("<I", file_data, 0x12)[0]
            payload_sz = struct.unpack_from("<I", file_data, 0x16)[0]
            nonce = file_data[0x1A:0x2A]
            ciphertext = file_data[0x80:0x80 + payload_sz]

            def _rotl32(v, n):
                v &= 0xFFFFFFFF
                return ((v << n) | (v >> (32 - n))) & 0xFFFFFFFF

            def _qr(s, a, b, c, d):
                s[a]=(s[a]+s[b])&0xFFFFFFFF;s[d]^=s[a];s[d]=_rotl32(s[d],16)
                s[c]=(s[c]+s[d])&0xFFFFFFFF;s[b]^=s[c];s[b]=_rotl32(s[b],12)
                s[a]=(s[a]+s[b])&0xFFFFFFFF;s[d]^=s[a];s[d]=_rotl32(s[d],8)
                s[c]=(s[c]+s[d])&0xFFFFFFFF;s[b]^=s[c];s[b]=_rotl32(s[b],7)

            def _cc_block(kw, ctr, nw):
                s=[0x61707865,0x3320646e,0x79622d32,0x6b206574,kw[0],kw[1],kw[2],kw[3],kw[4],kw[5],kw[6],kw[7],ctr&0xFFFFFFFF,nw[0],nw[1],nw[2]]
                w=list(s)
                for _ in range(10):
                    _qr(w,0,4,8,12);_qr(w,1,5,9,13);_qr(w,2,6,10,14);_qr(w,3,7,11,15)
                    _qr(w,0,5,10,15);_qr(w,1,6,11,12);_qr(w,2,7,8,13);_qr(w,3,4,9,14)
                r=bytearray(64)
                for i in range(16):struct.pack_into('<I',r,i*4,(w[i]+s[i])&0xFFFFFFFF)
                return bytes(r)

            def _cc_crypt(data, n16):
                ic=struct.unpack_from('<I',n16,0)[0];n12=n16[4:16]
                kw=[struct.unpack_from('<I',KEY,i*4)[0] for i in range(8)]
                nw=[struct.unpack_from('<I',n12,i*4)[0] for i in range(3)]
                out=bytearray(len(data));pos=0;ctr=ic
                while pos<len(data):
                    bl=_cc_block(kw,ctr,nw);end=min(pos+64,len(data))
                    for i in range(pos,end):out[i]=data[i]^bl[i-pos]
                    pos=end;ctr=(ctr+1)&0xFFFFFFFF
                return bytes(out)

            compressed = _cc_crypt(ciphertext, nonce)
            blob = lz4.block.decompress(compressed, uncompressed_size=uncomp)
    else:
        print("No save file or raw blob found.")
        sys.exit(1)

    if blob is None:
        print("Failed to load blob.")
        sys.exit(1)

    print(f"Blob size: {len(blob)} bytes")
    print()

    print("=" * 60)
    print("ROUND-TRIP VERIFICATION")
    print("=" * 60)
    success, msg = verify_round_trip(blob)
    print(msg)
    if success:
        print("PASS")
    else:
        print("FAIL")
    print()

    parc = parse_parc_blob(blob)
    print(f"Schema: {len(parc.types)} types")
    print(f"TOC: {len(parc.toc_entries)} entries")
    print(f"Data starts at: 0x{parc.data_start:X}")
    print(f"Stream size: {parc.stream_size}")

    sys.exit(0 if success else 1)
