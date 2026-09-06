/**
 * dye_cli.exe — Fast dye editing CLI for PARC save blobs.
 *
 * Uses the full C++ PARC tree parser (save_parser_cpp) for correct offsets,
 * then rebuilds dye entries with proper masks and PO fixup.
 *
 * Usage:
 *   dye_cli.exe <blob_path> <item_key> <command> [args...]
 *
 * Commands:
 *   read    — Print dye entries as JSON
 *   edit    — Edit dye values: <entry_idx> <field> <value> [<entry_idx> <field> <value> ...]
 *             Fields: r, g, b, a, slot, grime, group, material
 *   rebuild — Rebuild all dye entries with correct masks (full fixup)
 *
 * Input:  raw PARC blob (decompressed save data)
 * Output: modified blob written back to same file (for edit/rebuild)
 *         JSON to stdout (for read)
 *
 * Build: cl /std:c++17 /EHsc /O2 /Fe:dye_cli.exe /I"." dye_cli.cpp save_parser_cpp.cpp /link bcrypt.lib
 */

#include "save_parser_cpp.h"
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <vector>
#include <string>
#include <algorithm>

using namespace SaveParserCpp;

static const uint8_t SENTINEL[8] = {0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF};

// ── Dye entry representation ──

struct DyeEntry {
    int8_t  slot = 0;
    uint8_t r = 0, g = 0, b = 0, a = 255;
    int8_t  grime = 0;
    uint32_t group = 0;
    uint16_t material = 0;
    uint8_t orig_mask = 0;
    uint32_t elem_offset = 0;  // offset of element in blob
};

// ── Mask computation ──

static int mask_field_size(uint8_t mask) {
    int sz = 0;
    if (mask & 0x01) sz += 1;
    if (mask & 0x02) sz += 1;
    if (mask & 0x04) sz += 1;
    if (mask & 0x08) sz += 1;
    if (mask & 0x10) sz += 1;
    if (mask & 0x20) sz += 1;
    if (mask & 0x40) sz += 4;
    if (mask & 0x80) sz += 2;
    return sz;
}

static int element_total_size(uint8_t mask) {
    return 18 + 4 + mask_field_size(mask) + 4;
}

static uint8_t compute_mask(const DyeEntry& e) {
    // Start from original mask (preserve what game wrote), only ADD bits
    uint8_t mask = e.orig_mask;
    if (mask == 0) {
        // New insertion — build from scratch
        if (e.slot != 0) mask |= 0x01;
        mask |= 0x10; // A always present
    }
    // Add bits for non-zero values
    if (e.r > 0)         mask |= 0x02;
    if (e.g > 0)         mask |= 0x04;
    if (e.b > 0)         mask |= 0x08;
    if (e.grime != 0)    mask |= 0x20;
    if (e.group != 0)    mask |= 0x40;
    if (e.material != 0) mask |= 0x80;
    return mask;
}

// ── Build one dye element ──

static std::vector<uint8_t> build_element(const DyeEntry& e, uint8_t mask, uint16_t type_index) {
    std::vector<uint8_t> elem;
    elem.reserve(40);

    // header
    elem.push_back(0x01); elem.push_back(0x00); // mbc=1
    elem.push_back(mask);
    elem.push_back((uint8_t)(type_index & 0xFF));
    elem.push_back((uint8_t)(type_index >> 8));
    elem.push_back(0x00); // reserved u8
    elem.insert(elem.end(), SENTINEL, SENTINEL + 8);

    // PO placeholder (4B) — patched after
    size_t po_pos = elem.size();
    elem.push_back(0); elem.push_back(0); elem.push_back(0); elem.push_back(0);

    // reserved u32
    elem.push_back(0); elem.push_back(0); elem.push_back(0); elem.push_back(0);

    size_t payload_start = elem.size();

    // fields
    if (mask & 0x01) elem.push_back((uint8_t)e.slot);
    if (mask & 0x02) elem.push_back(e.r);
    if (mask & 0x04) elem.push_back(e.g);
    if (mask & 0x08) elem.push_back(e.b);
    if (mask & 0x10) elem.push_back(e.a);
    if (mask & 0x20) elem.push_back((uint8_t)e.grime);
    if (mask & 0x40) {
        elem.push_back((uint8_t)(e.group & 0xFF));
        elem.push_back((uint8_t)((e.group >> 8) & 0xFF));
        elem.push_back((uint8_t)((e.group >> 16) & 0xFF));
        elem.push_back((uint8_t)((e.group >> 24) & 0xFF));
    }
    if (mask & 0x80) {
        elem.push_back((uint8_t)(e.material & 0xFF));
        elem.push_back((uint8_t)((e.material >> 8) & 0xFF));
    }

    // trailing_size = reserved_u32(4) + field_data_size
    uint32_t trailing = (uint32_t)(elem.size() - payload_start + 4);
    elem.push_back((uint8_t)(trailing & 0xFF));
    elem.push_back((uint8_t)((trailing >> 8) & 0xFF));
    elem.push_back((uint8_t)((trailing >> 16) & 0xFF));
    elem.push_back((uint8_t)((trailing >> 24) & 0xFF));

    return elem;
}

// ── Find dye data in parsed result ──

struct DyeListInfo {
    const GenericFieldValue* dye_field = nullptr;
    uint32_t equip_toc_idx = 0;
    std::vector<DyeEntry> entries;
    uint16_t type_index = 0;
};

static bool find_dye_data(const ParseResult& result, const std::vector<uint8_t>& blob,
                          uint32_t item_key, DyeListInfo& info) {
    for (auto& obj : result.objects) {
        if (obj.class_name != "EquipmentSaveData") continue;
        for (auto& f : obj.fields) {
            if (f.name != "_list" || f.list_elements.empty()) continue;
            for (auto& elem : f.list_elements) {
                if (elem.child_fields.empty()) continue;
                for (auto& cf : elem.child_fields) {
                    if (cf.name != "_item" || cf.child_fields.empty()) continue;
                    bool found_key = false;
                    const GenericFieldValue* dye_fld = nullptr;
                    for (auto& icf : cf.child_fields) {
                        if (icf.name == "_itemKey" && icf.present) {
                            uint32_t k = 0;
                            memcpy(&k, blob.data() + icf.start_offset, 4);
                            if (k == item_key) found_key = true;
                        }
                        if (icf.name == "_itemDyeDataList" && icf.present &&
                            !icf.list_elements.empty()) {
                            dye_fld = &icf;
                        }
                    }
                    if (found_key && dye_fld) {
                        info.dye_field = dye_fld;
                        // Read entries
                        info.type_index = dye_fld->list_elements[0].child_type_index;
                        for (auto& de : dye_fld->list_elements) {
                            DyeEntry entry;
                            entry.orig_mask = 0;
                            entry.elem_offset = de.start_offset;
                            // Read mask from blob
                            if (de.start_offset + 3 <= blob.size()) {
                                entry.orig_mask = blob[de.start_offset + 2];
                            }
                            for (auto& dcf : de.child_fields) {
                                if (!dcf.present) continue;
                                if (dcf.name == "_dyeSlotNo")
                                    entry.slot = (int8_t)blob[dcf.start_offset];
                                else if (dcf.name == "_dyeColorR")
                                    entry.r = blob[dcf.start_offset];
                                else if (dcf.name == "_dyeColorG")
                                    entry.g = blob[dcf.start_offset];
                                else if (dcf.name == "_dyeColorB")
                                    entry.b = blob[dcf.start_offset];
                                else if (dcf.name == "_dyeColorA")
                                    entry.a = blob[dcf.start_offset];
                                else if (dcf.name == "_grimeOpacity")
                                    entry.grime = (int8_t)blob[dcf.start_offset];
                                else if (dcf.name == "_dyeColorGroupInfoKey")
                                    memcpy(&entry.group, blob.data() + dcf.start_offset, 4);
                                else if (dcf.name == "_texturePalleteKey") {
                                    uint32_t sz = dcf.end_offset - dcf.start_offset;
                                    if (sz == 2)
                                        memcpy(&entry.material, blob.data() + dcf.start_offset, 2);
                                    else {
                                        uint32_t m32 = 0;
                                        memcpy(&m32, blob.data() + dcf.start_offset, 4);
                                        entry.material = (uint16_t)m32;
                                    }
                                }
                            }
                            info.entries.push_back(entry);
                        }
                        // Find TOC index
                        for (auto& te : result.toc.entries) {
                            if (te.class_name == "EquipmentSaveData") {
                                info.equip_toc_idx = te.index;
                                break;
                            }
                        }
                        return true;
                    }
                }
            }
        }
    }
    return false;
}

// ── Collect all child_payload_offset positions from tree ──
// Ported exactly from Python parc_inserter2._compute_payload_offset_pos + _collect_from_fields

struct OffsetPos {
    uint32_t pos;     // position of the PO u32 in blob
    uint32_t value;   // current value
};

static inline uint32_t read_u32(const std::vector<uint8_t>& blob, uint32_t off) {
    uint32_t v = 0;
    if (off + 4 <= blob.size()) memcpy(&v, blob.data() + off, 4);
    return v;
}

static int compute_po_pos(const GenericFieldValue& f, const std::vector<uint8_t>& blob) {
    if (f.child_payload_offset == 0 || f.start_offset == 0) return -1;
    if (f.child_mask_byte_count == 0) return -1;

    uint32_t expected = f.child_payload_offset;

    // List elements: locator layout with variable MBC, PO at start + 2 + mbc + 11
    // (MBC=1 gives the old compact +14)
    if (f.note == "compact_list_element" || f.decode_kind == "list_element") {
        uint32_t pos = f.start_offset + 2 + f.child_mask_byte_count + 11;
        if (pos + 4 <= blob.size() && read_u32(blob, pos) == expected) return (int)pos;
        return -1;
    }

    // meta_kind 4: inline object, body = start_offset
    if (f.meta_kind == 4) {
        uint32_t pos = f.start_offset + 2 + f.child_mask_byte_count + 11;
        if (pos + 4 <= blob.size() && read_u32(blob, pos) == expected) return (int)pos;
        return -1;
    }

    // meta_kind 5: object pointer, try prefix 0, 1, 3
    if (f.meta_kind == 5) {
        for (uint32_t pd : {0u, 1u, 3u}) {
            uint32_t body = f.start_offset + pd;
            uint32_t pos = body + 2 + f.child_mask_byte_count + 11;
            if (pos + 4 <= blob.size() && read_u32(blob, pos) == expected) return (int)pos;
        }
        return -1;
    }

    // meta_kind 6 or 7: list element
    if (f.meta_kind == 6 || f.meta_kind == 7) {
        uint32_t pos = f.start_offset + 2 + f.child_mask_byte_count + 11;
        if (pos + 4 <= blob.size() && read_u32(blob, pos) == expected) return (int)pos;
        for (uint32_t pd : {1u, 3u}) {
            uint32_t body = f.start_offset + pd;
            pos = body + 2 + f.child_mask_byte_count + 11;
            if (pos + 4 <= blob.size() && read_u32(blob, pos) == expected) return (int)pos;
        }
        return -1;
    }

    return -1;
}

static void collect_offsets_recursive(const GenericFieldValue& fv,
                                      const std::vector<uint8_t>& blob,
                                      std::vector<OffsetPos>& offsets,
                                      std::vector<std::pair<uint32_t,uint32_t>>& trailing_sizes) {
    // Child payload offset
    if (fv.child_payload_offset > 0 && fv.start_offset > 0) {
        int pos = compute_po_pos(fv, blob);
        if (pos >= 0) {
            offsets.push_back({(uint32_t)pos, fv.child_payload_offset});
        }
    }

    // Trailing size: size_pos = payload_start + child_size_u32
    if (fv.child_size_u32 > 0 && fv.child_payload_offset > 0) {
        uint32_t size_pos = fv.child_payload_offset + fv.child_size_u32;
        trailing_sizes.push_back({size_pos, fv.child_payload_offset});
    }

    // Recurse
    for (auto& cf : fv.child_fields) {
        collect_offsets_recursive(cf, blob, offsets, trailing_sizes);
    }
    for (auto& le : fv.list_elements) {
        collect_offsets_recursive(le, blob, offsets, trailing_sizes);
    }
}

static void collect_all_offsets(const ParseResult& result, const std::vector<uint8_t>& blob,
                                std::vector<OffsetPos>& offsets,
                                std::vector<std::pair<uint32_t,uint32_t>>& trailing_sizes) {
    for (auto& obj : result.objects) {
        for (auto& f : obj.fields) {
            collect_offsets_recursive(f, blob, offsets, trailing_sizes);
        }
    }
    // Verify and filter bad positions
    std::vector<OffsetPos> verified;
    for (auto& op : offsets) {
        if (op.pos + 4 <= blob.size() && read_u32(blob, op.pos) == op.value) {
            verified.push_back(op);
        }
    }
    offsets = std::move(verified);
}

// ── Rebuild command: full dye list rebuild with PO fixup ���─

static int do_rebuild(std::vector<uint8_t>& blob, const ParseResult& result,
                      uint32_t item_key, DyeListInfo& dye_info,
                      const std::vector<DyeEntry>& new_entries) {
    const auto* dye_fld = dye_info.dye_field;
    uint32_t old_start = dye_fld->start_offset;
    uint32_t old_end = dye_fld->end_offset;
    uint32_t old_size = old_end - old_start;

    // Build new list
    std::vector<uint8_t> new_list;
    // List header: prefix(1) + count(4) + reserved(13)
    new_list.push_back(0);
    uint32_t count = (uint32_t)new_entries.size();
    new_list.push_back((uint8_t)(count & 0xFF));
    new_list.push_back((uint8_t)((count >> 8) & 0xFF));
    new_list.push_back((uint8_t)((count >> 16) & 0xFF));
    new_list.push_back((uint8_t)((count >> 24) & 0xFF));
    new_list.resize(18, 0); // 13 bytes reserved

    // Build elements
    std::vector<size_t> elem_offsets_in_list;
    for (size_t i = 0; i < new_entries.size(); i++) {
        uint8_t mask = compute_mask(new_entries[i]);
        auto elem = build_element(new_entries[i], mask, dye_info.type_index);
        elem_offsets_in_list.push_back(new_list.size());
        new_list.insert(new_list.end(), elem.begin(), elem.end());
    }

    uint32_t new_size = (uint32_t)new_list.size();
    int32_t delta = (int32_t)new_size - (int32_t)old_size;

    // Splice blob
    std::vector<uint8_t> new_blob;
    new_blob.reserve(blob.size() + delta);
    new_blob.insert(new_blob.end(), blob.begin(), blob.begin() + old_start);
    new_blob.insert(new_blob.end(), new_list.begin(), new_list.end());
    new_blob.insert(new_blob.end(), blob.begin() + old_end, blob.end());

    // Patch POs within our new elements (absolute offsets)
    for (size_t i = 0; i < new_entries.size(); i++) {
        uint32_t abs_elem = old_start + (uint32_t)elem_offsets_in_list[i];
        uint32_t sentinel_pos = abs_elem + 6;
        uint32_t po_pos = sentinel_pos + 8;
        uint32_t po_val = sentinel_pos + 12;
        memcpy(new_blob.data() + po_pos, &po_val, 4);
    }

    if (delta != 0) {
        // Collect all PO positions from tree (using ORIGINAL blob for verification)
        std::vector<OffsetPos> offsets;
        std::vector<std::pair<uint32_t,uint32_t>> trailing_sizes;
        collect_all_offsets(result, blob, offsets, trailing_sizes);

        // Fix POs
        for (auto& op : offsets) {
            if (old_start <= op.pos && op.pos < old_end) continue;
            uint32_t new_pos = (op.pos >= old_end) ? op.pos + delta : op.pos;
            if (new_pos + 4 > new_blob.size()) continue;
            uint32_t cur_val;
            memcpy(&cur_val, new_blob.data() + new_pos, 4);
            if (cur_val >= old_end) {
                uint32_t fixed = cur_val + delta;
                memcpy(new_blob.data() + new_pos, &fixed, 4);
            }
        }

        // Fix trailing sizes
        for (auto& [size_pos, payload_start] : trailing_sizes) {
            if (old_start <= size_pos && size_pos < old_end) continue;
            if (payload_start < old_end && size_pos >= old_end) {
                uint32_t new_size_pos = size_pos + delta;
                if (new_size_pos + 4 > new_blob.size()) continue;
                uint32_t old_val;
                memcpy(&old_val, new_blob.data() + new_size_pos, 4);
                uint32_t fixed = old_val + delta;
                memcpy(new_blob.data() + new_size_pos, &fixed, 4);
            }
        }

        // Fix TOC
        uint32_t schema_end = result.schema.schema_end;
        uint32_t stream_size_pos = schema_end + 8;
        uint32_t old_stream_size;
        memcpy(&old_stream_size, new_blob.data() + stream_size_pos, 4);
        uint32_t new_stream_size = old_stream_size + delta;
        memcpy(new_blob.data() + stream_size_pos, &new_stream_size, 4);

        for (auto& te : result.toc.entries) {
            uint32_t doff_pos = te.entry_offset + 12;
            uint32_t dsize_pos = te.entry_offset + 16;
            if (te.index == dye_info.equip_toc_idx) {
                uint32_t old_sz;
                memcpy(&old_sz, new_blob.data() + dsize_pos, 4);
                uint32_t new_sz = old_sz + delta;
                memcpy(new_blob.data() + dsize_pos, &new_sz, 4);
            }
            if (te.data_offset >= old_end) {
                uint32_t fixed = te.data_offset + delta;
                memcpy(new_blob.data() + doff_pos, &fixed, 4);
            }
        }
    }

    blob = std::move(new_blob);
    return 0;
}

// ── In-place edit (no size change) ──

static int do_edit_inplace(std::vector<uint8_t>& blob, DyeListInfo& info,
                           const std::vector<DyeEntry>& new_entries) {
    for (size_t i = 0; i < info.entries.size() && i < new_entries.size(); i++) {
        uint32_t off = info.entries[i].elem_offset;
        uint8_t orig_mask = info.entries[i].orig_mask;

        // Fields start at off + 22 (header 18 + reserved_u32 4)
        uint32_t foff = off + 22;
        const DyeEntry& e = new_entries[i];

        if (orig_mask & 0x01) blob[foff++] = (uint8_t)e.slot;
        if (orig_mask & 0x02) blob[foff++] = e.r;
        if (orig_mask & 0x04) blob[foff++] = e.g;
        if (orig_mask & 0x08) blob[foff++] = e.b;
        if (orig_mask & 0x10) blob[foff++] = e.a;
        if (orig_mask & 0x20) blob[foff++] = (uint8_t)e.grime;
        if (orig_mask & 0x40) { memcpy(blob.data() + foff, &e.group, 4); foff += 4; }
        if (orig_mask & 0x80) { memcpy(blob.data() + foff, &e.material, 2); foff += 2; }
    }
    return 0;
}

// ── Read blob from file ──

static std::vector<uint8_t> read_file(const char* path) {
    std::ifstream f(path, std::ios::binary);
    if (!f) { fprintf(stderr, "Cannot open: %s\n", path); exit(1); }
    f.seekg(0, std::ios::end);
    size_t sz = f.tellg();
    f.seekg(0);
    std::vector<uint8_t> data(sz);
    f.read(reinterpret_cast<char*>(data.data()), sz);
    return data;
}

static void write_file(const char* path, const std::vector<uint8_t>& data) {
    std::ofstream f(path, std::ios::binary);
    if (!f) { fprintf(stderr, "Cannot write: %s\n", path); exit(1); }
    f.write(reinterpret_cast<const char*>(data.data()), data.size());
}

// ── Main ──

int main(int argc, char** argv) {
    if (argc < 4) {
        fprintf(stderr,
            "Usage: dye_cli <blob_path> <item_key> <command> [args...]\n"
            "Commands:\n"
            "  read                    — Print dye entries as JSON\n"
            "  edit <idx> <field> <val> [...]  — Edit field values in-place\n"
            "  rebuild                 — Full rebuild with mask recompute + PO fixup\n"
            "  set_rgb <idx> <r> <g> <b>      — Set RGB on entry (auto in-place or rebuild)\n"
        );
        return 1;
    }

    const char* blob_path = argv[1];
    uint32_t item_key = (uint32_t)strtoul(argv[2], nullptr, 0);
    const char* command = argv[3];

    // Read blob
    auto blob = read_file(blob_path);

    // Parse
    // Write to temp file for the parser
    char temp_path[512];
    snprintf(temp_path, sizeof(temp_path), "%s.tmp_parse", blob_path);
    write_file(temp_path, blob);

    auto result = ParseRawFile(temp_path);
    remove(temp_path);

    if (result.objects.empty()) {
        fprintf(stderr, "Parse failed: no objects\n");
        return 1;
    }

    // Find dye data
    DyeListInfo dye_info;
    if (!find_dye_data(result, blob, item_key, dye_info)) {
        fprintf(stderr, "Item %u has no dye data\n", item_key);
        return 1;
    }

    if (strcmp(command, "read") == 0) {
        printf("{\"item_key\":%u,\"count\":%d,\"entries\":[\n", item_key, (int)dye_info.entries.size());
        for (size_t i = 0; i < dye_info.entries.size(); i++) {
            auto& e = dye_info.entries[i];
            printf("  {\"slot\":%d,\"r\":%u,\"g\":%u,\"b\":%u,\"a\":%u,"
                   "\"grime\":%d,\"group\":%u,\"material\":%u,\"mask\":\"0x%02X\"}%s\n",
                   e.slot, e.r, e.g, e.b, e.a, e.grime, e.group, e.material,
                   e.orig_mask, (i + 1 < dye_info.entries.size()) ? "," : "");
        }
        printf("]}\n");
        return 0;
    }

    if (strcmp(command, "edit") == 0 || strcmp(command, "set_rgb") == 0) {
        auto entries = dye_info.entries; // copy

        if (strcmp(command, "set_rgb") == 0) {
            if (argc < 8) {
                fprintf(stderr, "Usage: set_rgb <idx> <r> <g> <b>\n");
                return 1;
            }
            int idx = atoi(argv[4]);
            if (idx < 0 || idx >= (int)entries.size()) {
                fprintf(stderr, "Entry index %d out of range (0-%d)\n", idx, (int)entries.size() - 1);
                return 1;
            }
            entries[idx].r = (uint8_t)atoi(argv[5]);
            entries[idx].g = (uint8_t)atoi(argv[6]);
            entries[idx].b = (uint8_t)atoi(argv[7]);
            entries[idx].a = 255;
        } else {
            // Parse field edits: <idx> <field> <val> ...
            for (int i = 4; i + 2 < argc; i += 3) {
                int idx = atoi(argv[i]);
                const char* field = argv[i + 1];
                int val = atoi(argv[i + 2]);
                if (idx < 0 || idx >= (int)entries.size()) continue;
                if (strcmp(field, "r") == 0) entries[idx].r = (uint8_t)val;
                else if (strcmp(field, "g") == 0) entries[idx].g = (uint8_t)val;
                else if (strcmp(field, "b") == 0) entries[idx].b = (uint8_t)val;
                else if (strcmp(field, "a") == 0) entries[idx].a = (uint8_t)val;
                else if (strcmp(field, "slot") == 0) entries[idx].slot = (int8_t)val;
                else if (strcmp(field, "grime") == 0) entries[idx].grime = (int8_t)val;
                else if (strcmp(field, "group") == 0) entries[idx].group = (uint32_t)val;
                else if (strcmp(field, "material") == 0) entries[idx].material = (uint16_t)val;
            }
        }

        // Check if masks would change
        bool mask_changed = false;
        for (size_t i = 0; i < entries.size(); i++) {
            if (compute_mask(entries[i]) != dye_info.entries[i].orig_mask) {
                mask_changed = true;
                break;
            }
        }

        if (!mask_changed) {
            // Fast in-place edit — guaranteed delta=0, no PO fixup needed
            do_edit_inplace(blob, dye_info, entries);
            fprintf(stderr, "In-place edit OK (no mask change)\n");
        } else {
            // Mask change needs full rebuild with PO fixup.
            // Return 2 so GUI uses Python's proven fixup (112K+ positions).
            fprintf(stderr, "Mask change — needs full rebuild\n");
            return 2;
        }

        write_file(blob_path, blob);
        return 0;
    }

    if (strcmp(command, "rebuild") == 0) {
        do_rebuild(blob, result, item_key, dye_info, dye_info.entries);
        write_file(blob_path, blob);
        fprintf(stderr, "Rebuild OK, wrote %zu bytes\n", blob.size());
        return 0;
    }

    fprintf(stderr, "Unknown command: %s\n", command);
    return 1;
}
