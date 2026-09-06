/**
 * blob_fixup.h — Shared PO and trailing-size offset collection/fixup utilities.
 * Extracted from dye_cli.cpp for reuse across parc_engine, dye_cli, etc.
 */
#pragma once
#include "save_parser_cpp.h"
#include <vector>
#include <cstring>

namespace BlobFixup {

using namespace SaveParserCpp;

struct OffsetPos {
    uint32_t pos;     // position of the PO u32 in blob
    uint32_t value;   // current PO value
};

struct TrailingSize {
    uint32_t size_pos;       // position of the trailing_size u32
    uint32_t payload_start;  // payload start offset this size is relative to
};

inline uint32_t ReadU32(const std::vector<uint8_t>& blob, uint32_t off) {
    uint32_t v = 0;
    if (off + 4 <= blob.size()) memcpy(&v, blob.data() + off, 4);
    return v;
}

inline void WriteU32(std::vector<uint8_t>& blob, uint32_t off, uint32_t val) {
    if (off + 4 <= blob.size()) memcpy(blob.data() + off, &val, 4);
}

inline int ComputePoPos(const GenericFieldValue& f, const std::vector<uint8_t>& blob) {
    if (f.child_payload_offset == 0 || f.start_offset == 0) return -1;
    if (f.child_mask_byte_count == 0) return -1;

    uint32_t expected = f.child_payload_offset;

    if (f.note == "compact_list_element" || f.decode_kind == "list_element") {
        uint32_t pos = f.start_offset + 2 + f.child_mask_byte_count + 2 + 1 + 8;
        if (pos + 4 <= blob.size() && ReadU32(blob, pos) == expected) return (int)pos;
        return -1;
    }

    if (f.meta_kind == 4) {
        uint32_t pos = f.start_offset + 2 + f.child_mask_byte_count + 11;
        if (pos + 4 <= blob.size() && ReadU32(blob, pos) == expected) return (int)pos;
        return -1;
    }

    if (f.meta_kind == 5) {
        for (uint32_t pd : {0u, 1u, 3u}) {
            uint32_t body = f.start_offset + pd;
            uint32_t pos = body + 2 + f.child_mask_byte_count + 11;
            if (pos + 4 <= blob.size() && ReadU32(blob, pos) == expected) return (int)pos;
        }
        return -1;
    }

    if (f.meta_kind == 6 || f.meta_kind == 7) {
        uint32_t pos = f.start_offset + 2 + f.child_mask_byte_count + 11;
        if (pos + 4 <= blob.size() && ReadU32(blob, pos) == expected) return (int)pos;
        for (uint32_t pd : {1u, 3u}) {
            uint32_t body = f.start_offset + pd;
            pos = body + 2 + f.child_mask_byte_count + 11;
            if (pos + 4 <= blob.size() && ReadU32(blob, pos) == expected) return (int)pos;
        }
        return -1;
    }

    return -1;
}

inline void CollectOffsetsRecursive(const GenericFieldValue& fv,
                                     const std::vector<uint8_t>& blob,
                                     std::vector<OffsetPos>& offsets,
                                     std::vector<TrailingSize>& trailing_sizes) {
    if (fv.child_payload_offset > 0 && fv.start_offset > 0) {
        int pos = ComputePoPos(fv, blob);
        if (pos >= 0) {
            offsets.push_back({(uint32_t)pos, fv.child_payload_offset});
        }
    }

    if (fv.child_size_u32 > 0 && fv.child_payload_offset > 0) {
        uint32_t size_pos = fv.child_payload_offset + fv.child_size_u32;
        trailing_sizes.push_back({size_pos, fv.child_payload_offset});
    }

    for (auto& cf : fv.child_fields) {
        CollectOffsetsRecursive(cf, blob, offsets, trailing_sizes);
    }
    for (auto& le : fv.list_elements) {
        CollectOffsetsRecursive(le, blob, offsets, trailing_sizes);
    }
}

inline void CollectAllOffsets(const ParseResult& result,
                               const std::vector<uint8_t>& blob,
                               std::vector<OffsetPos>& offsets,
                               std::vector<TrailingSize>& trailing_sizes) {
    for (auto& obj : result.objects) {
        for (auto& f : obj.fields) {
            CollectOffsetsRecursive(f, blob, offsets, trailing_sizes);
        }
    }
    // Verify
    std::vector<OffsetPos> verified;
    verified.reserve(offsets.size());
    for (auto& op : offsets) {
        if (op.pos + 4 <= blob.size() && ReadU32(blob, op.pos) == op.value) {
            verified.push_back(op);
        }
    }
    offsets = std::move(verified);
}

/**
 * Apply fixups after inserting `delta` bytes at position `insert_at`.
 * Works on the ALREADY-SPLICED blob (insertion already done).
 *
 * @param blob         Modified blob (post-insertion)
 * @param offsets      PO positions collected from ORIGINAL blob
 * @param trailing     Trailing sizes collected from ORIGINAL blob
 * @param result       Parse result from ORIGINAL blob
 * @param insert_at    Where the insertion happened (in original blob coordinates)
 * @param delta        How many bytes were inserted (positive) or removed (negative)
 * @param block_toc_idx  TOC index of the block that was modified
 */
inline int ApplyFixups(std::vector<uint8_t>& blob,
                        const std::vector<OffsetPos>& offsets,
                        const std::vector<TrailingSize>& trailing,
                        const ParseResult& result,
                        uint32_t insert_at,
                        int32_t delta,
                        uint32_t block_toc_idx) {
    int fixed = 0;

    // Fix POs
    for (auto& op : offsets) {
        if (insert_at <= op.pos && op.pos < insert_at) continue; // skip replaced region (for future delete)
        uint32_t new_pos = (op.pos >= insert_at) ? op.pos + delta : op.pos;
        if (new_pos + 4 > blob.size()) continue;
        uint32_t cur_val = ReadU32(blob, new_pos);
        if (cur_val >= insert_at) {
            WriteU32(blob, new_pos, cur_val + delta);
            ++fixed;
        }
    }

    // Fix trailing sizes
    for (auto& ts : trailing) {
        uint32_t new_size_pos = (ts.size_pos >= insert_at) ? ts.size_pos + delta : ts.size_pos;
        if (ts.payload_start < insert_at && ts.size_pos >= insert_at) {
            if (new_size_pos + 4 > blob.size()) continue;
            uint32_t old_val = ReadU32(blob, new_size_pos);
            WriteU32(blob, new_size_pos, old_val + delta);
        }
    }

    // Fix TOC
    uint32_t schema_end = result.schema.schema_end;
    uint32_t stream_size_pos = schema_end + 8;
    WriteU32(blob, stream_size_pos, ReadU32(blob, stream_size_pos) + delta);

    for (auto& te : result.toc.entries) {
        uint32_t doff_pos = te.entry_offset + 12;
        uint32_t dsize_pos = te.entry_offset + 16;
        if (te.index == block_toc_idx) {
            WriteU32(blob, dsize_pos, ReadU32(blob, dsize_pos) + delta);
        }
        if (te.data_offset >= insert_at) {
            WriteU32(blob, doff_pos, ReadU32(blob, doff_pos) + delta);
        }
    }

    return fixed;
}

} // namespace BlobFixup
