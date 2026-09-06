
from __future__ import annotations

import struct
import logging
from dataclasses import dataclass, field as dc_field

log = logging.getLogger(__name__)

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'Communitydump', 'desktopeditor'))
from save_parser import GenericFieldValue, ObjectBlock, TypeDef


@dataclass
class _POFixup:
    buf_position: int
    payload_rel: int


def serialize_object_block(
    block: ObjectBlock,
    schema_types: dict[int, TypeDef],
    raw_blob: bytes,
    block_abs_start: int = 0,
) -> bytes:
    if _tree_is_modified(block.fields):
        return _serialize_modified_block(block, schema_types, raw_blob, block_abs_start)

    block_start = block.data_offset
    block_end = block.data_offset + block.data_size

    ranges = []
    _collect_ranges(block.fields, ranges)

    ranges.sort(key=lambda r: r[0])

    buf = bytearray()
    po_fixups: list[_POFixup] = []

    header_end = block_start + 2 + block.mask_byte_count + 4
    buf += raw_blob[block_start:header_end]

    cursor = header_end

    for rng_start, rng_end, fld, is_po_field in ranges:
        if rng_start < cursor:
            if rng_end <= cursor:
                continue
            rng_start = cursor

        if rng_start > cursor:
            buf += raw_blob[cursor:rng_start]

        if is_po_field and fld is not None:
            po_buf_pos = len(buf)
            buf += raw_blob[rng_start:rng_end]
            payload_abs = struct.unpack_from('<I', raw_blob, rng_start)[0]
            payload_rel = len(buf)
            po_fixups.append(_POFixup(buf_position=po_buf_pos, payload_rel=payload_rel))
        else:
            buf += raw_blob[rng_start:rng_end]

        cursor = rng_end

    if cursor < block_end:
        buf += raw_blob[cursor:block_end]

    orig_block_start = block.data_offset
    if block_abs_start != orig_block_start:
        delta = block_abs_start - orig_block_start
        _fixup_all_pos(buf, block.fields, orig_block_start, delta)

    return bytes(buf)


def _collect_ranges(
    fields: list[GenericFieldValue],
    out: list[tuple[int, int, GenericFieldValue | None, bool]],
) -> None:
    for f in fields:
        if not f.present:
            continue
        if f.start_offset <= 0 or f.end_offset <= f.start_offset:
            continue

        if f.meta_kind in (4, 5) and f.child_fields is not None:
            _collect_inline_object_ranges(f, out)
        elif f.meta_kind in (6, 7) and f.list_elements is not None:
            _collect_list_ranges(f, out)
        else:
            out.append((f.start_offset, f.end_offset, f, False))


def _collect_inline_object_ranges(
    f: GenericFieldValue,
    out: list[tuple[int, int, GenericFieldValue | None, bool]],
) -> None:
    if f.child_payload_offset > 0:
        po_start = f.child_payload_offset - 4
        if f.start_offset < po_start:
            out.append((f.start_offset, po_start, f, False))
        out.append((po_start, f.child_payload_offset, f, True))
        if f.child_fields:
            _collect_ranges(f.child_fields, out)
    else:
        out.append((f.start_offset, f.end_offset, f, False))


def _collect_list_ranges(
    f: GenericFieldValue,
    out: list[tuple[int, int, GenericFieldValue | None, bool]],
) -> None:
    elements = f.list_elements or []

    if not elements:
        out.append((f.start_offset, f.end_offset, f, False))
        return

    first_elem_start = elements[0].start_offset
    if first_elem_start > f.start_offset:
        out.append((f.start_offset, first_elem_start, f, False))

    for elem in elements:
        if elem.start_offset <= 0 or elem.end_offset <= elem.start_offset:
            continue
        if elem.child_fields is not None and elem.child_payload_offset > 0:
            _collect_inline_object_ranges(elem, out)
        else:
            out.append((elem.start_offset, elem.end_offset, elem, False))


def _fixup_all_pos(
    buf: bytearray,
    fields: list[GenericFieldValue],
    orig_block_start: int,
    delta: int,
) -> None:
    if delta == 0:
        return
    for f in fields:
        if not f.present:
            continue
        if f.meta_kind in (4, 5) and f.child_payload_offset > 0 and f.child_fields is not None:
            po_buf_pos = (f.child_payload_offset - 4) - orig_block_start
            if 0 <= po_buf_pos < len(buf) - 3:
                old_val = struct.unpack_from('<I', buf, po_buf_pos)[0]
                struct.pack_into('<I', buf, po_buf_pos, old_val + delta)
            _fixup_all_pos(buf, f.child_fields, orig_block_start, delta)

        elif f.meta_kind in (6, 7) and f.list_elements:
            for elem in f.list_elements:
                if elem.child_payload_offset > 0 and elem.child_fields is not None:
                    po_buf_pos = (elem.child_payload_offset - 4) - orig_block_start
                    if 0 <= po_buf_pos < len(buf) - 3:
                        old_val = struct.unpack_from('<I', buf, po_buf_pos)[0]
                        struct.pack_into('<I', buf, po_buf_pos, old_val + delta)
                    if elem.child_fields:
                        _fixup_all_pos(buf, elem.child_fields, orig_block_start, delta)


def _tree_is_modified(fields: list[GenericFieldValue]) -> bool:
    for f in fields:
        if f.note and '[modified]' in f.note:
            return True
        if f.note and '[expanded]' in f.note:
            return True
        if f.child_fields and _tree_is_modified(f.child_fields):
            return True
        if f.list_elements:
            for elem in f.list_elements:
                if elem.note and '[modified]' in elem.note:
                    return True
                if elem.start_offset == 0 and elem.end_offset == 0:
                    return True
                if elem.child_fields and _tree_is_modified(elem.child_fields):
                    return True
    return False


def _serialize_modified_block(
    block: ObjectBlock,
    schema_types: dict[int, TypeDef],
    raw_blob: bytes,
    block_abs_start: int,
) -> bytes:
    buf = bytearray()
    po_fixups: list[_POFixup] = []

    buf += struct.pack('<H', block.mask_byte_count)
    buf += block.header_mask_bytes
    buf += struct.pack('<I', block.reserved_u32)

    header_size = len(buf)
    block_abs_end = block_abs_start + block.data_size

    _emit_fields(
        block.fields, buf, po_fixups, raw_blob, schema_types,
        abs_start=block_abs_start + header_size,
        abs_end=block_abs_end,
    )

    for fixup in po_fixups:
        abs_addr = block_abs_start + fixup.payload_rel
        struct.pack_into('<I', buf, fixup.buf_position, abs_addr)

    return bytes(buf)


def _emit_fields(
    fields: list[GenericFieldValue],
    buf: bytearray,
    po_fixups: list[_POFixup],
    raw_blob: bytes,
    schema_types: dict[int, TypeDef],
    abs_start: int | None = None,
    abs_end: int | None = None,
) -> None:
    positioned = [f for f in fields if f.present and f.start_offset > 0]
    new_fields = [f for f in fields if f.present and f.start_offset == 0]
    positioned.sort(key=lambda f: f.start_offset)

    cursor = abs_start

    for f in positioned:
        if cursor is not None and f.start_offset > cursor:
            buf += raw_blob[cursor:f.start_offset]

        _emit_single_field(f, buf, po_fixups, raw_blob, schema_types)
        cursor = f.end_offset

    if cursor is not None and abs_end is not None and cursor < abs_end:
        buf += raw_blob[cursor:abs_end]

    for f in new_fields:
        _emit_single_field(f, buf, po_fixups, raw_blob, schema_types)


def _emit_single_field(
    field: GenericFieldValue,
    buf: bytearray,
    po_fixups: list[_POFixup],
    raw_blob: bytes,
    schema_types: dict[int, TypeDef],
) -> None:

    if field.meta_kind in (0, 1, 2, 3):
        if field.note and '[modified]' in field.note and field.edit_format and field.meta_kind in (0, 2):
            raw = _serialize_scalar_value(field)
            if raw:
                buf += raw
                return
        if field.start_offset > 0 and field.end_offset > field.start_offset:
            buf += raw_blob[field.start_offset:field.end_offset]
        elif hasattr(field, '_raw_bytes') and field._raw_bytes:
            buf += field._raw_bytes
        return

    if field.meta_kind in (4, 5):
        _emit_inline_object(field, buf, po_fixups, raw_blob, schema_types)
        return

    if field.meta_kind in (6, 7):
        _emit_object_list(field, buf, po_fixups, raw_blob, schema_types)
        return


def _serialize_scalar_value(field: GenericFieldValue) -> bytes | None:
    fmt = field.edit_format
    val_str = field.value_repr
    try:
        if fmt == 'bool':
            val = 1 if val_str.lower() in ('true', '1', 'yes') else 0
            return struct.pack('<B', val)
        elif fmt in ('<b', '<B'):
            return struct.pack(fmt, int(val_str))
        elif fmt in ('<h', '<H'):
            return struct.pack(fmt, int(val_str))
        elif fmt in ('<i', '<I'):
            return struct.pack(fmt, int(val_str))
        elif fmt in ('<q', '<Q'):
            return struct.pack(fmt, int(val_str))
        elif fmt == '<f':
            return struct.pack(fmt, float(val_str))
        elif fmt == '<d':
            return struct.pack(fmt, float(val_str))
    except (ValueError, struct.error) as e:
        log.warning("Failed to serialize %s = %s (fmt=%s): %s", field.name, val_str, fmt, e)
    return None


def _emit_inline_object(
    field: GenericFieldValue,
    buf: bytearray,
    po_fixups: list[_POFixup],
    raw_blob: bytes,
    schema_types: dict[int, TypeDef],
) -> None:
    if field.child_fields is None:
        if field.start_offset > 0 and field.end_offset > field.start_offset:
            buf += raw_blob[field.start_offset:field.end_offset]
        return

    if field.meta_kind == 5 and field.start_offset > 0:
        for delta in range(0, 9):
            probe = field.start_offset + delta
            if probe + 2 <= len(raw_blob):
                test_mbc = struct.unpack_from('<H', raw_blob, probe)[0]
                if test_mbc == field.child_mask_byte_count and 0 < test_mbc <= 16:
                    if delta > 0:
                        buf += raw_blob[field.start_offset:field.start_offset + delta]
                    break

    buf += struct.pack('<H', field.child_mask_byte_count)
    buf += field.child_mask_bytes
    buf += struct.pack('<H', field.child_type_index)
    buf += struct.pack('<B', field.child_reserved_u8)
    buf += struct.pack('<I', field.child_sentinel1_u32)
    buf += struct.pack('<I', field.child_sentinel2_u32)

    po_pos = len(buf)
    buf += struct.pack('<I', 0)
    payload_start = len(buf)
    po_fixups.append(_POFixup(buf_position=po_pos, payload_rel=payload_start))

    buf += struct.pack('<I', field.child_reserved_u32)

    if field.start_offset > 0 and field.child_payload_offset > 0 and field.end_offset > field.start_offset:
        payload_content_start = field.child_payload_offset + 4
        payload_content_end = field.end_offset - 4
    else:
        payload_content_start = None
        payload_content_end = None

    _emit_fields(field.child_fields, buf, po_fixups, raw_blob, schema_types,
                 abs_start=payload_content_start, abs_end=payload_content_end)

    trailing_size = len(buf) - payload_start
    buf += struct.pack('<I', trailing_size)


def _emit_object_list(
    field: GenericFieldValue,
    buf: bytearray,
    po_fixups: list[_POFixup],
    raw_blob: bytes,
    schema_types: dict[int, TypeDef],
) -> None:
    elements = field.list_elements or []

    if field.list_elements is None:
        if field.start_offset > 0 and field.end_offset > field.start_offset:
            buf += raw_blob[field.start_offset:field.end_offset]
        return

    actual_count = len(elements)
    if field.start_offset > 0 and field.list_header_size > 0:
        header_raw = bytearray(raw_blob[field.start_offset:field.start_offset + field.list_header_size])
        _patch_list_count(header_raw, field, actual_count)
        buf += header_raw
    elif hasattr(field, '_raw_bytes') and field._raw_bytes:
        buf += field._raw_bytes
        return
    else:
        log.warning("List %s has no original header", field.name)
        return

    for elem in elements:
        if elem.start_offset > 0 and elem.child_fields is None:
            buf += raw_blob[elem.start_offset:elem.end_offset]
        elif elem.start_offset > 0 and not _tree_is_modified(elem.child_fields or []) and elem.start_offset > 0:
            buf += raw_blob[elem.start_offset:elem.end_offset]
        else:
            _emit_list_element(elem, buf, po_fixups, raw_blob, schema_types)

    if elements and field.end_offset > 0:
        last_decoded_end = elements[-1].end_offset
        if last_decoded_end > 0 and last_decoded_end < field.end_offset:
            buf += raw_blob[last_decoded_end:field.end_offset]
    elif not elements and field.end_offset > 0 and field.start_offset > 0:
        list_header_end = field.start_offset + field.list_header_size
        if list_header_end < field.end_offset:
            buf += raw_blob[list_header_end:field.end_offset]


def _emit_list_element(
    elem: GenericFieldValue,
    buf: bytearray,
    po_fixups: list[_POFixup],
    raw_blob: bytes,
    schema_types: dict[int, TypeDef],
) -> None:
    if elem.child_fields is None:
        if hasattr(elem, '_raw_bytes') and elem._raw_bytes:
            buf += elem._raw_bytes
        return

    buf += struct.pack('<H', elem.child_mask_byte_count)
    buf += elem.child_mask_bytes
    buf += struct.pack('<H', elem.child_type_index)
    buf += struct.pack('<B', elem.child_reserved_u8)

    if elem.child_sentinel1_u32 == 0xFFFFFFFF and elem.child_sentinel2_u32 == 0xFFFFFFFF:
        buf += struct.pack('<Q', 0xFFFFFFFFFFFFFFFF)
    else:
        buf += struct.pack('<I', elem.child_sentinel1_u32)
        buf += struct.pack('<I', elem.child_sentinel2_u32)

    po_pos = len(buf)
    buf += struct.pack('<I', 0)
    payload_start = len(buf)
    po_fixups.append(_POFixup(buf_position=po_pos, payload_rel=payload_start))

    buf += struct.pack('<I', elem.child_reserved_u32)

    if elem.start_offset > 0 and elem.child_payload_offset > 0 and elem.end_offset > elem.start_offset:
        payload_content_start = elem.child_payload_offset + 4
        payload_content_end = elem.end_offset - 4
    else:
        payload_content_start = None
        payload_content_end = None

    _emit_fields(elem.child_fields, buf, po_fixups, raw_blob, schema_types,
                 abs_start=payload_content_start, abs_end=payload_content_end)

    trailing_size = len(buf) - payload_start
    buf += struct.pack('<I', trailing_size)


def _patch_list_count(header: bytearray, field: GenericFieldValue, count: int) -> None:
    prefix_u8 = field.list_prefix_u8
    header_offset = 0
    if field.note and 'header_offset=+' in field.note:
        try:
            header_offset = int(field.note.split('header_offset=+')[1].split()[0])
        except (ValueError, IndexError):
            pass

    body = header_offset

    if prefix_u8 == 1 and len(header) - body == 21:
        struct.pack_into('<I', header, body + 4, count)
    elif prefix_u8 == 1 and len(header) - body == 19:
        header[body + 1] = (count >> 8) & 0xFF
        header[body + 2] = count & 0xFF
    elif prefix_u8 == 0 and len(header) - body == 18:
        if header[body + 1] == 0 and header[body + 2] == 0 and header[body + 3] == 0:
            struct.pack_into('<I', header, body + 4, count)
        else:
            header[body + 1] = count & 0xFF
            header[body + 2] = (count >> 8) & 0xFF
            header[body + 3] = (count >> 16) & 0xFF
    else:
        if body + 4 <= len(header):
            header[body + 1] = count & 0xFF
            header[body + 2] = (count >> 8) & 0xFF
            header[body + 3] = (count >> 16) & 0xFF


def round_trip_test(raw_blob: bytes, result: dict) -> list[str]:
    schema_types = {t.index: t for t in result['schema']['types']}
    errors = []

    for block in result['objects']:
        original = raw_blob[block.data_offset:block.data_offset + block.data_size]
        try:
            serialized = serialize_object_block(
                block, schema_types, raw_blob, block.data_offset
            )
        except Exception as e:
            errors.append(f"{block.class_name}[{block.entry_index}]: serialize failed: {e}")
            continue

        if serialized != original:
            diff_pos = next(
                (i for i in range(min(len(serialized), len(original)))
                 if serialized[i] != original[i]),
                min(len(serialized), len(original))
            )
            if diff_pos < len(serialized) and diff_pos < len(original):
                errors.append(
                    f"{block.class_name}[{block.entry_index}]: "
                    f"size {len(original)} vs {len(serialized)}, "
                    f"first diff at +{diff_pos} "
                    f"(orig=0x{original[diff_pos]:02X} vs ser=0x{serialized[diff_pos]:02X})"
                )
            else:
                errors.append(
                    f"{block.class_name}[{block.entry_index}]: "
                    f"size mismatch {len(original)} vs {len(serialized)}"
                )

    return errors
