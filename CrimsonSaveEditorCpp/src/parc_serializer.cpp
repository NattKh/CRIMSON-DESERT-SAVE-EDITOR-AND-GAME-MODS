/**
 * parc_serializer.cpp — Tree-based PARC blob serialization.
 *
 * ALWAYS recurses into child_fields for kind 4/5/6/7 (inline objects and lists).
 * Every PO and trailing_size is computed fresh during the recursive write.
 * No delta fixup. No sentinel scanning. No false positives possible.
 * Scalars (kind 0/1/2/3) use raw_value directly — they never contain POs.
 *
 * Gap bytes between fields are copied from orig_blob using the parsed offsets.
 * This is correct for both unmodified and duplicated elements (same-save).
 */
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include "parc_serializer.h"
#include <cstring>
#include <algorithm>
#include <stdexcept>

namespace ParcSerializer {

using namespace SaveParserCpp;

// ── Write helpers ──

static inline void WriteU8(std::vector<uint8_t>& buf, uint8_t v) {
    buf.push_back(v);
}

static inline void WriteU16(std::vector<uint8_t>& buf, uint16_t v) {
    buf.push_back((uint8_t)(v & 0xFF));
    buf.push_back((uint8_t)((v >> 8) & 0xFF));
}

static inline void WriteU32(std::vector<uint8_t>& buf, uint32_t v) {
    buf.push_back((uint8_t)(v & 0xFF));
    buf.push_back((uint8_t)((v >> 8) & 0xFF));
    buf.push_back((uint8_t)((v >> 16) & 0xFF));
    buf.push_back((uint8_t)((v >> 24) & 0xFF));
}

static inline void PatchU32(std::vector<uint8_t>& buf, uint32_t offset, uint32_t v) {
    memcpy(buf.data() + offset, &v, 4);
}

static inline void WriteBytes(std::vector<uint8_t>& buf,
                               const std::vector<uint8_t>& data) {
    buf.insert(buf.end(), data.begin(), data.end());
}

static inline void WriteBytes(std::vector<uint8_t>& buf,
                               const uint8_t* data, size_t len) {
    buf.insert(buf.end(), data, data + len);
}

static inline void WriteSentinel(std::vector<uint8_t>& buf,
                                  uint32_t s1 = 0xFFFFFFFF,
                                  uint32_t s2 = 0xFFFFFFFF) {
    buf.push_back((uint8_t)(s1 & 0xFF));
    buf.push_back((uint8_t)((s1 >> 8) & 0xFF));
    buf.push_back((uint8_t)((s1 >> 16) & 0xFF));
    buf.push_back((uint8_t)((s1 >> 24) & 0xFF));
    buf.push_back((uint8_t)(s2 & 0xFF));
    buf.push_back((uint8_t)((s2 >> 8) & 0xFF));
    buf.push_back((uint8_t)((s2 >> 16) & 0xFF));
    buf.push_back((uint8_t)((s2 >> 24) & 0xFF));
}

// ── Forward declarations ──

static void SerializeField(std::vector<uint8_t>& buf,
                            const GenericFieldValue& f,
                            const std::vector<uint8_t>& orig_blob);

// ── Fix POs within raw payload bytes (fallback for elements without child_fields) ──
// Uses _is_real_po validation: a real PO in the original blob had value == sentinel_abs + 12.
// This rejects false sentinel patterns (data that happens to be 0xFF×8).

static void FixPayloadPOs(std::vector<uint8_t>& buf,
                           uint32_t buf_start, uint32_t buf_end,
                           const uint8_t* orig_payload,
                           uint32_t orig_payload_abs) {
    static const uint8_t SENT8[8] = {0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF};
    uint32_t len = buf_end - buf_start;

    if (orig_payload_abs > 0) {
        // Source position known: a real PO is any u32 equal to its own source
        // address + 4 (the self-referential invariant). No sentinel context
        // needed, so POs within 8 bytes of the region start are caught too.
        for (uint32_t r = 0; r + 4 <= len; ++r) {
            uint32_t orig_po_val = 0;
            memcpy(&orig_po_val, orig_payload + r, 4);
            if (orig_po_val != orig_payload_abs + r + 4) continue;
            uint32_t buf_po_pos = buf_start + r;
            PatchU32(buf, buf_po_pos, buf_po_pos + 4);
        }
        return;
    }

    for (uint32_t r = 0; r + 12 <= len; ++r) {
        // No original position (ItemFactory/external bytes).
        // Only fix at real sentinel positions (8 bytes of 0xFF before the PO).
        if (memcmp(orig_payload + r, SENT8, 8) != 0) continue;
        uint32_t buf_po_pos = buf_start + r + 8;
        PatchU32(buf, buf_po_pos, buf_po_pos + 4);
    }
}

// ── Source region for gap bytes ──
// Gap bytes (padding/undecoded data between fields) are addressed by the
// parse-time absolute offsets. For elements copied across saves or re-serialized
// after the blob shifted, orig_blob at those offsets is the WRONG data — but the
// element's own raw_value (captured at parse time from its source blob) is
// always right. SrcRegion lets every level resolve absolute offsets against the
// element's raw_value first, falling back to orig_blob for same-blob data.

struct SrcRegion {
    const uint8_t* data = nullptr;
    size_t size = 0;
    uint32_t base = 0;   // absolute offset of data[0] in the source blob
};

static SrcRegion RegionFor(const GenericFieldValue& f) {
    SrcRegion r;
    if (!f.raw_value.empty() && f.start_offset > 0 &&
        f.end_offset - f.start_offset == (uint32_t)f.raw_value.size()) {
        r.data = f.raw_value.data();
        r.size = f.raw_value.size();
        r.base = f.start_offset;
    }
    return r;
}

static void CopyRange(std::vector<uint8_t>& buf,
                      uint32_t abs_start, uint32_t abs_end,
                      const SrcRegion& src,
                      const std::vector<uint8_t>& orig_blob) {
    if (abs_end <= abs_start) return;
    const uint8_t* p = nullptr;
    if (src.data && abs_start >= src.base &&
        (uint64_t)abs_end - src.base <= src.size) {
        p = src.data + (abs_start - src.base);
    } else if (abs_end <= orig_blob.size()) {
        p = orig_blob.data() + abs_start;
    } else {
        return;
    }
    uint32_t write_start = (uint32_t)buf.size();
    WriteBytes(buf, p, abs_end - abs_start);
    // Opaque copies (gaps, trailing data, undecoded blocks) can contain
    // undecoded objects with ABSOLUTE POs. If the write position shifted,
    // verbatim bytes would keep stale POs — re-anchor every PO that was
    // self-referential at its source position. Identity writes are a no-op.
    FixPayloadPOs(buf, write_start, (uint32_t)buf.size(), p, abs_start);
}

// ── Write owned (imported) bytes, re-anchoring any POs they carry ──
// src_abs = the bytes' absolute position in their source blob (0 = unknown,
// fall back to sentinel-validated fixing).

static void WriteOwned(std::vector<uint8_t>& buf,
                       const std::vector<uint8_t>& bytes,
                       uint32_t src_abs) {
    uint32_t write_start = (uint32_t)buf.size();
    WriteBytes(buf, bytes);
    FixPayloadPOs(buf, write_start, (uint32_t)buf.size(), bytes.data(), src_abs);
}

// ── Serialize child fields with gap-copy from the element's own bytes ──

static void SerializeChildFields(std::vector<uint8_t>& buf,
                                  const std::vector<GenericFieldValue>& fields,
                                  uint32_t orig_payload_start,
                                  const std::vector<uint8_t>& orig_blob,
                                  const SrcRegion& src) {
    uint32_t orig_cursor = orig_payload_start + 4;

    for (auto& cf : fields) {
        if (!cf.present) continue;

        if (!cf.gap_before.empty()) {
            // Imported tree: gap bytes are carried on the field itself.
            WriteOwned(buf, cf.gap_before, cf.gap_before_src);
        } else if (cf.start_offset > orig_cursor) {
            CopyRange(buf, orig_cursor, cf.start_offset, src, orig_blob);
        }

        SerializeField(buf, cf, orig_blob);

        if (cf.end_offset > orig_cursor) {
            orig_cursor = cf.end_offset;
        }
    }
}

// ── Serialize inline object body (header + payload + trailing_size) ──
// Writes: MBC, mask_bytes, type_index, reserved_u8, sentinel, PO, reserved_u32,
//         [child fields with gaps], [trailing undecoded bytes], trailing_size.
// PO and trailing_size are COMPUTED FRESH — correct regardless of position.

static void SerializeInlineObjectBody(std::vector<uint8_t>& buf,
                                       const GenericFieldValue& f,
                                       const std::vector<uint8_t>& orig_blob) {
    SrcRegion src = RegionFor(f);

    WriteU16(buf, f.child_mask_byte_count);
    WriteBytes(buf, f.child_mask_bytes);
    WriteU16(buf, (uint16_t)f.child_type_index);
    WriteU8(buf, f.child_reserved_u8);
    WriteSentinel(buf, f.child_sentinel1_u32, f.child_sentinel2_u32);
    uint32_t po_pos = (uint32_t)buf.size();
    WriteU32(buf, 0);
    uint32_t payload_start = (uint32_t)buf.size();
    PatchU32(buf, po_pos, payload_start);
    WriteU32(buf, f.child_reserved_u32);

    SerializeChildFields(buf, f.child_fields, f.child_payload_offset, orig_blob, src);

    if (!f.trailing_bytes.empty()) {
        WriteOwned(buf, f.trailing_bytes, f.trailing_src);
    } else if (!f.child_fields.empty() && f.child_payload_offset > 0 && f.child_size_u32 > 0) {
        uint32_t last_end = f.child_payload_offset + 4;
        for (auto& cf : f.child_fields) {
            if (cf.present && cf.end_offset > last_end) {
                last_end = cf.end_offset;
            }
        }
        uint32_t trailing_pos = f.child_payload_offset + f.child_size_u32;
        if (last_end < trailing_pos) {
            CopyRange(buf, last_end, trailing_pos, src, orig_blob);
        }
    }

    uint32_t trailing_size = (uint32_t)buf.size() - payload_start;
    WriteU32(buf, trailing_size);

}

// ── Serialize compact list element ──
// Prefers tree path (child_fields populated). Falls back to raw payload + PO fix.

static void SerializeCompactElement(std::vector<uint8_t>& buf,
                                     const GenericFieldValue& elem,
                                     const std::vector<uint8_t>& orig_blob) {
    uint16_t mbc = elem.child_mask_byte_count;
    uint32_t header_size = 2 + mbc + 2 + 1 + 8 + 4;

    // Always write element header fresh (PO computed at write position)
    WriteU16(buf, mbc);
    WriteBytes(buf, elem.child_mask_bytes);
    WriteU16(buf, (uint16_t)elem.child_type_index);
    WriteU8(buf, elem.child_reserved_u8);
    WriteSentinel(buf, elem.child_sentinel1_u32, elem.child_sentinel2_u32);
    uint32_t po_pos = (uint32_t)buf.size();
    WriteU32(buf, 0);
    uint32_t payload_start = (uint32_t)buf.size();
    PatchU32(buf, po_pos, payload_start);

    if (!elem.child_fields.empty() || elem.raw_value.empty()) {
        // TREE PATH: recurse into child_fields — every nested PO computed fresh.
        // Gap/trailing bytes come from the element's own raw_value (parsed
        // elements) or from gap_before/trailing_bytes (imported elements,
        // child_payload_offset == 0).
        SrcRegion src = RegionFor(elem);
        WriteU32(buf, elem.child_reserved_u32);
        SerializeChildFields(buf, elem.child_fields, elem.child_payload_offset, orig_blob, src);

        if (!elem.trailing_bytes.empty()) {
            WriteOwned(buf, elem.trailing_bytes, elem.trailing_src);
        } else if (elem.child_payload_offset > 0 && elem.child_size_u32 > 0) {
            uint32_t last_end = elem.child_payload_offset + 4;
            for (auto& cf : elem.child_fields) {
                if (cf.present && cf.end_offset > last_end) {
                    last_end = cf.end_offset;
                }
            }
            uint32_t trailing_pos = elem.child_payload_offset + elem.child_size_u32;
            if (last_end < trailing_pos) {
                CopyRange(buf, last_end, trailing_pos, src, orig_blob);
            }
        }
    } else if (!elem.raw_value.empty() && elem.raw_value.size() >= header_size) {
        // RAW FALLBACK: for elements without child_fields (e.g., ParseElementFromBytes).
        // Copy payload bytes, then fix nested POs with _is_real_po validation.
        uint32_t payload_len = (uint32_t)elem.raw_value.size() - header_size;
        if (payload_len >= 4) payload_len -= 4;
        uint32_t write_start = (uint32_t)buf.size();
        WriteBytes(buf, elem.raw_value.data() + header_size, payload_len);

        FixPayloadPOs(buf, write_start, (uint32_t)buf.size(),
                      elem.raw_value.data() + header_size,
                      elem.start_offset > 0 ? elem.start_offset + header_size : 0);
    }

    uint32_t trailing_size = (uint32_t)buf.size() - payload_start;
    WriteU32(buf, trailing_size);

}

// ── Patch list count in header ──

static std::vector<uint8_t> PatchListHeader(const GenericFieldValue& f,
                                             uint32_t new_count) {
    std::vector<uint8_t> patched = f.list_header_raw;
    if (patched.empty()) return patched;

    // Preferred: exact count position recorded by the parser.
    if (f.list_count_format != 0 && f.list_count_offset < patched.size()) {
        uint32_t off = f.list_count_offset;
        switch (f.list_count_format) {
        case 1: // u32 LE
            if (off + 4 <= patched.size()) {
                patched[off]     = (uint8_t)(new_count & 0xFF);
                patched[off + 1] = (uint8_t)((new_count >> 8) & 0xFF);
                patched[off + 2] = (uint8_t)((new_count >> 16) & 0xFF);
                patched[off + 3] = (uint8_t)((new_count >> 24) & 0xFF);
                return patched;
            }
            break;
        case 2: // u24 LE
            if (off + 3 <= patched.size()) {
                patched[off]     = (uint8_t)(new_count & 0xFF);
                patched[off + 1] = (uint8_t)((new_count >> 8) & 0xFF);
                patched[off + 2] = (uint8_t)((new_count >> 16) & 0xFF);
                return patched;
            }
            break;
        case 3: // u16 BE
            if (off + 2 <= patched.size()) {
                patched[off]     = (uint8_t)((new_count >> 8) & 0xFF);
                patched[off + 1] = (uint8_t)(new_count & 0xFF);
                return patched;
            }
            break;
        }
    }

    // Legacy heuristic for trees parsed before count recording existed.
    uint8_t prefix = patched[0];
    if (prefix == 1 && patched.size() >= 3) {
        patched[1] = (uint8_t)((new_count >> 8) & 0xFF);
        patched[2] = (uint8_t)(new_count & 0xFF);
    } else if (prefix == 0 && patched.size() >= 4) {
        patched[1] = (uint8_t)(new_count & 0xFF);
        patched[2] = (uint8_t)((new_count >> 8) & 0xFF);
        patched[3] = (uint8_t)((new_count >> 16) & 0xFF);
    }
    return patched;
}

// ── Serialize a single field ──

static void SerializeField(std::vector<uint8_t>& buf,
                            const GenericFieldValue& f,
                            const std::vector<uint8_t>& orig_blob) {
    if (!f.present) return;

    switch (f.meta_kind) {
    case 0: case 1: case 2: case 3:
        // Scalars, byte arrays, dynamic arrays — no POs inside, safe to copy raw.
        if (!f.raw_value.empty()) {
            WriteBytes(buf, f.raw_value);
        }
        break;

    case 4:
        // Inline object — use raw_value if set and child tree is empty (transplant mode),
        // otherwise recurse to compute nested POs fresh.
        if (!f.raw_value.empty() && f.child_fields.empty()) {
            uint32_t write_start = (uint32_t)buf.size();
            WriteBytes(buf, f.raw_value);
            // Transplanted/imported data: source-validated PO fix when the
            // offsets demonstrably describe these bytes, sentinel-based otherwise.
            bool offs_match = f.start_offset > 0 &&
                f.end_offset - f.start_offset == (uint32_t)f.raw_value.size();
            FixPayloadPOs(buf, write_start, (uint32_t)buf.size(),
                          f.raw_value.data(), offs_match ? f.start_offset : 0);
        } else if (!f.child_fields.empty() || f.child_payload_offset > 0 ||
                   f.child_mask_byte_count > 0) {
            SerializeInlineObjectBody(buf, f, orig_blob);
        } else if (!f.raw_value.empty()) {
            uint32_t write_start = (uint32_t)buf.size();
            WriteBytes(buf, f.raw_value);
            FixPayloadPOs(buf, write_start, (uint32_t)buf.size(),
                          f.raw_value.data(),
                          f.start_offset);
        }
        break;

    case 5: {
        // Object pointer — use raw_value if set and child tree is empty (transplant mode).
        if (!f.raw_value.empty() && f.child_fields.empty()) {
            uint32_t write_start = (uint32_t)buf.size();
            WriteBytes(buf, f.raw_value);
            FixPayloadPOs(buf, write_start, (uint32_t)buf.size(),
                          f.raw_value.data(), 0);
            break;
        }
        if (!f.child_fields.empty() || f.child_payload_offset > 0 ||
            f.child_mask_byte_count > 0) {
            uint32_t prefix_len = f.child_prefix_len;
            if (prefix_len == 0) {
                // Legacy derivation for trees parsed before child_prefix_len existed
                uint32_t header_after_prefix = 2 + f.child_mask_byte_count + 2 + 1 + 8 + 4;
                if (f.child_payload_offset > f.start_offset + header_after_prefix) {
                    prefix_len = f.child_payload_offset - f.start_offset - header_after_prefix;
                }
            }
            if (prefix_len > 0 && prefix_len <= 3) {
                if (prefix_len >= 2) WriteU16(buf, f.child_prefix_u16);
                else WriteU8(buf, (uint8_t)f.child_prefix_u16);
                if (prefix_len == 3) WriteU8(buf, f.child_prefix_u8);
            }
            SerializeInlineObjectBody(buf, f, orig_blob);
        } else if (!f.raw_value.empty()) {
            uint32_t write_start = (uint32_t)buf.size();
            WriteBytes(buf, f.raw_value);
            FixPayloadPOs(buf, write_start, (uint32_t)buf.size(),
                          f.raw_value.data(),
                          f.start_offset);
        }
        break;
    }

    case 6: case 7: {
        // Object list serialization strategy:
        // 1. If raw_value is EMPTY (cleared by Insert/Remove) → reconstruct from elements
        // 2. If raw_value is present AND elements == count → reconstruct (tree path, POs fresh)
        // 3. If raw_value present AND elements < count → partial decode, use raw_value
        bool has_raw = !f.raw_value.empty();
        // Use raw_value (preserve original bytes) when:
        // - Parser decoded fewer elements than header count (partial decode)
        // - List has 0 elements but raw_value exists (empty/undecodeable list)
        bool use_raw = has_raw && (
            ((uint32_t)f.list_elements.size() < f.list_count && f.list_count > 0) ||
            (f.list_elements.empty() && f.raw_value.size() > 0)
        );

        if (!use_raw && !f.list_header_raw.empty()) {
            // Reconstruct from elements (tree path — all POs computed fresh)
            uint32_t list_write_start = (uint32_t)buf.size();
            auto header = f.list_header_raw;
            if ((uint32_t)f.list_elements.size() != f.list_count) {
                header = PatchListHeader(f, (uint32_t)f.list_elements.size());
            }
            WriteBytes(buf, header);
            for (auto& elem : f.list_elements) {
                SerializeCompactElement(buf, elem, orig_blob);
            }
            uint32_t list_written = (uint32_t)buf.size() - list_write_start;
            if (f.list_elements.size() >= 18 && f.name.find("inventorylist") != std::string::npos) {
                fprintf(stderr, "[SER] _inventorylist: wrote %u bytes for %zu elements (rv0=%zu)\n",
                    list_written, f.list_elements.size(),
                    f.list_elements.empty() ? 0 : f.list_elements[0].raw_value.size());
            }
        } else if (use_raw) {
            // Partial/empty/undecodeable list — preserve original bytes (or transplant data)
            uint32_t write_start = (uint32_t)buf.size();
            WriteBytes(buf, f.raw_value);
            // Only fix POs if the data originated from the SAME blob (not transplanted).
            // Transplanted data has start_offset=0 or mismatched — PO fixup would corrupt it.
            if (f.start_offset > 0 && f.start_offset + f.raw_value.size() <= orig_blob.size()) {
                FixPayloadPOs(buf, write_start, (uint32_t)buf.size(),
                              f.raw_value.data(),
                              f.start_offset);
            } else {
                // Transplanted data: POs are relative to position 0 within raw_value.
                // Shift all POs by write_start (absolute position in new blob).
                FixPayloadPOs(buf, write_start, (uint32_t)buf.size(),
                              f.raw_value.data(), 0);
            }
        }
        break;
    }

    default:
        if (!f.raw_value.empty()) {
            WriteBytes(buf, f.raw_value);
        }
        break;
    }
}

// ── Main serializer ──

std::vector<uint8_t> Serialize(const ParseResult& result,
                                const std::vector<uint8_t>& orig_blob) {
    std::vector<uint8_t> buf;
    buf.reserve(orig_blob.size() + 4096);

    // 1. Copy schema verbatim
    uint32_t schema_end = result.schema.schema_end;
    buf.insert(buf.end(), orig_blob.begin(), orig_blob.begin() + schema_end);

    // 2. Write TOC header
    uint32_t toc_header_pos = (uint32_t)buf.size();
    WriteU32(buf, 0);
    WriteU32(buf, (uint32_t)result.toc.entries.size());
    WriteU32(buf, 0); // stream_size placeholder

    // 3. Write TOC entry placeholders
    uint32_t toc_entries_pos = (uint32_t)buf.size();
    for (auto& te : result.toc.entries) {
        WriteU32(buf, te.class_index);
        WriteU32(buf, 0xFFFFFFFF);
        WriteU32(buf, 0xFFFFFFFF);
        WriteU32(buf, 0);
        WriteU32(buf, 0);
    }

    // 4. Serialize each object block
    for (size_t i = 0; i < result.objects.size(); ++i) {
        auto& obj = result.objects[i];
        uint32_t block_start = (uint32_t)buf.size();

        WriteU16(buf, obj.mask_byte_count);
        WriteBytes(buf, obj.header_mask_bytes);
        WriteU32(buf, obj.reserved_u32);

        uint32_t obj_header_size = 2 + (uint32_t)obj.header_mask_bytes.size() + 4;
        uint32_t orig_cursor = obj.data_offset + obj_header_size;

        for (auto& field : obj.fields) {
            if (!field.present) continue;

            if (!field.gap_before.empty()) {
                WriteOwned(buf, field.gap_before, field.gap_before_src);
            } else if (field.start_offset > orig_cursor) {
                CopyRange(buf, orig_cursor, field.start_offset, SrcRegion{}, orig_blob);
            }

            SerializeField(buf, field, orig_blob);

            if (field.end_offset > orig_cursor) {
                orig_cursor = field.end_offset;
            }
        }

        // Trailing data after the last field (PO-fixed — undecoded blocks
        // copied verbatim must still have their absolute POs re-anchored)
        if (!obj.trailing_bytes.empty()) {
            WriteOwned(buf, obj.trailing_bytes, obj.trailing_src);
        } else {
            uint32_t block_orig_end = obj.data_offset + obj.data_size;
            if (orig_cursor < block_orig_end) {
                CopyRange(buf, orig_cursor, block_orig_end, SrcRegion{}, orig_blob);
            }
        }

        uint32_t block_size = (uint32_t)buf.size() - block_start;
        uint32_t toc_entry_pos = toc_entries_pos + (uint32_t)(i * 20);
        PatchU32(buf, toc_entry_pos + 12, block_start);
        PatchU32(buf, toc_entry_pos + 16, block_size);
    }

    // 5. Patch stream_size
    PatchU32(buf, toc_header_pos + 8, (uint32_t)buf.size());

    // NO DELTA PO FIXUP NEEDED.
    // All POs were computed fresh during the recursive tree walk above.
    // SerializeInlineObjectBody and SerializeCompactElement write correct POs
    // at every nesting level. FixPayloadPOs handles the rare fallback case.

    return buf;
}

} // namespace ParcSerializer
