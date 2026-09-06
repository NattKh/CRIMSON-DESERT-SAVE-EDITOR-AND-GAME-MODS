/**
 * parc_engine.cpp — PARC manipulation engine implementation.
 *
 * Insert/Remove modify the in-memory parse tree, then call the
 * tree-based serializer. No blob-level PO fixup. No offset tables.
 */
#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#else
#include <unistd.h>
#define GetCurrentProcessId() ((unsigned)getpid())
#endif
#include "parc_engine.h"
#include "save_writer.h"
#include <fstream>
#include <stdexcept>
#include <algorithm>
#include <cstring>
#include <cstdio>
#include <unordered_set>
#include <functional>

namespace ParcEngine {

static const uint8_t SENTINEL_BYTES[8] = {0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF};

// ── Collect ALL PO positions (sentinel scan + tree scan) ──
// Returns {position, value} pairs for every PO in the blob.
// The sentinel scan finds 0xFF×8 POs. The tree scan finds zero-sentinel POs
// by looking for known child_payload_offset values in each element's bytes.

struct POEntry { uint32_t pos; uint32_t val; };

static uint32_t ReadU32At(const std::vector<uint8_t>& b, uint32_t o) {
    uint32_t v = 0;
    if (o + 4 <= b.size()) memcpy(&v, b.data() + o, 4);
    return v;
}

static std::vector<POEntry> CollectAllPOs(const SaveTree& tree) {
    auto& blob = tree.blob;
    std::vector<POEntry> out;

    // 1. Sentinel scan (0xFF×8 + self-referential)
    for (uint32_t p = 0; p + 12 <= (uint32_t)blob.size(); ++p) {
        if (blob[p] != 0xFF) continue;
        if (memcmp(blob.data() + p, SENTINEL_BYTES, 8) != 0) continue;
        uint32_t po_pos = p + 8;
        uint32_t po_val = ReadU32At(blob, po_pos);
        if (po_val == po_pos + 4)
            out.push_back({po_pos, po_val});
    }

    // 2. Tree scan — finds zero-sentinel POs
    std::unordered_set<uint32_t> seen;
    for (auto& e : out) seen.insert(e.pos);

    std::function<void(const GenericFieldValue&)> scan;
    scan = [&](const GenericFieldValue& fv) {
        if (fv.child_payload_offset > 0 && fv.start_offset > 0 && fv.end_offset > fv.start_offset) {
            uint32_t tv = fv.child_payload_offset;
            uint32_t s = fv.start_offset;
            uint32_t e = std::min(fv.end_offset, s + 32);
            for (uint32_t p = s; p + 4 <= e; p++) {
                if (ReadU32At(blob, p) == tv && seen.find(p) == seen.end()) {
                    out.push_back({p, tv});
                    seen.insert(p);
                    break;
                }
            }
        }
        for (auto& cf : fv.child_fields) scan(cf);
        for (auto& le : fv.list_elements) scan(le);
    };
    for (auto& obj : tree.parsed.objects)
        for (auto& f : obj.fields) scan(f);

    return out;
}

// ── Helpers ──

static std::vector<uint8_t> ReadFile(const std::string& path) {
    std::ifstream f(path, std::ios::binary);
    if (!f) throw std::runtime_error("Cannot open: " + path);
    f.seekg(0, std::ios::end);
    size_t sz = f.tellg();
    f.seekg(0);
    std::vector<uint8_t> data(sz);
    f.read(reinterpret_cast<char*>(data.data()), sz);
    return data;
}

static void WriteTempFile(const std::string& path, const std::vector<uint8_t>& data) {
    std::ofstream f(path, std::ios::binary);
    f.write(reinterpret_cast<const char*>(data.data()), data.size());
}

static uint16_t ReadU16LE(const uint8_t* p) {
    return (uint16_t)p[0] | ((uint16_t)p[1] << 8);
}

// ── LoadSave ──

SaveTree LoadSave(const std::string& path) {
    SaveTree tree;

    auto file_data = ReadFile(path);
    if (file_data.size() < 4) throw std::runtime_error("File too small");

    if (file_data[0] == 'S' && file_data[1] == 'A' && file_data[2] == 'V' && file_data[3] == 'E') {
        tree.is_encrypted = true;
        tree.original_header.assign(file_data.begin(), file_data.begin() + std::min<size_t>(0x80, file_data.size()));
        tree.parsed = SaveParserCpp::ParseFile(path);
    } else {
        tree.is_encrypted = false;
        tree.parsed = SaveParserCpp::ParseRawFile(path);
    }

    tree.blob = tree.parsed.raw_blob;

    for (size_t i = 0; i < tree.parsed.schema.types.size(); ++i) {
        tree.name_to_type_idx[tree.parsed.schema.types[i].name] = (uint32_t)i;
    }

    return tree;
}

// ── RebuildOffsetTables (legacy — now a no-op) ──

void RebuildOffsetTables(SaveTree& tree) {
    tree.po_table.clear();
    tree.ts_table.clear();
}

// ── Reparse ──

void Reparse(SaveTree& tree, bool rebuild_offsets) {
    char tmp_path[512];
    snprintf(tmp_path, sizeof(tmp_path), "%s\\parc_engine_%u.tmp",
             getenv("TEMP") ? getenv("TEMP") : ".", (unsigned)GetCurrentProcessId());
    WriteTempFile(tmp_path, tree.blob);
    tree.parsed = SaveParserCpp::ParseRawFile(tmp_path);
    std::remove(tmp_path);

    tree.name_to_type_idx.clear();
    for (size_t i = 0; i < tree.parsed.schema.types.size(); ++i) {
        tree.name_to_type_idx[tree.parsed.schema.types[i].name] = (uint32_t)i;
    }

    (void)rebuild_offsets;
}

// ── Find block and list field in parsed tree ──

struct ListLocation {
    ObjectBlock* block = nullptr;
    GenericFieldValue* list_field = nullptr;
    uint32_t block_toc_idx = 0;
    uint32_t element_count = 0;
};

static ListLocation FindListMut(SaveTree& tree,
                                 const std::string& block_class,
                                 const std::string& list_field_name) {
    ListLocation loc;

    for (auto& obj : tree.parsed.objects) {
        if (obj.class_name.find(block_class) == std::string::npos) continue;
        loc.block = &obj;
        loc.block_toc_idx = obj.entry_index;

        for (auto& f : obj.fields) {
            if (f.name != list_field_name) continue;
            if (f.meta_kind != 6 && f.meta_kind != 7) continue;

            loc.list_field = &f;
            loc.element_count = (uint32_t)f.list_elements.size();
            return loc;
        }
        break;
    }
    return loc;
}

// ── Parse compact element header from raw bytes ──
// Creates a GenericFieldValue with header fields set and raw_value
// containing the full element bytes (for the serializer's raw-element path).

static GenericFieldValue ParseElementFromBytes(const std::vector<uint8_t>& bytes) {
    if (bytes.size() < 18)
        throw std::runtime_error("Element bytes too small for compact header");

    GenericFieldValue elem;
    elem.present = true;
    elem.decode_kind = "object_locator";

    elem.child_prefix_u16 = ReadU16LE(bytes.data());
    uint16_t mbc = elem.child_prefix_u16;

    uint32_t min_size = 2 + mbc + 2 + 1 + 8 + 4;
    if (bytes.size() < min_size)
        throw std::runtime_error("Element bytes too small for header with MBC=" + std::to_string(mbc));

    elem.child_mask_byte_count = mbc;
    elem.child_mask_bytes.assign(bytes.begin() + 2, bytes.begin() + 2 + mbc);
    elem.child_type_index = (int32_t)ReadU16LE(bytes.data() + 2 + mbc);
    elem.child_reserved_u8 = bytes[2 + mbc + 2];

    uint32_t sentinel_off = 2 + mbc + 2 + 1;
    if (memcmp(bytes.data() + sentinel_off, SENTINEL_BYTES, 8) != 0)
        throw std::runtime_error("Element bytes missing sentinel at offset " + std::to_string(sentinel_off));

    uint32_t s1 = 0, s2 = 0;
    memcpy(&s1, bytes.data() + sentinel_off, 4);
    memcpy(&s2, bytes.data() + sentinel_off + 4, 4);
    elem.child_sentinel1_u32 = s1;
    elem.child_sentinel2_u32 = s2;

    uint32_t payload_off = sentinel_off + 8 + 4;
    if (bytes.size() >= payload_off + 4) {
        uint32_t res = 0;
        memcpy(&res, bytes.data() + payload_off, 4);
        elem.child_reserved_u32 = res;
    }

    elem.raw_value = bytes;
    return elem;
}

// ── InsertIntoList ──

InsertResult InsertIntoList(SaveTree& tree,
                            const std::string& block_class,
                            const std::string& list_field,
                            const std::vector<uint8_t>& element_bytes,
                            int position) {
    InsertResult r;

    // The blob is the source of truth (scalar edits memcpy into it directly).
    // Reparse so the tree we serialize from reflects every blob edit.
    Reparse(tree, false);

    auto loc = FindListMut(tree, block_class, list_field);
    if (!loc.block) {
        r.error = "Block not found: " + block_class;
        return r;
    }
    if (!loc.list_field) {
        r.error = "List field not found: " + list_field;
        return r;
    }

    // Parse element from raw bytes. Never copy child_fields from existing
    // elements — the new bytes may be from ItemFactory or a different item.
    GenericFieldValue new_elem;
    auto& elems = loc.list_field->list_elements;
    try {
        new_elem = ParseElementFromBytes(element_bytes);
    } catch (const std::exception& e) {
        r.error = std::string("Failed to parse element: ") + e.what();
        return r;
    }
    // start_offset stays 0 — triggers unconditional PO fix in serializer

    // Insert into the tree's list_elements
    if (position < 0 || position >= (int)elems.size()) {
        r.new_element_index = (int)elems.size();
        elems.push_back(std::move(new_elem));
    } else {
        r.new_element_index = position;
        elems.insert(elems.begin() + position, std::move(new_elem));
    }

    // Clear raw_value so the serializer reconstructs the list
    // from list_elements (which now includes the new element)
    loc.list_field->raw_value.clear();

    r.growth = (int32_t)element_bytes.size();

    // Serialize from the modified tree — POs computed fresh
    tree.blob = ParcSerializer::Serialize(tree.parsed, tree.blob);

    // Reparse to get a clean tree matching the new blob
    Reparse(tree, false);

    r.ok = true;
    return r;
}

// ── RemoveFromList ──

RemoveResult RemoveFromList(SaveTree& tree,
                            const std::string& block_class,
                            const std::string& list_field,
                            int element_index) {
    RemoveResult r;

    // Blob is canonical — pick up any direct blob edits before reserializing.
    Reparse(tree, false);

    auto loc = FindListMut(tree, block_class, list_field);
    if (!loc.block) { r.error = "Block not found: " + block_class; return r; }
    if (!loc.list_field) { r.error = "List field not found: " + list_field; return r; }
    if (element_index < 0 || element_index >= (int)loc.element_count) {
        r.error = "Element index out of range";
        return r;
    }

    // Compute the size of the removed element from raw_value
    auto& elems = loc.list_field->list_elements;
    auto& elem = elems[element_index];
    int32_t shrink = 0;
    if (elem.end_offset > elem.start_offset) {
        shrink = (int32_t)(elem.end_offset - elem.start_offset);
    } else if (!elem.raw_value.empty()) {
        shrink = (int32_t)elem.raw_value.size();
    }
    r.shrink = shrink;

    // Remove from the tree
    elems.erase(elems.begin() + element_index);

    // Clear raw_value so the serializer reconstructs from list_elements
    loc.list_field->raw_value.clear();

    // Serialize from the modified tree
    tree.blob = ParcSerializer::Serialize(tree.parsed, tree.blob);

    // Reparse to get a clean tree
    Reparse(tree, false);

    r.ok = true;
    return r;
}

// ── InsertNested / RemoveNested ──
// Navigate a dot-bracket path like "_inventorylist[1]._itemList" to find a nested list.
// Clear raw_value at every ancestor along the path so the serializer reconstructs everything.

struct PathSegment {
    std::string field_name;
    int index = -1; // -1 = not an index segment
};

static std::vector<PathSegment> ParsePath(const std::string& path) {
    std::vector<PathSegment> segments;
    size_t pos = 0;
    while (pos < path.size()) {
        if (path[pos] == '.') { pos++; continue; }
        PathSegment seg;
        size_t bracket = path.find('[', pos);
        size_t dot = path.find('.', pos);
        size_t end = std::min(bracket, dot);
        if (end == std::string::npos) end = path.size();
        seg.field_name = path.substr(pos, end - pos);
        pos = end;
        if (pos < path.size() && path[pos] == '[') {
            size_t close = path.find(']', pos);
            if (close != std::string::npos) {
                seg.index = atoi(path.substr(pos + 1, close - pos - 1).c_str());
                pos = close + 1;
            }
        }
        segments.push_back(seg);
    }
    return segments;
}

InsertResult InsertNested(SaveTree& tree,
                          const std::string& block_class,
                          const std::string& path,
                          const std::vector<uint8_t>& element_bytes,
                          int position) {
    InsertResult r;
    auto segments = ParsePath(path);
    if (segments.empty()) { r.error = "Empty path"; return r; }

    // Blob is canonical — pick up any direct blob edits before reserializing.
    Reparse(tree, false);

    // Find the block
    ObjectBlock* block = nullptr;
    for (auto& obj : tree.parsed.objects) {
        if (obj.class_name.find(block_class) != std::string::npos) {
            block = &obj;
            break;
        }
    }
    if (!block) { r.error = "Block not found: " + block_class; return r; }

    // Navigate through the path, collecting ancestors whose raw_value must be cleared
    struct Ancestor { GenericFieldValue* field; };
    std::vector<Ancestor> ancestors;

    // Start with block's fields
    std::vector<GenericFieldValue>* current_fields = &block->fields;
    GenericFieldValue* target_list = nullptr;

    for (size_t si = 0; si < segments.size(); si++) {
        auto& seg = segments[si];
        GenericFieldValue* found = nullptr;
        for (auto& f : *current_fields) {
            if (f.name == seg.field_name) { found = &f; break; }
        }
        if (!found) { r.error = "Field not found: " + seg.field_name; return r; }

        ancestors.push_back({found});

        if (si == segments.size() - 1) {
            target_list = found;
        } else {
            if (seg.index >= 0) {
                if (seg.index >= (int)found->list_elements.size()) {
                    r.error = "Index " + std::to_string(seg.index) + " out of range for " + seg.field_name;
                    return r;
                }
                auto& elem = found->list_elements[seg.index];
                // Do NOT add elem to ancestors — clearing an element's raw_value
                // deletes its data source and the serializer produces empty output.
                // Only fields need raw_value cleared to trigger reconstruction.
                current_fields = &elem.child_fields;
            } else {
                current_fields = &found->child_fields;
            }
        }
    }

    if (!target_list || (target_list->meta_kind != 6 && target_list->meta_kind != 7)) {
        r.error = "Target is not a list: " + segments.back().field_name;
        return r;
    }

    // Create new element from the provided bytes.
    // Parse the header (MBC, type_index, sentinel, etc.) from the raw bytes.
    // Do NOT copy child_fields from an existing element — the new bytes might
    // be a completely different item (from ItemFactory or cross-save transfer).
    // The serializer's raw fallback path handles POs correctly.
    GenericFieldValue new_elem;
    auto& elems = target_list->list_elements;
    try { new_elem = ParseElementFromBytes(element_bytes); }
    catch (const std::exception& e) { r.error = e.what(); return r; }
    // Leave start_offset=0: triggers unconditional sentinel-based PO fix in serializer.
    // This is correct for ItemFactory items, JSON dumps, and cross-save transfers
    // where PO positions don't match the target blob.

    // Insert
    if (position < 0 || position >= (int)elems.size()) {
        r.new_element_index = (int)elems.size();
        elems.push_back(std::move(new_elem));
    } else {
        r.new_element_index = position;
        elems.insert(elems.begin() + position, std::move(new_elem));
    }

    // Clear raw_value at EVERY ancestor level so serializer reconstructs the full path
    for (auto& anc : ancestors) {
        anc.field->raw_value.clear();
    }

    r.growth = (int32_t)element_bytes.size();

    // Serialize from modified tree — all POs computed fresh at every level
    tree.blob = ParcSerializer::Serialize(tree.parsed, tree.blob);
    Reparse(tree, false);

    r.ok = true;
    return r;
}

RemoveResult RemoveNested(SaveTree& tree,
                          const std::string& block_class,
                          const std::string& path,
                          int element_index) {
    RemoveResult r;
    auto segments = ParsePath(path);
    if (segments.empty()) { r.error = "Empty path"; return r; }

    // Blob is canonical — pick up any direct blob edits before reserializing.
    Reparse(tree, false);

    ObjectBlock* block = nullptr;
    for (auto& obj : tree.parsed.objects) {
        if (obj.class_name.find(block_class) != std::string::npos) {
            block = &obj; break;
        }
    }
    if (!block) { r.error = "Block not found: " + block_class; return r; }

    std::vector<GenericFieldValue*> ancestors_to_clear;
    std::vector<GenericFieldValue>* current_fields = &block->fields;
    GenericFieldValue* target_list = nullptr;

    for (size_t si = 0; si < segments.size(); si++) {
        auto& seg = segments[si];
        GenericFieldValue* found = nullptr;
        for (auto& f : *current_fields) {
            if (f.name == seg.field_name) { found = &f; break; }
        }
        if (!found) { r.error = "Field not found: " + seg.field_name; return r; }
        ancestors_to_clear.push_back(found);

        if (si == segments.size() - 1) {
            target_list = found;
        } else if (seg.index >= 0) {
            if (seg.index >= (int)found->list_elements.size()) {
                r.error = "Index out of range"; return r;
            }
            // Don't add the element to ancestors_to_clear — only fields
            current_fields = &found->list_elements[seg.index].child_fields;
        } else {
            current_fields = &found->child_fields;
        }
    }

    if (!target_list) { r.error = "Target not found"; return r; }
    auto& elems = target_list->list_elements;
    if (element_index < 0 || element_index >= (int)elems.size()) {
        r.error = "Element index out of range"; return r;
    }

    if (!elems[element_index].raw_value.empty())
        r.shrink = (int32_t)elems[element_index].raw_value.size();
    else if (elems[element_index].end_offset > elems[element_index].start_offset)
        r.shrink = (int32_t)(elems[element_index].end_offset - elems[element_index].start_offset);

    elems.erase(elems.begin() + element_index);

    for (auto* anc : ancestors_to_clear) anc->raw_value.clear();

    tree.blob = ParcSerializer::Serialize(tree.parsed, tree.blob);
    Reparse(tree, false);
    r.ok = true;
    return r;
}

// ── RemapTypeIndices ──

void RemapTypeIndices(std::vector<uint8_t>& tmpl,
                      const SaveTree& tree,
                      const std::vector<std::string>& structural_roles) {
    if (tmpl.size() < 10) return;

    uint16_t mbc;
    memcpy(&mbc, tmpl.data(), 2);

    uint16_t main_ti;
    memcpy(&main_ti, tmpl.data() + 2 + mbc, 2);

    std::vector<uint16_t> unique_tis;
    unique_tis.push_back(main_ti);

    for (size_t p = 23; p + 8 <= tmpl.size(); ++p) {
        if (memcmp(tmpl.data() + p, SENTINEL_BYTES, 8) == 0 && p >= 3) {
            uint16_t ti;
            memcpy(&ti, tmpl.data() + p - 3, 2);
            bool found = false;
            for (auto t : unique_tis) if (t == ti) { found = true; break; }
            if (!found) unique_tis.push_back(ti);
        }
    }

    std::unordered_map<uint16_t, std::string> src_to_role;
    for (size_t i = 0; i < unique_tis.size() && i < structural_roles.size(); ++i) {
        src_to_role[unique_tis[i]] = structural_roles[i];
    }

    auto it = src_to_role.find(main_ti);
    if (it != src_to_role.end()) {
        auto tgt = tree.name_to_type_idx.find(it->second);
        if (tgt != tree.name_to_type_idx.end()) {
            uint16_t new_ti = (uint16_t)tgt->second;
            memcpy(tmpl.data() + 2 + mbc, &new_ti, 2);
        }
    }

    for (size_t p = 23; p + 8 <= tmpl.size(); ++p) {
        if (memcmp(tmpl.data() + p, SENTINEL_BYTES, 8) == 0 && p >= 3) {
            uint16_t ti;
            memcpy(&ti, tmpl.data() + p - 3, 2);
            auto sit = src_to_role.find(ti);
            if (sit != src_to_role.end()) {
                auto tgt = tree.name_to_type_idx.find(sit->second);
                if (tgt != tree.name_to_type_idx.end()) {
                    uint16_t new_ti = (uint16_t)tgt->second;
                    memcpy(tmpl.data() + p - 3, &new_ti, 2);
                }
            }
        }
    }
}

// ── AdaptTypeIndicesFromReference ──
// Copies type indices from a reference element (last element in the target list)
// position-by-position. No role guessing. This is how the Python editor does it.

void AdaptTypeIndicesFromReference(std::vector<uint8_t>& tmpl,
                                    const std::vector<uint8_t>& blob,
                                    const SaveTree& tree,
                                    const std::string& block_class,
                                    const std::string& list_field_name) {
    if (tmpl.size() < 18) return;

    uint16_t tmpl_mbc;
    memcpy(&tmpl_mbc, tmpl.data(), 2);

    // Find the reference element (last element in the target list)
    const GenericFieldValue* ref_field = nullptr;
    for (auto& obj : tree.parsed.objects) {
        if (obj.class_name.find(block_class) == std::string::npos) continue;
        for (auto& f : obj.fields) {
            if (f.name == list_field_name && !f.list_elements.empty()) {
                ref_field = &f;
            }
        }
        break;
    }

    if (!ref_field || ref_field->list_elements.empty()) return;

    auto& ref_elem = ref_field->list_elements.back();
    if (ref_elem.raw_value.empty() && (ref_elem.start_offset == 0 || ref_elem.end_offset == 0)) return;

    // Get reference bytes
    const uint8_t* ref_data;
    size_t ref_size;
    if (!ref_elem.raw_value.empty()) {
        ref_data = ref_elem.raw_value.data();
        ref_size = ref_elem.raw_value.size();
    } else {
        if (ref_elem.end_offset > blob.size()) return;
        ref_data = blob.data() + ref_elem.start_offset;
        ref_size = ref_elem.end_offset - ref_elem.start_offset;
    }

    if (ref_size < 18) return;

    uint16_t ref_mbc;
    memcpy(&ref_mbc, ref_data, 2);

    // Copy main type index from reference
    uint16_t ref_main_ti;
    memcpy(&ref_main_ti, ref_data + 2 + ref_mbc, 2);
    memcpy(tmpl.data() + 2 + tmpl_mbc, &ref_main_ti, 2);

    // Collect nested type indices from reference (at each sentinel position)
    std::vector<uint16_t> ref_nested;
    for (size_t p = 23; p + 8 <= ref_size; ++p) {
        if (memcmp(ref_data + p, SENTINEL_BYTES, 8) == 0 && p >= 3) {
            uint16_t ti;
            memcpy(&ti, ref_data + p - 3, 2);
            ref_nested.push_back(ti);
        }
    }

    // Collect sentinel positions in template
    std::vector<size_t> tmpl_sentinel_pos;
    for (size_t p = 23; p + 8 <= tmpl.size(); ++p) {
        if (memcmp(tmpl.data() + p, SENTINEL_BYTES, 8) == 0 && p >= 3) {
            tmpl_sentinel_pos.push_back(p - 3); // position of type_index
        }
    }

    // Copy position-by-position
    for (size_t i = 0; i < tmpl_sentinel_pos.size(); ++i) {
        uint16_t new_ti;
        if (i < ref_nested.size()) {
            new_ti = ref_nested[i];
        } else if (!ref_nested.empty()) {
            new_ti = ref_nested.back(); // fallback to last known
        } else {
            continue;
        }
        memcpy(tmpl.data() + tmpl_sentinel_pos[i], &new_ti, 2);
    }
}

// ── FixTemplatePOs ──

void FixTemplatePOs(std::vector<uint8_t>& tmpl, uint32_t insert_pos) {
    for (size_t p = 0; p + 12 <= tmpl.size(); ++p) {
        if (memcmp(tmpl.data() + p, SENTINEL_BYTES, 8) == 0) {
            uint32_t po_pos = (uint32_t)(p + 8);
            uint32_t new_po = insert_pos + po_pos + 4;
            memcpy(tmpl.data() + po_pos, &new_po, 4);
        }
    }
}

// ── SpliceIntoList ──
// Direct byte-splice insertion. Same approach as Python parc_inserter3.
// 1. Fix POs inside the template (absolute, pointing to insert_pos + offset)
// 2. Splice template bytes into the blob at insert_pos
// 3. Fix all POs after insert_pos (add growth)
// 4. Fix trailing sizes in parent objects (add growth)
// 5. Patch list count in list header
// 6. Patch TOC (data_size for the modified block, data_offset for all after)
// No tree reconstruction. No serializer. Just byte surgery.

InsertResult SpliceIntoList(SaveTree& tree,
                            const std::string& block_class,
                            const std::string& list_field,
                            std::vector<uint8_t>& element_bytes,
                            uint32_t source_elem_offset,
                            int element_count) {
    InsertResult r;

    try {

    auto& blob = tree.blob;
    auto& parsed = tree.parsed;

    // Find the list field and insertion point
    uint32_t insert_pos = 0;
    uint32_t list_start = 0;
    uint32_t orig_count = 0;
    uint32_t block_toc_idx = 0;
    uint8_t list_prefix = 0;

    uint32_t list_body_offset = 0; // offset from start_offset to prefix byte
    uint32_t list_count_offset = 0;
    uint8_t list_count_format = 0;
    std::vector<uint8_t> list_header_raw;

    // Search for the list field — supports both top-level and nested lists
    // Uses iterative depth-first search through all fields
    {
        struct SearchItem { const GenericFieldValue* field; };
        std::vector<SearchItem> searchStack;
        searchStack.reserve(256);

        for (auto& obj : parsed.objects) {
            if (obj.class_name.find(block_class) == std::string::npos) continue;
            block_toc_idx = obj.entry_index;

            // Seed with all top-level fields
            for (size_t fi = 0; fi < obj.fields.size(); fi++)
                searchStack.push_back({&obj.fields[fi]});

            while (!searchStack.empty() && insert_pos == 0) {
                const GenericFieldValue* f = searchStack.back().field;
                searchStack.pop_back();

                // Check if this is the target list
                // Must have elements. If source_elem_offset is set, the list must contain that offset.
                if (f->name == list_field && (f->meta_kind == 6 || f->meta_kind == 7) &&
                    !f->list_elements.empty()) {
                    // If we have a source element offset, verify this list contains it
                    if (source_elem_offset > 0) {
                        bool contains = false;
                        for (auto& el : f->list_elements) {
                            if (el.start_offset == source_elem_offset) { contains = true; break; }
                        }
                        if (!contains) {
                            // Push children and keep searching
                            for (size_t ci2 = 0; ci2 < f->child_fields.size(); ci2++)
                                searchStack.push_back({&f->child_fields[ci2]});
                            for (size_t ei2 = 0; ei2 < f->list_elements.size(); ei2++)
                                for (size_t ci2 = 0; ci2 < f->list_elements[ei2].child_fields.size(); ci2++)
                                    searchStack.push_back({&f->list_elements[ei2].child_fields[ci2]});
                            continue;
                        }
                    }
                    insert_pos = f->list_elements.back().end_offset;
                    list_start = f->start_offset;
                    orig_count = (uint32_t)f->list_elements.size();
                    list_prefix = f->list_prefix_u8;
                    list_count_offset = f->list_count_offset;
                    list_count_format = f->list_count_format;
                    list_header_raw = f->list_header_raw;
                    if (!f->note.empty()) {
                        auto hpos = f->note.find("header_offset=+");
                        if (hpos != std::string::npos)
                            list_body_offset = atoi(f->note.c_str() + hpos + 15);
                    }
                    break;
                }

                // Push children for deeper search
                for (size_t ci = 0; ci < f->child_fields.size(); ci++)
                    searchStack.push_back({&f->child_fields[ci]});
                for (size_t ei = 0; ei < f->list_elements.size(); ei++) {
                    for (size_t ci = 0; ci < f->list_elements[ei].child_fields.size(); ci++)
                        searchStack.push_back({&f->list_elements[ei].child_fields[ci]});
                }
            }
            break;
        }
    }

    if (insert_pos == 0) {
        r.error = "Cannot find " + block_class + "." + list_field;
        return r;
    }

    int32_t growth = (int32_t)element_bytes.size();
    fprintf(stderr, "[SPLICE] insert_pos=0x%X count=%u growth=%d blob=%zu\n",
        insert_pos, orig_count, growth, blob.size());
    fprintf(stderr, "[SPLICE] list_start=0x%X prefix_u8=%u body_offset=%u\n",
        list_start, list_prefix, list_body_offset);
    fprintf(stderr, "[SPLICE] blob bytes at list_start: %02X %02X %02X %02X %02X %02X\n",
        blob[list_start], blob[list_start+1], blob[list_start+2],
        blob[list_start+3], blob[list_start+4], blob[list_start+5]);
    fprintf(stderr, "[SPLICE] list_header_raw (%zu bytes):", list_header_raw.size());
    for (size_t hi = 0; hi < list_header_raw.size() && hi < 16; hi++)
        fprintf(stderr, " %02X", list_header_raw[hi]);
    fprintf(stderr, "\n");

    // 1. Fix POs inside the template (make absolute for insert position)
    for (size_t p = 0; p + 12 <= element_bytes.size(); ++p) {
        if (memcmp(element_bytes.data() + p, SENTINEL_BYTES, 8) == 0) {
            uint32_t po_pos = (uint32_t)(p + 8);
            uint32_t new_po = insert_pos + po_pos + 4;
            memcpy(element_bytes.data() + po_pos, &new_po, 4);
        }
    }

    fprintf(stderr, "[SPLICE] step 1 done (template POs fixed)\n");

    // 2. Re-parse blob for fresh tree, collect trailing sizes AND all POs (incl zero-sentinel).
    Reparse(tree, false);
    auto all_pos = CollectAllPOs(tree);
    fprintf(stderr, "[SPLICE] collected %zu POs (sentinel + tree scan)\n", all_pos.size());
    auto& parsed2 = tree.parsed;
    //    Same logic as Python _fixup_trailing_sizes: find every inline object/list
    //    element whose range spans across insert_pos. Its trailing_size is at end-4.
    std::vector<uint32_t> ts_positions;
    {
        struct WalkItem { const GenericFieldValue* field; };
        std::vector<WalkItem> wstack;
        wstack.reserve(4096);

        // Find the block that CONTAINS the insert/replace point by offset
        // (not by class name — substring matches cause false hits)
        for (auto& obj : parsed2.objects) {
            if (insert_pos >= obj.data_offset && insert_pos < obj.data_offset + obj.data_size) {
                for (size_t fi = 0; fi < obj.fields.size(); fi++)
                    wstack.push_back({&obj.fields[fi]});
                break;
            }
        }

        while (!wstack.empty()) {
            const GenericFieldValue* fp = wstack.back().field;
            wstack.pop_back();

            uint32_t s = fp->start_offset;
            uint32_t e = fp->end_offset;
            bool is_inline = (fp->decode_kind == "list_element" ||
                              fp->decode_kind.find("locator") != std::string::npos);

            if (is_inline && s > 0 && e > s + 4) {
                uint32_t ts_pos = e - 4;
                if (s < insert_pos && insert_pos <= ts_pos && ts_pos + 4 <= blob.size()) {
                    uint32_t ts_val = 0;
                    memcpy(&ts_val, blob.data() + ts_pos, 4);
                    if (ts_val > 0 && ts_val < (e - s)) {
                        ts_positions.push_back(ts_pos);
                    }
                }
            }

            for (size_t ci = 0; ci < fp->child_fields.size(); ci++)
                wstack.push_back({&fp->child_fields[ci]});
            for (size_t ei = 0; ei < fp->list_elements.size(); ei++)
                wstack.push_back({&fp->list_elements[ei]});
        }
    }

    fprintf(stderr, "[SPLICE] step 2: %zu trailing sizes found (insert=0x%X, block=%s)\n",
        ts_positions.size(), insert_pos, block_class.c_str());
    // Also log to file via LogMsg (declared in inventory_editor but we're in parc_engine)
    // Use stderr which the deferred handler can capture

    // 3. Splice into blob
    blob.insert(blob.begin() + insert_pos, element_bytes.begin(), element_bytes.end());
    fprintf(stderr, "[SPLICE] step 3 done (blob spliced, new size=%zu)\n", blob.size());

    // 4. Fix ALL POs after insert_pos (using pre-collected list that includes zero-sentinel POs)
    {
        for (auto& po : all_pos) {
            if (po.pos < insert_pos) continue;
            uint32_t new_pos = po.pos + growth;
            if (new_pos + 4 > blob.size()) continue;
            uint32_t current = 0;
            memcpy(&current, blob.data() + new_pos, 4);
            uint32_t correct = new_pos + 4;
            if (current == correct) continue;
            if ((int32_t)current + growth == (int32_t)correct) {
                memcpy(blob.data() + new_pos, &correct, 4);
                r.po_fixed++;
            }
        }
    }
    fprintf(stderr, "[SPLICE] step 4 done (%d POs fixed from %zu candidates)\n", r.po_fixed, all_pos.size());

    // 5. Apply trailing size fixes (positions collected before splice)
    for (uint32_t orig_ts_pos : ts_positions) {
        uint32_t new_ts_pos = (orig_ts_pos >= insert_pos)
            ? orig_ts_pos + growth : orig_ts_pos;
        if (new_ts_pos + 4 > blob.size()) continue;
        uint32_t old_val = 0;
        memcpy(&old_val, blob.data() + new_ts_pos, 4);
        uint32_t new_val = old_val + growth;
        memcpy(blob.data() + new_ts_pos, &new_val, 4);
    }

    fprintf(stderr, "[SPLICE] trailing sizes applied\n");

    // 5. Patch list count
    //    Preferred: exact count position+encoding recorded by the parser.
    //    Fallback: scan the header region for a value matching orig_count.
    {
        bool patched = false;
        if (list_count_format != 0) {
            uint32_t off = list_start + list_count_offset;
            if (off + 4 <= blob.size()) {
                uint32_t cur = 0;
                switch (list_count_format) {
                case 1: // u32 LE
                    memcpy(&cur, blob.data() + off, 4);
                    cur += element_count;
                    memcpy(blob.data() + off, &cur, 4);
                    patched = true;
                    break;
                case 2: // u24 LE
                    cur = blob[off] | (blob[off+1] << 8) | (blob[off+2] << 16);
                    cur += element_count;
                    blob[off]   = cur & 0xFF;
                    blob[off+1] = (cur >> 8) & 0xFF;
                    blob[off+2] = (cur >> 16) & 0xFF;
                    patched = true;
                    break;
                case 3: // u16 BE
                    cur = ((uint32_t)blob[off] << 8) | blob[off+1];
                    cur += element_count;
                    blob[off]   = (cur >> 8) & 0xFF;
                    blob[off+1] = cur & 0xFF;
                    patched = true;
                    break;
                }
                if (patched) {
                    fprintf(stderr, "[SPLICE] list count at 0x%X (fmt=%u): -> %u (+%d)\n",
                        off, list_count_format, cur, element_count);
                }
            }
        }
        // Scan list_start through list_start+8 for the count value
        for (uint32_t off = list_start; !patched && off < list_start + 8 && off + 4 <= blob.size(); ++off) {
            // Try LE u24 (prefix=0 format): 3 bytes at off
            uint32_t le24 = blob[off] | (blob[off+1] << 8) | (blob[off+2] << 16);
            if (le24 == orig_count && orig_count > 0) {
                uint32_t nc = le24 + element_count;
                blob[off]   = nc & 0xFF;
                blob[off+1] = (nc >> 8) & 0xFF;
                blob[off+2] = (nc >> 16) & 0xFF;
                fprintf(stderr, "[SPLICE] list count at 0x%X (LE u24): %u -> %u (+%d)\n", off, le24, nc, element_count);
                patched = true;
                break;
            }
            // Try BE u16 (prefix=1 format)
            uint16_t be16 = (blob[off] << 8) | blob[off+1];
            if (be16 == orig_count && orig_count > 0 && orig_count <= 0xFFFF) {
                uint16_t nc = (uint16_t)(be16 + element_count);
                blob[off]   = (nc >> 8) & 0xFF;
                blob[off+1] = nc & 0xFF;
                fprintf(stderr, "[SPLICE] list count at 0x%X (BE u16): %u -> %u (+%d)\n", off, be16, nc, element_count);
                patched = true;
                break;
            }
        }
        if (!patched) {
            fprintf(stderr, "[SPLICE] WARNING: could not find count=%u in header region!\n", orig_count);
        }
    }

    // 6. Patch TOC entries
    //    - Modified block: data_size += growth
    //    - All blocks after: data_offset += growth
    {
        uint32_t toc_start = parsed.schema.schema_end + 12;
        for (size_t i = 0; i < parsed.toc.entries.size(); ++i) {
            uint32_t entry_pos = toc_start + (uint32_t)(i * 20);
            uint32_t data_off = 0, data_size = 0;
            memcpy(&data_off, blob.data() + entry_pos + 12, 4);
            memcpy(&data_size, blob.data() + entry_pos + 16, 4);

            if (i == block_toc_idx) {
                data_size += growth;
                memcpy(blob.data() + entry_pos + 16, &data_size, 4);
            } else if (data_off > insert_pos) {
                data_off += growth;
                memcpy(blob.data() + entry_pos + 12, &data_off, 4);
            }
        }

        // Patch stream_size
        uint32_t stream_size = (uint32_t)blob.size();
        memcpy(blob.data() + parsed.schema.schema_end + 8, &stream_size, 4);
    }

    r.ok = true;
    r.insert_offset = insert_pos;
    r.growth = growth;
    r.new_element_index = (int)orig_count;

    // Reparse
    Reparse(tree, false);

    } catch (const std::exception& e) {
        r.error = std::string("SpliceIntoList exception: ") + e.what();
    } catch (...) {
        r.error = "SpliceIntoList unknown exception";
    }

    return r;
}

// ── ReplaceElement ──
// Delete old bytes, insert new bytes at same position, fix everything.

InsertResult ReplaceElement(SaveTree& tree,
                            const std::string& block_class,
                            uint32_t old_start, uint32_t old_end,
                            std::vector<uint8_t>& new_bytes) {
    InsertResult r;
    auto& blob = tree.blob;
    auto& parsed = tree.parsed;

    if (old_start >= old_end || old_end > blob.size()) {
        r.error = "Invalid element range";
        return r;
    }

    uint32_t old_size = old_end - old_start;
    uint32_t new_size = (uint32_t)new_bytes.size();
    int32_t delta = (int32_t)new_size - (int32_t)old_size;

    fprintf(stderr, "[REPLACE] old=[0x%X..0x%X] (%u bytes) new=%u bytes delta=%d\n",
        old_start, old_end, old_size, new_size, delta);

    // Find TOC index for the block containing this element (by offset, not name)
    uint32_t block_toc_idx = 0;
    for (auto& obj : parsed.objects) {
        if (old_start >= obj.data_offset && old_start < obj.data_offset + obj.data_size) {
            block_toc_idx = obj.entry_index;
            break;
        }
    }

    // 1. Re-parse for fresh tree, then collect trailing size positions
    Reparse(tree, false);
    std::vector<uint32_t> ts_positions;
    if (delta != 0) {
        struct WalkItem { const GenericFieldValue* field; };
        std::vector<WalkItem> wstack;
        wstack.reserve(4096);

        // Find the block that CONTAINS the replace point by offset
        for (auto& obj : parsed.objects) {
            if (old_start >= obj.data_offset && old_start < obj.data_offset + obj.data_size) {
                for (size_t fi = 0; fi < obj.fields.size(); fi++)
                    wstack.push_back({&obj.fields[fi]});
                break;
            }
        }

        while (!wstack.empty()) {
            const GenericFieldValue* fp = wstack.back().field;
            wstack.pop_back();

            uint32_t s = fp->start_offset;
            uint32_t e = fp->end_offset;
            bool is_inline = (fp->decode_kind == "list_element" ||
                              fp->decode_kind.find("locator") != std::string::npos);

            if (is_inline && s > 0 && e > s + 4) {
                uint32_t ts_pos = e - 4;
                if (s < old_start && old_end <= ts_pos && ts_pos + 4 <= blob.size()) {
                    uint32_t ts_val = 0;
                    memcpy(&ts_val, blob.data() + ts_pos, 4);
                    if (ts_val > 0 && ts_val < (e - s)) {
                        ts_positions.push_back(ts_pos);
                        fprintf(stderr, "[REPLACE] trailing_size at 0x%X val=%u span=[0x%X..0x%X] dk=%s\n",
                            ts_pos, ts_val, s, e, fp->decode_kind.c_str());
                    }
                }
            }

            for (size_t ci = 0; ci < fp->child_fields.size(); ci++)
                wstack.push_back({&fp->child_fields[ci]});
            for (size_t ei = 0; ei < fp->list_elements.size(); ei++)
                wstack.push_back({&fp->list_elements[ei]});
        }
        fprintf(stderr, "[REPLACE] found %zu trailing sizes to fix\n", ts_positions.size());
    }

    // 2. Collect all POs BEFORE splice (includes zero-sentinel POs)
    auto all_pos = CollectAllPOs(tree);

    // 3. Fix POs inside new_bytes (make self-referential for target position)
    for (size_t p = 0; p + 12 <= new_bytes.size(); ++p) {
        if (memcmp(new_bytes.data() + p, SENTINEL_BYTES, 8) == 0) {
            uint32_t po_pos = (uint32_t)(p + 8);
            uint32_t new_po = old_start + po_pos + 4;
            memcpy(new_bytes.data() + po_pos, &new_po, 4);
        }
    }

    // 4. Delete old bytes, insert new bytes
    blob.erase(blob.begin() + old_start, blob.begin() + old_end);
    blob.insert(blob.begin() + old_start, new_bytes.begin(), new_bytes.end());

    // 5. Fix POs after the replacement (using pre-collected list incl zero-sentinel)
    if (delta != 0) {
        for (auto& po : all_pos) {
            if (po.pos >= old_start && po.pos < old_end) continue;
            uint32_t new_pos = (po.pos >= old_end) ? (uint32_t)((int32_t)po.pos + delta) : po.pos;
            if (new_pos + 4 > blob.size()) continue;
            uint32_t current = 0;
            memcpy(&current, blob.data() + new_pos, 4);
            uint32_t correct = new_pos + 4;
            if (current == correct) continue;
            if ((int32_t)current + delta == (int32_t)correct) {
                memcpy(blob.data() + new_pos, &correct, 4);
                r.po_fixed++;
            }
        }

        // 5. Fix trailing sizes
        for (uint32_t orig_ts_pos : ts_positions) {
            uint32_t new_ts_pos = (orig_ts_pos >= old_end)
                ? (uint32_t)((int32_t)orig_ts_pos + delta) : orig_ts_pos;
            if (new_ts_pos + 4 > blob.size()) continue;
            uint32_t old_val = 0;
            memcpy(&old_val, blob.data() + new_ts_pos, 4);
            uint32_t new_val = (uint32_t)((int32_t)old_val + delta);
            memcpy(blob.data() + new_ts_pos, &new_val, 4);
        }

        // 6. Fix TOC
        uint32_t toc_start = parsed.schema.schema_end + 12;
        for (size_t i = 0; i < parsed.toc.entries.size(); ++i) {
            uint32_t entry_pos = toc_start + (uint32_t)(i * 20);
            uint32_t data_off = 0, data_size = 0;
            memcpy(&data_off, blob.data() + entry_pos + 12, 4);
            memcpy(&data_size, blob.data() + entry_pos + 16, 4);

            if (i == block_toc_idx) {
                data_size = (uint32_t)((int32_t)data_size + delta);
                memcpy(blob.data() + entry_pos + 16, &data_size, 4);
            } else if (data_off > old_start) {
                data_off = (uint32_t)((int32_t)data_off + delta);
                memcpy(blob.data() + entry_pos + 12, &data_off, 4);
            }
        }

        // Fix stream_size
        uint32_t stream_size = (uint32_t)blob.size();
        memcpy(blob.data() + parsed.schema.schema_end + 8, &stream_size, 4);
    }

    r.ok = true;
    r.insert_offset = old_start;
    r.growth = delta;

    // Reparse
    Reparse(tree, false);

    fprintf(stderr, "[REPLACE] done: delta=%d, POs fixed=%d\n", delta, r.po_fixed);
    return r;
}

// ── WriteSave ──

void WriteSave(const SaveTree& tree, const std::string& path) {
    if (tree.is_encrypted) {
        SaveWriter::WriteSaveFile(path, tree.blob, tree.original_header);
    } else {
        SaveWriter::WriteRawFile(path, tree.blob);
    }
}

// ── Read/Write helpers ──

uint32_t ReadU32(const SaveTree& tree, uint32_t offset) {
    uint32_t v = 0;
    if (offset + 4 <= tree.blob.size()) memcpy(&v, tree.blob.data() + offset, 4);
    return v;
}

void WriteU32(SaveTree& tree, uint32_t offset, uint32_t value) {
    if (offset + 4 <= tree.blob.size()) memcpy(tree.blob.data() + offset, &value, 4);
}

} // namespace ParcEngine
