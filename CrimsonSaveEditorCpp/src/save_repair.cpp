#include "save_repair.h"
#include "editor_common.h"
#include "parc_serializer.h"
#include <fstream>
#include <cstdio>
#include <cstring>
#include <unordered_map>
#include <unordered_set>
#include <functional>
#include <utility>
#include <set>
#include <filesystem>

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <commdlg.h>
#endif

using namespace EditorCommon;

namespace SaveRepair {

// ── Transplant diagnostic logging ──

static void HexDump(const char* label, const std::vector<uint8_t>& data, uint32_t off, uint32_t len) {
    if (off >= data.size()) { EC::Log("  %s: offset 0x%X out of bounds (size=%zu)", label, off, data.size()); return; }
    uint32_t end = std::min<uint32_t>((uint32_t)data.size(), off + len);
    EC::Log("  %s @ 0x%X (%u bytes):", label, off, end - off);
    for (uint32_t row = off; row < end; row += 16) {
        char hex[80] = {};
        char asc[20] = {};
        int hpos = 0, apos = 0;
        for (uint32_t c = row; c < row + 16 && c < end; c++) {
            hpos += snprintf(hex + hpos, sizeof(hex) - hpos, "%02X ", data[c]);
            asc[apos++] = (data[c] >= 0x20 && data[c] < 0x7F) ? (char)data[c] : '.';
        }
        asc[apos] = 0;
        EC::Log("    %06X: %-48s %s", row, hex, asc);
    }
}

static void DumpToFile(const char* path, const std::vector<uint8_t>& data, uint32_t off, uint32_t len) {
    uint32_t end = std::min<uint32_t>((uint32_t)data.size(), off + len);
    FILE* f = fopen(path, "wb");
    if (f) { fwrite(data.data() + off, 1, end - off, f); fclose(f); }
}

static uint32_t ReadU32LE(const std::vector<uint8_t>& data, uint32_t off);

// Full save state dump — logs EVERY block, field, element count, raw TOC
void DumpFullSaveState(const char* label, const ParcEngine::SaveTree& tree) {
    EC::Log("╔══════════════════════════════════════════════════════════");
    EC::Log("║ FULL SAVE STATE: %s", label);
    EC::Log("║ blob=%zu  schema=%zu types  TOC=%zu entries  objects=%zu",
        tree.blob.size(), tree.parsed.schema.types.size(),
        tree.parsed.toc.entries.size(), tree.parsed.objects.size());
    EC::Log("║ schema_end=0x%X  stream_size=%u",
        tree.parsed.schema.schema_end, tree.parsed.toc.stream_size);
    EC::Log("╠──────────────────────────────────────────────────────────");

    // Raw TOC dump
    EC::Log("║ RAW TOC:");
    uint32_t toc_start = tree.parsed.schema.schema_end + 12;
    for (size_t i = 0; i < tree.parsed.toc.entries.size(); i++) {
        auto& te = tree.parsed.toc.entries[i];
        uint32_t ep = toc_start + (uint32_t)(i * 20);
        uint32_t raw_ci = ReadU32LE(tree.blob, ep);
        uint32_t raw_s1 = ReadU32LE(tree.blob, ep + 4);
        uint32_t raw_s2 = ReadU32LE(tree.blob, ep + 8);
        uint32_t raw_do = ReadU32LE(tree.blob, ep + 12);
        uint32_t raw_ds = ReadU32LE(tree.blob, ep + 16);
        EC::Log("║   [%3zu] ci=%3u s1=%08X s2=%08X off=0x%06X sz=%6u  %s",
            i, raw_ci, raw_s1, raw_s2, raw_do, raw_ds, te.class_name.c_str());
    }

    // Block details
    EC::Log("╠──────────────────────────────────────────────────────────");
    EC::Log("║ PARSED BLOCKS:");
    for (auto& obj : tree.parsed.objects) {
        int total_elems = 0;
        for (auto& f : obj.fields) total_elems += (int)f.list_elements.size();
        EC::Log("║   [%3u] %s  off=0x%06X sz=%6u  mbc=%u  fields=%zu  elems=%d",
            obj.entry_index, obj.class_name.c_str(), obj.data_offset, obj.data_size,
            obj.mask_byte_count, obj.fields.size(), total_elems);

        for (size_t fi = 0; fi < obj.fields.size(); fi++) {
            auto& f = obj.fields[fi];
            if (!f.present) continue;
            EC::Log("║     f[%zu] %s k=%u off=[0x%X..0x%X] rv=%zu elems=%zu cpo=0x%X",
                fi, f.name.c_str(), f.meta_kind, f.start_offset, f.end_offset,
                f.raw_value.size(), f.list_elements.size(), f.child_payload_offset);

            // First 3 elements
            for (size_t ei = 0; ei < std::min<size_t>(3, f.list_elements.size()); ei++) {
                auto& e = f.list_elements[ei];
                EC::Log("║       e[%zu] type=%s off=[0x%X..0x%X] ti=%d cpo=0x%X cf=%zu rv=%zu",
                    ei, e.child_type_name.c_str(), e.start_offset, e.end_offset,
                    e.child_type_index, e.child_payload_offset, e.child_fields.size(), e.raw_value.size());
            }
            if (f.list_elements.size() > 3)
                EC::Log("║       ... +%zu more elements", f.list_elements.size() - 3);
        }
    }

    // PO audit: scan ENTIRE blob for self-ref POs vs broken POs
    {
        static const uint8_t SENT[8] = {0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF};
        int valid = 0, broken = 0, toc_sents = 0;
        uint32_t toc_end = toc_start + (uint32_t)tree.parsed.toc.entries.size() * 20;
        for (uint32_t p = tree.parsed.schema.schema_end; p + 12 <= (uint32_t)tree.blob.size(); p++) {
            if (memcmp(tree.blob.data() + p, SENT, 8) != 0) continue;
            if (p >= toc_start && p < toc_end) { toc_sents++; continue; }
            uint32_t po_pos = p + 8;
            uint32_t po_val = ReadU32LE(tree.blob, po_pos);
            if (po_val == po_pos + 4) valid++;
            else broken++;
        }
        EC::Log("╠──────────────────────────────────────────────────────────");
        EC::Log("║ PO AUDIT: %d valid, %d broken (non-self-ref), %d TOC sentinels", valid, broken, toc_sents);
    }

    EC::Log("╚══════════════════════════════════════════════════════════");
}

// Compare two save states block-by-block, report any changes
void CompareStates(const char* label,
    const ParcEngine::SaveTree& before, const std::vector<uint8_t>& before_blob,
    const ParcEngine::SaveTree& after, const std::vector<std::string>& transplanted_blocks) {
    EC::Log("┌── COMPARE: %s ──", label);

    for (auto& obj_after : after.parsed.objects) {
        // Find matching block in before
        const SaveParserCpp::ObjectBlock* obj_before = nullptr;
        for (auto& ob : before.parsed.objects) {
            if (ob.class_name == obj_after.class_name && ob.entry_index == obj_after.entry_index) {
                obj_before = &ob; break;
            }
        }

        bool is_transplanted = false;
        for (auto& tn : transplanted_blocks)
            if (tn == obj_after.class_name) is_transplanted = true;

        if (!obj_before) {
            EC::Log("│ [%u] %s: NEW BLOCK (not in before)", obj_after.entry_index, obj_after.class_name.c_str());
            continue;
        }

        // Compare element counts
        int elems_before = 0, elems_after = 0;
        for (auto& f : obj_before->fields) elems_before += (int)f.list_elements.size();
        for (auto& f : obj_after.fields) elems_after += (int)f.list_elements.size();

        if (obj_before->data_size != obj_after.data_size || elems_before != elems_after) {
            EC::Log("│ [%u] %s: size %u->%u, elems %d->%d %s",
                obj_after.entry_index, obj_after.class_name.c_str(),
                obj_before->data_size, obj_after.data_size,
                elems_before, elems_after,
                is_transplanted ? "(TRANSPLANTED)" : "*** UNEXPECTED ***");
        }

        // For non-transplanted blocks: byte-compare the raw data
        if (!is_transplanted && obj_before->data_size == obj_after.data_size) {
            int diffs = 0;
            for (uint32_t i = 0; i < obj_before->data_size && i < obj_after.data_size; i++) {
                uint32_t pos_b = obj_before->data_offset + i;
                uint32_t pos_a = obj_after.data_offset + i;
                if (pos_b < before_blob.size() && pos_a < after.blob.size()) {
                    if (before_blob[pos_b] != after.blob[pos_a]) diffs++;
                }
            }
            if (diffs > 0) {
                EC::Log("│ [%u] %s: *** %d BYTE DIFFS in non-transplanted block! ***",
                    obj_after.entry_index, obj_after.class_name.c_str(), diffs);
            }
        }
    }
    EC::Log("└──────────────────────────────────────────");
}

struct RealPO {
    uint32_t pos = 0;
    uint32_t value = 0;
};

static std::vector<RealPO> CollectSelfReferentialPOs(const std::vector<uint8_t>& blob,
    uint32_t start = 0,
    uint32_t end = 0)
{
    static const uint8_t SENT[8] = {0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF};
    std::vector<RealPO> out;
    if (end == 0 || end > blob.size()) end = (uint32_t)blob.size();
    if (start >= end) return out;

    for (uint32_t p = start; p + 12 <= end; ++p) {
        if (blob[p] != 0xFF) continue;
        if (memcmp(blob.data() + p, SENT, 8) != 0) continue;
        uint32_t po_pos = p + 8;
        uint32_t po_val = ReadU32LE(blob, po_pos);
        if (po_val == po_pos + 4) {
            out.push_back({po_pos, po_val});
        }
    }
    return out;
}

static uint32_t ReadU32LE(const std::vector<uint8_t>& data, uint32_t off) {
    uint32_t v = 0;
    if (off + 4 <= data.size()) memcpy(&v, data.data() + off, 4);
    return v;
}

static void WriteU32LE(std::vector<uint8_t>& data, uint32_t off, uint32_t v) {
    if (off + 4 <= data.size()) memcpy(data.data() + off, &v, 4);
}

static void AppendU16(std::vector<uint8_t>& out, uint16_t v) {
    out.push_back((uint8_t)(v & 0xFF));
    out.push_back((uint8_t)((v >> 8) & 0xFF));
}

static void AppendU32(std::vector<uint8_t>& out, uint32_t v) {
    out.push_back((uint8_t)(v & 0xFF));
    out.push_back((uint8_t)((v >> 8) & 0xFF));
    out.push_back((uint8_t)((v >> 16) & 0xFF));
    out.push_back((uint8_t)((v >> 24) & 0xFF));
}

static void AppendString(std::vector<uint8_t>& out, const std::string& s) {
    AppendU32(out, (uint32_t)s.size());
    out.insert(out.end(), s.begin(), s.end());
}

static void RefreshNameMap(ParcEngine::SaveTree& tree) {
    tree.name_to_type_idx.clear();
    for (size_t i = 0; i < tree.parsed.schema.types.size(); ++i) {
        tree.name_to_type_idx[tree.parsed.schema.types[i].name] = (uint32_t)i;
        tree.parsed.schema.types[i].index = (uint32_t)i;
    }
    tree.parsed.schema.type_count = (uint16_t)tree.parsed.schema.types.size();
}

static std::vector<uint8_t> BuildSchemaBytes(const ParcEngine::SaveTree& tree) {
    const auto& schema = tree.parsed.schema;
    std::vector<uint8_t> out;

    if (tree.blob.size() < 0x0E) return out;
    out.insert(out.end(), tree.blob.begin(), tree.blob.begin() + 0x0E);
    AppendU16(out, schema.header_tag);
    AppendU16(out, schema.header_zero);
    AppendU16(out, (uint16_t)schema.types.size());
    AppendString(out, schema.root_type);

    for (size_t ti = 0; ti < schema.types.size(); ++ti) {
        const auto& type = schema.types[ti];
        AppendU16(out, (uint16_t)type.fields.size());
        for (const auto& field : type.fields) {
            AppendString(out, field.name);
            AppendString(out, field.type_name);
            AppendU16(out, field.meta_kind);
            AppendU16(out, field.meta_size);
            AppendU32(out, field.meta_aux);
        }
        if (ti + 1 < schema.types.size()) {
            AppendString(out, schema.types[ti + 1].name);
        }
    }

    return out;
}

static bool RewriteSchemaBytes(ParcEngine::SaveTree& tree) {
    RefreshNameMap(tree);

    uint32_t old_schema_end = tree.parsed.schema.schema_end;

    // Collect POs: sentinel scan + parse tree scan.
    // The sentinel scan only finds 0xFF×8 POs. Some PARC elements use 0x00×8
    // sentinels (e.g. FactionSpawnStageSaveData). The tree scan finds those too
    // by looking for the known child_payload_offset value in each element's bytes.
    auto offsets = CollectSelfReferentialPOs(tree.blob);
    size_t sentinel_count = offsets.size();
    {
        std::unordered_set<uint32_t> seen;
        for (auto& op : offsets) seen.insert(op.pos);

        std::function<void(const SaveParserCpp::GenericFieldValue&)> scanField;
        scanField = [&](const SaveParserCpp::GenericFieldValue& fv) {
            if (fv.child_payload_offset > 0 && fv.start_offset > 0 && fv.end_offset > fv.start_offset) {
                uint32_t target_val = fv.child_payload_offset;
                uint32_t s = fv.start_offset;
                uint32_t e = std::min(fv.end_offset, s + 32);
                for (uint32_t p = s; p + 4 <= e; p++) {
                    if (ReadU32LE(tree.blob, p) == target_val && seen.find(p) == seen.end()) {
                        offsets.push_back({p, target_val});
                        seen.insert(p);
                        break;
                    }
                }
            }
            for (auto& cf : fv.child_fields) scanField(cf);
            for (auto& le : fv.list_elements) scanField(le);
        };
        for (auto& obj : tree.parsed.objects)
            for (auto& f : obj.fields) scanField(f);
    }
    EC::Log("[SCHEMA_REWRITE] PO collection: %zu from sentinels, %zu total (with tree scan, +%zu from tree)",
        sentinel_count, offsets.size(), offsets.size() - sentinel_count);

    // Build schema with ONLY the original types first, to verify roundtrip
    uint16_t orig_type_count = tree.parsed.schema.type_count;
    auto schema_bytes = BuildSchemaBytes(tree);
    if (schema_bytes.empty()) return false;
    uint32_t new_schema_end = (uint32_t)schema_bytes.size();
    int32_t delta = (int32_t)new_schema_end - (int32_t)old_schema_end;

    // Verify the original schema portion roundtrips correctly
    {
        uint32_t cmp_len = std::min(old_schema_end, new_schema_end);
        int diffs = 0;
        uint32_t first_diff = 0;
        for (uint32_t i = 0; i < cmp_len; i++) {
            if (tree.blob[i] != schema_bytes[i]) {
                if (diffs == 0) first_diff = i;
                diffs++;
            }
        }
        EC::Log("[SCHEMA_VERIFY] Compared %u bytes of original schema region", cmp_len);
        EC::Log("[SCHEMA_VERIFY] old_schema_end=0x%X, new_schema_end=0x%X, delta=%d",
            old_schema_end, new_schema_end, delta);
        if (diffs > 0) {
            EC::Log("[SCHEMA_VERIFY] WARNING: %d bytes differ! First diff at offset 0x%X", diffs, first_diff);
            HexDump("ORIGINAL schema near first diff", tree.blob, first_diff > 16 ? first_diff - 16 : 0, 64);
            HexDump("REBUILT  schema near first diff", schema_bytes, first_diff > 16 ? first_diff - 16 : 0, 64);
        } else {
            EC::Log("[SCHEMA_VERIFY] MATCH — original schema bytes roundtrip correctly");
        }
    }

    if (delta == 0) return true;

    std::vector<uint8_t> new_blob;
    new_blob.reserve((size_t)((int64_t)tree.blob.size() + delta));
    new_blob.insert(new_blob.end(), schema_bytes.begin(), schema_bytes.end());
    new_blob.insert(new_blob.end(), tree.blob.begin() + old_schema_end, tree.blob.end());

    uint32_t new_toc_start = new_schema_end + 12;
    for (size_t i = 0; i < tree.parsed.toc.entries.size(); ++i) {
        uint32_t entry_pos = new_toc_start + (uint32_t)(i * 20);
        uint32_t old_off = ReadU32LE(new_blob, entry_pos + 12);
        WriteU32LE(new_blob, entry_pos + 12, (uint32_t)((int32_t)old_off + delta));
    }
    WriteU32LE(new_blob, new_schema_end + 8, (uint32_t)new_blob.size());

    for (const auto& op : offsets) {
        uint32_t new_pos = (uint32_t)((int32_t)op.pos + delta);
        uint32_t new_val = (uint32_t)((int32_t)op.value + delta);
        WriteU32LE(new_blob, new_pos, new_val);
    }

    tree.blob = std::move(new_blob);
    ParcEngine::Reparse(tree, false);
    return true;
}

// ── Diagnostics ──

DiagnosticResult DiagnoseSave(ParcEngine::SaveTree& tree) {
    DiagnosticResult r;
    r.decrypt_ok = true;
    r.decompress_ok = true;
    r.blob_size = (uint32_t)tree.blob.size();

    // Check PARC header
    if (tree.blob.size() < 16) {
        r.error_msg = "Blob too small for PARC header";
        return r;
    }
    r.parc_header_ok = true;

    // Check TOC
    if (tree.parsed.toc.entries.empty()) {
        r.error_msg = "TOC is empty — no blocks found";
        return r;
    }
    r.toc_ok = true;

    // Check each block
    for (auto& obj : tree.parsed.objects) {
        r.block_names.push_back(obj.class_name);
        r.blocks_parsed++;

        int obj_fields = 0, obj_present = 0;
        for (auto& f : obj.fields) {
            obj_fields++;
            if (f.present) {
                obj_present++;
                // Bounds check
                if (f.start_offset >= tree.blob.size() || f.end_offset > tree.blob.size()) {
                    r.warnings.push_back(obj.class_name + "." + f.name + ": offset out of bounds (" +
                        std::to_string(f.start_offset) + "-" + std::to_string(f.end_offset) +
                        ", blob=" + std::to_string(tree.blob.size()) + ")");
                }
                // Check list elements
                for (auto& el : f.list_elements) {
                    if (el.start_offset >= tree.blob.size() || el.end_offset > tree.blob.size()) {
                        r.warnings.push_back(obj.class_name + "." + f.name + " element: offset out of bounds");
                    }
                }
            }
        }
        r.total_fields += obj_fields;
        r.fields_present += obj_present;
    }

    r.tree_parse_ok = true;
    return r;
}

DiagnosticResult DiagnoseFile(const std::string& path) {
    DiagnosticResult r;

    // Try to load
    try {
        auto tree = ParcEngine::LoadSave(path);
        r.decrypt_ok = true;
        r.decompress_ok = true;
        r = DiagnoseSave(tree);
    } catch (const std::exception& e) {
        r.error_msg = e.what();
        // Try to determine which stage failed
        std::string err = e.what();
        if (err.find("ChaCha") != std::string::npos || err.find("decrypt") != std::string::npos)
            r.decrypt_ok = false;
        else if (err.find("LZ4") != std::string::npos || err.find("decompress") != std::string::npos) {
            r.decrypt_ok = true;
            r.decompress_ok = false;
        } else {
            r.decrypt_ok = true;
            r.decompress_ok = true;
        }
    }
    return r;
}

// ── Repair: Re-serialize ──

bool RepairReserialize(ParcEngine::SaveTree& tree) {
    try {
        auto fresh = ParcSerializer::Serialize(tree.parsed, tree.blob);
        if (fresh.empty()) return false;
        tree.blob = std::move(fresh);
        ParcEngine::Reparse(tree);
        return true;
    } catch (...) {
        return false;
    }
}

// ── Repair: Transplant block from reference save ──

// Build type_index remap table: donor_idx → recipient_idx (by type name)
static std::unordered_map<uint16_t, uint16_t> BuildTypeRemapTable(
    const ParcEngine::SaveTree& donor, const ParcEngine::SaveTree& recipient)
{
    // donor schema: index → name
    std::unordered_map<uint16_t, std::string> donor_idx_to_name;
    for (size_t i = 0; i < donor.parsed.schema.types.size(); i++)
        donor_idx_to_name[(uint16_t)i] = donor.parsed.schema.types[i].name;

    std::unordered_map<uint16_t, uint16_t> remap;
    for (auto& [didx, name] : donor_idx_to_name) {
        auto it = recipient.name_to_type_idx.find(name);
        if (it != recipient.name_to_type_idx.end()) {
            remap[didx] = (uint16_t)it->second;
        }
        // If not found in recipient, the index stays unmapped (will be flagged)
    }
    return remap;
}

// Ensure recipient schema has all types that donor uses
static int EnsureSchemaTypes(ParcEngine::SaveTree& recipient,
    const ParcEngine::SaveTree& donor)
{
    int added = 0;
    for (auto& dt : donor.parsed.schema.types) {
        if (recipient.name_to_type_idx.find(dt.name) == recipient.name_to_type_idx.end()) {
            // Add this type to recipient's schema
            uint32_t new_idx = (uint32_t)recipient.parsed.schema.types.size();
            auto copy = dt;
            copy.index = new_idx;
            recipient.parsed.schema.types.push_back(std::move(copy));
            recipient.name_to_type_idx[dt.name] = new_idx;
            added++;
        }
    }
    recipient.parsed.schema.type_count = (uint16_t)recipient.parsed.schema.types.size();
    return added;
}

static bool SameFieldDef(const SaveParserCpp::FieldDef& a, const SaveParserCpp::FieldDef& b) {
    return a.name == b.name &&
           a.type_name == b.type_name &&
           a.meta_kind == b.meta_kind &&
           a.meta_size == b.meta_size &&
           a.meta_aux == b.meta_aux;
}

static bool SameTypeDef(const SaveParserCpp::TypeDef& a, const SaveParserCpp::TypeDef& b) {
    if (a.name != b.name || a.fields.size() != b.fields.size()) return false;
    for (size_t i = 0; i < a.fields.size(); ++i) {
        if (!SameFieldDef(a.fields[i], b.fields[i])) return false;
    }
    return true;
}

static int FindCompatibleTypeIndex(const ParcEngine::SaveTree& recipient,
    const SaveParserCpp::TypeDef& donorType)
{
    for (size_t i = 0; i < recipient.parsed.schema.types.size(); ++i) {
        if (SameTypeDef(recipient.parsed.schema.types[i], donorType)) {
            return (int)i;
        }
    }
    return -1;
}

static int EnsureCompatibleSchemaTypes(ParcEngine::SaveTree& recipient,
    const ParcEngine::SaveTree& donor,
    std::unordered_map<uint16_t, uint16_t>& remap)
{
    int added = 0;
    remap.clear();

    for (size_t donor_idx = 0; donor_idx < donor.parsed.schema.types.size(); ++donor_idx) {
        const auto& donorType = donor.parsed.schema.types[donor_idx];
        int compatible_idx = FindCompatibleTypeIndex(recipient, donorType);
        if (compatible_idx < 0) {
            compatible_idx = (int)recipient.parsed.schema.types.size();
            auto copy = donorType;
            copy.index = (uint32_t)compatible_idx;
            recipient.parsed.schema.types.push_back(std::move(copy));
            added++;
        }
        remap[(uint16_t)donor_idx] = (uint16_t)compatible_idx;
    }

    RefreshNameMap(recipient);
    return added;
}

static int RemapBlockTypeIndicesFromTree(std::vector<uint8_t>& data,
    const SaveParserCpp::ObjectBlock& donorBlock,
    const ParcEngine::SaveTree& donor,
    const std::unordered_map<uint16_t, uint16_t>& remap)
{
    int remapped = 0;
    int skipped_bounds = 0;
    int unchanged = 0;
    int unmapped = 0;
    uint32_t block_start = donorBlock.data_offset;
    uint32_t block_end = donorBlock.data_offset + donorBlock.data_size;
    auto donorOffsets = CollectSelfReferentialPOs(donor.blob, block_start, block_end);
    EC::Log("[REMAP] Block %s: %zu POs found in donor range [0x%X..0x%X]",
        donorBlock.class_name.c_str(), donorOffsets.size(), block_start, block_end);

    for (const auto& op : donorOffsets) {
        uint32_t rel_po_pos = op.pos - block_start;
        if (rel_po_pos < 11 || rel_po_pos + 4 > data.size()) { skipped_bounds++; continue; }

        uint32_t ti_pos = rel_po_pos - 11;
        uint16_t ti = (uint16_t)data[ti_pos] | ((uint16_t)data[ti_pos + 1] << 8);
        auto it = remap.find(ti);
        if (it == remap.end()) {
            unmapped++;
            if (unmapped <= 5)
                EC::Log("[REMAP]   UNMAPPED type_index=%u at rel_offset=0x%X", ti, ti_pos);
        } else if (it->second == ti) {
            unchanged++;
        } else {
            if (remapped < 10)
                EC::Log("[REMAP]   %u -> %u at rel_offset=0x%X", ti, it->second, ti_pos);
            data[ti_pos] = (uint8_t)(it->second & 0xFF);
            data[ti_pos + 1] = (uint8_t)((it->second >> 8) & 0xFF);
            remapped++;
        }
    }
    EC::Log("[REMAP] Result: %d remapped, %d unchanged, %d unmapped, %d skipped_bounds",
        remapped, unchanged, unmapped, skipped_bounds);
    return remapped;
}

static int RebaseBlockPOsFromTree(std::vector<uint8_t>& data,
    const SaveParserCpp::ObjectBlock& donorBlock,
    const ParcEngine::SaveTree& donor,
    uint32_t targetBlockStart)
{
    int fixed = 0;
    int skipped = 0;
    uint32_t block_start = donorBlock.data_offset;
    uint32_t block_end = donorBlock.data_offset + donorBlock.data_size;
    auto donorOffsets = CollectSelfReferentialPOs(donor.blob, block_start, block_end);
    EC::Log("[REBASE] %zu POs to rebase, donor_block=[0x%X..0x%X], target_start=0x%X",
        donorOffsets.size(), block_start, block_end, targetBlockStart);

    for (const auto& op : donorOffsets) {
        uint32_t rel_po_pos = op.pos - block_start;
        if (rel_po_pos + 4 > data.size()) { skipped++; continue; }
        uint32_t old_po = 0;
        memcpy(&old_po, data.data() + rel_po_pos, 4);
        uint32_t new_po = targetBlockStart + rel_po_pos + 4;
        memcpy(data.data() + rel_po_pos, &new_po, 4);
        if (fixed < 5)
            EC::Log("[REBASE]   PO[%d] rel=0x%X: %u -> %u (self_ref_check: new==pos+4? %s)",
                fixed, rel_po_pos, old_po, new_po,
                (new_po == targetBlockStart + rel_po_pos + 4) ? "YES" : "NO");
        fixed++;
    }
    EC::Log("[REBASE] %d POs rebased, %d skipped", fixed, skipped);
    return fixed;
}

static bool ReplaceTopLevelBlockBytes(ParcEngine::SaveTree& target,
    const SaveParserCpp::ObjectBlock& targetBlock,
    const SaveParserCpp::ObjectBlock& donorBlock,
    const ParcEngine::SaveTree& donor,
    std::vector<uint8_t> donorBytes,
    uint16_t newClassIndex,
    int& poFixed)
{
    EC::Log("=== ReplaceTopLevelBlockBytes ===");
    EC::Log("  target block: class=%s idx=%u offset=0x%X size=%u",
        targetBlock.class_name.c_str(), targetBlock.class_index, targetBlock.data_offset, targetBlock.data_size);
    EC::Log("  donor  block: class=%s idx=%u offset=0x%X size=%u",
        donorBlock.class_name.c_str(), donorBlock.class_index, donorBlock.data_offset, donorBlock.data_size);
    EC::Log("  newClassIndex=%u, donorBytes=%zu", newClassIndex, donorBytes.size());

    if (targetBlock.data_offset + targetBlock.data_size > target.blob.size()) {
        EC::Log("  ERROR: target block out of bounds"); return false;
    }
    if (donorBlock.data_offset + donorBlock.data_size > donor.blob.size()) {
        EC::Log("  ERROR: donor block out of bounds"); return false;
    }

    HexDump("TARGET block header (before)", target.blob, targetBlock.data_offset, 64);
    HexDump("DONOR  block header (raw)", donorBytes, 0, 64);

    // Collect POs: sentinel scan + tree scan (same as RewriteSchemaBytes)
    auto targetOffsets = CollectSelfReferentialPOs(target.blob);
    size_t sentinel_po_count = targetOffsets.size();
    {
        std::unordered_set<uint32_t> seen;
        for (auto& op : targetOffsets) seen.insert(op.pos);
        std::function<void(const SaveParserCpp::GenericFieldValue&)> scanField;
        scanField = [&](const SaveParserCpp::GenericFieldValue& fv) {
            if (fv.child_payload_offset > 0 && fv.start_offset > 0 && fv.end_offset > fv.start_offset) {
                uint32_t tv = fv.child_payload_offset;
                uint32_t s = fv.start_offset;
                uint32_t e = std::min(fv.end_offset, s + 32);
                for (uint32_t p = s; p + 4 <= e; p++) {
                    if (ReadU32LE(target.blob, p) == tv && seen.find(p) == seen.end()) {
                        targetOffsets.push_back({p, tv});
                        seen.insert(p);
                        break;
                    }
                }
            }
            for (auto& cf : fv.child_fields) scanField(cf);
            for (auto& le : fv.list_elements) scanField(le);
        };
        for (auto& obj : target.parsed.objects)
            for (auto& f : obj.fields) scanField(f);
    }
    EC::Log("  target blob POs: %zu sentinel + %zu tree = %zu total",
        sentinel_po_count, targetOffsets.size() - sentinel_po_count, targetOffsets.size());

    const uint32_t old_start = targetBlock.data_offset;
    const uint32_t old_end = targetBlock.data_offset + targetBlock.data_size;
    const uint32_t old_size = targetBlock.data_size;
    const uint32_t new_size = (uint32_t)donorBytes.size();
    const int32_t delta = (int32_t)new_size - (int32_t)old_size;
    EC::Log("  splice: old=[0x%X..0x%X] (%u B), new=%u B, delta=%d",
        old_start, old_end, old_size, new_size, delta);

    poFixed += RebaseBlockPOsFromTree(donorBytes, donorBlock, donor, old_start);
    HexDump("DONOR  block header (after rebase)", donorBytes, 0, 64);

    // Dump donor bytes to file for offline comparison
    {
        std::string dumpDir = getenv("TEMP") ? getenv("TEMP") : ".";
        std::string donorDump = dumpDir + "\\transplant_donor_" + donorBlock.class_name + ".bin";
        std::string targetDump = dumpDir + "\\transplant_target_before_" + targetBlock.class_name + ".bin";
        DumpToFile(donorDump.c_str(), donorBytes, 0, (uint32_t)donorBytes.size());
        DumpToFile(targetDump.c_str(), target.blob, old_start, old_size);
        EC::Log("  Dumped donor bytes to: %s", donorDump.c_str());
        EC::Log("  Dumped target bytes to: %s", targetDump.c_str());
    }

    target.blob.erase(target.blob.begin() + old_start, target.blob.begin() + old_end);
    target.blob.insert(target.blob.begin() + old_start, donorBytes.begin(), donorBytes.end());
    EC::Log("  splice done, new blob size=%zu", target.blob.size());

    if (delta != 0) {
        int po_adjusted = 0;
        int po_skipped_in_block = 0;
        int po_skipped_oob = 0;
        int po_before_splice = 0;
        for (const auto& op : targetOffsets) {
            if (op.pos >= old_start && op.pos < old_end) { po_skipped_in_block++; continue; }
            uint32_t new_pos = op.pos >= old_end ? (uint32_t)((int32_t)op.pos + delta) : op.pos;
            if (new_pos + 4 > target.blob.size()) { po_skipped_oob++; continue; }
            if (op.value >= old_end) {
                uint32_t new_val = (uint32_t)((int32_t)op.value + delta);
                WriteU32LE(target.blob, new_pos, new_val);
                po_adjusted++;
            } else {
                po_before_splice++;
            }
        }
        poFixed += po_adjusted;
        EC::Log("  PO fixup: %d adjusted, %d in-block-skipped, %d before-splice, %d oob-skipped",
            po_adjusted, po_skipped_in_block, po_before_splice, po_skipped_oob);
    } else {
        EC::Log("  delta=0, no PO fixup needed");
    }

    // Verify self-referential invariant after fixup
    {
        int broken_pos = 0;
        auto postOffsets = CollectSelfReferentialPOs(target.blob);
        EC::Log("  post-splice self-ref POs: %zu (was %zu)", postOffsets.size(), targetOffsets.size());

        // Scan for sentinels that AREN'T self-referential (broken POs)
        static const uint8_t SENT[8] = {0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF};
        for (uint32_t p = 0; p + 12 <= (uint32_t)target.blob.size(); ++p) {
            if (target.blob[p] != 0xFF) continue;
            if (memcmp(target.blob.data() + p, SENT, 8) != 0) continue;
            uint32_t po_pos = p + 8;
            uint32_t po_val = ReadU32LE(target.blob, po_pos);
            if (po_val != po_pos + 4) {
                if (broken_pos < 10)
                    EC::Log("  BROKEN PO at 0x%X: value=%u, expected=%u (diff=%d)",
                        po_pos, po_val, po_pos + 4, (int32_t)po_val - (int32_t)(po_pos + 4));
                broken_pos++;
            }
        }
        EC::Log("  broken POs found: %d", broken_pos);
    }

    // TOC fixup
    EC::Log("  TOC fixup (schema_end=0x%X, %zu entries):",
        target.parsed.schema.schema_end, target.parsed.toc.entries.size());
    uint32_t toc_start = target.parsed.schema.schema_end + 12;
    for (size_t i = 0; i < target.parsed.toc.entries.size(); ++i) {
        uint32_t ep = toc_start + (uint32_t)(i * 20);
        uint32_t old_ci = ReadU32LE(target.blob, ep);
        uint32_t doff = ReadU32LE(target.blob, ep + 12);
        uint32_t dsz = ReadU32LE(target.blob, ep + 16);
        if (i == targetBlock.entry_index) {
            WriteU32LE(target.blob, ep, newClassIndex);
            WriteU32LE(target.blob, ep + 16, new_size);
            EC::Log("    [%zu] MODIFIED: class %u->%u, size %u->%u, offset=0x%X",
                i, old_ci, (uint32_t)newClassIndex, dsz, new_size, doff);
        } else if (doff > old_start) {
            uint32_t new_doff = (uint32_t)((int32_t)doff + delta);
            WriteU32LE(target.blob, ep + 12, new_doff);
            EC::Log("    [%zu] SHIFTED: offset 0x%X -> 0x%X (delta=%d)", i, doff, new_doff, delta);
        }
    }
    uint32_t new_stream_size = (uint32_t)target.blob.size();
    WriteU32LE(target.blob, target.parsed.schema.schema_end + 8, new_stream_size);
    EC::Log("  stream_size updated to %u", new_stream_size);

    HexDump("RESULT block header (after splice)", target.blob, old_start, 64);

    // Dump the result block for offline comparison
    {
        std::string dumpDir = getenv("TEMP") ? getenv("TEMP") : ".";
        std::string resultDump = dumpDir + "\\transplant_result_" + targetBlock.class_name + ".bin";
        DumpToFile(resultDump.c_str(), target.blob, old_start, new_size);
        EC::Log("  Dumped result block to: %s", resultDump.c_str());
    }

    ParcEngine::Reparse(target, false);

    // Post-reparse verification
    EC::Log("  post-reparse: %zu objects, %zu schema types, blob=%zu",
        target.parsed.objects.size(), target.parsed.schema.types.size(), target.blob.size());
    for (auto& obj : target.parsed.objects) {
        if (obj.class_name == targetBlock.class_name) {
            EC::Log("  reparsed block: offset=0x%X size=%u fields=%zu present=%zu",
                obj.data_offset, obj.data_size, obj.fields.size(),
                (size_t)std::count_if(obj.fields.begin(), obj.fields.end(),
                    [](const auto& f) { return f.present; }));
            break;
        }
    }

    // Roundtrip verification
    {
        std::string rt_report;
        bool rt_ok = VerifyRoundtrip(target, rt_report);
        EC::Log("  roundtrip check: %s", rt_report.c_str());
    }

    EC::Log("=== ReplaceTopLevelBlockBytes DONE ===");
    return true;
}

bool TransplantBlock(ParcEngine::SaveTree& tree, const std::string& refPath,
                     const std::string& blockName) {
    try {
        EC::Log("========================================");
        EC::Log("TRANSPLANT START: block=%s", blockName.c_str());
        EC::Log("  donor save: %s", refPath.c_str());
        EC::Log("  target blob: %zu bytes, %zu schema types, %zu TOC entries",
            tree.blob.size(), tree.parsed.schema.types.size(), tree.parsed.toc.entries.size());

        auto refTree = ParcEngine::LoadSave(refPath);
        EC::Log("  donor  blob: %zu bytes, %zu schema types, %zu TOC entries",
            refTree.blob.size(), refTree.parsed.schema.types.size(), refTree.parsed.toc.entries.size());

        // Step 1: Schema compatibility
        std::unordered_map<uint16_t, uint16_t> remap;
        int typesAdded = EnsureCompatibleSchemaTypes(tree, refTree, remap);
        EC::Log("  schema: %d types added, remap table has %zu entries", typesAdded, remap.size());

        // Log remap table (first 20 entries that differ)
        {
            int logged = 0;
            for (auto& [from, to] : remap) {
                if (from != to && logged < 20) {
                    std::string fromName = (from < refTree.parsed.schema.types.size()) ?
                        refTree.parsed.schema.types[from].name : "?";
                    EC::Log("    remap[%u] -> %u  (%s)", from, to, fromName.c_str());
                    logged++;
                }
            }
            if (logged == 0) EC::Log("    (all indices identical — no remapping needed)");
        }

        if (typesAdded > 0) {
            // Snapshot FactionSpawn BEFORE schema rewrite for diagnosis
            uint32_t fs_before_offset = 0, fs_before_size = 0;
            std::vector<uint8_t> fs_before_bytes;
            for (auto& obj : tree.parsed.objects) {
                if (obj.class_name == "FactionSpawnStageManagerSaveData") {
                    fs_before_offset = obj.data_offset;
                    fs_before_size = obj.data_size;
                    fs_before_bytes.assign(tree.blob.begin() + obj.data_offset,
                        tree.blob.begin() + obj.data_offset + obj.data_size);
                    EC::Log("  [DIAG] FactionSpawn BEFORE schema rewrite: offset=0x%X size=%u elems=%d",
                        obj.data_offset, obj.data_size,
                        obj.fields.empty() ? 0 : (int)obj.fields[0].list_elements.size());
                    break;
                }
            }

            EC::Log("  rewriting schema bytes for %d new types...", typesAdded);
            if (!RewriteSchemaBytes(tree)) {
                EC::Log("  ERROR: failed to rewrite expanded schema");
                return false;
            }
            EC::Log("  schema rewrite OK, new blob=%zu, schema_end=0x%X",
                tree.blob.size(), tree.parsed.schema.schema_end);

            // Check FactionSpawn AFTER schema rewrite — byte-compare entire block
            for (auto& obj : tree.parsed.objects) {
                if (obj.class_name == "FactionSpawnStageManagerSaveData") {
                    int elems = obj.fields.empty() ? 0 : (int)obj.fields[0].list_elements.size();
                    EC::Log("  [DIAG] FactionSpawn AFTER schema rewrite: offset=0x%X size=%u elems=%d",
                        obj.data_offset, obj.data_size, elems);

                    if (!fs_before_bytes.empty() && obj.data_size == fs_before_size) {
                        int byte_diffs = 0;
                        uint32_t first_diff_off = 0;
                        for (uint32_t i = 0; i < fs_before_size; i++) {
                            uint8_t orig = fs_before_bytes[i];
                            uint8_t cur = tree.blob[obj.data_offset + i];
                            if (orig != cur) {
                                if (byte_diffs == 0) first_diff_off = i;
                                if (byte_diffs < 20) {
                                    EC::Log("  [DIAG]   BYTE DIFF at block+0x%X: orig=0x%02X new=0x%02X (abs: 0x%X vs 0x%X)",
                                        i, orig, cur, fs_before_offset + i, obj.data_offset + i);
                                }
                                byte_diffs++;
                            }
                        }
                        if (byte_diffs == 0) {
                            EC::Log("  [DIAG]   ALL %u BYTES IDENTICAL — parser bug, not data corruption", fs_before_size);
                        } else {
                            EC::Log("  [DIAG]   %d bytes differ (first at block+0x%X)", byte_diffs, first_diff_off);
                        }
                    }
                    break;
                }
            }
        }

        // Find blocks
        const SaveParserCpp::ObjectBlock* refBlock = nullptr;
        for (auto& obj : refTree.parsed.objects) {
            if (obj.class_name == blockName) { refBlock = &obj; break; }
        }
        if (!refBlock) {
            EC::Log("  ERROR: block '%s' not found in donor", blockName.c_str());
            return false;
        }

        SaveParserCpp::ObjectBlock* targetBlock = nullptr;
        for (auto& obj : tree.parsed.objects) {
            if (obj.class_name == blockName) { targetBlock = &obj; break; }
        }
        if (!targetBlock) {
            EC::Log("  ERROR: block '%s' not found in target", blockName.c_str());
            return false;
        }

        EC::Log("  donor  block: class_index=%u, offset=0x%X, size=%u, mbc=%u, fields=%zu",
            refBlock->class_index, refBlock->data_offset, refBlock->data_size,
            refBlock->mask_byte_count, refBlock->fields.size());
        EC::Log("  target block: class_index=%u, offset=0x%X, size=%u, mbc=%u, fields=%zu",
            targetBlock->class_index, targetBlock->data_offset, targetBlock->data_size,
            targetBlock->mask_byte_count, targetBlock->fields.size());

        // Field presence comparison
        {
            EC::Log("  field presence comparison:");
            size_t nf = std::max(refBlock->fields.size(), targetBlock->fields.size());
            for (size_t fi = 0; fi < nf; fi++) {
                bool dp = fi < refBlock->fields.size() && refBlock->fields[fi].present;
                bool tp = fi < targetBlock->fields.size() && targetBlock->fields[fi].present;
                const char* dn = fi < refBlock->fields.size() ? refBlock->fields[fi].name.c_str() : "(n/a)";
                const char* tn = fi < targetBlock->fields.size() ? targetBlock->fields[fi].name.c_str() : "(n/a)";
                if (dp != tp) {
                    EC::Log("    [%zu] MISMATCH: donor=%s(%s) target=%s(%s)",
                        fi, dn, dp ? "Y" : "N", tn, tp ? "Y" : "N");
                }
            }
        }

        // Extract donor bytes and remap
        std::vector<uint8_t> donorBytes(refTree.blob.begin() + refBlock->data_offset,
                                        refTree.blob.begin() + refBlock->data_offset + refBlock->data_size);
        int totalRemapped = RemapBlockTypeIndicesFromTree(donorBytes, *refBlock, refTree, remap);

        auto classIt = remap.find((uint16_t)refBlock->class_index);
        if (classIt == remap.end()) {
            EC::Log("  ERROR: donor class_index %u not in remap table", refBlock->class_index);
            return false;
        }
        EC::Log("  class_index remap: %u -> %u", refBlock->class_index, classIt->second);

        int poFixed = 0;
        bool ok = ReplaceTopLevelBlockBytes(tree, *targetBlock, *refBlock, refTree,
                                            std::move(donorBytes), classIt->second, poFixed);
        if (!ok) {
            EC::Log("  ERROR: ReplaceTopLevelBlockBytes failed");
            return false;
        }

        EC::Log("TRANSPLANT DONE: %d types added, %d type indices remapped, %d POs fixed, blob=%zu",
            typesAdded, totalRemapped, poFixed, tree.blob.size());
        EC::Log("========================================");
        return true;
    } catch (const std::exception& e) {
        EC::Log("TRANSPLANT EXCEPTION: %s", e.what());
        return false;
    }
}

// ── Repair: Zero out block ──

bool ZeroBlock(ParcEngine::SaveTree& tree, const std::string& blockName) {
    for (auto& obj : tree.parsed.objects) {
        if (obj.class_name != blockName) continue;
        for (auto& f : obj.fields) {
            if (!f.present) continue;
            if (f.start_offset > 0 && f.end_offset > f.start_offset &&
                f.end_offset <= tree.blob.size()) {
                memset(&tree.blob[f.start_offset], 0, f.end_offset - f.start_offset);
            }
        }
        return true;
    }
    return false;
}

// ── Verify roundtrip ──

bool VerifyRoundtrip(ParcEngine::SaveTree& tree, std::string& diff_report) {
    try {
        auto fresh = ParcSerializer::Serialize(tree.parsed, tree.blob);
        if (fresh.empty()) {
            diff_report = "Serializer returned empty blob";
            return false;
        }
        if (fresh.size() != tree.blob.size()) {
            char buf[256];
            snprintf(buf, sizeof(buf), "Size mismatch: original=%u, serialized=%u (delta=%d)",
                (uint32_t)tree.blob.size(), (uint32_t)fresh.size(),
                (int)fresh.size() - (int)tree.blob.size());
            diff_report = buf;
            return false;
        }
        // Byte compare
        int diffs = 0;
        uint32_t first_diff = 0;
        for (size_t i = 0; i < tree.blob.size(); i++) {
            if (tree.blob[i] != fresh[i]) {
                if (diffs == 0) first_diff = (uint32_t)i;
                diffs++;
            }
        }
        if (diffs > 0) {
            char buf[256];
            snprintf(buf, sizeof(buf), "%d bytes differ. First diff at offset 0x%X", diffs, first_diff);
            diff_report = buf;
            return false;
        }
        diff_report = "PERFECT MATCH";
        return true;
    } catch (const std::exception& e) {
        diff_report = std::string("Serialize error: ") + e.what();
        return false;
    }
}

// ── Block health scanner ──
// Detects corruption by INTERNAL CONSISTENCY — the save contradicts itself.
// No reference save needed for detection. Reference is only used for fixing.

static std::vector<BlockHealth> ScanBlockHealth(const ParcEngine::SaveTree& tree) {
    std::vector<BlockHealth> out;
    std::set<std::string> seen;

    for (auto& obj : tree.parsed.objects) {
        if (seen.count(obj.class_name)) continue;
        seen.insert(obj.class_name);

        BlockHealth bh;
        bh.name = obj.class_name;
        bh.entry_index = obj.entry_index;
        bh.block_size = obj.data_size;
        bh.fields_total = (int)obj.fields.size();
        for (auto& f : obj.fields) {
            if (f.present) bh.fields_present++;
            bh.elem_count += (int)f.list_elements.size();
        }
        bh.undecoded_ranges = (int)obj.undecoded_ranges.size();
        for (auto& [us, ue] : obj.undecoded_ranges)
            bh.undecoded_bytes += (ue - us);

        // Check 1: block extends past blob
        if (obj.data_offset + obj.data_size > tree.blob.size()) {
            bh.status = BlockHealth::BROKEN;
            bh.issue = "Extends past blob end";
            bh.fixable = true;
            out.push_back(std::move(bh));
            continue;
        }

        // Check 2: field offsets out of bounds
        for (auto& f : obj.fields) {
            if (!f.present) continue;
            if (f.start_offset > 0 && (f.start_offset >= tree.blob.size() || f.end_offset > tree.blob.size())) {
                bh.status = BlockHealth::BROKEN;
                bh.issue = f.name + ": offset out of bounds";
                bh.fixable = true;
                break;
            }
        }
        if (bh.status == BlockHealth::BROKEN) { out.push_back(std::move(bh)); continue; }

        // Check 3: collapsed lists — list header says N but raw_value is too small or too few elements parsed
        for (auto& f : obj.fields) {
            if (!f.present) continue;
            if (f.meta_kind != 6 && f.meta_kind != 7) continue;

            uint32_t field_size = (f.end_offset > f.start_offset) ? (f.end_offset - f.start_offset) : (uint32_t)f.raw_value.size();
            int parsed_elems = (int)f.list_elements.size();

            if (parsed_elems > 0 && field_size > 0) {
                uint32_t avg_elem_size = field_size / (uint32_t)parsed_elems;
                // If the field is large but only 1-2 elements parsed, and each element
                // would need to be unreasonably huge, the list is probably collapsed
                if (parsed_elems <= 2 && field_size > 500 && avg_elem_size > 1000) {
                    bh.status = BlockHealth::BROKEN;
                    bh.issue = f.name + ": " + std::to_string(parsed_elems) + " elems in " +
                        std::to_string(field_size) + "B (collapsed?)";
                    bh.fixable = true;
                    break;
                }
            }

            // Check list_count vs actual parsed elements
            if (f.list_count > 0 && parsed_elems > 0 && f.list_count > (uint32_t)parsed_elems * 2) {
                bh.status = BlockHealth::BROKEN;
                bh.issue = f.name + ": header says " + std::to_string(f.list_count) +
                    " but only " + std::to_string(parsed_elems) + " parsed";
                bh.fixable = true;
                break;
            }
        }
        if (bh.status == BlockHealth::BROKEN) { out.push_back(std::move(bh)); continue; }

        // Check 4: large undecoded regions (parser couldn't make sense of the data)
        if (bh.undecoded_bytes > 0 && obj.data_size > 0) {
            float undecoded_pct = (float)bh.undecoded_bytes / (float)obj.data_size * 100.0f;
            if (undecoded_pct > 50.0f && bh.undecoded_bytes > 100) {
                bh.status = BlockHealth::WARN;
                bh.issue = std::to_string((int)undecoded_pct) + "% undecoded (" +
                    std::to_string(bh.undecoded_bytes) + "B)";
                bh.fixable = true;
            }
        }

        // Check 5: all fields zeroed in a block that should have data
        if (bh.fields_present == 0 && bh.fields_total > 2 && obj.data_size > 20) {
            bh.status = BlockHealth::WARN;
            bh.issue = "No fields present (" + std::to_string(bh.fields_total) + " expected)";
            bh.fixable = true;
        }

        out.push_back(std::move(bh));
    }
    return out;
}

// ── UI ──

static DiagnosticResult g_diag;
static bool g_diagRun = false;
static char g_repairStatus[512] = {};

static ParcEngine::SaveTree g_refTree;
static bool g_refLoaded = false;
static std::string g_refPathStr;
static char g_refPath[512] = {};
static std::vector<BlockHealth> g_health;
static bool g_healthScanned = false;

void RenderRepairTab(ParcEngine::SaveTree& tree, bool& dirty, const std::string& savePath) {
    ImGui::TextColored(ImVec4(1.0f, 0.5f, 0.3f, 1.0f), "Save Repair Tool");
    ImGui::TextColored(ImVec4(0.7f, 0.7f, 0.7f, 1.0f),
        "Diagnose corrupted saves and auto-fix broken blocks from a healthy reference.");
    ImGui::Separator();

    if (g_repairStatus[0]) {
        bool isErr = (strstr(g_repairStatus, "FAIL") || strstr(g_repairStatus, "ERROR"));
        ImVec4 col = isErr ? ImVec4(1, 0.3f, 0.3f, 1) : ImVec4(0.3f, 1.0f, 0.4f, 1);
        ImGui::TextColored(col, "%s", g_repairStatus);
    }

    // ── Basic diagnostics ──
    if (ImGui::Button("Run Diagnostics", ImVec2(200, 0))) {
        g_diag = DiagnoseSave(tree);
        g_diagRun = true;
        snprintf(g_repairStatus, sizeof(g_repairStatus), "Diagnostics complete");
    }
    ImGui::SameLine();
    if (ImGui::Button("Verify Roundtrip", ImVec2(200, 0))) {
        std::string report;
        VerifyRoundtrip(tree, report);
        snprintf(g_repairStatus, sizeof(g_repairStatus), "Roundtrip: %s", report.c_str());
    }
    ImGui::SameLine();
    if (ImGui::Button("Re-serialize", ImVec2(150, 0))) {
        if (RepairReserialize(tree)) {
            dirty = true;
            snprintf(g_repairStatus, sizeof(g_repairStatus), "Re-serialized OK");
        } else {
            snprintf(g_repairStatus, sizeof(g_repairStatus), "Re-serialize FAILED");
        }
    }
    if (ImGui::IsItemHovered())
        ImGui::SetTooltip("Rebuilds PARC blob from parse tree.\nFixes all PO/trailing-size corruption.");

    if (g_diagRun) {
        auto okc = [](bool v) { return v ? ImVec4(0.2f,0.9f,0.2f,1) : ImVec4(1,0.3f,0.3f,1); };
        ImGui::TextColored(okc(g_diag.decrypt_ok), "Decrypt: %s", g_diag.decrypt_ok ? "OK" : "FAIL");
        ImGui::SameLine();
        ImGui::TextColored(okc(g_diag.decompress_ok), "Decompress: %s", g_diag.decompress_ok ? "OK" : "FAIL");
        ImGui::SameLine();
        ImGui::TextColored(okc(g_diag.tree_parse_ok), "Parse: %s", g_diag.tree_parse_ok ? "OK" : "FAIL");
        ImGui::SameLine();
        ImGui::Text("Blob: %uB | %d blocks | %d/%d fields",
            g_diag.blob_size, g_diag.blocks_parsed, g_diag.fields_present, g_diag.total_fields);
    }

    ImGui::Separator();

    // ── Block Health Scanner ──
    ImGui::TextColored(ImVec4(0.5f, 0.8f, 1.0f, 1.0f), "Block Health Scanner");
    ImGui::TextColored(ImVec4(0.6f, 0.6f, 0.6f, 1.0f),
        "Detects internal corruption: collapsed lists, broken offsets, unparseable data.");

    if (ImGui::Button("Scan Health", ImVec2(150, 0))) {
        g_health = ScanBlockHealth(tree);
        g_healthScanned = true;
        int broken = 0, warn = 0;
        for (auto& bh : g_health) {
            if (bh.status == BlockHealth::BROKEN) broken++;
            else if (bh.status == BlockHealth::WARN) warn++;
        }
        snprintf(g_repairStatus, sizeof(g_repairStatus),
            "Health scan: %d blocks, %d broken, %d warnings", (int)g_health.size(), broken, warn);
    }

    if (!g_healthScanned) return;

    // Count issues
    int broken_count = 0, warn_count = 0, fixable_count = 0;
    for (auto& bh : g_health) {
        if (bh.status == BlockHealth::BROKEN) { broken_count++; if (bh.fixable) fixable_count++; }
        else if (bh.status == BlockHealth::WARN) warn_count++;
    }

    if (broken_count == 0 && warn_count == 0) {
        ImGui::TextColored(ImVec4(0.2f, 0.9f, 0.2f, 1), "All %d blocks healthy", (int)g_health.size());
    } else {
        if (broken_count > 0)
            ImGui::TextColored(ImVec4(1, 0.4f, 0.3f, 1), "%d BROKEN", broken_count);
        if (broken_count > 0 && warn_count > 0) ImGui::SameLine();
        if (warn_count > 0)
            ImGui::TextColored(ImVec4(1, 0.8f, 0.3f, 1), "%d warnings", warn_count);
    }

    // ── Reference save for FIXING (not detection) ──
    if (fixable_count > 0) {
        ImGui::Separator();
        ImGui::TextColored(ImVec4(1, 0.7f, 0.3f, 1), "Fix broken blocks from a healthy save:");
        ImGui::Text("Reference:");
        ImGui::SameLine();
        ImGui::SetNextItemWidth(300);
        ImGui::InputText("##refpath", g_refPath, sizeof(g_refPath));
        ImGui::SameLine();
        if (ImGui::SmallButton("Browse##ref")) {
#ifdef _WIN32
            OPENFILENAMEA ofn = {};
            char path[MAX_PATH] = {};
            ofn.lStructSize = sizeof(ofn);
            ofn.lpstrFilter = "Save files (*.save)\0*.save\0All\0*.*\0";
            ofn.lpstrFile = path;
            ofn.nMaxFile = MAX_PATH;
            ofn.Flags = OFN_FILEMUSTEXIST;
            ofn.lpstrTitle = "Select Healthy Reference Save";
            if (GetOpenFileNameA(&ofn)) {
                strncpy(g_refPath, path, sizeof(g_refPath) - 1);
                try {
                    g_refTree = ParcEngine::LoadSave(path);
                    g_refLoaded = true;
                    g_refPathStr = path;
                    snprintf(g_repairStatus, sizeof(g_repairStatus), "Reference loaded: %zu blocks",
                        g_refTree.parsed.objects.size());
                } catch (const std::exception& e) {
                    g_refLoaded = false;
                    snprintf(g_repairStatus, sizeof(g_repairStatus), "Reference FAILED: %s", e.what());
                }
            }
#endif
        }
        if (g_refLoaded) {
            ImGui::SameLine();
            ImGui::TextColored(ImVec4(0.4f, 1.0f, 0.6f, 1.0f), "[Loaded]");
            ImGui::SameLine();
            if (ImGui::Button("Fix All Broken", ImVec2(150, 0))) {
                int ok = 0, fail = 0;
                for (auto& bh : g_health) {
                    if (bh.status != BlockHealth::BROKEN || !bh.fixable) continue;
                    if (TransplantBlock(tree, g_refPathStr, bh.name)) { dirty = true; ok++; }
                    else fail++;
                }
                snprintf(g_repairStatus, sizeof(g_repairStatus),
                    "Auto-fix: %d repaired, %d failed", ok, fail);
                g_health = ScanBlockHealth(tree);
            }
            if (ImGui::IsItemHovered())
                ImGui::SetTooltip("Replaces each BROKEN block with the same block\n"
                    "from the reference save. Only touches broken blocks.");
        }
    }

    // ── Health table ──
    ImGui::Separator();
    float tableH = ImGui::GetContentRegionAvail().y - 10;
    if (tableH < 200) tableH = 200;

    if (ImGui::BeginTable("##health", 6,
        ImGuiTableFlags_Borders | ImGuiTableFlags_RowBg |
        ImGuiTableFlags_ScrollY | ImGuiTableFlags_SizingFixedFit,
        ImVec2(0, tableH)))
    {
        ImGui::TableSetupScrollFreeze(0, 1);
        ImGui::TableSetupColumn("Status", ImGuiTableColumnFlags_WidthFixed, 50);
        ImGui::TableSetupColumn("Block", ImGuiTableColumnFlags_WidthStretch);
        ImGui::TableSetupColumn("Elems", ImGuiTableColumnFlags_WidthFixed, 55);
        ImGui::TableSetupColumn("Size", ImGuiTableColumnFlags_WidthFixed, 65);
        ImGui::TableSetupColumn("Issue", ImGuiTableColumnFlags_WidthStretch);
        ImGui::TableSetupColumn("##fix", ImGuiTableColumnFlags_WidthFixed, 45);
        ImGui::TableHeadersRow();

        for (auto& bh : g_health) {
            ImGui::TableNextRow();

            ImGui::TableSetColumnIndex(0);
            if (bh.status == BlockHealth::OK)
                ImGui::TextColored(ImVec4(0.2f, 0.9f, 0.2f, 1), "OK");
            else if (bh.status == BlockHealth::WARN)
                ImGui::TextColored(ImVec4(1.0f, 0.8f, 0.3f, 1), "WARN");
            else
                ImGui::TextColored(ImVec4(1.0f, 0.3f, 0.3f, 1), "BROKE");

            ImGui::TableSetColumnIndex(1);
            ImGui::Text("%s", bh.name.c_str());

            ImGui::TableSetColumnIndex(2);
            ImGui::Text("%d", bh.elem_count);

            ImGui::TableSetColumnIndex(3);
            ImGui::Text("%u", bh.block_size);

            ImGui::TableSetColumnIndex(4);
            if (!bh.issue.empty()) {
                ImVec4 ic = (bh.status == BlockHealth::BROKEN) ?
                    ImVec4(1, 0.4f, 0.3f, 1) : ImVec4(1, 0.8f, 0.3f, 1);
                ImGui::TextColored(ic, "%s", bh.issue.c_str());
            }

            ImGui::TableSetColumnIndex(5);
            if (bh.fixable && g_refLoaded) {
                char fbtn[64];
                snprintf(fbtn, sizeof(fbtn), "Fix##%s", bh.name.c_str());
                if (ImGui::SmallButton(fbtn)) {
                    if (TransplantBlock(tree, g_refPathStr, bh.name)) {
                        dirty = true;
                        snprintf(g_repairStatus, sizeof(g_repairStatus), "Fixed %s", bh.name.c_str());
                    } else {
                        snprintf(g_repairStatus, sizeof(g_repairStatus), "Fix FAILED: %s", bh.name.c_str());
                    }
                    g_health = ScanBlockHealth(tree);
                }
            }
        }
        ImGui::EndTable();
    }
}

} // namespace SaveRepair
