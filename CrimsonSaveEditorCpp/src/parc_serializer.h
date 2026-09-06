/**
 * parc_serializer.h — Serialize a ParseResult back to a PARC blob from scratch.
 *
 * No PO fixup needed — all offsets computed fresh during serialization.
 * This is the "Skyrim approach": parse → modify tree → reserialize everything.
 */
#pragma once
#include "save_parser_cpp.h"
#include <vector>
#include <cstdint>

namespace ParcSerializer {

using namespace SaveParserCpp;

/**
 * Serialize the entire ParseResult back to a raw PARC blob.
 * Schema + TOC + all object blocks are written from the in-memory tree.
 * All POs and trailing sizes are computed fresh — no fixup needed.
 *
 * The input ParseResult must have:
 * - schema.types with field definitions
 * - toc.entries with class_index (data_offset/data_size will be recomputed)
 * - objects with fields/child_fields/list_elements
 *
 * For fields that weren't decoded (undecoded_ranges), the original raw bytes
 * from orig_blob are copied verbatim.
 */
std::vector<uint8_t> Serialize(const ParseResult& result,
                                const std::vector<uint8_t>& orig_blob);

} // namespace ParcSerializer
