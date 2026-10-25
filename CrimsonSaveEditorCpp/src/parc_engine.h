/**
 * parc_engine.h — PARC manipulation engine.
 * Load saves, insert/remove elements from lists, write back.
 *
 * Tree-based approach: modifications change the in-memory parse tree,
 * then the serializer writes a fresh blob. No PO fixup needed.
 */
#pragma once
#include "save_parser_cpp.h"
#include "blob_fixup.h"
#include "parc_serializer.h"
#include <vector>
#include <string>
#include <cstdint>
#include <unordered_map>

namespace ParcEngine {

using namespace SaveParserCpp;

struct SaveTree {
    std::vector<uint8_t> blob;
    std::vector<uint8_t> original_header;
    bool is_encrypted = false;

    ParseResult parsed;

    // Legacy — kept for API compat, no longer populated by the engine.
    std::vector<BlobFixup::OffsetPos> po_table;
    std::vector<BlobFixup::TrailingSize> ts_table;

    std::unordered_map<std::string, uint32_t> name_to_type_idx;
};

struct InsertResult {
    bool ok = false;
    std::string error;
    int new_element_index = -1;
    uint32_t insert_offset = 0;
    int32_t growth = 0;
    int po_fixed = 0;
};

struct RemoveResult {
    bool ok = false;
    std::string error;
    int32_t shrink = 0;
    int po_fixed = 0;
};

SaveTree LoadSave(const std::string& path);
void RebuildOffsetTables(SaveTree& tree);
void Reparse(SaveTree& tree, bool rebuild_offsets = true);

InsertResult InsertIntoList(SaveTree& tree,
                            const std::string& block_class,
                            const std::string& list_field,
                            const std::vector<uint8_t>& element_bytes,
                            int position = -1);

// Nested insertion: insert into a list at any depth in the tree.
// path = "parentList[index].childList" e.g. "_inventorylist[1]._itemList"
// Clears raw_value at every ancestor level so the serializer reconstructs the full path.
InsertResult InsertNested(SaveTree& tree,
                          const std::string& block_class,
                          const std::string& path,
                          const std::vector<uint8_t>& element_bytes,
                          int position = -1);

RemoveResult RemoveFromList(SaveTree& tree,
                            const std::string& block_class,
                            const std::string& list_field,
                            int element_index);

// Nested removal: remove from a list at any depth.
RemoveResult RemoveNested(SaveTree& tree,
                          const std::string& block_class,
                          const std::string& path,
                          int element_index);

void RemapTypeIndices(std::vector<uint8_t>& tmpl,
                      const SaveTree& tree,
                      const std::vector<std::string>& structural_roles);

void AdaptTypeIndicesFromReference(std::vector<uint8_t>& tmpl,
                                    const std::vector<uint8_t>& blob,
                                    const SaveTree& tree,
                                    const std::string& block_class,
                                    const std::string& list_field_name);

void FixTemplatePOs(std::vector<uint8_t>& tmpl, uint32_t insert_pos);

// Byte-splice insertion — inserts raw bytes into the blob, fixes POs,
// trailing sizes, list count, and TOC. No tree reconstruction.
// Same approach as the Python parc_inserter3.
InsertResult SpliceIntoList(SaveTree& tree,
                            const std::string& block_class,
                            const std::string& list_field,
                            std::vector<uint8_t>& element_bytes,
                            uint32_t source_elem_offset = 0,
                            int element_count = 1);

// Replace an element's bytes in the blob. Handles size changes (grow/shrink).
// old_start/old_end = current element range. new_bytes = replacement.
// Fixes POs, trailing sizes, TOC — same as splice but with delete+insert.
InsertResult ReplaceElement(SaveTree& tree,
                            const std::string& block_class,
                            uint32_t old_start, uint32_t old_end,
                            std::vector<uint8_t>& new_bytes);

void WriteSave(const SaveTree& tree, const std::string& path);

uint32_t ReadU32(const SaveTree& tree, uint32_t offset);
void WriteU32(SaveTree& tree, uint32_t offset, uint32_t value);

} // namespace ParcEngine
